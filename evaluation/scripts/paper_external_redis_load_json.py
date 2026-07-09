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


def normalize_allocator_name(raw: Any) -> str:
    return str(raw or "").strip().lower().replace("_", "-")


def allocator_feature(allocator: Any) -> str:
    return ALLOCATOR_FEATURES.get(normalize_allocator_name(allocator), "")


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


def allocator_semantics_record(args: argparse.Namespace) -> Dict[str, Any]:
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
        implementation_kind = "std_alloc_system_fallback"
        paper_allocator_equivalent = False
        blockers.append(
            "RRedis bench_scudo currently routes to std::alloc::System; scudo paper evidence requires sanitizer runtime/toolchain support or an external wrapper"
        )
    elif feature:
        paper_allocator_equivalent = True
    else:
        blockers.append("RRedis allocator selector is not mapped to a benchmark allocator feature")

    return {
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

    def text_tail(self, limit: int = 4000) -> str:
        data = self.bytes()[-max(1, int(limit)) :]
        return data.decode("utf-8", errors="replace")


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
    if keep_server_logs:
        log_dir = Path(tempfile.mkdtemp(prefix="paper-redis-load-logs-"))
        stdout_path = log_dir / "server.stdout.tail.txt"
        stderr_path = log_dir / "server.stderr.tail.txt"
        stdout_path.write_bytes(stdout_capture.bytes() if stdout_capture is not None else b"")
        stderr_path.write_bytes(stderr_capture.bytes() if stderr_capture is not None else b"")
        metadata.update(
            {
                "server_log_dir": str(log_dir),
                "server_stdout_path": str(stdout_path),
                "server_stderr_path": str(stderr_path),
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


def build_common_payload(args: argparse.Namespace, command: List[str], cwd: str) -> Dict[str, Any]:
    allocator_semantics = allocator_semantics_record(args)
    bridge_blocker = (
        "Redis load bridge executes a real Redis-compatible server surface but does not yet prove "
        "paper allocator selection, variants, repetitions, exact provenance, or full macro methodology"
    )
    return {
        "schema_version": 1,
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
        "claim_grade_blockers": unique_strings([bridge_blocker, *allocator_semantics.get("claim_grade_blockers", [])]),
        "not_claim_grade_reason": bridge_blocker + ".",
        "allocator_feature": allocator_semantics.get("allocator_feature"),
        "allocator_semantics": allocator_semantics,
        "server_command": command,
        "server_cwd": cwd,
    }


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
    parser.add_argument("server_command", nargs=argparse.REMAINDER, help="Use -- followed by the upstream Redis server command")
    args = parser.parse_args(argv)

    command = list(args.server_command)
    if command and command[0] == "--":
        command = command[1:]
    cwd = os.getcwd()
    payload = build_common_payload(args, command, cwd)
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
