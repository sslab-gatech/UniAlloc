#!/usr/bin/env python3
"""Run one configured external paper workload without inventing timing data.

This wrapper is intentionally generic: a bundle JSON names real workload
repositories, commands, allocator selection, and JSON timing fields.  The wrapper
executes exactly that command, preserves stdout/stderr evidence, and emits one
JSON record for the higher-level UniAlloc evaluation importer.
"""

from __future__ import annotations

import argparse
import datetime as _dt
import fnmatch
import hashlib
import json
import math
import os
import platform
import re
import shlex
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

ROOT = Path(__file__).resolve().parents[2]
EVAL = ROOT / "evaluation"
RAW = EVAL / "raw"

DEFAULT_MAX_CHILD_OUTPUT_BYTES = 16 * 1024 * 1024

SUPPORTED_TIME_FIELDS = {
    "seconds",
    "time_seconds",
    "wall_seconds",
    "elapsed_seconds",
    "ns_per_iter",
    "time_ns",
    "nanoseconds",
    "ms",
    "milliseconds",
}


def now_iso() -> str:
    return _dt.datetime.now(_dt.timezone.utc).isoformat().replace("+00:00", "Z")


def slugify(value: Any, default: str = "workload") -> str:
    text = str(value or "").strip()
    text = re.sub(r"[^A-Za-z0-9_.-]+", "-", text).strip("-._")
    return text[:96] or default


def read_json(path: Path) -> Any:
    with path.open("r", encoding="utf-8") as f:
        return json.load(f)


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def finite_number(value: Any) -> Optional[float]:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if math.isfinite(number):
        return number
    return None


def unique_strings(values: Iterable[Any]) -> List[str]:
    seen: set[str] = set()
    out: List[str] = []
    for value in values:
        text = str(value or "").strip()
        if not text or text in seen:
            continue
        seen.add(text)
        out.append(text)
    return out


def claim_grade_blocker_values(value: Any) -> List[str]:
    if isinstance(value, list):
        return unique_strings(value)
    if value:
        return unique_strings([value])
    return []


def benchmark_owned_json_declared(value: Optional[Dict[str, Any]]) -> bool:
    if not isinstance(value, dict):
        return False
    contract = value.get("timing_contract") if isinstance(value.get("timing_contract"), dict) else {}
    measurement_source = str(
        value.get("measurement_source")
        or value.get("timing_source")
        or contract.get("measurement_source")
        or contract.get("source")
        or value.get("measurement")
        or ""
    ).strip().lower()
    return (
        value.get("benchmark_owned_json") is True
        or contract.get("benchmark_owned_json") is True
        or measurement_source in {"benchmark_owned_json", "benchmark-json", "benchmark_json"}
    )


def child_timing_claim_blockers(metric_metadata: Optional[Dict[str, Any]]) -> List[str]:
    if not isinstance(metric_metadata, dict):
        return []
    blockers = claim_grade_blocker_values(metric_metadata.get("claim_grade_blockers"))
    allocator_semantics = metric_metadata.get("allocator_semantics")
    if isinstance(allocator_semantics, dict):
        blockers.extend(claim_grade_blocker_values(allocator_semantics.get("claim_grade_blockers")))
    if metric_metadata.get("claim_grade") is False:
        blockers.append("child timing JSON explicitly marks claim_grade=false")
    if metric_metadata.get("success") is False:
        blockers.append("child timing JSON explicitly reports success=false")
    return unique_strings(blockers)


def sample_time_value(record: Dict[str, Any]) -> Optional[float]:
    for field in (
        "seconds",
        "time_seconds",
        "wall_seconds",
        "elapsed_seconds",
    ):
        if field in record:
            value = finite_number(record.get(field))
            if value is not None:
                return value
    for field in ("ns_per_iter", "time_ns", "nanoseconds"):
        if field in record:
            value = finite_number(record.get(field))
            if value is not None:
                return value / 1_000_000_000.0
    for field in ("ms", "milliseconds"):
        if field in record:
            value = finite_number(record.get(field))
            if value is not None:
                return value / 1_000.0
    return None


def stdout_json_last_object(stdout: Path) -> Optional[Dict[str, Any]]:
    if not stdout.exists():
        return None
    for raw in reversed(stdout.read_text(encoding="utf-8", errors="replace").splitlines()):
        line = raw.strip()
        if not line:
            continue
        try:
            value = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(value, dict):
            return value
    return None


def stdout_json_timing_seconds(stdout: Path, fields: Iterable[str]) -> Tuple[Optional[float], Optional[Dict[str, Any]]]:
    value = stdout_json_last_object(stdout)
    if value is None:
        return None, None
    wanted = [str(field) for field in fields if str(field)]
    if wanted:
        for field in wanted:
            if field not in value:
                continue
            if field in SUPPORTED_TIME_FIELDS:
                converted = sample_time_value({field: value.get(field)})
            else:
                converted = finite_number(value.get(field))
            if converted is not None:
                return converted, value
    else:
        converted = sample_time_value(value)
        if converted is not None:
            return converted, value
    return None, value


def bundle_rules(bundle: Dict[str, Any]) -> List[Dict[str, Any]]:
    raw = bundle.get("workloads")
    if raw is None:
        raw = bundle.get("rules")
    if not isinstance(raw, list):
        return []
    return [rule for rule in raw if isinstance(rule, dict)]


def match_values(rule: Dict[str, Any], singular: str, plural: str) -> Optional[List[str]]:
    if plural in rule:
        raw = rule.get(plural)
    elif singular in rule:
        raw = rule.get(singular)
    else:
        return None
    if raw is None:
        return None
    if isinstance(raw, list):
        return [str(item) for item in raw]
    return [str(raw)]


def patterns_match(patterns: Optional[List[str]], value: Any) -> bool:
    if not patterns:
        return True
    text = str(value or "")
    return any(pattern == "*" or fnmatch.fnmatchcase(text, pattern) for pattern in patterns)


def rule_matches(rule: Dict[str, Any], cell: Dict[str, Any]) -> bool:
    if rule.get("enabled") is False:
        return False
    dimensions = [
        ("dataset", "datasets", True),
        ("benchmark", "benchmarks", True),
        ("allocator", "allocators", True),
        ("category", "categories", False),
        ("variant_feature", "variant_features", False),
    ]
    for singular, plural, required in dimensions:
        value = cell.get(singular)
        if not required and (value is None or str(value).strip() == ""):
            continue
        if not patterns_match(match_values(rule, singular, plural), value):
            return False
    return True


def find_rule(bundle: Dict[str, Any], cell: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    for rule in bundle_rules(bundle):
        if rule_matches(rule, cell):
            return rule
    return None


def path_from_raw(raw: Any, *, base: Path = ROOT) -> Path:
    path = Path(str(raw or ".")).expanduser()
    if path.is_absolute():
        return path
    return base / path


def template_fields(
    *,
    cell: Dict[str, Any],
    rule: Dict[str, Any],
    bundle_path: Path,
    workload_dir: Path,
    run_id: str,
    run_index: int,
) -> Dict[str, str]:
    dataset = str(cell.get("dataset") or "")
    benchmark = str(cell.get("benchmark") or "")
    allocator = str(cell.get("allocator") or "")
    variant_feature = str(cell.get("variant_feature") or "")
    category = str(cell.get("category") or "")
    return {
        "root": str(ROOT),
        "evaluation_dir": str(EVAL),
        "bundle": str(bundle_path),
        "bundle_dir": str(bundle_path.parent),
        "workload_dir": str(workload_dir),
        "rule_id": str(rule.get("id") or rule.get("name") or ""),
        "dataset": dataset,
        "benchmark": benchmark,
        "allocator": allocator,
        "variant_feature": variant_feature,
        "category": category,
        "run_id": run_id,
        "run_index": str(run_index),
        "dataset_slug": slugify(dataset, "dataset"),
        "benchmark_slug": slugify(benchmark, "benchmark"),
        "allocator_slug": slugify(allocator, "allocator"),
        "variant_slug": slugify(variant_feature, "variant"),
    }


def render_json_literal_value(value: Any, fields: Dict[str, str]) -> Any:
    """Render placeholders inside a JSON literal without treating keys as fields."""

    if isinstance(value, str):
        return value.format(**fields)
    if isinstance(value, list):
        return [render_json_literal_value(item, fields) for item in value]
    if isinstance(value, dict):
        return {
            str(key): render_json_literal_value(item, fields)
            for key, item in value.items()
        }
    return value


def render_value(value: Any, fields: Dict[str, str]) -> Any:
    if isinstance(value, str):
        stripped = value.strip()
        if stripped.startswith("{") or stripped.startswith("["):
            try:
                parsed = json.loads(value)
            except json.JSONDecodeError:
                pass
            else:
                rendered = render_json_literal_value(parsed, fields)
                return json.dumps(rendered, sort_keys=True, separators=(",", ":"))
        return value.format(**fields)
    if isinstance(value, list):
        return [render_value(item, fields) for item in value]
    if isinstance(value, dict):
        return {str(key): render_value(item, fields) for key, item in value.items()}
    return value


def normalize_command(command_template: Any, fields: Dict[str, str], *, shell: bool) -> Tuple[Any, List[str]]:
    rendered = render_value(command_template, fields)
    if shell:
        if isinstance(rendered, list):
            shell_command = " ".join(shlex.quote(str(part)) for part in rendered)
        else:
            shell_command = str(rendered)
        return shell_command, [shell_command]
    if isinstance(rendered, list):
        argv = [str(part) for part in rendered]
    else:
        argv = shlex.split(str(rendered))
    if not argv:
        raise ValueError("rendered command is empty")
    return argv, argv


def build_env(rule: Dict[str, Any], fields: Dict[str, str], allocator: str) -> Tuple[Dict[str, str], List[str]]:
    env = os.environ.copy()
    env.setdefault("GLIBC_TUNABLES", "glibc.pthread.rseq=0")
    env.setdefault("UNIALLOC_PAPER_DATASET", fields["dataset"])
    env.setdefault("UNIALLOC_PAPER_BENCHMARK", fields["benchmark"])
    env.setdefault("UNIALLOC_PAPER_ALLOCATOR", allocator)
    env.setdefault("UNIALLOC_PAPER_VARIANT", fields["variant_feature"])
    applied: List[str] = []
    common_env = rule.get("env")
    if isinstance(common_env, dict):
        for key, value in render_value(common_env, fields).items():
            env[str(key)] = str(value)
            applied.append(str(key))
    allocator_env = rule.get("allocator_env")
    if isinstance(allocator_env, dict):
        raw = allocator_env.get(allocator) or allocator_env.get("*")
        if isinstance(raw, dict):
            for key, value in render_value(raw, fields).items():
                env[str(key)] = str(value)
                applied.append(str(key))
    return env, sorted(set(applied))


def timing_from_rule(rule: Dict[str, Any], stdout: Path, wall_seconds: float) -> Tuple[Optional[float], str, Optional[str], Optional[Dict[str, Any]]]:
    fields: List[str] = []
    if rule.get("time_field"):
        fields.append(str(rule.get("time_field")))
    for item in rule.get("time_fields", []) or []:
        fields.append(str(item))
    measurement = str(rule.get("measurement") or "stdout_json").strip().lower()
    if measurement in {"stdout_json", "json", "auto"}:
        parsed, metadata = stdout_json_timing_seconds(stdout, fields)
        if parsed is not None:
            return parsed, "stdout_json", None, metadata
        if measurement != "auto":
            wanted = ", ".join(fields) if fields else "standard timing fields"
            return None, "stdout_json", f"stdout JSON timing not found ({wanted})", metadata
    if measurement in {"wall_seconds", "wall", "auto"}:
        return wall_seconds, "wall_seconds", None, None
    return None, measurement or "unknown", f"unsupported measurement mode: {measurement}", None


def terminate_process_group(proc: subprocess.Popen[Any], *, grace_seconds: float = 2.0) -> bool:
    """Terminate an external workload subprocess and any children it spawned."""
    if proc.poll() is not None:
        return False
    try:
        if os.name == "posix":
            os.killpg(proc.pid, signal.SIGTERM)
        else:  # pragma: no cover - non-posix fallback for Windows runners.
            proc.terminate()
        try:
            proc.wait(timeout=grace_seconds)
        except subprocess.TimeoutExpired:
            if os.name == "posix":
                os.killpg(proc.pid, signal.SIGKILL)
            else:  # pragma: no cover - non-posix fallback for Windows runners.
                proc.kill()
            proc.wait(timeout=grace_seconds)
        return True
    except ProcessLookupError:
        return False


def positive_int_value(value: Any, default: int) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return default
    return parsed if parsed > 0 else default


class BoundedPipeCapture:
    """Drain a child pipe while retaining only a bounded tail in memory."""

    def __init__(self, pipe: Any, max_bytes: int) -> None:
        self.pipe = pipe
        self.max_bytes = max(1, int(max_bytes))
        self.total_bytes = 0
        self._tail = bytearray()
        self.thread = threading.Thread(target=self._drain, daemon=True)

    def start(self) -> None:
        self.thread.start()

    def join(self, timeout: float = 5.0) -> None:
        self.thread.join(timeout=timeout)

    def _drain(self) -> None:
        try:
            while True:
                chunk = self.pipe.read(64 * 1024)
                if not chunk:
                    break
                if isinstance(chunk, str):
                    chunk = chunk.encode("utf-8", errors="replace")
                self.total_bytes += len(chunk)
                self._tail.extend(chunk)
                if len(self._tail) > self.max_bytes:
                    del self._tail[: len(self._tail) - self.max_bytes]
        finally:
            try:
                self.pipe.close()
            except Exception:
                pass

    @property
    def retained_bytes(self) -> int:
        return len(self._tail)

    @property
    def truncated(self) -> bool:
        return self.total_bytes > self.retained_bytes

    def bytes(self) -> bytes:
        return bytes(self._tail)


def run_command_bounded(
    command: Any,
    *,
    cwd: Path,
    env: Dict[str, str],
    shell: bool,
    timeout_seconds: int,
    max_output_bytes: int,
) -> Dict[str, Any]:
    started_at = now_iso()
    try:
        proc = subprocess.Popen(
            command,
            cwd=str(cwd),
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            shell=shell,
            start_new_session=(os.name == "posix"),
        )
    except FileNotFoundError as exc:
        return {
            "exit_code": 127,
            "error": str(exc),
            "stdout_tail": b"",
            "stderr_tail": b"",
            "stdout_bytes": 0,
            "stderr_bytes": 0,
            "stdout_retained_bytes": 0,
            "stderr_retained_bytes": 0,
            "stdout_truncated": False,
            "stderr_truncated": False,
            "process_group_pid": None,
            "process_group_terminated": False,
            "started_at": started_at,
            "ended_at": now_iso(),
        }
    assert proc.stdout is not None
    assert proc.stderr is not None
    stdout_capture = BoundedPipeCapture(proc.stdout, max_output_bytes)
    stderr_capture = BoundedPipeCapture(proc.stderr, max_output_bytes)
    stdout_capture.start()
    stderr_capture.start()
    error = None
    process_group_terminated = False
    try:
        exit_code = proc.wait(timeout=timeout_seconds)
    except subprocess.TimeoutExpired as exc:
        exit_code = 124
        error = f"timeout after {timeout_seconds}s: {exc}"
        process_group_terminated = terminate_process_group(proc)
    stdout_capture.join()
    stderr_capture.join()
    return {
        "exit_code": exit_code,
        "error": error,
        "stdout_tail": stdout_capture.bytes(),
        "stderr_tail": stderr_capture.bytes(),
        "stdout_bytes": stdout_capture.total_bytes,
        "stderr_bytes": stderr_capture.total_bytes,
        "stdout_retained_bytes": stdout_capture.retained_bytes,
        "stderr_retained_bytes": stderr_capture.retained_bytes,
        "stdout_truncated": stdout_capture.truncated,
        "stderr_truncated": stderr_capture.truncated,
        "process_group_pid": proc.pid,
        "process_group_terminated": process_group_terminated,
        "started_at": started_at,
        "ended_at": now_iso(),
    }


def host_metadata() -> Dict[str, Any]:
    return {
        "system": platform.system(),
        "release": platform.release(),
        "machine": platform.machine(),
        "python": sys.version.split()[0],
    }


def failure_record(args: argparse.Namespace, message: str, *, exit_code: int = 2) -> Dict[str, Any]:
    return {
        "schema_version": 1,
        "source": "paper-external-workload-run",
        "run_id": args.run_id,
        "run_index": args.run_index,
        "dataset": args.dataset,
        "benchmark": args.benchmark,
        "allocator": args.allocator,
        "variant_feature": args.variant_feature,
        "success": False,
        "exit_code": exit_code,
        "error": message,
        "host": host_metadata(),
        "claim_grade": False,
        "claim_grade_blockers": [message],
    }


def run(args: argparse.Namespace) -> int:
    bundle_path = Path(args.bundle).expanduser()
    if not bundle_path.exists():
        print(json.dumps(failure_record(args, f"bundle not found: {bundle_path}"), sort_keys=True))
        return 2
    try:
        bundle = read_json(bundle_path)
    except json.JSONDecodeError as exc:
        print(json.dumps(failure_record(args, f"bundle is not valid JSON: {exc}"), sort_keys=True))
        return 2
    if not isinstance(bundle, dict):
        print(json.dumps(failure_record(args, "bundle must be a JSON object"), sort_keys=True))
        return 2
    cell = {
        "dataset": args.dataset,
        "benchmark": args.benchmark,
        "allocator": args.allocator,
        "variant_feature": args.variant_feature,
        "category": args.category,
    }
    rule = find_rule(bundle, cell)
    if rule is None:
        print(json.dumps(failure_record(args, "no bundle workload rule matches requested cell"), sort_keys=True))
        return 2
    rule_id = str(rule.get("id") or rule.get("name") or "matched-rule")
    workload_dir = path_from_raw(rule.get("workload_dir"), base=ROOT)
    fields = template_fields(
        cell=cell,
        rule=rule,
        bundle_path=bundle_path.resolve(),
        workload_dir=workload_dir,
        run_id=args.run_id,
        run_index=args.run_index,
    )
    blockers: List[str] = []
    if rule.get("template_only") and not args.allow_template:
        blockers.append("matched bundle workload is template_only")
    if not workload_dir.exists():
        blockers.append(f"workload_dir does not exist: {workload_dir}")
    command_template = rule.get("command_template") if "command_template" in rule else rule.get("command")
    if command_template is None:
        blockers.append("matched bundle workload has no command_template/command")
    cwd_raw = render_value(rule.get("cwd", "{workload_dir}"), fields)
    cwd = path_from_raw(cwd_raw, base=ROOT)
    if not cwd.exists():
        blockers.append(f"cwd does not exist: {cwd}")
    shell = bool(rule.get("shell"))
    command: Any = None
    command_for_record: List[str] = []
    if command_template is not None:
        try:
            command, command_for_record = normalize_command(command_template, fields, shell=shell)
        except (KeyError, ValueError) as exc:
            blockers.append(str(exc))
    if blockers:
        record = failure_record(args, "; ".join(blockers))
        record.update(
            {
                "rule_id": rule_id,
                "workload_dir": str(workload_dir),
                "cwd": str(cwd),
                "command": command_for_record,
                "dry_run": bool(args.dry_run),
            }
        )
        print(json.dumps(record, sort_keys=True))
        return 2

    output_dir = Path(args.output_dir).expanduser() if args.output_dir else RAW / "paper-external-workload-runs" / args.run_id
    log_dir = output_dir / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    label = slugify(f"{args.dataset}-{args.benchmark}-{args.allocator}-run{args.run_index}")
    stdout = log_dir / f"{label}.stdout.txt"
    stderr = log_dir / f"{label}.stderr.txt"
    timeout = positive_int_value(args.timeout or rule.get("timeout_seconds") or rule.get("timeout"), 1800)
    max_output_bytes = positive_int_value(
        args.max_output_bytes or rule.get("max_output_bytes") or rule.get("max_log_bytes"),
        DEFAULT_MAX_CHILD_OUTPUT_BYTES,
    )
    env, applied_env_keys = build_env(rule, fields, args.allocator)

    if args.dry_run:
        record = {
            "schema_version": 1,
            "source": "paper-external-workload-run",
            "dry_run": True,
            "run_id": args.run_id,
            "run_index": args.run_index,
            "dataset": args.dataset,
            "benchmark": args.benchmark,
            "allocator": args.allocator,
            "variant_feature": args.variant_feature,
            "rule_id": rule_id,
            "workload_dir": str(workload_dir),
            "cwd": str(cwd),
            "command": command_for_record,
            "shell": shell,
            "timeout_seconds": timeout,
            "max_output_bytes": max_output_bytes,
            "applied_env_keys": applied_env_keys,
            "success": True,
            "runnable": True,
            "host": host_metadata(),
        }
        print(json.dumps(record, sort_keys=True))
        return 0

    started = now_iso()
    start = time.perf_counter()
    try:
        child_result = run_command_bounded(
            command,
            cwd=cwd,
            env=env,
            shell=shell,
            timeout_seconds=timeout,
            max_output_bytes=max_output_bytes,
        )
    except KeyboardInterrupt:
        raise
    except Exception as exc:  # pragma: no cover - diagnostic safety net
        child_result = {
            "exit_code": 1,
            "error": str(exc),
            "stdout_tail": b"",
            "stderr_tail": b"",
            "stdout_bytes": 0,
            "stderr_bytes": 0,
            "stdout_retained_bytes": 0,
            "stderr_retained_bytes": 0,
            "stdout_truncated": False,
            "stderr_truncated": False,
            "process_group_pid": None,
            "process_group_terminated": False,
        }
    stdout.write_bytes(child_result.get("stdout_tail", b""))
    stderr.write_bytes(child_result.get("stderr_tail", b""))
    exit_code = int(child_result.get("exit_code") or 0)
    error = child_result.get("error")
    process_group_pid = child_result.get("process_group_pid")
    process_group_terminated = bool(child_result.get("process_group_terminated"))
    wall = time.perf_counter() - start
    ended = now_iso()
    seconds, measurement_source, metric_error, metric_metadata = timing_from_rule(rule, stdout, wall)
    rule_benchmark_owned_json = benchmark_owned_json_declared(rule)
    child_benchmark_owned_json = benchmark_owned_json_declared(metric_metadata)
    child_claim_blockers = child_timing_claim_blockers(metric_metadata)
    success = exit_code == 0 and seconds is not None and metric_error is None
    evidence = [
        {"kind": "stdout", "path": str(stdout), "sha256": file_sha256(stdout)},
        {"kind": "stderr", "path": str(stderr), "sha256": file_sha256(stderr)},
        {"kind": "bundle", "path": str(bundle_path.resolve()), "sha256": file_sha256(bundle_path)},
    ]
    record = {
        "schema_version": 1,
        "source": "paper-external-workload-run",
        "run_id": args.run_id,
        "run_index": args.run_index,
        "dataset": args.dataset,
        "benchmark": args.benchmark,
        "allocator": args.allocator,
        "variant_feature": args.variant_feature,
        "category": args.category,
        "rule_id": rule_id,
        "workload_dir": str(workload_dir),
        "cwd": str(cwd),
        "command": command_for_record,
        "shell": shell,
        "timeout_seconds": timeout,
        "max_output_bytes": max_output_bytes,
        "process_group_pid": process_group_pid,
        "process_group_terminated": process_group_terminated,
        "started_at": started,
        "ended_at": ended,
        "wall_seconds": wall,
        "seconds": seconds,
        "measurement_source": measurement_source,
        "rule_benchmark_owned_json": rule_benchmark_owned_json,
        "child_benchmark_owned_json": child_benchmark_owned_json,
        "benchmark_owned_json": bool(rule_benchmark_owned_json and child_benchmark_owned_json),
        "exit_code": exit_code,
        "success": success,
        "stdout": str(stdout),
        "stderr": str(stderr),
        "stdout_bytes": child_result.get("stdout_bytes"),
        "stderr_bytes": child_result.get("stderr_bytes"),
        "stdout_retained_bytes": child_result.get("stdout_retained_bytes"),
        "stderr_retained_bytes": child_result.get("stderr_retained_bytes"),
        "stdout_truncated": child_result.get("stdout_truncated"),
        "stderr_truncated": child_result.get("stderr_truncated"),
        "evidence": evidence,
        "host": host_metadata(),
        "applied_env_keys": applied_env_keys,
        "error": error,
        "metric_error": metric_error,
    }
    if metric_metadata is not None:
        record["metric_metadata"] = metric_metadata
    blockers: List[str] = []
    if error:
        blockers.append(error)
    if metric_error:
        blockers.append(metric_error)
    if measurement_source != "stdout_json":
        blockers.append("claim-grade paper workloads should emit benchmark-owned stdout JSON timing")
    if rule.get("claim_grade") is True and not rule_benchmark_owned_json:
        blockers.append("bundle rule timing contract does not mark benchmark_owned_json=true")
    if rule.get("claim_grade") is True and not child_benchmark_owned_json:
        blockers.append("child stdout JSON timing record does not mark benchmark_owned_json=true")
    for child_blocker in child_claim_blockers:
        blockers.append(child_blocker)
    if child_result.get("stdout_truncated"):
        blockers.append(f"child stdout exceeded max-output-bytes={max_output_bytes}; retained tail only")
    if child_result.get("stderr_truncated"):
        blockers.append(f"child stderr exceeded max-output-bytes={max_output_bytes}; retained tail only")
    if rule.get("claim_grade") is False:
        blockers.append("bundle rule explicitly marks this workload non-claim-grade")
    if blockers:
        record["claim_grade"] = False
        record["claim_grade_blockers"] = sorted(set(blockers))
    elif rule.get("claim_grade") is True:
        record["claim_grade"] = True
        record["claim_grade_blockers"] = []
    print(json.dumps(record, sort_keys=True))
    return 0 if success else 1


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--bundle", required=True, help="External workload bundle JSON")
    parser.add_argument("--dataset", required=True)
    parser.add_argument("--benchmark", required=True)
    parser.add_argument("--allocator", required=True)
    parser.add_argument("--variant-feature", default="")
    parser.add_argument("--category", default="")
    parser.add_argument("--run-id", default=_dt.datetime.now().strftime("paper-external-workload-%Y%m%d-%H%M%S"))
    parser.add_argument("--run-index", type=int, default=1)
    parser.add_argument("--timeout", type=int)
    parser.add_argument("--max-output-bytes", type=int, help="Maximum stdout/stderr bytes retained per child stream")
    parser.add_argument("--output-dir")
    parser.add_argument("--dry-run", action="store_true", help="Render and validate the command without executing it")
    parser.add_argument("--allow-template", action="store_true", help="Allow template_only rules for harness debugging only")
    return run(parser.parse_args(argv))


if __name__ == "__main__":
    raise SystemExit(main())
