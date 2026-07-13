#!/usr/bin/env python3
"""Run the paper Collections workload through a Linux Docker runner.

This wrapper exists for host-specific paper allocator cells such as Linux/glibc
ptmalloc.  It is deliberately fail-closed: dry-run probes must prove that Docker
is reachable and that the container image has Python plus the repo rust-toolchain
before it delegates to paper_workload_driver.py inside the container.
"""

from __future__ import annotations

import argparse
import json
import os
import platform as host_platform
import re
import signal
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import uuid
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

ROOT = Path(__file__).resolve().parents[2]
DEFAULT_IMAGE = os.environ.get("UNIALLOC_COLLECTIONS_DOCKER_IMAGE", "unialloc-collections-runner:nightly-2022-07-01")
DEFAULT_PLATFORM = os.environ.get("UNIALLOC_COLLECTIONS_DOCKER_PLATFORM", "linux/arm64" if host_platform.machine() in {"arm64", "aarch64"} else "linux/amd64")
DEFAULT_TARGET_VOLUME_ENV = os.environ.get("UNIALLOC_COLLECTIONS_DOCKER_TARGET_VOLUME")
RUST_TOOLCHAIN_ENV = "UNIALLOC_RUST_TOOLCHAIN"
REPO_TOOLCHAIN_ALIASES = {"", "repo", "pinned", "rust-toolchain", "rust_toolchain", "default"}
SYSTEM_TOOLCHAIN_ALIASES = {"none", "system", "host", "path"}
LATEST_TOOLCHAIN_ALIASES = {"latest", "current-nightly", "current_nightly"}
STABLE_TOOLCHAIN_ALIASES = {"current-stable", "current_stable"}
PAPER_EXACT_RUST_TOOLCHAIN = "nightly-2022-07-01"
TIMEOUT_REPORT_GRACE_SECONDS = 120
DOCKER_CLEANUP_COMMAND_TIMEOUT_SECONDS = 15
DOCKER_CONTAINER_STOP_GRACE_SECONDS = 5
INTERRUPTED_OUTPUT_TAIL_BYTES = 64 * 1024
INTERRUPTED_DOCKER_CLI_SIGINT_GRACE_SECONDS = 0.5
INTERRUPTED_DOCKER_CLI_TERM_GRACE_SECONDS = 0.25
INTERRUPTED_DOCKER_CLEANUP_COMMAND_TIMEOUT_SECONDS = 1.0
COMMAND_OUTPUT_RING_BYTES = 1024 * 1024
COMMAND_CAPTURE_JOIN_GRACE_SECONDS = 0.25


def json_error(message: str, *, code: int = 2, **extra: Any) -> int:
    print(json.dumps({"ok": False, "source": "paper-collections-docker-driver", "error": message, **extra}, sort_keys=True), file=sys.stderr)
    return code


def subprocess_text(value: Any) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    return ""


def bounded_text_tail(value: Any, max_bytes: int) -> Tuple[str, int, bool]:
    """Return a UTF-8-safe bounded tail plus its original byte count."""

    raw = subprocess_text(value).encode("utf-8", errors="replace")
    limit = max(1, int(max_bytes))
    retained = raw[-limit:]
    return retained.decode("utf-8", errors="ignore"), len(raw), len(raw) > len(retained)


def relay_interrupted_docker_output(result: Dict[str, Any]) -> Dict[str, Any]:
    """Relay only bounded Docker-client tails to the outer plan supervisor."""

    stdout_tail, observed_stdout_bytes, observed_stdout_truncated = bounded_text_tail(
        result.get("stdout"), INTERRUPTED_OUTPUT_TAIL_BYTES
    )
    stderr_tail, observed_stderr_bytes, observed_stderr_truncated = bounded_text_tail(
        result.get("stderr"), INTERRUPTED_OUTPUT_TAIL_BYTES
    )
    stdout_bytes = int(result.get("stdout_bytes") or observed_stdout_bytes)
    stderr_bytes = int(result.get("stderr_bytes") or observed_stderr_bytes)
    stdout_truncated = bool(result.get("stdout_truncated")) or observed_stdout_truncated
    stderr_truncated = bool(result.get("stderr_truncated")) or observed_stderr_truncated
    if stdout_tail:
        try:
            sys.stdout.write(stdout_tail)
            if not stdout_tail.endswith("\n"):
                sys.stdout.write("\n")
            sys.stdout.flush()
        except (BrokenPipeError, OSError):
            pass
    if stderr_tail:
        try:
            sys.stderr.write(stderr_tail)
            if not stderr_tail.endswith("\n"):
                sys.stderr.write("\n")
            sys.stderr.flush()
        except (BrokenPipeError, OSError):
            pass
    return {
        "stdout_bytes": stdout_bytes,
        "stderr_bytes": stderr_bytes,
        "stdout_retained_bytes": len(stdout_tail.encode("utf-8", errors="replace")),
        "stderr_retained_bytes": len(stderr_tail.encode("utf-8", errors="replace")),
        "stdout_truncated": stdout_truncated,
        "stderr_truncated": stderr_truncated,
        "max_relay_bytes_per_stream": INTERRUPTED_OUTPUT_TAIL_BYTES,
    }


class RingPipeCapture:
    """Continuously drain a pipe while retaining only a fixed-size byte tail."""

    def __init__(self, pipe: Any, max_bytes: Optional[int] = None) -> None:
        self.pipe = pipe
        self.max_bytes = max(
            1,
            int(COMMAND_OUTPUT_RING_BYTES if max_bytes is None else max_bytes),
        )
        self.total_bytes = 0
        self._tail = bytearray()
        self._lock = threading.Lock()
        self._supervisor_closed = False
        self.thread = threading.Thread(target=self._drain, daemon=True)

    def start(self) -> None:
        self.thread.start()

    def join(self, timeout: float) -> None:
        self.thread.join(timeout=max(0.0, timeout))

    def close(self) -> None:
        self._supervisor_closed = True
        try:
            self.pipe.close()
        except (OSError, ValueError):
            pass

    def _drain(self) -> None:
        try:
            while True:
                chunk = self.pipe.read(64 * 1024)
                if not chunk:
                    break
                if isinstance(chunk, str):
                    chunk = chunk.encode("utf-8", errors="replace")
                with self._lock:
                    self.total_bytes += len(chunk)
                    self._tail.extend(chunk)
                    if len(self._tail) > self.max_bytes:
                        del self._tail[: len(self._tail) - self.max_bytes]
        except (OSError, ValueError):
            # A bounded supervisor close is expected when a detached descendant
            # keeps the inherited pipe open after the Docker CLI has exited.
            if not self._supervisor_closed:
                raise
        finally:
            self.close()

    def snapshot(self, max_bytes: int) -> Tuple[str, int, int, bool]:
        limit = max(1, int(max_bytes))
        with self._lock:
            total_bytes = self.total_bytes
            raw = bytes(self._tail[-limit:])
        truncated = total_bytes > len(raw)
        errors = "ignore" if truncated else "replace"
        return raw.decode("utf-8", errors=errors), total_bytes, len(raw), truncated


def finalize_capture_threads(
    captures: Iterable[RingPipeCapture],
    *,
    suppress_keyboard_interrupt: bool,
) -> Tuple[bool, int]:
    """Bound capture joining so inherited descendant pipes cannot stall cleanup."""

    capture_list = list(captures)
    nested_interrupts = 0
    deadline = time.monotonic() + COMMAND_CAPTURE_JOIN_GRACE_SECONDS
    for capture in capture_list:
        try:
            capture.join(max(0.0, deadline - time.monotonic()))
        except KeyboardInterrupt:
            if not suppress_keyboard_interrupt:
                raise
            nested_interrupts += 1
    for capture in capture_list:
        if capture.thread.is_alive():
            capture.close()
    close_deadline = time.monotonic() + 0.05
    for capture in capture_list:
        if not capture.thread.is_alive():
            continue
        try:
            capture.join(max(0.0, close_deadline - time.monotonic()))
        except KeyboardInterrupt:
            if not suppress_keyboard_interrupt:
                raise
            nested_interrupts += 1
    return not any(capture.thread.is_alive() for capture in capture_list), nested_interrupts


def command_capture_fields(
    stdout_capture: RingPipeCapture,
    stderr_capture: RingPipeCapture,
    *,
    max_bytes: int,
    capture_complete: bool,
) -> Dict[str, Any]:
    stdout, stdout_bytes, stdout_retained_bytes, stdout_truncated = (
        stdout_capture.snapshot(max_bytes)
    )
    stderr, stderr_bytes, stderr_retained_bytes, stderr_truncated = (
        stderr_capture.snapshot(max_bytes)
    )
    return {
        "stdout": stdout,
        "stderr": stderr,
        "stdout_tail": stdout[-4000:],
        "stderr_tail": stderr[-4000:],
        "stdout_bytes": stdout_bytes,
        "stderr_bytes": stderr_bytes,
        "stdout_retained_bytes": stdout_retained_bytes,
        "stderr_retained_bytes": stderr_retained_bytes,
        "stdout_truncated": stdout_truncated,
        "stderr_truncated": stderr_truncated,
        "capture_complete": capture_complete,
        "capture_ring_bytes_per_stream": COMMAND_OUTPUT_RING_BYTES,
    }


class DeferredSigint:
    """Count repeated SIGINTs without letting them replace an active interrupt."""

    def __init__(self) -> None:
        self.count = 0
        self.active = False
        self.previous_handler: Any = None

    def _handle(self, _signum: int, _frame: Any) -> None:
        self.count += 1

    def __enter__(self) -> "DeferredSigint":
        if threading.current_thread() is not threading.main_thread():
            return self
        try:
            self.previous_handler = signal.getsignal(signal.SIGINT)
            signal.signal(signal.SIGINT, self._handle)
            self.active = True
        except (ValueError, OSError):  # pragma: no cover - non-main/platform fallback.
            self.active = False
        return self

    def __exit__(self, _exc_type: Any, _exc: Any, _traceback: Any) -> None:
        if not self.active:
            return
        signal.signal(signal.SIGINT, self.previous_handler)
        self.active = False


def terminate_interrupted_command(
    proc: subprocess.Popen[Any],
) -> Dict[str, Any]:
    """Stop an interrupted Docker CLI within the outer cleanup grace window."""

    signal_sent: Optional[str] = None
    forced_kill = False
    nested_interrupts = 0
    if proc.poll() is None:
        try:
            if os.name == "posix":
                proc.send_signal(signal.SIGINT)
                signal_sent = "SIGINT"
            else:  # pragma: no cover - Windows runner fallback.
                proc.terminate()
                signal_sent = "terminate"
        except (ProcessLookupError, PermissionError):
            pass
    try:
        proc.wait(timeout=INTERRUPTED_DOCKER_CLI_SIGINT_GRACE_SECONDS)
    except (subprocess.TimeoutExpired, KeyboardInterrupt) as exc:
        if isinstance(exc, KeyboardInterrupt):
            nested_interrupts += 1
        if proc.poll() is None:
            try:
                proc.terminate()
                signal_sent = "SIGTERM" if os.name == "posix" else "terminate"
            except (ProcessLookupError, PermissionError):
                pass
        try:
            proc.wait(timeout=INTERRUPTED_DOCKER_CLI_TERM_GRACE_SECONDS)
        except (subprocess.TimeoutExpired, KeyboardInterrupt) as exc:
            if isinstance(exc, KeyboardInterrupt):
                nested_interrupts += 1
            if proc.poll() is None:
                try:
                    proc.kill()
                    forced_kill = True
                    signal_sent = "SIGKILL" if os.name == "posix" else "kill"
                except (ProcessLookupError, PermissionError):
                    pass
            try:
                proc.wait(timeout=INTERRUPTED_DOCKER_CLI_TERM_GRACE_SECONDS)
            except subprocess.TimeoutExpired:  # pragma: no cover - defensive.
                pass
            except KeyboardInterrupt:
                nested_interrupts += 1
    return {
        "docker_cli_signal_sent": signal_sent,
        "docker_cli_forced_kill": forced_kill,
        "docker_cli_nested_interrupts": nested_interrupts,
        "child_returncode_after_cleanup": proc.poll(),
    }


def run_command(command: List[str], *, timeout: float, cwd: Path = ROOT) -> Dict[str, Any]:
    started = time.time()
    proc = subprocess.Popen(
        command,
        cwd=str(cwd),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    assert proc.stdout is not None
    assert proc.stderr is not None
    stdout_capture = RingPipeCapture(proc.stdout, COMMAND_OUTPUT_RING_BYTES)
    stderr_capture = RingPipeCapture(proc.stderr, COMMAND_OUTPUT_RING_BYTES)
    stdout_capture.start()
    stderr_capture.start()
    try:
        proc.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait()
        capture_complete, _nested = finalize_capture_threads(
            (stdout_capture, stderr_capture),
            suppress_keyboard_interrupt=False,
        )
        return {
            "command": command,
            "ok": False,
            "exit_code": None,
            "timed_out": True,
            "duration_seconds": round(time.time() - started, 3),
            **command_capture_fields(
                stdout_capture,
                stderr_capture,
                max_bytes=INTERRUPTED_OUTPUT_TAIL_BYTES,
                capture_complete=capture_complete,
            ),
        }
    except KeyboardInterrupt as exc:
        with DeferredSigint() as deferred_sigint:
            cleanup = terminate_interrupted_command(proc)
            capture_complete, capture_nested_interrupts = finalize_capture_threads(
                (stdout_capture, stderr_capture),
                suppress_keyboard_interrupt=True,
            )
            nested_interrupts = (
                int(cleanup.get("docker_cli_nested_interrupts") or 0)
                + capture_nested_interrupts
                + deferred_sigint.count
            )
            result = {
                "command": command,
                "ok": False,
                "exit_code": 130,
                "timed_out": False,
                "interrupted": True,
                "status": "interrupted",
                "interrupt_signal": "SIGINT",
                "duration_seconds": round(time.time() - started, 3),
                **command_capture_fields(
                    stdout_capture,
                    stderr_capture,
                    max_bytes=INTERRUPTED_OUTPUT_TAIL_BYTES,
                    capture_complete=capture_complete,
                ),
                **cleanup,
                "deferred_sigint_count": deferred_sigint.count,
                "docker_cli_nested_interrupts": nested_interrupts,
            }
            setattr(exc, "_unialloc_bounded_child_result", result)
            raise
    capture_complete, _nested = finalize_capture_threads(
        (stdout_capture, stderr_capture),
        suppress_keyboard_interrupt=False,
    )
    return {
        "command": command,
        "ok": proc.returncode == 0,
        "exit_code": proc.returncode,
        "duration_seconds": round(time.time() - started, 3),
        **command_capture_fields(
            stdout_capture,
            stderr_capture,
            max_bytes=COMMAND_OUTPUT_RING_BYTES,
            capture_complete=capture_complete,
        ),
    }


def unique(values: Iterable[Optional[str]]) -> List[str]:
    seen = set()
    out: List[str] = []
    for value in values:
        if not value:
            continue
        text = str(value)
        if text in seen:
            continue
        seen.add(text)
        out.append(text)
    return out


def read_repo_rust_toolchain() -> str:
    toolchain_file = ROOT / "rust-toolchain"
    if not toolchain_file.exists():
        return ""
    return toolchain_file.read_text(encoding="utf-8").strip()


def raw_requested_rust_toolchain(args: Optional[argparse.Namespace]) -> Dict[str, Optional[str]]:
    if args is not None:
        value = getattr(args, "rust_toolchain", None)
        if value is not None and str(value).strip():
            return {"value": str(value).strip(), "source": "cli-or-env-forwarded"}
    env_value = os.environ.get(RUST_TOOLCHAIN_ENV)
    if env_value is not None and str(env_value).strip():
        return {"value": str(env_value).strip(), "source": "env"}
    return {"value": None, "source": "repo-rust-toolchain"}


def normalize_rust_toolchain(value: Optional[str], repo_toolchain: str) -> str:
    raw = str(value or "").strip()
    if raw.startswith("+"):
        raw = raw[1:]
    lowered = raw.lower()
    if lowered in REPO_TOOLCHAIN_ALIASES:
        return repo_toolchain
    if lowered in SYSTEM_TOOLCHAIN_ALIASES:
        return ""
    if lowered in LATEST_TOOLCHAIN_ALIASES:
        return "nightly"
    if lowered in STABLE_TOOLCHAIN_ALIASES:
        return "stable"
    return raw


def rust_toolchain_provenance(args: Optional[argparse.Namespace] = None) -> Dict[str, Any]:
    repo_toolchain = read_repo_rust_toolchain()
    requested = raw_requested_rust_toolchain(args)
    raw_value = requested.get("value")
    effective = normalize_rust_toolchain(raw_value, repo_toolchain)
    source = str(requested.get("source") or "repo-rust-toolchain")
    explicit_non_repo = source != "repo-rust-toolchain" and effective != repo_toolchain
    system_toolchain = effective == ""
    paper_exact_toolchain = effective == PAPER_EXACT_RUST_TOOLCHAIN
    accepted_non_paper_toolchain = bool(effective) and not paper_exact_toolchain
    return {
        "repo_toolchain": repo_toolchain,
        "requested_toolchain": raw_value,
        "requested_source": source,
        "effective_toolchain": effective,
        "rustup_arg": f"+{effective}" if effective else None,
        "uses_repo_rust_toolchain": bool(repo_toolchain) and effective == repo_toolchain,
        "uses_system_toolchain": system_toolchain,
        "paper_exact_rust_toolchain": PAPER_EXACT_RUST_TOOLCHAIN,
        "source_accepted_newer_toolchain": accepted_non_paper_toolchain,
        "source_accepted_non_repo_toolchain": explicit_non_repo,
        "paper_exact_toolchain": paper_exact_toolchain,
        "claim_grade_note": (
            None
            if paper_exact_toolchain
            else "accepted current/non-paper Rust toolchain; timing may be valid but is not paper-exact toolchain evidence"
        ),
    }


def sanitized_volume_label(value: str) -> str:
    text = re.sub(r"[^A-Za-z0-9_.-]+", "-", value.strip() or "system").strip("-")
    return text or "system"


def default_target_volume(args: argparse.Namespace) -> str:
    if DEFAULT_TARGET_VOLUME_ENV is not None:
        return DEFAULT_TARGET_VOLUME_ENV
    toolchain = rust_toolchain_provenance(args).get("effective_toolchain") or "system"
    return f"unialloc-collections-target-{sanitized_volume_label(str(toolchain))}"


def docker_base_command(args: argparse.Namespace) -> List[str]:
    return [args.docker_bin or shutil.which("docker") or "docker"]


def docker_run_prefix(
    args: argparse.Namespace,
    *,
    readonly: bool,
    container_name: Optional[str] = None,
    cidfile: Optional[Path] = None,
) -> List[str]:
    mount_mode = "ro" if readonly else "rw"
    target_volume = str(getattr(args, "target_volume", "") or "").strip()
    cargo_target_dir = "/cargo-target" if target_volume else "/tmp/unialloc-target"
    volume_args: List[str] = []
    if target_volume:
        volume_args = ["-v", f"{target_volume}:/cargo-target"]
    identity_args: List[str] = []
    if container_name:
        identity_args.extend(["--name", container_name])
    if cidfile is not None:
        identity_args.extend(["--cidfile", str(cidfile)])
    return [
        *docker_base_command(args),
        "run",
        "--rm",
        *identity_args,
        "--platform",
        args.platform,
        "-v",
        f"{ROOT}:/work:{mount_mode}",
        *volume_args,
        "-w",
        "/work",
        "-e",
        f"CARGO_TARGET_DIR={cargo_target_dir}",
        "-e",
        "GLIBC_TUNABLES=glibc.pthread.rseq=0",
        args.image,
    ]


def docker_container_missing(record: Dict[str, Any]) -> bool:
    if record.get("ok"):
        return False
    output = "\n".join(
        [
            str(record.get("stdout") or ""),
            str(record.get("stderr") or ""),
            str(record.get("stdout_tail") or ""),
            str(record.get("stderr_tail") or ""),
        ]
    ).lower()
    return "no such container" in output or "no such object" in output


def docker_cleanup_record(record: Dict[str, Any]) -> Dict[str, Any]:
    return {
        key: value
        for key, value in record.items()
        if key not in {"stdout", "stderr"}
    }


def docker_interrupt_cleanup_record(record: Dict[str, Any]) -> Dict[str, Any]:
    """Keep interruption diagnostics compact enough for a bounded outer tail."""

    return {
        key: value
        for key, value in record.items()
        if key not in {"stdout", "stderr", "stdout_tail", "stderr_tail"}
    }


def cleanup_timed_out_docker_container(
    args: argparse.Namespace,
    *,
    container_reference: str,
) -> Dict[str, Any]:
    docker = docker_base_command(args)
    stop = run_command(
        [
            *docker,
            "container",
            "stop",
            "-t",
            str(DOCKER_CONTAINER_STOP_GRACE_SECONDS),
            container_reference,
        ],
        timeout=DOCKER_CLEANUP_COMMAND_TIMEOUT_SECONDS,
    )
    kill: Optional[Dict[str, Any]] = None
    if not stop.get("ok") and not docker_container_missing(stop):
        kill = run_command(
            [*docker, "container", "kill", container_reference],
            timeout=DOCKER_CLEANUP_COMMAND_TIMEOUT_SECONDS,
        )
    remove = run_command(
        [*docker, "container", "rm", "-f", container_reference],
        timeout=DOCKER_CLEANUP_COMMAND_TIMEOUT_SECONDS,
    )
    inspect = run_command(
        [*docker, "container", "inspect", container_reference],
        timeout=DOCKER_CLEANUP_COMMAND_TIMEOUT_SECONDS,
    )
    confirmed_absent = docker_container_missing(inspect)
    blockers = []
    if not confirmed_absent:
        blockers.append(
            "Docker invocation timed out and cleanup could not confirm that "
            f"container {container_reference} is absent"
        )
    return {
        "mode": "timeout-graceful-cleanup",
        "container_reference": container_reference,
        "stop": docker_cleanup_record(stop),
        "kill": docker_cleanup_record(kill) if kill is not None else None,
        "remove": docker_cleanup_record(remove),
        "inspect": docker_cleanup_record(inspect),
        "cleanup_confirmed_absent": confirmed_absent,
        "blockers": blockers,
    }


def run_interrupt_cleanup_command(command: List[str]) -> Dict[str, Any]:
    """Run Docker cleanup without allowing a repeated Ctrl-C to abort the sequence."""

    try:
        return run_command(
            command,
            timeout=INTERRUPTED_DOCKER_CLEANUP_COMMAND_TIMEOUT_SECONDS,
        )
    except KeyboardInterrupt as exc:
        nested = getattr(exc, "_unialloc_bounded_child_result", None)
        if isinstance(nested, dict):
            return {
                **nested,
                "ok": False,
                "interrupted_during_interrupt_cleanup": True,
            }
        return {
            "command": command,
            "ok": False,
            "exit_code": 130,
            "interrupted": True,
            "interrupted_during_interrupt_cleanup": True,
            "stdout": "",
            "stderr": "",
            "stdout_tail": "",
            "stderr_tail": "",
        }
    except (OSError, subprocess.SubprocessError) as exc:
        return {
            "command": command,
            "ok": False,
            "exit_code": None,
            "cleanup_command_error": str(exc),
            "stdout": "",
            "stderr": "",
            "stdout_tail": "",
            "stderr_tail": "",
        }


def cleanup_interrupted_docker_container(
    args: argparse.Namespace,
    *,
    container_reference: str,
) -> Dict[str, Any]:
    """Immediately kill/remove an interrupted container and prove it is absent."""

    docker = docker_base_command(args)
    kill = run_interrupt_cleanup_command(
        [*docker, "container", "kill", container_reference]
    )
    remove = run_interrupt_cleanup_command(
        [*docker, "container", "rm", "-f", container_reference]
    )
    inspect = run_interrupt_cleanup_command(
        [*docker, "container", "inspect", container_reference]
    )
    confirmed_absent = docker_container_missing(inspect)
    blockers = []
    if not confirmed_absent:
        blockers.append(
            "Docker invocation was interrupted and cleanup could not confirm that "
            f"container {container_reference} is absent"
        )
    return {
        "mode": "interrupt-force-cleanup",
        "container_reference": container_reference,
        "kill": docker_interrupt_cleanup_record(kill),
        "remove": docker_interrupt_cleanup_record(remove),
        "inspect": docker_interrupt_cleanup_record(inspect),
        "cleanup_confirmed_absent": confirmed_absent,
        "blockers": blockers,
    }


def docker_container_reference(cidfile: Path, container_name: str) -> Tuple[str, Optional[str]]:
    container_id = ""
    try:
        if cidfile.exists():
            container_id = cidfile.read_text(encoding="utf-8", errors="replace").strip()
    except OSError:
        container_id = ""
    if re.fullmatch(r"[0-9A-Fa-f]{64}", container_id):
        return container_id, container_id
    return container_name, None


def docker_run_with_timeout_cleanup(
    args: argparse.Namespace,
    *,
    readonly: bool,
    purpose: str,
    container_command: List[str],
    timeout: int,
) -> Dict[str, Any]:
    container_name = f"unialloc-collections-{purpose}-{uuid.uuid4().hex}"
    with tempfile.TemporaryDirectory(prefix="unialloc-collections-cid-") as raw_tmp:
        cidfile = Path(raw_tmp) / "container.cid"
        command = [
            *docker_run_prefix(
                args,
                readonly=readonly,
                container_name=container_name,
                cidfile=cidfile,
            ),
            *container_command,
        ]
        try:
            record = run_command(command, timeout=timeout)
        except KeyboardInterrupt as exc:
            child_result = getattr(exc, "_unialloc_bounded_child_result", None)
            if not isinstance(child_result, dict):
                child_result = {
                    "command": command,
                    "ok": False,
                    "exit_code": 130,
                    "interrupted": True,
                    "status": "interrupted",
                    "interrupt_signal": "SIGINT",
                    "stdout": "",
                    "stderr": "",
                }
            with DeferredSigint() as deferred_sigint:
                # Container absence is the first invariant after Ctrl-C. Do not
                # spend the outer grace window relaying logs before cleanup.
                reference, container_id = docker_container_reference(
                    cidfile, container_name
                )
                cleanup = cleanup_interrupted_docker_container(
                    args,
                    container_reference=reference,
                )
                relay = relay_interrupted_docker_output(child_result)
                nested_interrupts = int(
                    child_result.get("docker_cli_nested_interrupts") or 0
                ) + deferred_sigint.count
                diagnostic = {
                    "schema_version": 1,
                    "ok": False,
                    "success": False,
                    "source": "paper-collections-docker-driver-interruption",
                    "status": "interrupted",
                    "error": "Docker Collections invocation interrupted",
                    "code": 130,
                    "interrupted": True,
                    "interrupt_signal": "SIGINT",
                    "diagnostic_only": True,
                    "claim_grade": False,
                    "measurement_eligible": False,
                    "import_eligible": False,
                    "sample_persisted": False,
                    "seconds": None,
                    "purpose": purpose,
                    "container_name": container_name,
                    "container_id": container_id,
                    "container_reference": reference,
                    "cidfile_present": cidfile.exists(),
                    "cleanup_confirmed_absent": cleanup.get(
                        "cleanup_confirmed_absent"
                    ),
                    "cleanup_blockers": cleanup.get("blockers", []),
                    "cleanup": cleanup,
                    "docker_cli_returncode_after_cleanup": child_result.get(
                        "child_returncode_after_cleanup"
                    ),
                    "deferred_sigint_count": deferred_sigint.count,
                    "nested_sigint_count": nested_interrupts,
                    **relay,
                }
                try:
                    print(
                        json.dumps(diagnostic, sort_keys=True),
                        file=sys.stderr,
                        flush=True,
                    )
                except (BrokenPipeError, OSError):
                    pass
                setattr(
                    exc,
                    "_unialloc_bounded_child_result",
                    {
                        **child_result,
                        **diagnostic,
                        "stdout": child_result.get("stdout") or "",
                        "stderr": child_result.get("stderr") or "",
                    },
                )
                raise
        record["container_name"] = container_name
        record["cidfile"] = str(cidfile)
        if record.get("timed_out"):
            reference, _container_id = docker_container_reference(cidfile, container_name)
            record["timeout_cleanup"] = cleanup_timed_out_docker_container(
                args,
                container_reference=reference,
            )
        return record


def docker_available(args: argparse.Namespace) -> Dict[str, Any]:
    docker = docker_base_command(args)[0]
    if not shutil.which(docker) and not Path(docker).exists():
        return {"ok": False, "docker": docker, "error": "docker executable is not available"}
    record = run_command([docker, "version", "--format", "{{json .}}"], timeout=min(args.timeout, 30))
    parsed: Any = None
    if record.get("ok"):
        try:
            parsed = json.loads(str(record.get("stdout") or "{}"))
        except json.JSONDecodeError:
            parsed = None
    return {"ok": bool(record.get("ok")), "docker": docker, "version_probe": {k: v for k, v in record.items() if k not in {"stdout", "stderr"}}, "version": parsed}


def container_probe(args: argparse.Namespace) -> Dict[str, Any]:
    toolchain_info = rust_toolchain_provenance(args)
    toolchain = str(toolchain_info.get("effective_toolchain") or "")
    probe_script = " && ".join(
        [
            "set -eu",
            "echo UNAME=$(uname -s)-$(uname -m)",
            "(ldd --version 2>&1 | head -1) || true",
            "command -v python3",
            "python3 --version",
            "command -v cargo",
            "cargo --version",
            f"cargo +{shlex_quote(toolchain)} --version" if toolchain else "cargo --version",
            "command -v cmake",
            "cmake --version | head -1",
            "SCUDO_RUNTIME=$(find /usr/lib -name 'libclang_rt.scudo_standalone-*.so' -print -quit 2>/dev/null || true); "
            "test -z \"$SCUDO_RUNTIME\" || echo SCUDO_RUNTIME=$SCUDO_RUNTIME",
        ]
    )
    # Use a non-login shell: Debian login shells can reset PATH and hide the
    # Rust image's /usr/local/cargo/bin even when Docker image metadata has it.
    record = docker_run_with_timeout_cleanup(
        args,
        readonly=True,
        purpose="probe",
        container_command=["sh", "-c", probe_script],
        timeout=args.timeout,
    )
    stdout = str(record.get("stdout") or "")
    stderr = str(record.get("stderr") or "")
    blockers: List[str] = []
    if not record.get("ok"):
        blockers.append("container toolchain probe failed")
    cleanup = record.get("timeout_cleanup")
    if isinstance(cleanup, dict):
        blockers.extend(str(value) for value in cleanup.get("blockers", []))
    if "GLIBC" not in stdout.upper() and "GNU LIBC" not in stdout.upper():
        blockers.append("container did not report a glibc ldd version")
    for marker in ("python", "cargo", "cmake"):
        if marker not in stdout.lower():
            blockers.append(f"container probe did not report {marker}")
    if getattr(args, "allocator", None) == "scudo" and "SCUDO_RUNTIME=" not in stdout:
        blockers.append("container probe did not report a Scudo standalone runtime")
    # Do not key readiness off cargo's human version string: `cargo
    # +nightly-2022-07-01 --version` may print a semver/hash without echoing
    # the requested toolchain label.  The chained shell probe already fails if
    # rustup cannot resolve the repo-pinned toolchain.
    return {
        "ok": not blockers,
        "image": args.image,
        "platform": args.platform,
        "rust_toolchain": toolchain,
        "rust_toolchain_provenance": toolchain_info,
        "probe": {k: v for k, v in record.items() if k not in {"stdout", "stderr"}},
        "stdout_tail": stdout[-4000:],
        "stderr_tail": stderr[-4000:],
        "blockers": unique(blockers),
    }


def inner_driver_args(args: argparse.Namespace) -> List[str]:
    inner_timeout = max(1, int(args.inner_timeout or args.timeout))
    cmd = [
        "python3",
        "evaluation/scripts/paper_workload_driver.py",
        "--dataset",
        args.dataset,
        "--benchmark",
        args.benchmark,
        "--allocator",
        args.allocator,
        "--timeout",
        str(inner_timeout),
    ]
    if args.variant_feature:
        cmd.extend(["--variant-feature", args.variant_feature])
    if args.bench_filter:
        cmd.extend(["--bench-filter", args.bench_filter])
    for feature in args.extra_feature:
        cmd.extend(["--extra-feature", feature])
    if args.tcmalloc_lib_dir:
        cmd.extend(["--tcmalloc-lib-dir", args.tcmalloc_lib_dir])
    if args.cmake_bin:
        cmd.extend(["--cmake-bin", args.cmake_bin])
    requested_toolchain = raw_requested_rust_toolchain(args).get("value")
    if requested_toolchain:
        cmd.extend(["--rust-toolchain", str(requested_toolchain)])
    scudo_mode = getattr(args, "scudo_mode", None)
    if scudo_mode:
        cmd.extend(["--scudo-mode", str(scudo_mode)])
    scudo_runtime_library = getattr(args, "scudo_runtime_library", None)
    if scudo_runtime_library:
        cmd.extend(["--scudo-runtime-library", str(scudo_runtime_library)])
    if args.allow_host_allocator_mismatch:
        cmd.append("--allow-host-allocator-mismatch")
    if args.dry_run:
        cmd.append("--dry-run")
    return cmd


def last_json_object(text: str) -> Optional[Dict[str, Any]]:
    for raw in reversed(text.splitlines()):
        line = raw.strip()
        if not line.startswith("{") or not line.endswith("}"):
            continue
        try:
            value = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(value, dict):
            return value
    return None


def run_inner_driver(args: argparse.Namespace) -> Dict[str, Any]:
    inner_timeout = max(1, int(args.inner_timeout or args.timeout))
    docker_timeout = max(
        max(1, int(args.timeout)),
        inner_timeout + TIMEOUT_REPORT_GRACE_SECONDS,
    )
    inner = inner_driver_args(args)
    record = docker_run_with_timeout_cleanup(
        args,
        readonly=not bool(args.writable_mount),
        purpose="run",
        # Make the inner driver the container's direct process so Docker can
        # proxy SIGINT instead of interposing a shell that may swallow it.
        container_command=inner,
        timeout=docker_timeout,
    )
    command = record.get("command") if isinstance(record.get("command"), list) else []
    parsed = last_json_object(str(record.get("stdout") or "")) or last_json_object(str(record.get("stderr") or ""))
    timed_out = bool(record.get("timed_out"))
    returncode = 124 if timed_out else record.get("exit_code")
    if timed_out and not isinstance(parsed, dict):
        parsed = {
            "ok": False,
            "source": "paper-workload-driver",
            "error": "Docker invocation timed out before the inner Collections driver reported",
            "code": 124,
            "timeout_seconds": docker_timeout,
        }
    cleanup = record.get("timeout_cleanup")
    if timed_out and isinstance(parsed, dict) and isinstance(cleanup, dict):
        cleanup_blockers = [str(value) for value in cleanup.get("blockers", [])]
        if cleanup_blockers:
            parsed["docker_cleanup_blockers"] = cleanup_blockers
    return {
        "ok": bool(record.get("ok")) and isinstance(parsed, dict) and bool(parsed.get("ok", True)),
        "inner_command": inner,
        "docker_command": command,
        "returncode": returncode,
        "timed_out": timed_out,
        "inner_timeout_seconds": inner_timeout,
        "docker_timeout_seconds": docker_timeout,
        "docker_timeout_cleanup": cleanup,
        "duration_seconds": record.get("duration_seconds"),
        "stdout_tail": str(record.get("stdout") or "")[-4000:],
        "stderr_tail": str(record.get("stderr") or "")[-4000:],
        "record": parsed,
    }


def shlex_quote(value: str) -> str:
    if re.fullmatch(r"[A-Za-z0-9_@%+=:,./-]+", value):
        return value
    return "'" + value.replace("'", "'\\''") + "'"


def run(args: argparse.Namespace) -> int:
    if args.benchmark != "Collections":
        return json_error("Docker driver only supports the paper Collections row", dataset=args.dataset, benchmark=args.benchmark, allocator=args.allocator)
    docker = docker_available(args)
    if not docker.get("ok"):
        return json_error("Docker is not available", docker_probe=docker)
    probe = container_probe(args)
    if args.probe_only:
        print(json.dumps({"ok": bool(probe.get("ok")), "source": "paper-collections-docker-driver", "docker": docker, "container_probe": probe}, sort_keys=True))
        return 0 if probe.get("ok") else 2
    if not probe.get("ok"):
        return json_error("Docker runner image is not ready for claim-grade Collections execution", docker=docker, container_probe=probe)
    inner = run_inner_driver(args)
    record = inner.get("record") if isinstance(inner.get("record"), dict) else {}
    output: Dict[str, Any] = {
        **record,
        "ok": bool(inner.get("ok")),
        "source": "paper-collections-docker-driver",
        "docker_host": docker,
        "container_probe": probe,
        "container_image": args.image,
        "container_platform": args.platform,
        "rust_toolchain_provenance": rust_toolchain_provenance(args),
        "inner_driver": {k: v for k, v in inner.items() if k != "record"},
        "docker_claim_scope": "linux-glibc-container-collections",
    }
    if args.run_index is not None:
        output["run_index"] = args.run_index
    print(json.dumps(output, sort_keys=True))
    return 0 if inner.get("ok") else int(inner.get("returncode") or 1)


def parse_args(argv: Optional[List[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dataset", required=True)
    parser.add_argument("--benchmark", required=True)
    parser.add_argument("--allocator", required=True)
    parser.add_argument("--variant-feature")
    parser.add_argument("--run-index", type=int)
    parser.add_argument("--image", default=DEFAULT_IMAGE)
    parser.add_argument("--platform", default=DEFAULT_PLATFORM)
    parser.add_argument("--docker-bin")
    parser.add_argument(
        "--target-volume",
        default=DEFAULT_TARGET_VOLUME_ENV,
        help=(
            "Named Docker volume mounted at /cargo-target for CARGO_TARGET_DIR. "
            "Pass an empty string or set UNIALLOC_COLLECTIONS_DOCKER_TARGET_VOLUME= "
            "to fall back to /tmp/unialloc-target. When unset, the driver names "
            "the volume after the effective Rust toolchain to avoid mixing old "
            "nightly and accepted-newer build artifacts."
        ),
    )
    parser.add_argument("--timeout", type=int, default=1800)
    parser.add_argument("--inner-timeout", type=int)
    parser.add_argument("--bench-filter")
    parser.add_argument("--extra-feature", action="append", default=[])
    parser.add_argument("--tcmalloc-lib-dir")
    parser.add_argument("--cmake-bin")
    parser.add_argument(
        "--rust-toolchain",
        default=os.environ.get(RUST_TOOLCHAIN_ENV),
        help=(
            "Forwarded Rustup toolchain override. Defaults to the repo rust-toolchain "
            "file; use 'latest'/'nightly', 'stable', or 'system'/'none'. Non-repo "
            "selections are accepted-newer/non-paper-exact evidence."
        ),
    )
    parser.add_argument(
        "--scudo-mode",
        choices=("auto", "rust-sanitizer", "ld-preload"),
        default=os.environ.get("UNIALLOC_SCUDO_MODE", "auto"),
        help="Forwarded to paper_workload_driver.py for bench_scudo runtime selection.",
    )
    parser.add_argument(
        "--scudo-runtime-library",
        default=os.environ.get("UNIALLOC_SCUDO_RUNTIME_LIBRARY")
        or os.environ.get("SCUDO_RUNTIME_LIBRARY")
        or os.environ.get("SCUDO_STANDALONE_LIBRARY"),
        help="Forwarded Scudo standalone runtime path/directory for --scudo-mode ld-preload.",
    )
    parser.add_argument("--allow-host-allocator-mismatch", action="store_true")
    parser.add_argument(
        "--writable-mount",
        action="store_true",
        help=(
            "Mount the repository read-write in the container. The default is a "
            "read-only repo mount with CARGO_TARGET_DIR redirected to /tmp."
        ),
    )
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--probe-only", action="store_true")
    args = parser.parse_args(argv)
    if args.target_volume is None:
        args.target_volume = default_target_volume(args)
    return args


if __name__ == "__main__":
    raise SystemExit(run(parse_args()))
