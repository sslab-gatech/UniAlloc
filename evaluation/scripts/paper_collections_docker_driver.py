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
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional

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


def json_error(message: str, *, code: int = 2, **extra: Any) -> int:
    print(json.dumps({"ok": False, "source": "paper-collections-docker-driver", "error": message, **extra}, sort_keys=True), file=sys.stderr)
    return code


def subprocess_text(value: Any) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    return ""


def run_command(command: List[str], *, timeout: int, cwd: Path = ROOT) -> Dict[str, Any]:
    started = time.time()
    try:
        proc = subprocess.run(
            command,
            cwd=str(cwd),
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=timeout,
        )
        return {
            "command": command,
            "ok": proc.returncode == 0,
            "exit_code": proc.returncode,
            "duration_seconds": round(time.time() - started, 3),
            "stdout": proc.stdout,
            "stderr": proc.stderr,
            "stdout_tail": proc.stdout[-4000:],
            "stderr_tail": proc.stderr[-4000:],
        }
    except subprocess.TimeoutExpired as exc:
        return {
            "command": command,
            "ok": False,
            "exit_code": None,
            "timed_out": True,
            "duration_seconds": round(time.time() - started, 3),
            "stdout": subprocess_text(exc.stdout),
            "stderr": subprocess_text(exc.stderr),
            "stdout_tail": subprocess_text(exc.stdout)[-4000:],
            "stderr_tail": subprocess_text(exc.stderr)[-4000:],
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


def docker_run_prefix(args: argparse.Namespace, *, readonly: bool) -> List[str]:
    mount_mode = "ro" if readonly else "rw"
    target_volume = str(getattr(args, "target_volume", "") or "").strip()
    cargo_target_dir = "/cargo-target" if target_volume else "/tmp/unialloc-target"
    volume_args: List[str] = []
    if target_volume:
        volume_args = ["-v", f"{target_volume}:/cargo-target"]
    return [
        *docker_base_command(args),
        "run",
        "--rm",
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
    command = [*docker_run_prefix(args, readonly=True), "sh", "-c", probe_script]
    record = run_command(command, timeout=args.timeout)
    stdout = str(record.get("stdout") or "")
    stderr = str(record.get("stderr") or "")
    blockers: List[str] = []
    if not record.get("ok"):
        blockers.append("container toolchain probe failed")
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
        str(args.inner_timeout or args.timeout),
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
    inner = inner_driver_args(args)
    quoted_inner = " ".join(shlex_quote(part) for part in inner)
    command = [*docker_run_prefix(args, readonly=not bool(args.writable_mount)), "sh", "-c", quoted_inner]
    record = run_command(command, timeout=args.timeout)
    parsed = last_json_object(str(record.get("stdout") or "")) or last_json_object(str(record.get("stderr") or ""))
    return {
        "ok": bool(record.get("ok")) and isinstance(parsed, dict) and bool(parsed.get("ok", True)),
        "inner_command": inner,
        "docker_command": command,
        "returncode": record.get("exit_code"),
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
