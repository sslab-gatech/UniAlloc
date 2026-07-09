#!/usr/bin/env python3
"""Run a real HTTP workload command and emit finite JSON timing.

This bridge is intentionally small and stdlib-only. It is for non-claim-grade
external web workload integration: launch an upstream example/server command,
wait for a ready URL, drive deterministic HTTP requests, then print one JSON
object for the strict paper workload adapter. It is not a replacement for the
paper's full macro methodology, allocator controls, repetitions, or provenance
requirements.
"""

from __future__ import annotations

import argparse
import json
import math
import os
import queue
import signal
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple


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


def read_tail(path: Path, limit: int = 4000) -> str:
    if not path.exists():
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
        log_dir = Path(tempfile.mkdtemp(prefix="paper-http-load-logs-"))
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


def http_request(url: str, *, method: str, headers: Dict[str, str], body: Optional[bytes], timeout: float) -> Tuple[bool, float, int, int, str]:
    start = now_seconds()
    status = 0
    size = 0
    error = ""
    try:
        req = urllib.request.Request(url, data=body, headers=headers, method=method)
        with urllib.request.urlopen(req, timeout=timeout) as response:
            data = response.read()
            status = int(getattr(response, "status", response.getcode()))
            size = len(data)
        ok = True
    except urllib.error.HTTPError as exc:
        status = int(exc.code)
        try:
            size = len(exc.read())
        except Exception:
            size = 0
        error = f"http_error:{exc.code}"
        ok = False
    except Exception as exc:
        error = f"{type(exc).__name__}: {exc}"
        ok = False
    elapsed = now_seconds() - start
    return ok, elapsed, status, size, error


def wait_until_ready(url: str, *, method: str, headers: Dict[str, str], body: Optional[bytes], timeout: float, request_timeout: float, expected_status: int) -> Tuple[bool, str, int]:
    deadline = now_seconds() + timeout
    attempts = 0
    last_error = ""
    last_status = 0
    while now_seconds() < deadline:
        attempts += 1
        ok, _, status, _, error = http_request(url, method=method, headers=headers, body=body, timeout=request_timeout)
        last_status = status
        if ok and status == expected_status:
            return True, f"ready after {attempts} attempts", status
        last_error = error or f"status:{status}"
        time.sleep(0.1)
    return False, last_error or "startup timeout", last_status


def parse_headers(values: List[str]) -> Dict[str, str]:
    headers: Dict[str, str] = {}
    for value in values:
        if ":" not in value:
            raise ValueError(f"invalid header {value!r}; expected 'Name: value'")
        name, header_value = value.split(":", 1)
        headers[name.strip()] = header_value.strip()
    return headers


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


def run_load(args: argparse.Namespace, headers: Dict[str, str], body: Optional[bytes]) -> Dict[str, Any]:
    request_count = int(args.requests)
    concurrency = max(1, int(args.concurrency))
    work: "queue.Queue[int]" = queue.Queue()
    for idx in range(request_count):
        work.put(idx)
    lock = threading.Lock()
    latencies: List[float] = []
    statuses: Dict[str, int] = {}
    error_counts: Dict[str, int] = {}
    bytes_total = 0
    ok_count = 0
    failed_count = 0

    def worker() -> None:
        nonlocal bytes_total, ok_count, failed_count
        while True:
            try:
                work.get_nowait()
            except queue.Empty:
                return
            ok, latency, status, size, error = http_request(
                args.url,
                method=args.method,
                headers=headers,
                body=body,
                timeout=float(args.request_timeout),
            )
            expected_ok = ok and status == int(args.expected_status)
            with lock:
                latencies.append(latency)
                statuses[str(status)] = statuses.get(str(status), 0) + 1
                bytes_total += size
                if expected_ok:
                    ok_count += 1
                else:
                    failed_count += 1
                    key = error or f"unexpected_status:{status}"
                    error_counts[key] = error_counts.get(key, 0) + 1
            work.task_done()

    start = now_seconds()
    threads = [threading.Thread(target=worker, daemon=True) for _ in range(min(concurrency, request_count or 1))]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    elapsed = now_seconds() - start
    sorted_latencies = sorted(latencies)
    avg_latency = sum(latencies) / len(latencies) if latencies else None
    return {
        "seconds": elapsed,
        "request_count": request_count,
        "successful_requests": ok_count,
        "failed_requests": failed_count,
        "requests_per_second": (ok_count / elapsed) if elapsed > 0 else None,
        "bytes_total": bytes_total,
        "average_latency_seconds": avg_latency,
        "min_latency_seconds": sorted_latencies[0] if sorted_latencies else None,
        "max_latency_seconds": sorted_latencies[-1] if sorted_latencies else None,
        "p50_latency_seconds": percentile(sorted_latencies, 0.50),
        "p95_latency_seconds": percentile(sorted_latencies, 0.95),
        "status_counts": statuses,
        "error_counts": error_counts,
    }


def build_common_payload(args: argparse.Namespace, command: List[str], cwd: str) -> Dict[str, Any]:
    return {
        "schema_version": 1,
        "dataset": args.dataset,
        "benchmark": args.benchmark,
        "allocator": args.allocator,
        "variant_feature": args.variant_feature,
        "run_index": args.run_index,
        "url": args.url,
        "ready_url": args.ready_url or args.url,
        "method": args.method,
        "expected_status": int(args.expected_status),
        "requests": int(args.requests),
        "concurrency": int(args.concurrency),
        "measurement_source": "http_load_json",
        "benchmark_owned_json": True,
        "claim_grade": False,
        "not_claim_grade_reason": "HTTP load bridge executes a real upstream server surface but does not yet prove paper allocator selection, variants, repetitions, exact provenance, or full macro methodology.",
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
    parser.add_argument("--url", required=True)
    parser.add_argument("--ready-url", default="")
    parser.add_argument("--method", default="GET")
    parser.add_argument("--header", action="append", default=[])
    parser.add_argument("--body", default=None)
    parser.add_argument("--expected-status", type=int, default=200)
    parser.add_argument("--requests", type=int, default=200)
    parser.add_argument("--concurrency", type=int, default=16)
    parser.add_argument("--warmup-requests", type=int, default=5)
    parser.add_argument("--startup-timeout", type=float, default=30.0)
    parser.add_argument("--request-timeout", type=float, default=2.0)
    parser.add_argument("--server-timeout", type=float, default=0.0, help="Optional hard timeout for the whole bridge; 0 disables")
    parser.add_argument("--grace-seconds", type=float, default=2.0)
    parser.add_argument("--max-server-log-bytes", type=int, default=1024 * 1024, help="Retain at most this many bytes per server stdout/stderr stream")
    parser.add_argument("--server-tail-bytes", type=int, default=4000, help="Embed at most this many retained server log bytes per stream in result JSON")
    parser.add_argument("--keep-server-logs", action="store_true", help="Write retained server log tails to a temporary directory and report the paths")
    parser.add_argument("server_command", nargs=argparse.REMAINDER, help="Use -- followed by the upstream server command")
    args = parser.parse_args(argv)

    command = list(args.server_command)
    if command and command[0] == "--":
        command = command[1:]
    headers = parse_headers(args.header)
    body = args.body.encode("utf-8") if args.body is not None else None
    cwd = os.getcwd()
    payload = build_common_payload(args, command, cwd)
    proc: Optional[subprocess.Popen[Any]] = None
    stdout_capture: Optional[BoundedPipeCapture] = None
    stderr_capture: Optional[BoundedPipeCapture] = None
    start_all = now_seconds()
    process_group_terminated = False
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
        ready_url = args.ready_url or args.url
        ready, ready_detail, ready_status = wait_until_ready(
            ready_url,
            method=args.method,
            headers=headers,
            body=body,
            timeout=float(args.startup_timeout),
            request_timeout=float(args.request_timeout),
            expected_status=int(args.expected_status),
        )
        payload["ready_detail"] = ready_detail
        payload["ready_status"] = ready_status
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
        for _ in range(max(0, int(args.warmup_requests))):
            http_request(args.url, method=args.method, headers=headers, body=body, timeout=float(args.request_timeout))
        load = run_load(args, headers, body)
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
            "success": load["failed_requests"] == 0 and not timed_out,
            "timed_out": timed_out,
            "process_group_terminated": process_group_terminated,
            "server_exit_code": child_code,
        })
        payload.update(log_metadata)
        if timed_out:
            payload["error"] = "server-timeout exceeded"
            return emit(payload, 124)
        if load["failed_requests"]:
            payload["error"] = "one or more HTTP requests failed"
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
