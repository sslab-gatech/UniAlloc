#!/usr/bin/env python3
"""Strict configurable adapter for one paper macro workload family.

Generated per-workload wrappers call this module.  The adapter refuses to emit
paper timing unless a local workload_config.json explicitly points at a real
benchmark checkout and that benchmark emits finite JSON timing.
"""

from __future__ import annotations

import argparse
import datetime as _dt
import json
import math
import os
import platform
import re
import shlex
import signal
import socket
import subprocess
import sys
import threading
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional


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

SELECTOR_PLACEHOLDERS = ["dataset", "benchmark", "allocator", "variant_feature", "run_index"]
DEFAULT_CHILD_TIMEOUT_SECONDS = 1800
DEFAULT_MAX_CHILD_OUTPUT_BYTES = 4 * 1024 * 1024


def now_iso() -> str:
    return _dt.datetime.now(_dt.timezone.utc).isoformat().replace("+00:00", "Z")


def read_json(path: Path) -> Any:
    with path.open("r", encoding="utf-8") as handle:
        return json.load(handle)


def finite_number(value: Any) -> Optional[float]:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def convert_time_seconds(field: str, value: Any) -> Optional[float]:
    number = finite_number(value)
    if number is None:
        return None
    if field in {"ns_per_iter", "time_ns", "nanoseconds"}:
        return number / 1_000_000_000.0
    if field in {"ms", "milliseconds"}:
        return number / 1_000.0
    return number


def last_json_object(text: str) -> Optional[Dict[str, Any]]:
    for raw in reversed(text.splitlines()):
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


def emit(record: Dict[str, Any]) -> None:
    record.setdefault("source", "paper-external-workload-adapter")
    print(json.dumps(record, sort_keys=True))


def render_json_literal_value(value: Any, fields: Dict[str, str]) -> Any:
    """Render placeholders inside JSON literal command arguments safely.

    Some workload wrappers pass structured JSON as a single argv token (for
    example an input-contract object).  Calling ``str.format`` directly on a
    literal such as ``{"blockers":[]}`` treats ``"blockers"`` as a template
    field name.  If the whole token is valid JSON, parse it first and render
    placeholders only inside JSON string values, then serialize it back to a
    deterministic compact JSON token.
    """

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


def render_string(value: str, fields: Dict[str, str]) -> str:
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


def render_value(value: Any, fields: Dict[str, str]) -> Any:
    if isinstance(value, str):
        return render_string(value, fields)
    if isinstance(value, list):
        return [render_value(item, fields) for item in value]
    if isinstance(value, dict):
        return {str(key): render_value(item, fields) for key, item in value.items()}
    return value


def unresolved_template(value: Any) -> bool:
    if isinstance(value, str):
        return bool(re.search(r"\{[A-Za-z0-9_]+\}", value))
    if isinstance(value, list):
        return any(unresolved_template(item) for item in value)
    if isinstance(value, dict):
        return any(unresolved_template(item) for item in value.values())
    return False


def normalize_argv(command: Any) -> List[str]:
    if isinstance(command, list):
        return [str(part) for part in command]
    return shlex.split(str(command or ""))


def config_fields(args: argparse.Namespace, adapter_dir: Path, config_dir: Path, config: Dict[str, Any]) -> Dict[str, str]:
    real_workload_dir_raw = config.get("real_workload_dir") or ""
    real_workload_dir = Path(str(real_workload_dir_raw)).expanduser()
    if real_workload_dir_raw and not real_workload_dir.is_absolute():
        real_workload_dir = (config_dir / real_workload_dir).resolve()
    return {
        "adapter_dir": str(adapter_dir),
        "config_dir": str(config_dir),
        "real_workload_dir": str(real_workload_dir) if real_workload_dir_raw else "",
        "dataset": str(args.dataset or ""),
        "benchmark": str(args.benchmark or ""),
        "allocator": str(args.allocator or ""),
        "variant_feature": str(args.variant_feature or ""),
        "run_index": str(args.run_index),
    }


def find_time_seconds(record: Dict[str, Any], fields: Iterable[str]) -> Optional[float]:
    wanted = [str(field) for field in fields if str(field)]
    if not wanted:
        wanted = [
            "seconds",
            "time_seconds",
            "wall_seconds",
            "elapsed_seconds",
            "ns_per_iter",
            "time_ns",
            "nanoseconds",
            "ms",
            "milliseconds",
        ]
    for field in wanted:
        if field not in record:
            continue
        seconds = convert_time_seconds(field, record.get(field))
        if seconds is not None:
            return seconds
    return None


def load_config(config_dir: Path) -> tuple[Optional[Path], Optional[Dict[str, Any]]]:
    for name in ("workload_config.json", "workload_config.local.json"):
        path = config_dir / name
        if path.exists():
            value = read_json(path)
            if not isinstance(value, dict):
                raise ValueError(f"adapter config is not a JSON object: {path}")
            return path, value
    return None, None


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


def host_metadata() -> Dict[str, Any]:
    return {
        "system": platform.system(),
        "release": platform.release(),
        "version": platform.version(),
        "machine": platform.machine(),
        "processor": platform.processor(),
        "python": sys.version.split()[0],
        "hostname": socket.gethostname(),
    }


HEAD_LIKE_REFS = {"", "head", "main", "master", "trunk", "latest"}
GIT_FULL_SHA_RE = re.compile(r"^[0-9a-fA-F]{40}$")
NON_EXACT_REPRODUCIBLE_REF_KINDS = {"fallback", "newer"}
USER_ACCEPTED_NEWER_SOURCE_PROVENANCE_CLASS = "user_accepted_newer_pinned_source"


def accepted_newer_benchmark_source_policy(*containers: Any) -> Dict[str, Any]:
    """Return explicit user policy for non-paper-exact pinned newer sources."""

    accepted = False
    reason = ""
    for container in containers:
        if not isinstance(container, dict):
            continue
        raw = (
            container.get("accepted_newer_benchmark_source")
            if "accepted_newer_benchmark_source" in container
            else container.get("accept_newer_benchmark_source")
        )
        if raw is None:
            raw = container.get("newer_benchmark_source_policy")
        if isinstance(raw, dict):
            if (
                raw.get("accepted") is True
                or raw.get("allow") is True
                or raw.get("enabled") is True
                or raw.get("allow_newer_benchmark_source") is True
            ):
                accepted = True
                reason = first_nonempty_text(
                    [raw.get("reason"), raw.get("rationale"), raw.get("source")],
                    reason,
                )
        elif raw is True:
            accepted = True
        if container.get("user_accepted_newer_source") is True:
            accepted = True
            reason = first_nonempty_text([container.get("reason")], reason)
    if accepted and not reason:
        reason = "user explicitly allowed pinned newer benchmark source for current non-paper-exact evaluation"
    return {"accepted": bool(accepted), "reason": reason or None, "paper_exact": False if accepted else None}


def source_contract_status(config: Dict[str, Any]) -> Dict[str, Any]:
    """Validate paper-source provenance without blocking diagnostic execution."""

    source = config.get("paper_source") if isinstance(config.get("paper_source"), dict) else {}
    accepted_newer_policy = accepted_newer_benchmark_source_policy(config, source)
    accept_newer_source = accepted_newer_policy.get("accepted") is True
    upstream_url = str(source.get("upstream_url") or "").strip()
    raw_candidates = source.get("ref_candidates") or []
    ref_candidates: List[Dict[str, Any]] = []
    exact_candidates: List[Dict[str, Any]] = []
    reproducible_non_exact_candidates: List[Dict[str, Any]] = []
    newer_candidates: List[Dict[str, Any]] = []
    fallback_candidates: List[Dict[str, Any]] = []
    head_candidates: List[Dict[str, Any]] = []
    if isinstance(raw_candidates, list):
        for raw in raw_candidates:
            if not isinstance(raw, dict):
                continue
            ref = str(raw.get("ref") or "").strip()
            if not ref:
                continue
            candidate = {**raw, "ref": ref}
            ref_candidates.append(candidate)
            kind = str(raw.get("kind") or "").strip().lower()
            lowered_ref = ref.lower()
            if kind == "exact" and lowered_ref not in HEAD_LIKE_REFS:
                exact_candidates.append(candidate)
            if kind in NON_EXACT_REPRODUCIBLE_REF_KINDS and lowered_ref not in HEAD_LIKE_REFS:
                reproducible_non_exact_candidates.append(candidate)
                if kind == "newer":
                    newer_candidates.append(candidate)
                if kind == "fallback":
                    fallback_candidates.append(candidate)
            if kind == "head" or lowered_ref in HEAD_LIKE_REFS:
                head_candidates.append(candidate)
    matchers = source.get("matchers") if isinstance(source.get("matchers"), list) else []
    matchers = [str(item).strip() for item in matchers if str(item).strip()]
    blockers: List[str] = []
    if not source:
        blockers.append("adapter paper_source metadata is missing")
    if not upstream_url:
        blockers.append("adapter paper_source upstream_url is missing")
    if not ref_candidates:
        blockers.append("adapter paper_source ref_candidates are missing")
    elif not exact_candidates:
        if accept_newer_source and reproducible_non_exact_candidates:
            pass
        elif head_candidates:
            blockers.append("adapter paper_source has only HEAD-like refs; exact paper version/ref provenance is required")
        else:
            blockers.append("adapter paper_source lacks an exact non-HEAD ref candidate")
    if not matchers:
        blockers.append("adapter paper_source repository matchers are missing")
    accepted_newer_ref_complete = bool(
        accept_newer_source and upstream_url and reproducible_non_exact_candidates and matchers
    )
    return {
        "complete": bool(upstream_url and exact_candidates and matchers),
        "paper_exact_ref_complete": bool(upstream_url and exact_candidates and matchers),
        "accepted_newer_ref_complete": accepted_newer_ref_complete,
        "accepted_non_exact_ref_complete": accepted_newer_ref_complete,
        "accepted_newer_policy": accepted_newer_policy,
        "source_provenance_class": (
            USER_ACCEPTED_NEWER_SOURCE_PROVENANCE_CLASS
            if accepted_newer_ref_complete
            else ("paper_exact_ref" if upstream_url and exact_candidates and matchers else "non_paper_exact")
        ),
        "reproducible_snapshot_complete": False,
        "upstream_url": upstream_url or None,
        "ref_candidates": ref_candidates,
        "exact_ref_candidates": exact_candidates,
        "reproducible_non_exact_ref_candidates": reproducible_non_exact_candidates,
        "newer_ref_candidates": newer_candidates,
        "fallback_ref_candidates": fallback_candidates,
        "head_ref_candidates": head_candidates,
        "has_exact_ref_candidate": bool(exact_candidates),
        "has_newer_ref_candidate": bool(newer_candidates),
        "has_fallback_ref_candidate": bool(fallback_candidates),
        "has_reproducible_non_exact_ref_candidate": bool(reproducible_non_exact_candidates),
        "has_head_ref_candidate": bool(head_candidates),
        "head_only": bool(head_candidates and not exact_candidates and not reproducible_non_exact_candidates),
        "matcher_count": len(matchers),
        "blockers": unique_strings(blockers),
    }


def source_contract_claim_grade_blockers(source_contract: Optional[Dict[str, Any]]) -> List[str]:
    if not isinstance(source_contract, dict):
        return ["adapter source contract is missing"]
    if source_contract.get("paper_exact_ref_complete") is True:
        return []
    if source_contract.get("accepted_newer_ref_complete") is True:
        return []
    blockers: List[str] = []
    if source_contract.get("head_only") is True:
        blockers.append("adapter source is HEAD-only and not paper-exact provenance")
    else:
        blockers.append("adapter source contract is not paper-exact complete")
    raw_blockers = source_contract.get("blockers")
    if isinstance(raw_blockers, list):
        blockers.extend(str(item) for item in raw_blockers if str(item).strip())
    elif raw_blockers:
        blockers.append(str(raw_blockers))
    return unique_strings(blockers)


def checkout_pin_validation(config: Dict[str, Any], real_workload_dir: Optional[Path]) -> Dict[str, Any]:
    """Validate an embedded exact-checkout pin when the config carries one.

    Config promotion stores the checkout commit that was audited as paper-exact.
    The adapter re-checks that commit at execution time so a later checkout
    drift cannot silently emit claim-grade timing.
    """

    activation = config.get("claim_grade_activation") if isinstance(config.get("claim_grade_activation"), dict) else {}
    source = config.get("paper_source") if isinstance(config.get("paper_source"), dict) else {}
    expected_commit = str(
        activation.get("checkout_commit")
        or source.get("checkout_commit")
        or source.get("expected_checkout_commit")
        or ""
    ).strip()
    blockers: List[str] = []
    current_commit: Optional[str] = None
    rev_parse: Dict[str, Any] = {}
    if not expected_commit:
        return {
            "expected_commit": None,
            "current_commit": None,
            "validated": None,
            "blockers": [],
        }
    if not GIT_FULL_SHA_RE.match(expected_commit):
        blockers.append("expected checkout commit is not a 40-hex git SHA")
    if real_workload_dir is None or not real_workload_dir.exists():
        blockers.append("cannot validate checkout pin because real_workload_dir is missing")
    else:
        try:
            proc = subprocess.run(
                ["git", "rev-parse", "HEAD"],
                cwd=str(real_workload_dir),
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                timeout=10,
            )
            current_commit = (proc.stdout or "").splitlines()[0].strip() if proc.stdout else ""
            rev_parse = {
                "exit_code": proc.returncode,
                "stdout": (proc.stdout or "").strip()[:2000],
                "stderr": (proc.stderr or "").strip()[:2000],
            }
            if proc.returncode != 0:
                blockers.append("git rev-parse HEAD failed while validating exact checkout pin")
            elif current_commit != expected_commit:
                blockers.append("real_workload_dir git HEAD does not match the promoted exact checkout commit")
        except (OSError, subprocess.SubprocessError) as exc:
            blockers.append(f"git rev-parse HEAD failed while validating exact checkout pin: {exc}")
    return {
        "expected_commit": expected_commit,
        "current_commit": current_commit or None,
        "validated": bool(expected_commit and not blockers and current_commit == expected_commit),
        "blockers": unique_strings(blockers),
        "activation_source": activation.get("source") if activation else None,
        "requested_ref": activation.get("requested_ref") if activation else None,
        "resolved_ref": activation.get("resolved_ref") if activation else None,
        "rev_parse": rev_parse or None,
    }


def adapter_claim_grade_blockers(
    *,
    config: Optional[Dict[str, Any]] = None,
    selector_contract: Optional[Dict[str, Any]] = None,
    timing_contract: Optional[Dict[str, Any]] = None,
    source_contract: Optional[Dict[str, Any]] = None,
    issues: Iterable[Any] = (),
    dry_run: bool = False,
    workload_failed: bool = False,
    reason: str = "",
) -> List[str]:
    blockers: List[Any] = []
    if reason:
        blockers.append(reason)
    if config is None:
        blockers.append("missing adapter configuration")
    else:
        if config.get("configured") is not True:
            blockers.append("adapter config remains configured=false")
        if config.get("claim_grade") is not True:
            blockers.append("adapter config remains claim_grade=false")
    if selector_contract is not None and not selector_contract.get("complete"):
        missing = ", ".join(str(item) for item in selector_contract.get("missing", []))
        blockers.append(
            "adapter selector contract is incomplete"
            + (f": missing {missing}" if missing else "")
        )
    if timing_contract is not None:
        if not timing_contract.get("declares_time_fields"):
            blockers.append("adapter timing contract does not declare stdout JSON timing fields")
        if not timing_contract.get("benchmark_owned_json"):
            blockers.append("adapter timing contract is not marked benchmark_owned_json=true")
    if source_contract is not None and config is not None and config.get("claim_grade") is True:
        blockers.extend(source_contract_claim_grade_blockers(source_contract))
    if dry_run:
        blockers.append("adapter dry-run validates shape only and is not timing evidence")
    if workload_failed:
        blockers.append("workload command failed or did not emit finite JSON timing")
    blockers.extend(str(issue) for issue in issues if str(issue).strip())
    return unique_strings(blockers)


def serialized_template_text(value: Any) -> str:
    return json.dumps(value, sort_keys=True, default=str)


def template_uses_placeholder(value: Any, name: str) -> bool:
    return f"{{{name}}}" in serialized_template_text(value)


def selector_contract_status(config: Dict[str, Any]) -> Dict[str, Any]:
    surfaces = {
        "command_template": config.get("command_template"),
        "cwd": config.get("cwd") or "",
        "env": config.get("env") or {},
    }
    present = {
        name: any(template_uses_placeholder(surface, name) for surface in surfaces.values())
        for name in SELECTOR_PLACEHOLDERS
    }
    missing = [name for name in SELECTOR_PLACEHOLDERS if not present.get(name)]
    return {
        "required_placeholders": SELECTOR_PLACEHOLDERS,
        "present": present,
        "missing": missing,
        "complete": not missing,
        "checked_surfaces": list(surfaces.keys()),
    }


def is_cargo_token(token: str) -> bool:
    text = str(token or "").strip()
    if not text:
        return False
    return Path(text).name == "cargo"


def read_rust_toolchain_file(base: Path) -> tuple[Optional[str], str]:
    for candidate in (base / "rust-toolchain", base / "rust-toolchain.toml"):
        if not candidate.exists():
            continue
        try:
            text = candidate.read_text(encoding="utf-8", errors="replace")
        except OSError:
            text = ""
        if candidate.name == "rust-toolchain.toml":
            match = re.search(r'(?m)^\s*channel\s*=\s*"([^"]+)"', text)
            return str(candidate), match.group(1).strip() if match else ""
        return str(candidate), next((line.strip() for line in text.splitlines() if line.strip()), "")
    return None, ""


def repo_root_from_script() -> Path:
    return Path(__file__).resolve().parents[2]


def cargo_toolchain_status(
    argv: List[str],
    rendered_env: Any,
    config: Dict[str, Any],
    cwd: Optional[Path] = None,
    inherited_env: Optional[Dict[str, str]] = None,
) -> Dict[str, Any]:
    env_values = rendered_env if isinstance(rendered_env, dict) else {}
    inherited = inherited_env if inherited_env is not None else os.environ
    invocations: List[Dict[str, Any]] = []
    for index, token in enumerate(argv):
        if not is_cargo_token(token):
            continue
        next_token = argv[index + 1] if index + 1 < len(argv) else ""
        invocations.append(
            {
                "index": index,
                "token": token,
                "explicit_toolchain": bool(str(next_token).startswith("+")),
                "next_token": next_token if str(next_token).startswith("+") else None,
            }
        )
    unpinned_cargo = any(not item["explicit_toolchain"] for item in invocations)
    rendered_toolchain = str(env_values.get("RUSTUP_TOOLCHAIN") or "").strip()
    inherited_toolchain = str(inherited.get("RUSTUP_TOOLCHAIN") or "").strip()
    allocator = str(env_values.get("UNIALLOC_PAPER_ALLOCATOR") or "").strip().lower()
    repo_toolchain_file, repo_toolchain = read_rust_toolchain_file(repo_root_from_script())
    repo_toolchain_applicable = allocator in {"unialloc", "ourself"}
    configured_default = first_nonempty_text(
        [
            config.get("external_rustup_toolchain"),
            config.get("rustup_toolchain"),
            inherited.get("UNIALLOC_EXTERNAL_RUSTUP_TOOLCHAIN"),
            repo_toolchain if repo_toolchain_applicable else "",
        ],
        "stable",
    )
    local_toolchain_file, local_toolchain = read_rust_toolchain_file(cwd) if cwd is not None else (None, "")
    should_inject = bool(unpinned_cargo and not rendered_toolchain and not inherited_toolchain and not local_toolchain)
    return {
        "cargo_invocations": invocations,
        "cargo_command_present": bool(invocations),
        "unpinned_cargo_command_present": bool(unpinned_cargo),
        "rendered_env_rustup_toolchain": rendered_toolchain or None,
        "inherited_rustup_toolchain": inherited_toolchain or None,
        "local_rust_toolchain_file": local_toolchain_file,
        "local_rust_toolchain": local_toolchain or None,
        "repo_rust_toolchain_file": repo_toolchain_file,
        "repo_rust_toolchain": repo_toolchain or None,
        "repo_rust_toolchain_applicable": repo_toolchain_applicable,
        "configured_default_toolchain": configured_default,
        "rustup_toolchain_injected": should_inject,
        "effective_rustup_toolchain": (
            rendered_toolchain
            or inherited_toolchain
            or local_toolchain
            or (configured_default if should_inject else None)
        ),
    }


def first_nonempty_text(values: Iterable[Any], default: str = "") -> str:
    for value in values:
        text = str(value or "").strip()
        if text:
            return text
    return default


def positive_int_value(value: Any, default: int) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return default
    return parsed if parsed > 0 else default


class BoundedPipeCapture:
    """Drain a child pipe while keeping only a bounded tail in memory."""

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

    def text(self) -> str:
        return bytes(self._tail).decode("utf-8", errors="replace")


def terminate_process_group(proc: subprocess.Popen[Any], *, grace_seconds: float = 2.0) -> bool:
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


def run_child_bounded(
    argv: List[str],
    *,
    cwd: Path,
    env: Dict[str, str],
    timeout_seconds: int,
    max_output_bytes: int,
) -> Dict[str, Any]:
    started_at = now_iso()
    try:
        proc = subprocess.Popen(
            argv,
            cwd=str(cwd),
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            start_new_session=(os.name == "posix"),
        )
    except FileNotFoundError as exc:
        return {
            "returncode": 127,
            "spawn_error": str(exc),
            "stdout": "",
            "stderr": "",
            "stdout_bytes": 0,
            "stderr_bytes": 0,
            "stdout_retained_bytes": 0,
            "stderr_retained_bytes": 0,
            "stdout_truncated": False,
            "stderr_truncated": False,
            "timed_out": False,
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
    timed_out = False
    process_group_terminated = False
    try:
        returncode = proc.wait(timeout=timeout_seconds)
    except subprocess.TimeoutExpired:
        timed_out = True
        process_group_terminated = terminate_process_group(proc)
        returncode = 124
    stdout_capture.join()
    stderr_capture.join()
    return {
        "returncode": returncode,
        "spawn_error": None,
        "stdout": stdout_capture.text(),
        "stderr": stderr_capture.text(),
        "stdout_bytes": stdout_capture.total_bytes,
        "stderr_bytes": stderr_capture.total_bytes,
        "stdout_retained_bytes": stdout_capture.retained_bytes,
        "stderr_retained_bytes": stderr_capture.retained_bytes,
        "stdout_truncated": stdout_capture.truncated,
        "stderr_truncated": stderr_capture.truncated,
        "timed_out": timed_out,
        "process_group_pid": proc.pid,
        "process_group_terminated": process_group_terminated,
        "started_at": started_at,
        "ended_at": now_iso(),
    }


def child_output_blockers(child_result: Dict[str, Any], max_output_bytes: int) -> List[str]:
    blockers: List[str] = []
    if child_result.get("timed_out"):
        blockers.append(f"workload command timed out after {child_result.get('timeout_seconds', '?')}s")
    if child_result.get("stdout_truncated"):
        blockers.append(
            f"child stdout exceeded max-output-bytes={max_output_bytes}; retained tail only"
        )
    if child_result.get("stderr_truncated"):
        blockers.append(
            f"child stderr exceeded max-output-bytes={max_output_bytes}; retained tail only"
        )
    spawn_error = child_result.get("spawn_error")
    if spawn_error:
        blockers.append(str(spawn_error))
    return blockers


def child_claim_grade_blockers(child_record: Optional[Dict[str, Any]]) -> List[str]:
    if not isinstance(child_record, dict):
        return []
    blockers: List[Any] = []
    raw = child_record.get("claim_grade_blockers")
    if isinstance(raw, list):
        blockers.extend(raw)
    elif raw:
        blockers.append(raw)
    allocator_semantics = child_record.get("allocator_semantics")
    if isinstance(allocator_semantics, dict):
        raw_semantic_blockers = allocator_semantics.get("claim_grade_blockers")
        if isinstance(raw_semantic_blockers, list):
            blockers.extend(raw_semantic_blockers)
        elif raw_semantic_blockers:
            blockers.append(raw_semantic_blockers)
    return unique_strings(blockers)


def command_bench_filters(command: Any) -> List[str]:
    parts = normalize_argv(command)
    filters: List[str] = []
    for index, part in enumerate(parts):
        if part in {"--bench-filter", "--bench-name-filter"} and index + 1 < len(parts):
            filters.append(parts[index + 1])
        elif part.startswith("--bench-filter=") or part.startswith("--bench-name-filter="):
            filters.append(part.split("=", 1)[1])
    return unique_strings(filters)


def command_claim_grade_contract(command: Any) -> str:
    parts = normalize_argv(command)
    for index, part in enumerate(parts):
        if part == "--claim-grade-contract" and index + 1 < len(parts):
            return parts[index + 1].strip()
        if part.startswith("--claim-grade-contract="):
            return part.split("=", 1)[1].strip()
    return ""


def configured_benchmark_names(config: Dict[str, Any]) -> List[str]:
    values: List[str] = []
    for key in ("benchmark", "benchmarks"):
        raw = config.get(key)
        if isinstance(raw, list):
            values.extend(str(item).strip() for item in raw if str(item).strip())
        elif str(raw or "").strip():
            values.append(str(raw).strip())
    return unique_strings(values)


def cargo_wrapper_required_claim_grade_contract(config: Dict[str, Any], command: Any) -> str:
    benchmarks = set(configured_benchmark_names(config))
    if "R-Polars" in benchmarks:
        return "rpolars-paper-run"
    if "RJS-Compiler" in benchmarks:
        return "rjs-compiler-paper-run"
    if "RPython" in benchmarks:
        return "rpython-paper-run"
    if "R-Oxipng*" in benchmarks or "R-Oxipng" in benchmarks:
        return "roxipng-paper-fragment"
    return "rpolars-paper-run"


def adapter_claim_grade_runner_surface_blockers(config: Dict[str, Any], command: Any) -> List[str]:
    blockers: List[str] = []
    for candidate in (
        config,
        config.get("cargo_bench_json_wrapper") if isinstance(config.get("cargo_bench_json_wrapper"), dict) else {},
    ):
        if not isinstance(candidate, dict):
            continue
        reason = str(candidate.get("not_claim_grade_reason") or "").strip()
        if reason:
            blockers.append(reason)
        if candidate.get("diagnostic_only") is True:
            blockers.append("adapter config is marked diagnostic_only=true")
    serialized_command = json.dumps(command, sort_keys=True, default=str)
    cargo_wrapper = isinstance(config.get("cargo_bench_json_wrapper"), dict)
    cargo_wrapper = cargo_wrapper or str(config.get("source") or "") == "paper-external-workload-cargo-bench-json-wrapper"
    cargo_wrapper = cargo_wrapper or "paper_external_cargo_bench_json.py" in serialized_command
    if cargo_wrapper:
        contract = command_claim_grade_contract(command)
        required_contract = cargo_wrapper_required_claim_grade_contract(config, command)
        if contract != required_contract:
            blockers.append(
                "cargo-bench JSON wrapper config does not request the required full-row claim-grade contract"
                + (f": {contract}" if contract else "")
                + f" (expected {required_contract})"
            )
        bench_filters = command_bench_filters(command)
        if bench_filters:
            blockers.append(
                "cargo-bench JSON wrapper representative command is bench-filtered subset evidence: "
                + ", ".join(bench_filters)
            )
    return unique_strings(blockers)


def child_allocator_metadata(child_record: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    if not isinstance(child_record, dict):
        return {}
    metadata: Dict[str, Any] = {}
    measurement_source = first_nonempty_text([child_record.get("measurement_source")])
    if measurement_source:
        metadata["measurement_source"] = measurement_source
    if child_record.get("benchmark_owned_json") is True:
        metadata["benchmark_owned_json"] = True
    paper_context = child_record.get("paper_context")
    if isinstance(paper_context, dict):
        metadata["paper_context"] = paper_context
    for key in (
        "uses_redis_benchmark",
        "uses_resp_bridge",
        "operations_per_second",
        "operation_count",
        "successful_operations",
        "failed_operations",
    ):
        if key in child_record:
            metadata[key] = child_record.get(key)
    allocator_feature = first_nonempty_text([child_record.get("allocator_feature")])
    if allocator_feature:
        metadata["allocator_feature"] = allocator_feature
    allocator_routing = child_record.get("allocator_routing")
    if isinstance(allocator_routing, dict):
        metadata["allocator_routing"] = allocator_routing
    allocator_semantics = child_record.get("allocator_semantics")
    if isinstance(allocator_semantics, dict):
        metadata["allocator_semantics"] = allocator_semantics
    child_blockers = child_claim_grade_blockers(child_record)
    if child_blockers:
        metadata["child_claim_grade_blockers"] = child_blockers
    return metadata


def timing_contract_status(config: Dict[str, Any]) -> Dict[str, Any]:
    contract = config.get("timing_contract") if isinstance(config.get("timing_contract"), dict) else {}
    time_fields = config.get("time_fields")
    if not isinstance(time_fields, list):
        time_fields = []
    time_field = str(config.get("time_field") or "").strip()
    declared_fields = unique_strings([time_field, *[str(field or "").strip() for field in time_fields]])
    measurement_source = first_nonempty_text(
        [
            config.get("measurement_source"),
            config.get("timing_source"),
            contract.get("measurement_source"),
            contract.get("source"),
            config.get("measurement"),
        ]
    )
    benchmark_owned_json = (
        config.get("benchmark_owned_json") is True
        or contract.get("benchmark_owned_json") is True
        or measurement_source.lower() in {"benchmark_owned_json", "benchmark-json", "benchmark_json"}
    )
    return {
        "declared_time_fields": declared_fields,
        "declares_time_fields": bool(declared_fields),
        "measurement_source": measurement_source or None,
        "benchmark_owned_json": bool(benchmark_owned_json),
    }


def not_configured_record(
    args: argparse.Namespace,
    adapter_dir: Path,
    config_dir: Path,
    reason: str,
    config: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    return {
        "success": False,
        "runnable": False,
        "claim_grade": False,
        "claim_grade_blockers": adapter_claim_grade_blockers(config=config, reason=reason),
        "adapter_configured": False,
        "dry_run": bool(args.dry_run),
        "error": reason,
        "host": host_metadata(),
        "expected_config": str(config_dir / "workload_config.json"),
        "accepted_local_config": str(config_dir / "workload_config.local.json"),
        "template_config": str(config_dir / "workload_config.template.json"),
        "adapter_dir": str(adapter_dir),
        "dataset": args.dataset,
        "benchmark": args.benchmark,
        "allocator": args.allocator,
        "variant_feature": args.variant_feature,
        "run_index": args.run_index,
        "generated_at": now_iso(),
    }


def main(default_config_dir: Optional[Path] = None, argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Run one configured paper macro workload adapter")
    parser.add_argument("--dataset", required=True)
    parser.add_argument("--benchmark", required=True)
    parser.add_argument("--allocator", required=True)
    parser.add_argument("--variant-feature", default="")
    parser.add_argument("--run-index", type=int, default=1)
    parser.add_argument("--json", action="store_true", help="Emit JSON status/timing records")
    parser.add_argument("--dry-run", action="store_true", help="Validate adapter configuration without running the workload")
    parser.add_argument("--timeout", type=int, help="Maximum seconds for the real child workload command")
    parser.add_argument(
        "--max-output-bytes",
        type=int,
        help="Maximum stdout/stderr bytes retained per stream from the child workload",
    )
    parser.add_argument("--config-dir", help="Directory containing workload_config.json; default is wrapper directory")
    args = parser.parse_args(argv)

    adapter_dir = Path(default_config_dir or Path.cwd()).resolve()
    config_dir = Path(args.config_dir).expanduser().resolve() if args.config_dir else adapter_dir
    try:
        config_path, config = load_config(config_dir)
    except Exception as exc:  # keep adapter failures machine-readable
        emit(not_configured_record(args, adapter_dir, config_dir, f"failed to read adapter config: {exc}"))
        return 2
    if config is None:
        emit(not_configured_record(args, adapter_dir, config_dir, "missing workload_config.json"))
        return 2
    if config.get("configured") is not True:
        emit(not_configured_record(args, adapter_dir, config_dir, "adapter config is not marked configured=true", config))
        return 2

    fields = config_fields(args, adapter_dir, config_dir, config)
    issues: List[str] = []
    selector_contract = selector_contract_status(config)
    timing_contract = timing_contract_status(config)
    source_contract = source_contract_status(config)
    if not selector_contract["complete"]:
        issues.append(
            "adapter command/env/cwd is missing selector placeholders: "
            + ", ".join(str(item) for item in selector_contract["missing"])
        )
    if not timing_contract["declares_time_fields"]:
        issues.append("adapter config does not declare stdout JSON time_field/time_fields")
    if config.get("claim_grade") is True and not timing_contract["benchmark_owned_json"]:
        issues.append("claim_grade adapter timing contract is not marked benchmark_owned_json=true")
    real_workload_dir = Path(fields["real_workload_dir"]) if fields["real_workload_dir"] else None
    if real_workload_dir is None or not str(real_workload_dir):
        issues.append("real_workload_dir is missing")
    elif not real_workload_dir.exists():
        issues.append(f"real_workload_dir does not exist: {real_workload_dir}")
    checkout_pin = checkout_pin_validation(config, real_workload_dir)
    if config.get("claim_grade") is True:
        issues.extend(str(item) for item in checkout_pin.get("blockers", []) if str(item).strip())
    command_template = config.get("command_template")
    if not command_template:
        issues.append("command_template is missing")
    try:
        rendered_command = render_value(command_template, fields) if command_template else []
        rendered_env = render_value(config.get("env") or {}, fields)
        rendered_cwd_raw = render_value(config.get("cwd") or "{real_workload_dir}", fields)
    except KeyError as exc:
        issues.append(f"unknown template field: {exc}")
        rendered_command = []
        rendered_env = {}
        rendered_cwd_raw = fields.get("real_workload_dir") or str(config_dir)
    if unresolved_template(rendered_command) or unresolved_template(rendered_env) or unresolved_template(rendered_cwd_raw):
        issues.append("rendered adapter command/env/cwd still contains template markers")
    argv_rendered = normalize_argv(rendered_command)
    if not argv_rendered:
        issues.append("rendered command is empty")
    if config.get("claim_grade") is True:
        issues.extend(
            f"adapter claim-grade runner blocker: {blocker}"
            for blocker in adapter_claim_grade_runner_surface_blockers(config, argv_rendered)
        )
    cwd_path = Path(str(rendered_cwd_raw)).expanduser()
    if not cwd_path.is_absolute():
        cwd_path = (config_dir / cwd_path).resolve()
    if not cwd_path.exists():
        issues.append(f"cwd does not exist: {cwd_path}")
    external_rustup_toolchain = cargo_toolchain_status(argv_rendered, rendered_env, config, cwd_path)
    timeout_seconds = positive_int_value(
        args.timeout or config.get("timeout_seconds") or config.get("timeout"),
        DEFAULT_CHILD_TIMEOUT_SECONDS,
    )
    max_output_bytes = positive_int_value(
        args.max_output_bytes or config.get("max_output_bytes"),
        DEFAULT_MAX_CHILD_OUTPUT_BYTES,
    )

    if issues:
        blockers = adapter_claim_grade_blockers(
            config=config,
            selector_contract=selector_contract,
            timing_contract=timing_contract,
            source_contract=source_contract,
            issues=issues,
            dry_run=bool(args.dry_run),
        )
        emit(
            {
                "success": False,
                "runnable": False,
                "claim_grade": False,
                "claim_grade_blockers": blockers,
                "adapter_configured": True,
                "dry_run": bool(args.dry_run),
                "error": "; ".join(issues),
                "host": host_metadata(),
                "config": str(config_path),
                "dataset": args.dataset,
                "benchmark": args.benchmark,
                "allocator": args.allocator,
                "variant_feature": args.variant_feature,
                "run_index": args.run_index,
                "selector_contract": selector_contract,
                "timing_contract": timing_contract,
                "source_contract": source_contract,
                "checkout_pin_validation": checkout_pin,
                "external_rustup_toolchain": external_rustup_toolchain,
                "timeout_seconds": timeout_seconds,
                "max_output_bytes": max_output_bytes,
                "generated_at": now_iso(),
            }
        )
        return 2

    if args.dry_run:
        blockers = adapter_claim_grade_blockers(
            config=config,
            selector_contract=selector_contract,
            timing_contract=timing_contract,
            source_contract=source_contract,
            dry_run=True,
        )
        emit(
            {
                "success": True,
                "runnable": True,
                "claim_grade": False,
                "claim_grade_blockers": blockers,
                "dry_run": True,
                "adapter_configured": True,
                "host": host_metadata(),
                "config": str(config_path),
                "cwd": str(cwd_path),
                "command": argv_rendered,
                "dataset": args.dataset,
                "benchmark": args.benchmark,
                "allocator": args.allocator,
                "variant_feature": args.variant_feature,
                "run_index": args.run_index,
                "selector_contract": selector_contract,
                "timing_contract": timing_contract,
                "source_contract": source_contract,
                "checkout_pin_validation": checkout_pin,
                "external_rustup_toolchain": external_rustup_toolchain,
                "timeout_seconds": timeout_seconds,
                "max_output_bytes": max_output_bytes,
                "generated_at": now_iso(),
            }
        )
        return 0

    env = os.environ.copy()
    if isinstance(rendered_env, dict):
        env.update({str(key): str(value) for key, value in rendered_env.items()})
    if external_rustup_toolchain["rustup_toolchain_injected"] and external_rustup_toolchain["effective_rustup_toolchain"]:
        env["RUSTUP_TOOLCHAIN"] = str(external_rustup_toolchain["effective_rustup_toolchain"])
    child_result = run_child_bounded(
        argv_rendered,
        cwd=cwd_path,
        env=env,
        timeout_seconds=timeout_seconds,
        max_output_bytes=max_output_bytes,
    )
    child_result["timeout_seconds"] = timeout_seconds
    if child_result.get("stdout_truncated"):
        sys.stdout.write(f"[paper adapter retained last {child_result['stdout_retained_bytes']} of {child_result['stdout_bytes']} child stdout bytes]\n")
    if child_result.get("stdout"):
        sys.stdout.write(str(child_result["stdout"]))
        if not str(child_result["stdout"]).endswith("\n"):
            sys.stdout.write("\n")
    if child_result.get("stderr_truncated"):
        sys.stderr.write(f"[paper adapter retained last {child_result['stderr_retained_bytes']} of {child_result['stderr_bytes']} child stderr bytes]\n")
    if child_result.get("stderr"):
        sys.stderr.write(str(child_result["stderr"]))
        if not str(child_result["stderr"]).endswith("\n"):
            sys.stderr.write("\n")
    child_record = last_json_object(str(child_result.get("stdout") or ""))
    seconds = find_time_seconds(child_record or {}, config.get("time_fields") or [config.get("time_field") or "seconds"])
    child_blockers = child_output_blockers(child_result, max_output_bytes)
    if child_result.get("timed_out"):
        child_blockers = unique_strings(child_blockers)
    if child_result.get("returncode") != 0 or child_record is None or seconds is None:
        child_blockers = unique_strings([*child_blockers, *child_claim_grade_blockers(child_record)])
        blockers = unique_strings([
            *adapter_claim_grade_blockers(
                config=config,
                selector_contract=selector_contract,
                timing_contract=timing_contract,
                source_contract=source_contract,
                workload_failed=True,
            ),
            *child_blockers,
        ])
        emit(
            {
                "success": False,
                "runnable": True,
                "claim_grade": False,
                "claim_grade_blockers": blockers,
                "adapter_configured": True,
                "error": "workload command failed or did not emit finite JSON timing",
                "child_returncode": child_result.get("returncode"),
                "child_json_found": child_record is not None,
                "seconds_found": seconds is not None,
                **child_allocator_metadata(child_record),
                "host": host_metadata(),
                "config": str(config_path),
                "command": argv_rendered,
                "cwd": str(cwd_path),
                "dataset": args.dataset,
                "benchmark": args.benchmark,
                "allocator": args.allocator,
                "variant_feature": args.variant_feature,
                "run_index": args.run_index,
                "selector_contract": selector_contract,
                "timing_contract": timing_contract,
                "source_contract": source_contract,
                "checkout_pin_validation": checkout_pin,
                "external_rustup_toolchain": external_rustup_toolchain,
                "timeout_seconds": timeout_seconds,
                "max_output_bytes": max_output_bytes,
                "child_stdout_bytes": child_result.get("stdout_bytes"),
                "child_stderr_bytes": child_result.get("stderr_bytes"),
                "child_stdout_retained_bytes": child_result.get("stdout_retained_bytes"),
                "child_stderr_retained_bytes": child_result.get("stderr_retained_bytes"),
                "child_stdout_truncated": child_result.get("stdout_truncated"),
                "child_stderr_truncated": child_result.get("stderr_truncated"),
                "timed_out": child_result.get("timed_out"),
                "process_group_pid": child_result.get("process_group_pid"),
                "process_group_terminated": child_result.get("process_group_terminated"),
                "generated_at": now_iso(),
            }
        )
        return int(child_result.get("returncode") or 2)

    blockers = unique_strings([
        *adapter_claim_grade_blockers(
            config=config,
            selector_contract=selector_contract,
            timing_contract=timing_contract,
            source_contract=source_contract,
        ),
        *child_blockers,
        *child_claim_grade_blockers(child_record),
    ])
    child_benchmarks: List[str] = []
    raw_child_benchmarks = child_record.get("benchmarks")
    if isinstance(raw_child_benchmarks, list):
        child_benchmarks.extend(str(item) for item in raw_child_benchmarks)
    raw_child_rows = child_record.get("bench_rows")
    if isinstance(raw_child_rows, list):
        for row in raw_child_rows:
            if isinstance(row, dict):
                child_benchmarks.append(str(row.get("name") or ""))
    child_benchmarks = unique_strings(child_benchmarks)
    child_bench_filter = first_nonempty_text(
        [
            child_record.get("bench_filter"),
            child_record.get("bench_name_filter"),
            child_record.get("filter"),
        ]
    )
    child_ns_per_iter = finite_number(child_record.get("ns_per_iter"))
    emit(
        {
            "success": True,
            "runnable": True,
            "claim_grade_blockers": blockers,
            "claim_grade": bool(config.get("claim_grade", False)) and not blockers,
            "adapter_configured": True,
            "seconds": seconds,
            "benchmarks": child_benchmarks,
            "bench_filter": child_bench_filter or None,
            "ns_per_iter": child_ns_per_iter,
            **child_allocator_metadata(child_record),
            "child_returncode": child_result.get("returncode"),
            "child_record": child_record,
            "timeout_seconds": timeout_seconds,
            "max_output_bytes": max_output_bytes,
            "child_stdout_bytes": child_result.get("stdout_bytes"),
            "child_stderr_bytes": child_result.get("stderr_bytes"),
            "child_stdout_retained_bytes": child_result.get("stdout_retained_bytes"),
            "child_stderr_retained_bytes": child_result.get("stderr_retained_bytes"),
            "child_stdout_truncated": child_result.get("stdout_truncated"),
            "child_stderr_truncated": child_result.get("stderr_truncated"),
            "timed_out": child_result.get("timed_out"),
            "process_group_pid": child_result.get("process_group_pid"),
            "process_group_terminated": child_result.get("process_group_terminated"),
            "host": host_metadata(),
            "config": str(config_path),
            "command": argv_rendered,
            "cwd": str(cwd_path),
            "dataset": args.dataset,
            "benchmark": args.benchmark,
            "allocator": args.allocator,
            "variant_feature": args.variant_feature,
            "run_index": args.run_index,
            "selector_contract": selector_contract,
            "timing_contract": timing_contract,
            "source_contract": source_contract,
            "checkout_pin_validation": checkout_pin,
            "external_rustup_toolchain": external_rustup_toolchain,
            "generated_at": now_iso(),
        }
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
