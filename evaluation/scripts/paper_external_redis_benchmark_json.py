#!/usr/bin/env python3
"""Run redis-benchmark against a real Redis-compatible server and emit JSON timing.

This bridge is stdlib-only and intentionally conservative: it launches the
configured upstream server command, waits for RESP PING readiness, invokes a
redis-benchmark-compatible client, parses text or CSV throughput output, and
prints one machine-readable JSON object.  The record proves benchmark-client
plumbing, not full paper-grade provenance; exact paper command, repetitions,
allocator/runtime parity, and raw-environment controls remain separate gates.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import importlib.util
import io
import json
import math
import os
import platform
import re
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

try:
    from prepare_redis_benchmark_tool import probe_redis_benchmark_tool
except Exception:  # pragma: no cover - direct script fallback if helper is unavailable
    probe_redis_benchmark_tool = None  # type: ignore[assignment]


ALLOCATOR_FEATURES = {
    "default": "bench_ourself",
    "ourself": "bench_ourself",
    "unialloc": "bench_ourself",
    "jemalloc": "bench_jemalloc",
    "ptmalloc": "bench_ptmalloc",
    "mimalloc": "bench_mimalloc",
    "tcmalloc": "bench_tcmalloc",
    "snmalloc": "bench_snmalloc",
    "scudo": "bench_scudo",
}

SCUDO_RUNTIME_IDENTITY_MARKER = "unialloc: verified Scudo runtime identity\n"
SCUDO_CONFIGURATION_ERROR = 78


def _load_paper_workload_driver() -> Tuple[Optional[Any], Optional[str]]:
    """Load the canonical Scudo probe without relying on the caller's cwd/sys.path."""

    try:
        import paper_workload_driver as helper

        return helper, None
    except Exception as direct_exc:
        helper_path = Path(__file__).resolve().with_name("paper_workload_driver.py")
        try:
            spec = importlib.util.spec_from_file_location(
                "_unialloc_redis_benchmark_paper_workload_driver",
                helper_path,
            )
            if spec is None or spec.loader is None:
                raise ImportError(f"could not create module spec for {helper_path}")
            helper = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(helper)
            return helper, None
        except Exception as fallback_exc:
            return None, f"direct import failed: {direct_exc}; local import failed: {fallback_exc}"


_PAPER_WORKLOAD_DRIVER, _PAPER_WORKLOAD_DRIVER_IMPORT_ERROR = _load_paper_workload_driver()


def _load_rredis_runner() -> Tuple[Optional[Any], Optional[str]]:
    try:
        import paper_external_rredis_runner as helper

        return helper, None
    except Exception as direct_exc:
        helper_path = Path(__file__).resolve().with_name("paper_external_rredis_runner.py")
        try:
            spec = importlib.util.spec_from_file_location(
                "_unialloc_redis_benchmark_rredis_runner",
                helper_path,
            )
            if spec is None or spec.loader is None:
                raise ImportError(f"could not create module spec for {helper_path}")
            helper = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(helper)
            return helper, None
        except Exception as fallback_exc:
            return None, f"direct import failed: {direct_exc}; local import failed: {fallback_exc}"


_RREDIS_RUNNER, _RREDIS_RUNNER_IMPORT_ERROR = _load_rredis_runner()

TEXT_RPS_RE = re.compile(
    r"^\s*(?P<name>[A-Za-z0-9_./ -]+?)\s*:\s*(?P<rps>[0-9][0-9,]*(?:\.[0-9]+)?)\s+requests\s+per\s+second\b",
    re.IGNORECASE,
)
TEXT_SECTION_RE = re.compile(r"^\s*=+\s*(?P<name>[A-Za-z0-9_./ -]+?)\s*=+\s*$")
TEXT_THROUGHPUT_RE = re.compile(
    r"(?:throughput\s+summary\s*:\s*)?(?P<rps>[0-9][0-9,]*(?:\.[0-9]+)?)\s+requests\s+per\s+second\b",
    re.IGNORECASE,
)
TEXT_LATENCY_RE = re.compile(
    r"(?P<key>p50|p95|p99|avg|min|max)(?:_latency)?\s*[=:]\s*(?P<value>[0-9][0-9,]*(?:\.[0-9]+)?)\s*(?P<unit>msec|ms|sec|s|usec|us)?",
    re.IGNORECASE,
)


def normalize_allocator_name(raw: Any) -> str:
    return str(raw or "").strip().lower().replace("_", "-")


def allocator_feature(allocator: Any) -> str:
    normalized = normalize_allocator_name(allocator)
    if normalized == "bench-scudo":
        return "bench_scudo"
    return ALLOCATOR_FEATURES.get(normalized, "")


def scudo_allocator_requested(allocator: Any) -> bool:
    return allocator_feature(allocator) == "bench_scudo"


def verify_scudo_runner_attestation(
    args: argparse.Namespace,
    command: List[str],
    cwd: str,
) -> Dict[str, Any]:
    if not scudo_allocator_requested(args.allocator):
        return {"required": False, "ok": True, "blockers": []}
    if _RREDIS_RUNNER is None:
        return {
            "required": True,
            "ok": False,
            "blockers": [f"canonical RRedis runner is unavailable: {_RREDIS_RUNNER_IMPORT_ERROR}"],
        }
    record = _RREDIS_RUNNER.verify_scudo_runner_attestation(
        str(getattr(args, "scudo_runner_attestation_json", "") or ""),
        cwd=Path(cwd),
        server_command=command,
    )
    return {**record, "required": True}


def _scudo_probe_args(
    args: argparse.Namespace,
    *,
    command: Optional[List[str]] = None,
    cwd: Optional[str] = None,
) -> argparse.Namespace:
    return _PAPER_WORKLOAD_DRIVER.scudo_child_probe_args(
        args,
        command=command or [],
        cwd=Path(cwd or os.getcwd()),
    )


def prepare_scudo_server_execution(
    args: argparse.Namespace,
    *,
    command: Optional[List[str]] = None,
    cwd: Optional[str] = None,
    base_env: Optional[Dict[str, str]] = None,
) -> Tuple[Dict[str, str], Dict[str, Any]]:
    """Plan the canonical Scudo route and apply it only to the server child."""

    env = dict(base_env) if base_env is not None else os.environ.copy()
    if not scudo_allocator_requested(args.allocator):
        return env, {"requested": False, "ok": True}
    if _PAPER_WORKLOAD_DRIVER is None:
        return env, {
            "requested": True,
            "ok": False,
            "error": f"canonical Scudo workload driver is unavailable: {_PAPER_WORKLOAD_DRIVER_IMPORT_ERROR}",
            "execution": None,
            "subprocess_env_delta": {},
        }
    probe_args = _scudo_probe_args(args, command=command, cwd=cwd)
    toolchain_alignment = _PAPER_WORKLOAD_DRIVER.align_scudo_child_toolchain_env(
        env,
        probe_args,
    )
    try:
        execution = _PAPER_WORKLOAD_DRIVER.scudo_execution_probe(probe_args)
    except Exception as exc:
        return env, {
            "requested": True,
            "ok": False,
            "error": f"canonical Scudo execution probe failed: {type(exc).__name__}: {exc}",
            "execution": None,
            "subprocess_env_delta": {},
        }
    if not execution.get("ok"):
        blockers = "; ".join(str(item) for item in execution.get("blockers", []) if str(item).strip())
        return env, {
            "requested": True,
            "ok": False,
            "error": blockers or "no canonical Scudo execution route is available",
            "execution": execution,
            "subprocess_env_delta": {},
        }

    mode = execution.get("selected_mode")
    removed_env: List[str] = list(toolchain_alignment.get("subprocess_env_removed", []))
    if mode == "rust-sanitizer":
        sanitizer_removed = list(_PAPER_WORKLOAD_DRIVER.SCUDO_SANITIZER_CONFLICTING_ENV_NAMES)
        removed_env = sorted(set(removed_env) | set(sanitizer_removed))
        for name in sanitizer_removed:
            env.pop(name, None)
        flags = str(execution.get("rust_sanitizer_rustflags") or "-Z sanitizer=scudo")
        env["RUSTFLAGS"] = _PAPER_WORKLOAD_DRIVER.append_env_flags(env.get("RUSTFLAGS"), flags)
    elif mode == "ld-preload":
        runtime = str(execution.get("runtime_library") or "").strip()
        if not runtime:
            return env, {
                "requested": True,
                "ok": False,
                "error": "canonical Scudo LD_PRELOAD route selected without a runtime library",
                "execution": execution,
                "subprocess_env_delta": {},
            }
        runtime_path = Path(runtime)
        env["UNIALLOC_SCUDO_RUNTIME_LIBRARY"] = str(runtime_path)
        env["LD_PRELOAD"] = _PAPER_WORKLOAD_DRIVER.prepend_preload_library(
            env.get("LD_PRELOAD"), runtime_path
        )
    else:
        return env, {
            "requested": True,
            "ok": False,
            "error": f"unsupported canonical Scudo execution mode: {mode}",
            "execution": execution,
            "subprocess_env_delta": {},
        }

    planned = _PAPER_WORKLOAD_DRIVER.scudo_execution_runtime_record(
        execution,
        runtime_verified=False,
        verification_status="planned",
    )
    setup = {
        "requested": True,
        "ok": True,
        "execution": planned,
        "subprocess_env_delta": _PAPER_WORKLOAD_DRIVER.env_delta_for_record(env),
        "subprocess_env_effective": {
            key: env[key]
            for key in (
                "UNIALLOC_SCUDO_RUNTIME_LIBRARY",
                "LD_PRELOAD",
                "RUSTFLAGS",
                "UNIALLOC_RUST_TOOLCHAIN",
                "RUSTUP_TOOLCHAIN",
            )
            if key in env
        },
        "scudo_toolchain_alignment": toolchain_alignment,
    }
    if removed_env:
        setup["subprocess_env_removed"] = removed_env
        setup["subprocess_env_effective_absence"] = {
            name: name not in env for name in removed_env
        }
    return env, setup


def verified_scudo_runtime_records(scudo_setup: Dict[str, Any]) -> Tuple[Dict[str, Any], Dict[str, Any]]:
    execution = scudo_setup.get("execution")
    if not isinstance(execution, dict) or _PAPER_WORKLOAD_DRIVER is None:
        raise RuntimeError("Scudo execution cannot be verified without the canonical execution record")
    verified = _PAPER_WORKLOAD_DRIVER.scudo_execution_runtime_record(
        execution,
        runtime_verified=True,
        verification_status="runtime-verified",
    )
    semantics = _PAPER_WORKLOAD_DRIVER.scudo_allocator_semantics(
        verified,
        runtime_verified=True,
        verification_status="runtime-verified",
    )
    return verified, semantics


def finite_number(value: Any) -> Optional[float]:
    try:
        number = float(str(value).replace(",", ""))
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


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


def slugify_label(value: Any, fallback: str) -> str:
    text = str(value or "").strip().lower()
    text = re.sub(r"[^a-z0-9_.-]+", "-", text).strip("-._")
    return text or fallback


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def resolve_evidence_dir(raw_dir: str, args: argparse.Namespace) -> Optional[Path]:
    raw_dir = str(raw_dir or "").strip()
    if not raw_dir:
        return None
    base = Path(raw_dir).expanduser()
    if not base.is_absolute():
        base = Path.cwd() / base
    label = "-".join(
        [
            slugify_label(args.dataset, "dataset"),
            slugify_label(args.benchmark, "benchmark"),
            slugify_label(args.allocator, "allocator"),
            f"run{slugify_label(args.run_index, '0')}",
            str(os.getpid()),
            str(int(time.time() * 1000)),
        ]
    )
    evidence_dir = base / label
    evidence_dir.mkdir(parents=True, exist_ok=True)
    return evidence_dir


def write_evidence_file(evidence_dir: Optional[Path], name: str, data: bytes, kind: str) -> Optional[Dict[str, Any]]:
    if evidence_dir is None:
        return None
    safe_name = slugify_label(name, "evidence")
    path = evidence_dir / safe_name
    path.write_bytes(data)
    return {
        "kind": kind,
        "path": str(path),
        "sha256": file_sha256(path),
        "bytes": path.stat().st_size,
    }


def append_evidence(payload: Dict[str, Any], entries: Iterable[Optional[Dict[str, Any]]]) -> None:
    current = payload.get("evidence")
    if not isinstance(current, list):
        current = []
    seen = {
        str(entry.get("path"))
        for entry in current
        if isinstance(entry, dict) and str(entry.get("path") or "").strip()
    }
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        path = str(entry.get("path") or "").strip()
        if not path or path in seen:
            continue
        seen.add(path)
        current.append(entry)
    if current:
        payload["evidence"] = current
        payload["raw_evidence"] = current
        first = current[0]
        if isinstance(first, dict):
            payload.setdefault("raw_evidence_path_or_uri", first.get("path"))
            payload.setdefault("raw_evidence_sha256", first.get("sha256"))


def now_seconds() -> float:
    return time.monotonic()


def read_tail(path: Optional[Path], limit: int = 4000) -> str:
    if path is None or not path.exists():
        return ""
    data = path.read_bytes()[-limit:]
    return data.decode("utf-8", errors="replace")


def positive_int_value(value: Any, default: int) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return default
    return parsed if parsed > 0 else default


def emit(payload: Dict[str, Any], returncode: int) -> int:
    print(json.dumps(payload, sort_keys=True))
    return returncode


def allocator_semantics_record(
    args: argparse.Namespace,
    scudo_setup: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    allocator = normalize_allocator_name(args.allocator)
    feature = allocator_feature(allocator)
    system = platform.system()
    libc_name, libc_version = platform.libc_ver()
    blockers: List[str] = []
    implementation_kind = "allocator_feature_global_allocator" if feature else "unknown_allocator_selector"
    paper_allocator_equivalent: Optional[bool] = None
    notes: List[str] = []

    if feature == "bench_ptmalloc":
        implementation_kind = "std_alloc_system_fallback"
        is_linux_glibc = system == "Linux" and libc_name.lower() == "glibc"
        paper_allocator_equivalent = is_linux_glibc and allocator == "ptmalloc"
        if paper_allocator_equivalent:
            notes.append("bench_ptmalloc routes through std::alloc::System on a Linux/glibc host")
        else:
            blockers.append(
                "RRedis bench_ptmalloc routes to std::alloc::System on this host, not Linux/glibc ptmalloc paper evidence"
            )
    elif feature == "bench_scudo":
        execution = scudo_setup.get("execution") if isinstance(scudo_setup, dict) else None
        runtime_verified = bool(isinstance(execution, dict) and execution.get("runtime_verified") is True)
        implementation_kind = (
            "verified_external_scudo_runtime" if runtime_verified else "planned_scudo_runtime_route"
        )
        paper_allocator_equivalent = runtime_verified
        if not isinstance(scudo_setup, dict) or not scudo_setup.get("ok"):
            blockers.append(
                str((scudo_setup or {}).get("error") or "no canonical Scudo execution route is available")
            )
        elif not runtime_verified:
            blockers.append("Scudo server runtime identity has not been verified before workload timing")
        else:
            notes.append("server emitted the canonical Scudo runtime identity marker before workload timing")
    elif feature:
        paper_allocator_equivalent = True
    else:
        blockers.append("RRedis allocator selector is not mapped to a benchmark allocator feature")

    record = {
        "schema_version": 1,
        "source": "paper-external-redis-benchmark-json-allocator-semantics",
        "requested_allocator": allocator or None,
        "allocator_feature": feature or None,
        "implementation_kind": implementation_kind,
        "host_system": system,
        "host_libc": libc_name or None,
        "host_libc_version": libc_version or None,
        "paper_allocator_equivalent": paper_allocator_equivalent,
        "claim_grade_blockers": blockers,
        "notes": notes,
    }
    if feature == "bench_scudo" and isinstance(scudo_setup, dict):
        execution = scudo_setup.get("execution")
        if isinstance(execution, dict):
            record.update(
                {
                    "runtime_identity_verified": execution.get("runtime_verified") is True,
                    "runtime_identity_status": execution.get("execution_state"),
                    "runtime_identity_marker": SCUDO_RUNTIME_IDENTITY_MARKER,
                    "scudo_runtime_mode": execution.get("selected_mode"),
                    "scudo_runtime_library": execution.get("runtime_library"),
                    "scudo_execution_probe": execution,
                }
            )
    return record


def parse_json_arg(raw: str) -> Dict[str, Any]:
    if not str(raw or "").strip():
        return {}
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        return {}
    return parsed if isinstance(parsed, dict) else {}


def current_source_claim_contract(
    args: argparse.Namespace,
    *,
    allocator_semantics: Dict[str, Any],
    benchmark_path: Optional[str],
) -> Dict[str, Any]:
    """Return runtime claim contract for RRedis accepted-current executions.

    This deliberately does not assert paper-exact RRedis methodology.  It only
    permits a sample to be marked claim-grade when the caller supplied the
    explicit current-source contract, the source is an accepted pinned
    fallback/newer checkout, redis-benchmark is actually available, and the
    selected allocator is not known to fall back to a non-equivalent runtime.
    """

    contract = str(getattr(args, "claim_grade_contract", "") or "").strip()
    source_contract = parse_json_arg(str(getattr(args, "paper_source_contract_json", "") or ""))
    blockers: List[str] = []
    if contract != "rredis-redis-benchmark-current-run":
        blockers.append("missing RRedis accepted-current claim-grade contract")
    if source_contract.get("accepted_newer_checkout_pin_complete") is not True:
        blockers.append("paper source contract is not an accepted pinned fallback/newer checkout")
    if source_contract.get("source_provenance_class") != "user_accepted_newer_pinned_source":
        blockers.append("source provenance is not labelled user_accepted_newer_pinned_source")
    if not benchmark_path:
        blockers.append("redis-benchmark binary is unavailable")
    blockers.extend(str(item) for item in allocator_semantics.get("claim_grade_blockers", []) if str(item).strip())
    return {
        "contract": contract or None,
        "source_contract": source_contract,
        "claim_grade": not blockers,
        "claim_grade_blockers": unique_strings(blockers),
        "paper_exact": False,
        "source_provenance_class": source_contract.get("source_provenance_class"),
        "accepted_current_source": source_contract.get("accepted_newer_checkout_pin_complete") is True,
    }


def terminate_process_group(proc: Optional[subprocess.Popen[Any]], grace_seconds: float = 2.0) -> Tuple[bool, Optional[int]]:
    if proc is None:
        return False, None
    if proc.poll() is not None:
        return False, proc.returncode
    terminated = False
    try:
        os.killpg(proc.pid, signal.SIGTERM)
        terminated = True
    except ProcessLookupError:
        return False, proc.poll()
    except Exception:
        try:
            proc.terminate()
            terminated = True
        except Exception:
            pass
    deadline = now_seconds() + grace_seconds
    while now_seconds() < deadline:
        code = proc.poll()
        if code is not None:
            return terminated, code
        time.sleep(0.05)
    try:
        os.killpg(proc.pid, signal.SIGKILL)
        terminated = True
    except Exception:
        try:
            proc.kill()
            terminated = True
        except Exception:
            pass
    return terminated, proc.wait(timeout=5)


class BoundedPipeCapture:
    """Drain a child pipe while retaining only a bounded tail."""

    def __init__(self, pipe: Any, max_bytes: int) -> None:
        self.pipe = pipe
        self.max_bytes = max(1, int(max_bytes))
        self.total_bytes = 0
        self._tail = bytearray()
        self._scudo_marker_seen = False
        self._scudo_marker_line_buffer = b""
        self.thread = threading.Thread(target=self._drain, daemon=True)

    def start(self) -> None:
        self.thread.start()

    def join(self, timeout: float = 5.0) -> None:
        self.thread.join(timeout=timeout)

    def _drain(self) -> None:
        try:
            while True:
                read_chunk = getattr(self.pipe, "read1", self.pipe.read)
                chunk = read_chunk(64 * 1024)
                if not chunk:
                    break
                if isinstance(chunk, str):
                    chunk = chunk.encode("utf-8", errors="replace")
                marker = SCUDO_RUNTIME_IDENTITY_MARKER.encode("utf-8")
                marker_scan = self._scudo_marker_line_buffer + chunk
                lines = marker_scan.splitlines(keepends=True)
                if lines and not lines[-1].endswith((b"\n", b"\r")):
                    self._scudo_marker_line_buffer = lines.pop()
                else:
                    self._scudo_marker_line_buffer = b""
                if marker in lines:
                    self._scudo_marker_seen = True
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

    @property
    def scudo_runtime_identity_marker_seen(self) -> bool:
        return self._scudo_marker_seen

    def text_tail(self, limit: int = 4000) -> str:
        data = self.bytes()[-max(1, int(limit)) :]
        return data.decode("utf-8", errors="replace")


def wait_for_scudo_runtime_identity_marker(
    stderr_capture: Optional[BoundedPipeCapture],
    *,
    timeout: float = 1.0,
) -> bool:
    if stderr_capture is None:
        return False
    deadline = now_seconds() + max(0.0, float(timeout))
    while now_seconds() < deadline:
        if stderr_capture.scudo_runtime_identity_marker_seen:
            return True
        time.sleep(0.01)
    return stderr_capture.scudo_runtime_identity_marker_seen


def server_log_metadata(
    stdout_capture: Optional[BoundedPipeCapture],
    stderr_capture: Optional[BoundedPipeCapture],
    *,
    max_server_log_bytes: int,
    server_tail_bytes: int = 4000,
    keep_server_logs: bool = False,
    evidence_dir: Optional[Path] = None,
) -> Dict[str, Any]:
    for capture in (stdout_capture, stderr_capture):
        if capture is not None:
            capture.join()
    metadata: Dict[str, Any] = {
        "server_log_max_bytes": max_server_log_bytes,
        "server_stdout_tail": stdout_capture.text_tail(server_tail_bytes) if stdout_capture is not None else "",
        "server_stderr_tail": stderr_capture.text_tail(server_tail_bytes) if stderr_capture is not None else "",
        "server_stdout_bytes": stdout_capture.total_bytes if stdout_capture is not None else 0,
        "server_stderr_bytes": stderr_capture.total_bytes if stderr_capture is not None else 0,
        "server_stdout_retained_bytes": stdout_capture.retained_bytes if stdout_capture is not None else 0,
        "server_stderr_retained_bytes": stderr_capture.retained_bytes if stderr_capture is not None else 0,
        "server_stdout_truncated": stdout_capture.truncated if stdout_capture is not None else False,
        "server_stderr_truncated": stderr_capture.truncated if stderr_capture is not None else False,
        "server_logs_retained": bool(keep_server_logs),
    }
    if keep_server_logs or evidence_dir is not None:
        log_dir = evidence_dir if evidence_dir is not None else Path(tempfile.mkdtemp(prefix="paper-redis-benchmark-logs-"))
        log_dir.mkdir(parents=True, exist_ok=True)
        stdout_entry = write_evidence_file(
            log_dir,
            "server.stdout.retained.txt",
            stdout_capture.bytes() if stdout_capture is not None else b"",
            "server_stdout_retained",
        )
        stderr_entry = write_evidence_file(
            log_dir,
            "server.stderr.retained.txt",
            stderr_capture.bytes() if stderr_capture is not None else b"",
            "server_stderr_retained",
        )
        marker_entry = None
        if stderr_capture is not None and stderr_capture.scudo_runtime_identity_marker_seen:
            marker_entry = write_evidence_file(
                log_dir,
                "scudo-runtime-marker.stderr.txt",
                SCUDO_RUNTIME_IDENTITY_MARKER.encode("utf-8"),
                "scudo_runtime_marker_stderr",
            )
        stdout_path = Path(str(stdout_entry.get("path"))) if isinstance(stdout_entry, dict) else log_dir / "server.stdout.retained.txt"
        stderr_path = Path(str(stderr_entry.get("path"))) if isinstance(stderr_entry, dict) else log_dir / "server.stderr.retained.txt"
        metadata.update(
            {
                "server_log_dir": str(log_dir),
                "server_stdout_path": str(stdout_path),
                "server_stdout_sha256": stdout_entry.get("sha256") if isinstance(stdout_entry, dict) else None,
                "server_stderr_path": str(stderr_path),
                "server_stderr_sha256": stderr_entry.get("sha256") if isinstance(stderr_entry, dict) else None,
                "evidence": [entry for entry in (stdout_entry, stderr_entry, marker_entry) if entry],
            }
        )
    return metadata


def run_command_bounded(
    command: List[str],
    *,
    cwd: str,
    timeout_seconds: float,
    max_output_bytes: int,
) -> Dict[str, Any]:
    started = now_seconds()
    proc = subprocess.Popen(
        command,
        cwd=cwd,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        start_new_session=(os.name == "posix"),
    )
    assert proc.stdout is not None
    assert proc.stderr is not None
    stdout_capture = BoundedPipeCapture(proc.stdout, max_output_bytes)
    stderr_capture = BoundedPipeCapture(proc.stderr, max_output_bytes)
    stdout_capture.start()
    stderr_capture.start()
    timed_out = False
    try:
        returncode = proc.wait(timeout=timeout_seconds)
    except subprocess.TimeoutExpired:
        timed_out = True
        if os.name == "posix":
            try:
                os.killpg(proc.pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
        else:
            proc.terminate()
        try:
            returncode = proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            if os.name == "posix":
                try:
                    os.killpg(proc.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
            else:
                proc.kill()
            returncode = proc.wait(timeout=5)
    finally:
        stdout_capture.join()
        stderr_capture.join()
    return {
        "returncode": 124 if timed_out else int(returncode or 0),
        "elapsed_seconds": now_seconds() - started,
        "timed_out": timed_out,
        "stdout": stdout_capture.bytes().decode("utf-8", errors="replace"),
        "stderr": stderr_capture.bytes().decode("utf-8", errors="replace"),
        "stdout_bytes": stdout_capture.total_bytes,
        "stderr_bytes": stderr_capture.total_bytes,
        "stdout_retained_bytes": stdout_capture.retained_bytes,
        "stderr_retained_bytes": stderr_capture.retained_bytes,
        "stdout_truncated": stdout_capture.truncated,
        "stderr_truncated": stderr_capture.truncated,
    }


def encode_resp_array(parts: List[bytes]) -> bytes:
    chunks = [f"*{len(parts)}\r\n".encode("ascii")]
    for part in parts:
        chunks.append(f"${len(part)}\r\n".encode("ascii"))
        chunks.append(part)
        chunks.append(b"\r\n")
    return b"".join(chunks)


def encode_command(*parts: str | bytes) -> bytes:
    encoded = [part if isinstance(part, bytes) else part.encode("utf-8") for part in parts]
    return encode_resp_array(encoded)


def read_resp_line(stream: Any) -> bytes:
    line = stream.readline()
    if not line:
        raise EOFError("connection closed while reading RESP line")
    if not line.endswith(b"\r\n"):
        raise ValueError(f"invalid RESP line terminator: {line!r}")
    return line[:-2]


def read_exact(stream: Any, size: int) -> bytes:
    data = stream.read(size)
    if data is None or len(data) != size:
        raise EOFError(f"connection closed while reading {size} RESP bytes")
    return data


def read_resp(stream: Any) -> Any:
    prefix = read_exact(stream, 1)
    if prefix == b"+":
        return read_resp_line(stream).decode("utf-8", errors="replace")
    if prefix == b"-":
        message = read_resp_line(stream).decode("utf-8", errors="replace")
        raise RuntimeError(f"redis error: {message}")
    if prefix == b":":
        return int(read_resp_line(stream))
    if prefix == b"$":
        length = int(read_resp_line(stream))
        if length < 0:
            return None
        data = read_exact(stream, length)
        terminator = read_exact(stream, 2)
        if terminator != b"\r\n":
            raise ValueError("invalid RESP bulk terminator")
        return data
    if prefix == b"*":
        count = int(read_resp_line(stream))
        if count < 0:
            return None
        return [read_resp(stream) for _ in range(count)]
    raise ValueError(f"unsupported RESP prefix: {prefix!r}")


def redis_request(host: str, port: int, command: bytes, timeout: float) -> Tuple[bool, Any, str]:
    try:
        with socket.create_connection((host, port), timeout=timeout) as sock:
            sock.settimeout(timeout)
            stream = sock.makefile("rwb", buffering=0)
            stream.write(command)
            return True, read_resp(stream), ""
    except Exception as exc:
        return False, None, f"{type(exc).__name__}: {exc}"


def wait_until_ready(
    host: str,
    port: int,
    *,
    timeout: float,
    request_timeout: float,
    proc: Optional[subprocess.Popen[Any]] = None,
) -> Tuple[bool, str, int, Optional[int]]:
    deadline = now_seconds() + timeout
    attempts = 0
    last_error = ""
    while now_seconds() < deadline:
        if proc is not None:
            exit_code = proc.poll()
            if exit_code is not None:
                detail = f"server process exited before readiness with code {exit_code}"
                if last_error:
                    detail = f"{detail}; last readiness error: {last_error}"
                return False, detail, attempts, exit_code
        attempts += 1
        ok, response, error = redis_request(host, port, encode_command("PING"), request_timeout)
        if ok and str(response).upper() == "PONG":
            return True, f"ready after {attempts} attempts", attempts, None
        last_error = error or f"unexpected response: {response!r}"
        time.sleep(0.1)
    return False, last_error or "startup timeout", attempts, None


def latency_seconds(value: Any, unit: str = "ms") -> Optional[float]:
    number = finite_number(value)
    if number is None:
        return None
    normalized = (unit or "ms").strip().lower()
    if normalized in {"sec", "s"}:
        return number
    if normalized in {"usec", "us"}:
        return number / 1_000_000.0
    return number / 1_000.0


def parse_csv_rows(text: str, requests_per_test: int) -> List[Dict[str, Any]]:
    rows: List[Dict[str, Any]] = []
    csv_lines = [line for line in text.splitlines() if line.strip().startswith('"') or "," in line]
    if not csv_lines:
        return rows
    try:
        parsed = list(csv.reader(io.StringIO("\n".join(csv_lines))))
    except csv.Error:
        return rows
    if not parsed:
        return rows
    header: Optional[List[str]] = None
    first = [cell.strip().strip('"').lower() for cell in parsed[0]]
    if first and ("test" in first[0] or "rps" in first or "requests per second" in first):
        header = first
        data_rows = parsed[1:]
    else:
        data_rows = parsed
    for raw in data_rows:
        cells = [cell.strip().strip('"') for cell in raw]
        if len(cells) < 2:
            continue
        if header:
            by_name = {header[i]: cells[i] for i in range(min(len(header), len(cells)))}
            name = by_name.get("test") or by_name.get("name") or cells[0]
            rps_value = by_name.get("rps") or by_name.get("requests per second") or by_name.get("requests_per_second") or cells[1]
            avg = by_name.get("avg_latency_ms") or by_name.get("avg")
            p50 = by_name.get("p50_latency_ms") or by_name.get("p50")
            p95 = by_name.get("p95_latency_ms") or by_name.get("p95")
            p99 = by_name.get("p99_latency_ms") or by_name.get("p99")
        else:
            name, rps_value = cells[0], cells[1]
            avg = cells[2] if len(cells) > 2 else None
            p50 = cells[4] if len(cells) > 4 else None
            p95 = cells[5] if len(cells) > 5 else None
            p99 = cells[6] if len(cells) > 6 else None
        rps = finite_number(rps_value)
        if not name or rps is None or rps <= 0:
            continue
        seconds = requests_per_test / rps if requests_per_test > 0 else None
        rows.append({
            "name": name,
            "requests": requests_per_test,
            "requests_per_second": rps,
            "seconds_estimate": seconds,
            "average_latency_seconds": latency_seconds(avg) if avg not in (None, "") else None,
            "p50_latency_seconds": latency_seconds(p50) if p50 not in (None, "") else None,
            "p95_latency_seconds": latency_seconds(p95) if p95 not in (None, "") else None,
            "p99_latency_seconds": latency_seconds(p99) if p99 not in (None, "") else None,
            "parser": "csv",
        })
    return rows


def parse_text_rows(text: str, requests_per_test: int) -> List[Dict[str, Any]]:
    rows: List[Dict[str, Any]] = []
    current_name: Optional[str] = None
    latency_buffer: Dict[str, Optional[float]] = {}
    for line in text.splitlines():
        section = TEXT_SECTION_RE.match(line)
        if section:
            current_name = section.group("name").strip()
            latency_buffer = {}
            continue
        latency_matches = list(TEXT_LATENCY_RE.finditer(line))
        for match in latency_matches:
            key = match.group("key").lower()
            normalized_key = {
                "avg": "average_latency_seconds",
                "min": "min_latency_seconds",
                "max": "max_latency_seconds",
                "p50": "p50_latency_seconds",
                "p95": "p95_latency_seconds",
                "p99": "p99_latency_seconds",
            }.get(key)
            if normalized_key:
                latency_buffer[normalized_key] = latency_seconds(match.group("value"), match.group("unit") or "ms")
        direct = TEXT_RPS_RE.match(line)
        if direct:
            name = direct.group("name").strip()
            rps = finite_number(direct.group("rps"))
        else:
            throughput = TEXT_THROUGHPUT_RE.search(line)
            name = current_name or "redis-benchmark"
            rps = finite_number(throughput.group("rps")) if throughput else None
        if not name or rps is None or rps <= 0:
            continue
        seconds = requests_per_test / rps if requests_per_test > 0 else None
        row: Dict[str, Any] = {
            "name": name,
            "requests": requests_per_test,
            "requests_per_second": rps,
            "seconds_estimate": seconds,
            "parser": "text",
        }
        row.update({key: value for key, value in latency_buffer.items() if value is not None})
        rows.append(row)
        latency_buffer = {}
    return rows


def parse_redis_benchmark_output(text: str, *, requests_per_test: int) -> Tuple[List[Dict[str, Any]], str]:
    rows = parse_csv_rows(text, requests_per_test)
    if rows:
        return rows, "csv"
    rows = parse_text_rows(text, requests_per_test)
    if rows:
        return rows, "text"
    return [], "none"


def redis_benchmark_command(args: argparse.Namespace, binary: str) -> List[str]:
    command = [
        binary,
        "-h",
        str(args.host),
        "-p",
        str(int(args.port)),
        "-n",
        str(int(args.requests)),
        "-c",
        str(int(args.clients)),
        "-t",
        str(args.tests),
    ]
    if int(args.data_size) > 0:
        command.extend(["-d", str(int(args.data_size))])
    if int(args.threads) > 0:
        command.extend(["--threads", str(int(args.threads))])
    if args.csv:
        command.append("--csv")
    for extra in args.benchmark_arg or []:
        command.extend(str(extra).split(" ") if " " in str(extra).strip() else [str(extra)])
    return command


def resolve_redis_benchmark_binary(raw: str) -> Tuple[Optional[str], Dict[str, Any]]:
    if probe_redis_benchmark_tool is not None:
        try:
            probe = probe_redis_benchmark_tool(raw)
            selected = probe.get("selected_path")
            return (str(selected) if selected else None), probe
        except Exception as exc:
            return None, {
                "source": "redis-benchmark-tool-probe",
                "available": False,
                "error": f"{type(exc).__name__}: {exc}",
            }
    path = shutil.which(str(raw))
    return path, {
        "source": "redis-benchmark-tool-probe-fallback",
        "available": bool(path),
        "selected_path": path,
        "selected_source": "path",
    }


def runtime_claim_grade_blockers(current_contract: Dict[str, Any], *, runtime_complete: bool) -> List[str]:
    """Return top-level blockers for the RRedis accepted-current claim flag.

    ``current_source_claim_contract`` only proves that the caller supplied the
    required source/allocator/tool contract.  The emitted benchmark record must
    still fail closed until redis-benchmark exits successfully and yields
    parseable throughput rows.  Keeping this as a helper avoids a subtle
    overclaim where startup failures could inherit a pre-execution
    ``claim_grade=true`` value from the contract alone.
    """

    blockers = [str(item) for item in current_contract.get("claim_grade_blockers", []) if str(item).strip()]
    if not runtime_complete:
        blockers.append("redis-benchmark execution has not completed successfully with parseable throughput rows")
    return unique_strings(blockers)


def promote_successful_claim_grade(payload: Dict[str, Any]) -> None:
    """Promote a completed redis-benchmark record to accepted-current claim-grade.

    The promotion is intentionally separate from payload construction so every
    failure path remains fail-closed by default.  This still records
    ``paper_exact=false``; the evidence is for the explicitly accepted current
    RRedis source/command, not for exact paper methodology.
    """

    current_contract = payload.get("claim_grade_contract") if isinstance(payload.get("claim_grade_contract"), dict) else {}
    runtime_complete = (
        payload.get("success") is True
        and positive_int_value(payload.get("benchmark_count"), 0) > 0
        and positive_int_value(payload.get("operation_count"), 0) > 0
    )
    blockers = runtime_claim_grade_blockers(current_contract, runtime_complete=runtime_complete)
    if current_contract.get("claim_grade") is True and not blockers:
        payload["claim_grade"] = True
        payload["claim_grade_blockers"] = []
        payload.pop("not_claim_grade_reason", None)
        return
    payload["claim_grade"] = False
    payload["claim_grade_blockers"] = blockers or ["RRedis accepted-current claim-grade contract is incomplete"]
    payload["not_claim_grade_reason"] = "RRedis accepted-current claim-grade contract is incomplete."


def build_common_payload(
    args: argparse.Namespace,
    server_command: List[str],
    cwd: str,
    benchmark_path: Optional[str],
    *,
    scudo_setup: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    allocator_semantics = allocator_semantics_record(args, scudo_setup)
    current_contract = current_source_claim_contract(
        args,
        allocator_semantics=allocator_semantics,
        benchmark_path=benchmark_path,
    )
    host = host_metadata()
    paper_exact_limitation = (
        "redis-benchmark JSON bridge records an accepted-current benchmark-client execution path; "
        "it does not prove exact paper command provenance, six-run/last-five aggregation, "
        "Linux host parity, or final allocator-runtime equivalence"
    )
    initial_blockers = runtime_claim_grade_blockers(current_contract, runtime_complete=False)
    payload = {
        "schema_version": 1,
        "source": "paper-external-redis-benchmark-json",
        "dataset": args.dataset,
        "benchmark": args.benchmark,
        "allocator": args.allocator,
        "variant_feature": args.variant_feature,
        "run_index": args.run_index,
        "host": args.host,
        "runner_host": host,
        "host_system": host.get("system"),
        "host_machine": host.get("machine"),
        "platform": platform.platform(),
        "target": f"{args.host}:{int(args.port)}",
        "port": int(args.port),
        "requests": int(args.requests),
        "clients": int(args.clients),
        "tests": str(args.tests),
        "data_size": int(args.data_size),
        "threads": int(args.threads),
        "measurement_source": "redis_benchmark_json",
        "benchmark_owned_json": True,
        "uses_redis_benchmark": True,
        "uses_resp_bridge": False,
        "claim_grade": False,
        "claim_grade_blockers": initial_blockers,
        "not_claim_grade_reason": "redis-benchmark execution has not completed successfully with parseable throughput rows.",
        "paper_exact": False,
        "paper_exact_limitations": [paper_exact_limitation],
        "claim_grade_contract": current_contract,
        "claim_grade_contract_preconditions_complete": bool(current_contract.get("claim_grade")),
        "allocator_feature": allocator_semantics.get("allocator_feature"),
        "allocator_semantics": allocator_semantics,
        "redis_benchmark_binary": args.redis_benchmark_bin,
        "redis_benchmark_path": benchmark_path,
        "server_command": server_command,
        "command": server_command,
        "server_cwd": cwd,
        "paper_context": {
            "uses_redis_benchmark": True,
            "uses_resp_bridge": False,
            "redis_benchmark_parity_verified": benchmark_path is not None,
            "paper_command_provenance_verified": False,
            "paper_exact_command_specified": False,
        },
    }
    if scudo_allocator_requested(args.allocator) and isinstance(scudo_setup, dict):
        payload["scudo_execution_probe"] = scudo_setup.get("execution")
        payload["subprocess_env_delta"] = scudo_setup.get("subprocess_env_delta", {})
        for key in (
            "subprocess_env_effective",
            "subprocess_env_removed",
            "subprocess_env_effective_absence",
        ):
            if key in scudo_setup:
                payload[key] = scudo_setup[key]
    return payload


def promote_verified_scudo_runtime(
    args: argparse.Namespace,
    payload: Dict[str, Any],
    scudo_setup: Dict[str, Any],
    *,
    benchmark_path: Optional[str],
) -> None:
    verified, semantics = verified_scudo_runtime_records(scudo_setup)
    verified_setup = {**scudo_setup, "execution": verified}
    allocator_semantics = {
        **allocator_semantics_record(args, verified_setup),
        **semantics,
        "schema_version": 1,
        "source": "paper-external-redis-benchmark-json-allocator-semantics",
        "claim_grade_blockers": [],
        "notes": ["server emitted the canonical Scudo runtime identity marker before workload timing"],
    }
    current_contract = current_source_claim_contract(
        args,
        allocator_semantics=allocator_semantics,
        benchmark_path=benchmark_path,
    )
    payload["scudo_execution_probe"] = verified
    payload["allocator_semantics"] = allocator_semantics
    payload["claim_grade_contract"] = current_contract
    payload["claim_grade_contract_preconditions_complete"] = bool(current_contract.get("claim_grade"))
    payload["claim_grade_blockers"] = runtime_claim_grade_blockers(
        current_contract,
        runtime_complete=False,
    )


def summarize_rows(rows: List[Dict[str, Any]]) -> Dict[str, Any]:
    total_requests = sum(int(row.get("requests") or 0) for row in rows)
    row_seconds = [finite_number(row.get("seconds_estimate")) for row in rows]
    total_seconds = sum(value for value in row_seconds if value is not None and value > 0)
    rps_values = [finite_number(row.get("requests_per_second")) for row in rows]
    weighted_rps = (total_requests / total_seconds) if total_requests > 0 and total_seconds > 0 else None
    latencies = [finite_number(row.get("p50_latency_seconds")) for row in rows]
    return {
        "benchmark_count": len(rows),
        "benchmarks": [row.get("name") for row in rows],
        "operation_count": total_requests,
        "successful_operations": total_requests,
        "failed_operations": 0,
        "seconds": total_seconds if total_seconds > 0 else None,
        "operations_per_second": weighted_rps,
        "min_requests_per_second": min(rps_values) if rps_values else None,
        "max_requests_per_second": max(rps_values) if rps_values else None,
        "p50_latency_seconds": min(latencies) if latencies else None,
    }


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dataset", required=True)
    parser.add_argument("--benchmark", required=True)
    parser.add_argument("--allocator", required=True)
    parser.add_argument("--variant-feature", default="")
    parser.add_argument("--run-index", required=True)
    parser.add_argument("--redis-benchmark-bin", default="redis-benchmark")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=6379)
    parser.add_argument("--requests", type=int, default=1000)
    parser.add_argument("--clients", type=int, default=50)
    parser.add_argument("--tests", default="set,get")
    parser.add_argument("--data-size", type=int, default=64)
    parser.add_argument("--threads", type=int, default=0)
    parser.add_argument("--csv", action="store_true")
    parser.add_argument("--benchmark-arg", action="append", help="Extra redis-benchmark argument; repeatable")
    parser.add_argument("--startup-timeout", type=float, default=30.0)
    parser.add_argument("--request-timeout", type=float, default=2.0)
    parser.add_argument("--benchmark-timeout", type=float, default=60.0)
    parser.add_argument("--grace-seconds", type=float, default=2.0)
    parser.add_argument(
        "--claim-grade-contract",
        default="",
        help="Explicit runtime contract; RRedis accepted-current evidence uses rredis-redis-benchmark-current-run.",
    )
    parser.add_argument(
        "--paper-source-contract-json",
        default="",
        help="Serialized source contract proving the pinned non-paper-exact checkout accepted for this current run.",
    )
    parser.add_argument("--max-server-log-bytes", type=int, default=1024 * 1024, help="Retain at most this many bytes per server stdout/stderr stream")
    parser.add_argument("--server-tail-bytes", type=int, default=4000, help="Embed at most this many retained server log bytes per stream in result JSON")
    parser.add_argument("--keep-server-logs", action="store_true", help="Write retained server log tails to a temporary directory and report the paths")
    parser.add_argument("--max-benchmark-output-bytes", type=int, default=1024 * 1024, help="Retain at most this many bytes per redis-benchmark stdout/stderr stream")
    parser.add_argument("--benchmark-tail-bytes", type=int, default=4000, help="Embed at most this many retained redis-benchmark output bytes per stream in result JSON")
    parser.add_argument(
        "--evidence-dir",
        default=os.environ.get("UNIALLOC_PAPER_EVIDENCE_DIR", ""),
        help=(
            "Repo-local base directory for retained redis-benchmark/server stdout/stderr evidence. "
            "A unique per-run subdirectory is created and each retained file is reported with sha256."
        ),
    )
    parser.add_argument(
        "--scudo-mode",
        choices=getattr(_PAPER_WORKLOAD_DRIVER, "SCUDO_MODES", ("auto", "rust-sanitizer", "ld-preload")),
        default=os.environ.get("UNIALLOC_SCUDO_MODE", "auto"),
    )
    parser.add_argument(
        "--scudo-runtime-library",
        default=os.environ.get("UNIALLOC_SCUDO_RUNTIME_LIBRARY")
        or os.environ.get("SCUDO_RUNTIME_LIBRARY")
        or os.environ.get("SCUDO_STANDALONE_LIBRARY", ""),
    )
    parser.add_argument("--rust-toolchain", default=os.environ.get("UNIALLOC_RUST_TOOLCHAIN", ""))
    parser.add_argument("--scudo-runner-attestation-json", default="", help=argparse.SUPPRESS)
    parser.add_argument("server_command", nargs=argparse.REMAINDER, help="Use -- followed by the upstream Redis server command")
    args = parser.parse_args(argv)

    server_command = list(args.server_command)
    if server_command and server_command[0] == "--":
        server_command = server_command[1:]
    cwd = os.getcwd()
    server_env, scudo_setup = prepare_scudo_server_execution(
        args,
        command=server_command,
        cwd=cwd,
    )
    scudo_attestation = verify_scudo_runner_attestation(args, server_command, cwd)
    benchmark_path, benchmark_probe = resolve_redis_benchmark_binary(str(args.redis_benchmark_bin))
    payload = build_common_payload(
        args,
        server_command,
        cwd,
        benchmark_path,
        scudo_setup=scudo_setup,
    )
    if scudo_attestation.get("required"):
        payload["scudo_runner_attestation"] = scudo_attestation
    evidence_dir = resolve_evidence_dir(str(args.evidence_dir or ""), args)
    if scudo_setup.get("requested") and evidence_dir is None:
        evidence_dir = Path(tempfile.mkdtemp(prefix="paper-redis-benchmark-scudo-evidence-"))
    if evidence_dir is not None:
        payload["raw_evidence_dir"] = str(evidence_dir)
    payload["redis_benchmark_tool_probe"] = {
        "source": benchmark_probe.get("source"),
        "available": benchmark_probe.get("available"),
        "selected_path": benchmark_probe.get("selected_path"),
        "selected_source": benchmark_probe.get("selected_source"),
        "selected_version": benchmark_probe.get("selected_version"),
        "recommended_config_value": benchmark_probe.get("recommended_config_value"),
        "candidate_count": len(benchmark_probe.get("candidates", [])) if isinstance(benchmark_probe.get("candidates"), list) else 0,
        "claim_grade": benchmark_probe.get("claim_grade"),
        "claim_grade_blockers": benchmark_probe.get("claim_grade_blockers"),
    }
    proc: Optional[subprocess.Popen[Any]] = None
    stdout_capture: Optional[BoundedPipeCapture] = None
    stderr_capture: Optional[BoundedPipeCapture] = None
    max_server_log_bytes = positive_int_value(args.max_server_log_bytes, 1024 * 1024)
    server_tail_bytes = positive_int_value(args.server_tail_bytes, 4000)
    max_benchmark_output_bytes = positive_int_value(args.max_benchmark_output_bytes, 1024 * 1024)
    benchmark_tail_bytes = positive_int_value(args.benchmark_tail_bytes, 4000)
    payload["server_log_max_bytes"] = max_server_log_bytes
    payload["redis_benchmark_output_max_bytes"] = max_benchmark_output_bytes

    if scudo_setup.get("requested") and not server_command:
        payload.update(
            {
                "success": False,
                "error": "Scudo evaluation requires a wrapper-launched server command so its runtime identity can be verified",
            }
        )
        return emit(payload, SCUDO_CONFIGURATION_ERROR)
    if not scudo_setup.get("ok"):
        payload.update(
            {
                "success": False,
                "error": f"Scudo execution unavailable: {scudo_setup.get('error')}",
            }
        )
        return emit(payload, SCUDO_CONFIGURATION_ERROR)
    if not scudo_attestation.get("ok"):
        payload.update(
            {
                "success": False,
                "error": "Scudo runner attestation failed: "
                + "; ".join(str(item) for item in scudo_attestation.get("blockers", [])),
            }
        )
        return emit(payload, SCUDO_CONFIGURATION_ERROR)

    if not benchmark_path:
        payload.update({
            "success": False,
            "error": f"redis-benchmark binary is not available: {args.redis_benchmark_bin}",
            "tool_probe": benchmark_probe,
            "paper_context": {**payload["paper_context"], "redis_benchmark_parity_verified": False},
        })
        return emit(payload, 127)

    try:
        if server_command:
            proc = subprocess.Popen(
                server_command,
                cwd=cwd,
                env=server_env,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                start_new_session=True,
            )
            payload["process_group_pid"] = proc.pid
            assert proc.stdout is not None
            assert proc.stderr is not None
            stdout_capture = BoundedPipeCapture(proc.stdout, max_server_log_bytes)
            stderr_capture = BoundedPipeCapture(proc.stderr, max_server_log_bytes)
            stdout_capture.start()
            stderr_capture.start()
        ready, ready_detail, ready_attempts, early_exit_code = wait_until_ready(
            args.host,
            int(args.port),
            timeout=float(args.startup_timeout),
            request_timeout=float(args.request_timeout),
            proc=proc,
        )
        payload["ready_detail"] = ready_detail
        payload["ready_attempts"] = ready_attempts
        if early_exit_code is not None:
            payload["server_early_exit_code"] = early_exit_code
        if not ready:
            process_group_terminated, child_code = terminate_process_group(proc, float(args.grace_seconds))
            payload.update({
                "success": False,
                "error": f"server was not ready: {ready_detail}",
                "process_group_terminated": process_group_terminated,
                "server_exit_code": child_code,
            })
            payload.update(
                server_log_metadata(
                    stdout_capture,
                    stderr_capture,
                    max_server_log_bytes=max_server_log_bytes,
                    server_tail_bytes=server_tail_bytes,
                    keep_server_logs=bool(args.keep_server_logs),
                    evidence_dir=evidence_dir,
                )
            )
            append_evidence(payload, payload.pop("evidence", []))
            return emit(payload, 1)

        if scudo_setup.get("requested"):
            marker_seen = wait_for_scudo_runtime_identity_marker(stderr_capture)
            payload["scudo_runtime_identity_marker_seen"] = marker_seen
            if not marker_seen:
                process_group_terminated, child_code = terminate_process_group(proc, float(args.grace_seconds))
                failed_execution = dict(scudo_setup.get("execution") or {})
                failed_execution.update(
                    {
                        "execution_state": "verification-failed",
                        "runtime_verified": False,
                        "paper_allocator_equivalent": False,
                    }
                )
                payload.update(
                    {
                        "success": False,
                        "error": "Scudo server became ready without the canonical runtime identity marker; redis-benchmark timing was not started",
                        "scudo_execution_probe": failed_execution,
                        "process_group_terminated": process_group_terminated,
                        "server_exit_code": child_code,
                    }
                )
                payload.update(
                    server_log_metadata(
                        stdout_capture,
                        stderr_capture,
                        max_server_log_bytes=max_server_log_bytes,
                        server_tail_bytes=server_tail_bytes,
                        keep_server_logs=bool(args.keep_server_logs),
                        evidence_dir=evidence_dir,
                    )
                )
                append_evidence(payload, payload.pop("evidence", []))
                return emit(payload, 86)
            promote_verified_scudo_runtime(
                args,
                payload,
                scudo_setup,
                benchmark_path=benchmark_path,
            )

        command = redis_benchmark_command(args, benchmark_path)
        payload["redis_benchmark_command"] = command
        payload["command"] = command
        payload["argv"] = command
        bench = run_command_bounded(
            command,
            cwd=cwd,
            timeout_seconds=float(args.benchmark_timeout),
            max_output_bytes=max_benchmark_output_bytes,
        )
        benchmark_stdout_entry = write_evidence_file(
            evidence_dir,
            "redis-benchmark.stdout.retained.txt",
            str(bench["stdout"] or "").encode("utf-8", errors="replace"),
            "redis_benchmark_stdout_retained",
        )
        benchmark_stderr_entry = write_evidence_file(
            evidence_dir,
            "redis-benchmark.stderr.retained.txt",
            str(bench["stderr"] or "").encode("utf-8", errors="replace"),
            "redis_benchmark_stderr_retained",
        )
        combined = "\n".join(part for part in [bench["stdout"], bench["stderr"]] if part)
        rows, parser_name = parse_redis_benchmark_output(combined, requests_per_test=int(args.requests))
        process_group_terminated, child_code = terminate_process_group(proc, float(args.grace_seconds))
        log_metadata = server_log_metadata(
            stdout_capture,
            stderr_capture,
            max_server_log_bytes=max_server_log_bytes,
            server_tail_bytes=server_tail_bytes,
            keep_server_logs=bool(args.keep_server_logs),
            evidence_dir=evidence_dir,
        )
        server_evidence = log_metadata.pop("evidence", [])
        append_evidence(payload, [benchmark_stdout_entry, benchmark_stderr_entry, *server_evidence])
        payload.update({
            "redis_benchmark_returncode": bench["returncode"],
            "redis_benchmark_elapsed_seconds": bench["elapsed_seconds"],
            "redis_benchmark_timed_out": bench["timed_out"],
            "redis_benchmark_stdout_tail": bench["stdout"][-benchmark_tail_bytes:],
            "redis_benchmark_stderr_tail": bench["stderr"][-benchmark_tail_bytes:],
            "redis_benchmark_stdout_bytes": bench["stdout_bytes"],
            "redis_benchmark_stderr_bytes": bench["stderr_bytes"],
            "redis_benchmark_stdout_retained_bytes": bench["stdout_retained_bytes"],
            "redis_benchmark_stderr_retained_bytes": bench["stderr_retained_bytes"],
            "redis_benchmark_stdout_truncated": bench["stdout_truncated"],
            "redis_benchmark_stderr_truncated": bench["stderr_truncated"],
            "redis_benchmark_stdout_path": benchmark_stdout_entry.get("path") if isinstance(benchmark_stdout_entry, dict) else None,
            "redis_benchmark_stdout_sha256": benchmark_stdout_entry.get("sha256") if isinstance(benchmark_stdout_entry, dict) else None,
            "redis_benchmark_stderr_path": benchmark_stderr_entry.get("path") if isinstance(benchmark_stderr_entry, dict) else None,
            "redis_benchmark_stderr_sha256": benchmark_stderr_entry.get("sha256") if isinstance(benchmark_stderr_entry, dict) else None,
            "redis_benchmark_parser": parser_name,
            "redis_benchmark_rows": rows,
            "process_group_terminated": process_group_terminated,
            "server_exit_code": child_code,
        })
        payload.update(log_metadata)
        if int(bench["returncode"]) != 0:
            payload.update({"success": False, "error": f"redis-benchmark exited with code {bench['returncode']}"})
            return emit(payload, int(bench["returncode"]) or 1)
        if not rows:
            payload.update({"success": False, "error": "redis-benchmark output did not contain parseable throughput rows"})
            return emit(payload, 1)
        payload.update(summarize_rows(rows))
        payload.update({"success": True, "timed_out": False})
        promote_successful_claim_grade(payload)
        if scudo_setup.get("requested"):
            _PAPER_WORKLOAD_DRIVER.attach_scudo_timing_binding(
                payload,
                evidence_dir=evidence_dir,
            )
        return emit(payload, 0)
    except subprocess.TimeoutExpired as exc:
        process_group_terminated, child_code = terminate_process_group(proc, float(args.grace_seconds))
        payload.update({
            "success": False,
            "timed_out": True,
            "error": f"redis-benchmark timeout after {args.benchmark_timeout} seconds",
            "redis_benchmark_stdout_tail": (exc.stdout or "")[-4000:] if isinstance(exc.stdout, str) else "",
            "redis_benchmark_stderr_tail": (exc.stderr or "")[-4000:] if isinstance(exc.stderr, str) else "",
            "process_group_terminated": process_group_terminated,
            "server_exit_code": child_code,
        })
        payload.update(
                server_log_metadata(
                    stdout_capture,
                    stderr_capture,
                    max_server_log_bytes=max_server_log_bytes,
                    server_tail_bytes=server_tail_bytes,
                    keep_server_logs=bool(args.keep_server_logs),
                    evidence_dir=evidence_dir,
                )
            )
        append_evidence(payload, payload.pop("evidence", []))
        return emit(payload, 124)
    except KeyboardInterrupt:
        process_group_terminated, child_code = terminate_process_group(proc, float(args.grace_seconds))
        payload.update({"success": False, "interrupted": True, "process_group_terminated": process_group_terminated, "server_exit_code": child_code})
        payload.update(
            server_log_metadata(
                stdout_capture,
                stderr_capture,
                max_server_log_bytes=max_server_log_bytes,
                server_tail_bytes=server_tail_bytes,
                keep_server_logs=bool(args.keep_server_logs),
                evidence_dir=evidence_dir,
            )
        )
        append_evidence(payload, payload.pop("evidence", []))
        return emit(payload, 130)
    except Exception as exc:
        process_group_terminated, child_code = terminate_process_group(proc, float(args.grace_seconds))
        payload.update({
            "success": False,
            "error": f"{type(exc).__name__}: {exc}",
            "process_group_terminated": process_group_terminated,
            "server_exit_code": child_code,
        })
        payload.update(
            server_log_metadata(
                stdout_capture,
                stderr_capture,
                max_server_log_bytes=max_server_log_bytes,
                server_tail_bytes=server_tail_bytes,
                keep_server_logs=bool(args.keep_server_logs),
                evidence_dir=evidence_dir,
            )
        )
        append_evidence(payload, payload.pop("evidence", []))
        return emit(payload, 1)


if __name__ == "__main__":
    raise SystemExit(main())
