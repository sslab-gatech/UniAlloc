#!/usr/bin/env python3
"""Run a real Redis-compatible workload command and emit finite JSON timing.

This bridge is intentionally stdlib-only. It is for non-claim-grade external
RRedis/Rsedis integration: launch a Redis-compatible server command, wait for
RESP PING readiness, drive deterministic SET/GET operations, then print one JSON
object for the strict paper workload adapter. It is not a replacement for the
paper's full macro methodology, allocator controls, repetitions, or provenance
requirements.
"""

from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import math
import os
import platform
import queue
import signal
import socket
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple



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
REDIS_LOAD_BRIDGE_BLOCKER = (
    "Redis load bridge executes a real Redis-compatible server surface but does not yet prove "
    "paper allocator selection, variants, repetitions, exact provenance, or full macro methodology"
)


def _load_paper_workload_driver() -> Tuple[Optional[Any], Optional[str]]:
    """Load the canonical Scudo probe without relying on the caller's cwd/sys.path."""

    try:
        import paper_workload_driver as helper

        return helper, None
    except Exception as direct_exc:
        helper_path = Path(__file__).resolve().with_name("paper_workload_driver.py")
        try:
            spec = importlib.util.spec_from_file_location(
                "_unialloc_redis_load_paper_workload_driver",
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
                "_unialloc_redis_load_rredis_runner",
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


def unique_strings(values: List[Any]) -> List[str]:
    seen: set[str] = set()
    out: List[str] = []
    for value in values:
        text = str(value or "").strip()
        if not text or text in seen:
            continue
        seen.add(text)
        out.append(text)
    return out


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
        "source": "paper-external-redis-load-json-allocator-semantics",
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

def finite_number(value: Any) -> Optional[float]:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if math.isfinite(number):
        return number
    return None


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
    """Drain a server pipe while retaining only a bounded tail."""

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
) -> Dict[str, Any]:
    for capture in (stdout_capture, stderr_capture):
        if capture is not None:
            capture.join()
    marker_seen = bool(
        stderr_capture is not None and stderr_capture.scudo_runtime_identity_marker_seen
    )
    retain_logs = bool(keep_server_logs or marker_seen)
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
        "server_logs_retained": retain_logs,
    }
    if retain_logs:
        log_dir = Path(tempfile.mkdtemp(prefix="paper-redis-load-logs-"))
        stdout_path = log_dir / "server.stdout.tail.txt"
        stderr_path = log_dir / "server.stderr.tail.txt"
        stdout_path.write_bytes(stdout_capture.bytes() if stdout_capture is not None else b"")
        stderr_path.write_bytes(stderr_capture.bytes() if stderr_capture is not None else b"")
        stdout_sha256 = hashlib.sha256(stdout_path.read_bytes()).hexdigest()
        stderr_sha256 = hashlib.sha256(stderr_path.read_bytes()).hexdigest()
        evidence = [
            {
                "kind": "server_stdout_retained",
                "path": str(stdout_path),
                "sha256": stdout_sha256,
                "bytes": stdout_path.stat().st_size,
            },
            {
                "kind": "server_stderr_retained",
                "path": str(stderr_path),
                "sha256": stderr_sha256,
                "bytes": stderr_path.stat().st_size,
            },
        ]
        if marker_seen:
            marker_path = log_dir / "scudo-runtime-marker.stderr.txt"
            marker_path.write_text(SCUDO_RUNTIME_IDENTITY_MARKER, encoding="utf-8")
            evidence.append(
                {
                    "kind": "scudo_runtime_marker_stderr",
                    "path": str(marker_path),
                    "sha256": hashlib.sha256(marker_path.read_bytes()).hexdigest(),
                    "bytes": marker_path.stat().st_size,
                }
            )
        metadata.update(
            {
                "server_log_dir": str(log_dir),
                "server_stdout_path": str(stdout_path),
                "server_stderr_path": str(stderr_path),
                "server_stdout_sha256": stdout_sha256,
                "server_stderr_sha256": stderr_sha256,
                "evidence": evidence,
                "raw_evidence": evidence,
            }
        )
    return metadata


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
        if proc is not None:
            exit_code = proc.poll()
            if exit_code is not None:
                return (
                    False,
                    f"server process exited before readiness with code {exit_code}; last readiness error: {last_error}",
                    attempts,
                    exit_code,
                )
        time.sleep(0.1)
    return False, last_error or "startup timeout", attempts, None


def percentile(sorted_values: List[float], q: float) -> Optional[float]:
    if not sorted_values:
        return None
    if len(sorted_values) == 1:
        return sorted_values[0]
    pos = (len(sorted_values) - 1) * q
    lo = int(math.floor(pos))
    hi = int(math.ceil(pos))
    if lo == hi:
        return sorted_values[lo]
    return sorted_values[lo] * (hi - pos) + sorted_values[hi] * (pos - lo)


def value_bytes(size: int) -> bytes:
    return (b"x" * max(1, size))[: max(1, size)]


def run_one_operation(
    stream: Any,
    *,
    key: bytes,
    value: bytes,
    set_only: bool,
) -> None:
    stream.write(encode_command(b"SET", key, value))
    set_response = read_resp(stream)
    if str(set_response).upper() != "OK":
        raise RuntimeError(f"unexpected SET response: {set_response!r}")
    if set_only:
        return
    stream.write(encode_command(b"GET", key))
    get_response = read_resp(stream)
    if get_response != value:
        raise RuntimeError("GET response did not match SET value")


def run_load(args: argparse.Namespace) -> Dict[str, Any]:
    operation_count = int(args.operations)
    concurrency = max(1, int(args.concurrency))
    prefix = args.key_prefix.encode("utf-8")
    value = value_bytes(int(args.value_size))
    set_only = args.operation.lower() == "set"
    work: "queue.Queue[int]" = queue.Queue()
    for idx in range(operation_count):
        work.put(idx)
    lock = threading.Lock()
    latencies: List[float] = []
    error_counts: Dict[str, int] = {}
    ok_count = 0
    failed_count = 0

    def record_failure(error: str) -> None:
        nonlocal failed_count
        with lock:
            failed_count += 1
            error_counts[error] = error_counts.get(error, 0) + 1

    def worker(worker_index: int) -> None:
        nonlocal ok_count
        stream = None
        sock: Optional[socket.socket] = None
        try:
            sock = socket.create_connection((args.host, int(args.port)), timeout=float(args.request_timeout))
            sock.settimeout(float(args.request_timeout))
            stream = sock.makefile("rwb", buffering=0)
            while True:
                try:
                    idx = work.get_nowait()
                except queue.Empty:
                    return
                key = b"%s:%d:%d" % (prefix, worker_index, idx)
                start = now_seconds()
                try:
                    run_one_operation(stream, key=key, value=value, set_only=set_only)
                    elapsed = now_seconds() - start
                    with lock:
                        ok_count += 1
                        latencies.append(elapsed)
                except Exception as exc:
                    record_failure(f"{type(exc).__name__}: {exc}")
                    try:
                        if sock is not None:
                            sock.close()
                    except Exception:
                        pass
                    sock = socket.create_connection((args.host, int(args.port)), timeout=float(args.request_timeout))
                    sock.settimeout(float(args.request_timeout))
                    stream = sock.makefile("rwb", buffering=0)
                finally:
                    work.task_done()
        except Exception as exc:
            record_failure(f"worker_connect_{type(exc).__name__}: {exc}")
            while True:
                try:
                    work.get_nowait()
                    work.task_done()
                except queue.Empty:
                    return
        finally:
            try:
                if sock is not None:
                    sock.close()
            except Exception:
                pass

    start = now_seconds()
    threads = [threading.Thread(target=worker, args=(idx,), daemon=True) for idx in range(min(concurrency, operation_count or 1))]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    elapsed = now_seconds() - start
    sorted_latencies = sorted(latencies)
    avg_latency = sum(latencies) / len(latencies) if latencies else None
    return {
        "seconds": elapsed,
        "operation": args.operation.lower(),
        "operation_count": operation_count,
        "successful_operations": ok_count,
        "failed_operations": failed_count,
        "operations_per_second": (ok_count / elapsed) if elapsed > 0 else None,
        "average_latency_seconds": avg_latency,
        "min_latency_seconds": sorted_latencies[0] if sorted_latencies else None,
        "max_latency_seconds": sorted_latencies[-1] if sorted_latencies else None,
        "p50_latency_seconds": percentile(sorted_latencies, 0.50),
        "p95_latency_seconds": percentile(sorted_latencies, 0.95),
        "value_size": int(args.value_size),
        "error_counts": error_counts,
    }


def run_warmup(args: argparse.Namespace) -> Tuple[int, int, Dict[str, int]]:
    ok = 0
    failed = 0
    errors: Dict[str, int] = {}
    value = value_bytes(int(args.value_size))
    set_only = args.operation.lower() == "set"
    for idx in range(max(0, int(args.warmup_operations))):
        try:
            with socket.create_connection((args.host, int(args.port)), timeout=float(args.request_timeout)) as sock:
                sock.settimeout(float(args.request_timeout))
                stream = sock.makefile("rwb", buffering=0)
                key = f"{args.key_prefix}:warmup:{idx}".encode("utf-8")
                run_one_operation(stream, key=key, value=value, set_only=set_only)
                ok += 1
        except Exception as exc:
            failed += 1
            key = f"{type(exc).__name__}: {exc}"
            errors[key] = errors.get(key, 0) + 1
    return ok, failed, errors


def build_common_payload(
    args: argparse.Namespace,
    command: List[str],
    cwd: str,
    *,
    scudo_setup: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    allocator_semantics = allocator_semantics_record(args, scudo_setup)
    payload = {
        "schema_version": 1,
        "source": "paper-external-redis-load-json",
        "dataset": args.dataset,
        "benchmark": args.benchmark,
        "allocator": args.allocator,
        "variant_feature": args.variant_feature,
        "run_index": args.run_index,
        "host": args.host,
        "port": int(args.port),
        "operations": int(args.operations),
        "concurrency": int(args.concurrency),
        "warmup_operations": int(args.warmup_operations),
        "measurement_source": "redis_load_json",
        "benchmark_owned_json": True,
        "claim_grade": False,
        "claim_grade_blockers": unique_strings(
            [REDIS_LOAD_BRIDGE_BLOCKER, *allocator_semantics.get("claim_grade_blockers", [])]
        ),
        "not_claim_grade_reason": REDIS_LOAD_BRIDGE_BLOCKER + ".",
        "allocator_feature": allocator_semantics.get("allocator_feature"),
        "allocator_semantics": allocator_semantics,
        "server_command": command,
        "server_cwd": cwd,
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
) -> None:
    verified, semantics = verified_scudo_runtime_records(scudo_setup)
    verified_setup = {**scudo_setup, "execution": verified}
    payload["scudo_execution_probe"] = verified
    payload["allocator_semantics"] = {
        **allocator_semantics_record(args, verified_setup),
        **semantics,
        "schema_version": 1,
        "source": "paper-external-redis-load-json-allocator-semantics",
        "claim_grade_blockers": [],
        "notes": ["server emitted the canonical Scudo runtime identity marker before workload timing"],
    }
    payload["claim_grade_blockers"] = [REDIS_LOAD_BRIDGE_BLOCKER]


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dataset", required=True)
    parser.add_argument("--benchmark", required=True)
    parser.add_argument("--allocator", required=True)
    parser.add_argument("--variant-feature", default="")
    parser.add_argument("--run-index", required=True)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=6379)
    parser.add_argument("--operations", type=int, default=200)
    parser.add_argument("--concurrency", type=int, default=8)
    parser.add_argument("--warmup-operations", type=int, default=10)
    parser.add_argument("--operation", choices=["set_get", "set"], default="set_get")
    parser.add_argument("--key-prefix", default="unialloc-paper-redis-load")
    parser.add_argument("--value-size", type=int, default=64)
    parser.add_argument("--startup-timeout", type=float, default=30.0)
    parser.add_argument("--request-timeout", type=float, default=2.0)
    parser.add_argument("--server-timeout", type=float, default=0.0, help="Optional hard timeout for the whole bridge; 0 disables")
    parser.add_argument("--grace-seconds", type=float, default=2.0)
    parser.add_argument("--max-server-log-bytes", type=int, default=1024 * 1024, help="Retain at most this many bytes per server stdout/stderr stream")
    parser.add_argument("--server-tail-bytes", type=int, default=4000, help="Embed at most this many retained server log bytes per stream in result JSON")
    parser.add_argument("--keep-server-logs", action="store_true", help="Write retained server log tails to a temporary directory and report the paths")
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

    command = list(args.server_command)
    if command and command[0] == "--":
        command = command[1:]
    cwd = os.getcwd()
    server_env, scudo_setup = prepare_scudo_server_execution(
        args,
        command=command,
        cwd=cwd,
    )
    scudo_attestation = verify_scudo_runner_attestation(args, command, cwd)
    payload = build_common_payload(args, command, cwd, scudo_setup=scudo_setup)
    if scudo_attestation.get("required"):
        payload["scudo_runner_attestation"] = scudo_attestation
    if scudo_setup.get("requested") and not command:
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
    proc: Optional[subprocess.Popen[Any]] = None
    stdout_capture: Optional[BoundedPipeCapture] = None
    stderr_capture: Optional[BoundedPipeCapture] = None
    start_all = now_seconds()
    max_server_log_bytes = positive_int_value(args.max_server_log_bytes, 1024 * 1024)
    server_tail_bytes = positive_int_value(args.server_tail_bytes, 4000)
    payload["server_log_max_bytes"] = max_server_log_bytes
    try:
        if command:
            proc = subprocess.Popen(
                command,
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
                )
            )
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
                        "error": "Scudo server became ready without the canonical runtime identity marker; workload timing was not started",
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
                    )
                )
                return emit(payload, 86)
            promote_verified_scudo_runtime(args, payload, scudo_setup)
        warmup_ok, warmup_failed, warmup_errors = run_warmup(args)
        payload.update({
            "warmup_successful_operations": warmup_ok,
            "warmup_failed_operations": warmup_failed,
            "warmup_error_counts": warmup_errors,
        })
        load = run_load(args)
        timed_out = bool(args.server_timeout and (now_seconds() - start_all) > float(args.server_timeout))
        process_group_terminated, child_code = terminate_process_group(proc, float(args.grace_seconds))
        log_metadata = server_log_metadata(
            stdout_capture,
            stderr_capture,
            max_server_log_bytes=max_server_log_bytes,
            server_tail_bytes=server_tail_bytes,
            keep_server_logs=bool(args.keep_server_logs),
        )
        payload.update(load)
        payload.update({
            "success": load["failed_operations"] == 0 and warmup_failed == 0 and not timed_out,
            "timed_out": timed_out,
            "process_group_terminated": process_group_terminated,
            "server_exit_code": child_code,
        })
        payload.update(log_metadata)
        if timed_out:
            payload["error"] = "server-timeout exceeded"
            return emit(payload, 124)
        if warmup_failed:
            payload["error"] = "one or more Redis warmup operations failed"
            return emit(payload, 1)
        if load["failed_operations"]:
            payload["error"] = "one or more Redis operations failed"
            return emit(payload, 1)
        if scudo_setup.get("requested"):
            evidence_dir = (
                Path(str(payload["server_log_dir"]))
                if payload.get("server_log_dir")
                else None
            )
            _PAPER_WORKLOAD_DRIVER.attach_scudo_timing_binding(
                payload,
                evidence_dir=evidence_dir,
            )
        return emit(payload, 0)
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
            )
        )
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
            )
        )
        return emit(payload, 1)


if __name__ == "__main__":
    raise SystemExit(main())
