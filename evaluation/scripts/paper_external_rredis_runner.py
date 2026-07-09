#!/usr/bin/env python3
"""Prepare the upstream Rsedis checkout, then run the Redis load JSON bridge.

The paper's RRedis row points at an old upstream Rsedis tree.  That tree gates
its `syslog` dependency to a small set of host triples, so modern Apple Silicon
hosts can fail before the benchmark surface starts.  This runner performs a
minimal, auditable, idempotent checkout preparation step for the active host
triple and then delegates timing to paper_external_redis_load_json.py.

The emitted preparation JSON is not timing evidence; it is provenance for the
local compatibility step needed to make the real upstream server surface build.
"""

from __future__ import annotations

import argparse
import json
import os
import platform
import re
import subprocess
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional


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

RREDIS_CARGO_OVERLAY_MARKER = "# UniAlloc evaluation allocator feature overlay."
RREDIS_MAIN_OVERLAY_MARKER = "// UniAlloc evaluation allocator feature overlay."


def emit(record: Dict[str, Any]) -> None:
    print(json.dumps(record, sort_keys=True), flush=True)


def run_text(command: List[str], cwd: Optional[Path] = None) -> str:
    proc = subprocess.run(
        command,
        cwd=str(cwd) if cwd else None,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    if proc.returncode != 0:
        raise RuntimeError(f"{command!r} failed: {proc.stderr.strip() or proc.stdout.strip()}")
    return proc.stdout


def detect_host_triple(real_workload_dir: Path) -> str:
    for command in (["rustc", "-vV"], ["cargo", "-vV"]):
        try:
            text = run_text(command, cwd=real_workload_dir)
        except Exception:
            continue
        for line in text.splitlines():
            key, _, value = line.partition(":")
            if key.strip() == "host" and value.strip():
                return value.strip()
    machine = platform.machine().lower()
    system = platform.system().lower()
    if system == "darwin":
        arch = "aarch64" if machine in {"arm64", "aarch64"} else "x86_64"
        return f"{arch}-apple-darwin"
    if system == "linux":
        arch = "aarch64" if machine in {"arm64", "aarch64"} else "x86_64"
        return f"{arch}-unknown-linux-gnu"
    return f"{machine or 'unknown'}-{system or 'unknown'}"


def target_dependency_header(host_triple: str) -> str:
    return f"[target.{host_triple}.dependencies]"


def section_bounds(text: str, header: str) -> Optional[tuple[int, int]]:
    start = text.find(header)
    if start < 0:
        return None
    next_section = re.search(r"(?m)^\[", text[start + len(header) :])
    end = len(text) if next_section is None else start + len(header) + next_section.start()
    return start, end


def section_has_dependency(text: str, header: str, dependency_name: str) -> bool:
    bounds = section_bounds(text, header)
    if bounds is None:
        return False
    section = text[bounds[0] : bounds[1]]
    return re.search(rf"(?m)^\s*{re.escape(dependency_name)}\s*=", section) is not None


def ensure_target_dependencies(cargo_toml: Path, host_triple: str, dependencies: Dict[str, str]) -> Dict[str, Any]:
    record: Dict[str, Any] = {
        "schema_version": 1,
        "source": "paper-external-rredis-runner-target-dependency",
        "cargo_toml": str(cargo_toml),
        "host_triple": host_triple,
        "changed": False,
        "prepared": False,
        "dependencies": dependencies,
        "missing_dependencies": [],
    }
    if not cargo_toml.exists():
        record.update({"error": f"missing Cargo.toml: {cargo_toml}"})
        return record

    text = cargo_toml.read_text(encoding="utf-8")
    header = target_dependency_header(host_triple)
    record["target_dependency_header"] = header
    missing = [
        name
        for name in dependencies
        if not section_has_dependency(text, header, name)
    ]
    record["missing_dependencies"] = missing
    if not missing:
        record["prepared"] = True
        return record

    lines = [f'{name} = "{dependencies[name]}"' for name in missing]
    bounds = section_bounds(text, header)
    if bounds is None:
        addition = (
            "\n"
            "# Added by UniAlloc evaluation runner for host-local RRedis compatibility.\n"
            f"{header}\n"
            + "\n".join(lines)
            + "\n"
        )
        next_text = text.rstrip() + addition
    else:
        insert_at = bounds[0] + len(header)
        next_text = text[:insert_at] + "\n" + "\n".join(lines) + text[insert_at:]
    cargo_toml.write_text(next_text, encoding="utf-8")
    record["changed"] = True
    record["prepared"] = True
    return record


def repo_root_from_runner() -> Path:
    return Path(__file__).resolve().parents[2]


def selected_option_value(argv: List[str], option: str) -> str:
    prefix = option + "="
    for index, token in enumerate(argv):
        if token == option and index + 1 < len(argv):
            return str(argv[index + 1]).strip()
        if str(token).startswith(prefix):
            return str(token)[len(prefix):].strip()
    return ""


def normalize_allocator_name(raw: str) -> str:
    return str(raw or "").strip().lower().replace("_", "-")


def selected_allocator(wrapper_args: List[str]) -> str:
    from_argv = selected_option_value(wrapper_args, "--allocator")
    if from_argv:
        return normalize_allocator_name(from_argv)
    return normalize_allocator_name(os.environ.get("UNIALLOC_PAPER_ALLOCATOR", ""))


def allocator_feature(allocator: str) -> str:
    return ALLOCATOR_FEATURES.get(normalize_allocator_name(allocator), "")


def prepend_env_path(env: Dict[str, str], key: str, values: List[Path]) -> None:
    existing = env.get(key, "")
    prefixes = [str(path) for path in values if path.exists()]
    if not prefixes:
        return
    env[key] = os.pathsep.join(prefixes + ([existing] if existing else []))


def external_workload_env() -> Dict[str, str]:
    env = os.environ.copy()
    # Old Rsedis builds run through the repo-pinned 2022 nightly.  Without an
    # explicit sparse registry protocol, Cargo can spend minutes updating the
    # legacy git index before the server process even starts, causing the
    # redis-benchmark bridge to fail with a misleading "server not ready"
    # timeout.  Sparse fetches are deterministic dependency preparation, not
    # timing evidence, and keep focused RRedis probes from degenerating into a
    # slow registry-index benchmark.
    env.setdefault("CARGO_REGISTRIES_CRATES_IO_PROTOCOL", "sparse")
    env.setdefault("CARGO_NET_RETRY", "2")
    home = Path.home()
    repo_root = repo_root_from_runner()
    local_gperftools = repo_root / "evaluation" / "external" / "_deps" / "homebrew-cellar" / "gperftools" / "2.18.1"
    candidate_bins = [
        home / "Library" / "Python" / f"{sys.version_info.major}.{sys.version_info.minor}" / "bin",
        home / "Library" / "Python" / "3.9" / "bin",
        Path("/opt/homebrew/opt/cmake/bin"),
    ]
    candidate_libs = [
        local_gperftools / "lib",
        Path("/opt/homebrew/opt/gperftools/lib"),
    ]
    candidate_includes = [
        local_gperftools / "include",
        Path("/opt/homebrew/opt/gperftools/include"),
    ]
    prepend_env_path(env, "PATH", candidate_bins)
    prepend_env_path(env, "LIBRARY_PATH", candidate_libs)
    prepend_env_path(env, "DYLD_FALLBACK_LIBRARY_PATH", candidate_libs)
    include_flags = " ".join(f"-I{path}" for path in candidate_includes if path.exists())
    library_flags = " ".join(f"-L{path}" for path in candidate_libs if path.exists())
    if include_flags:
        env["CFLAGS"] = (include_flags + " " + env.get("CFLAGS", "")).strip()
        env["CXXFLAGS"] = (include_flags + " " + env.get("CXXFLAGS", "")).strip()
    if library_flags:
        env["LDFLAGS"] = (library_flags + " " + env.get("LDFLAGS", "")).strip()
    return env


def ensure_allocator_cargo_overlay(real_workload_dir: Path, repo_root: Path) -> Dict[str, Any]:
    cargo_toml = real_workload_dir / "Cargo.toml"
    record: Dict[str, Any] = {
        "schema_version": 1,
        "source": "paper-external-rredis-runner-allocator-cargo-overlay",
        "cargo_toml": str(cargo_toml),
        "changed": False,
        "prepared": False,
    }
    if not cargo_toml.exists():
        record["error"] = f"missing Cargo.toml: {cargo_toml}"
        return record
    unialloc_path = os.path.relpath(repo_root / "unialloc", real_workload_dir)
    overlay = f"""
{RREDIS_CARGO_OVERLAY_MARKER}
[dependencies.unialloc]
path = "{unialloc_path}"
optional = true

[dependencies.jemallocator]
version = "0.3.2"
optional = true

[dependencies.mimalloc]
version = "0.1.25"
default-features = false
optional = true

[dependencies.tcmalloc]
version = "0.3.0"
optional = true

[dependencies.snmalloc-rs]
version = "=0.2.27"
optional = true

[dependencies.lock_api]
version = "=0.4.3"

[dependencies.scopeguard]
version = "=1.1.0"

[dependencies.num_cpus]
version = "=1.13.0"

[dependencies.log]
version = "=0.4.14"

[dependencies.cmake]
version = "=0.1.49"

[features]
default = []
bench_ourself = ["unialloc"]
bench_jemalloc = ["jemallocator"]
bench_ptmalloc = []
bench_mimalloc = ["mimalloc"]
bench_tcmalloc = ["tcmalloc"]
bench_snmalloc = ["snmalloc-rs"]
bench_scudo = []
"""
    text = cargo_toml.read_text(encoding="utf-8")
    if RREDIS_CARGO_OVERLAY_MARKER in text:
        next_text = text
        snmalloc_header = "[dependencies.snmalloc-rs]"
        snmalloc_bounds = section_bounds(next_text, snmalloc_header)
        if snmalloc_bounds is not None:
            start, end = snmalloc_bounds
            section = next_text[start:end]
            exact_section = re.sub(
                r'(?m)^version\s*=\s*"[^"]+"',
                'version = "=0.2.27"',
                section,
                count=1,
            )
            next_text = next_text[:start] + exact_section + next_text[end:]
        missing_blocks = []
        required_pin_blocks = {
            "lock_api": '[dependencies.lock_api]\nversion = "=0.4.3"\n',
            "scopeguard": '[dependencies.scopeguard]\nversion = "=1.1.0"\n',
            "num_cpus": '[dependencies.num_cpus]\nversion = "=1.13.0"\n',
            "log": '[dependencies.log]\nversion = "=0.4.14"\n',
            "cmake": '[dependencies.cmake]\nversion = "=0.1.49"\n',
        }
        for dependency_name, block in required_pin_blocks.items():
            if f"[dependencies.{dependency_name}]" not in next_text:
                missing_blocks.append(block)
        compat_pin_overlay = "\n# UniAlloc evaluation old-nightly dependency pins.\n" + "\n".join(missing_blocks)
        next_text = next_text.rstrip() + "\n\n" + compat_pin_overlay if missing_blocks else next_text
    else:
        next_text = text.rstrip() + "\n\n" + overlay.lstrip()
    if next_text != text:
        cargo_toml.write_text(next_text, encoding="utf-8")
        record["changed"] = True
    record.update({"prepared": True, "unialloc_path": unialloc_path})
    return record


def ensure_allocator_main_overlay(real_workload_dir: Path) -> Dict[str, Any]:
    main_rs = real_workload_dir / "src" / "main.rs"
    record: Dict[str, Any] = {
        "schema_version": 1,
        "source": "paper-external-rredis-runner-allocator-main-overlay",
        "main_rs": str(main_rs),
        "changed": False,
        "prepared": False,
    }
    if not main_rs.exists():
        record["error"] = f"missing Rsedis main.rs: {main_rs}"
        return record
    overlay = f"""
{RREDIS_MAIN_OVERLAY_MARKER}
#[cfg(feature = "bench_ourself")]
use unialloc::UniAlloc as RRedisUniAlloc;
#[cfg(feature = "bench_ourself")]
#[global_allocator]
static RREDIS_UNIALLOC: RRedisUniAlloc = RRedisUniAlloc;

#[cfg(feature = "bench_jemalloc")]
use jemallocator::Jemalloc as RRedisJemalloc;
#[cfg(feature = "bench_jemalloc")]
#[global_allocator]
static RREDIS_JEMALLOC: RRedisJemalloc = RRedisJemalloc;

#[cfg(feature = "bench_mimalloc")]
#[global_allocator]
static RREDIS_MIMALLOC: mimalloc::MiMalloc = mimalloc::MiMalloc;

#[cfg(feature = "bench_tcmalloc")]
#[global_allocator]
static RREDIS_TCMALLOC: tcmalloc::TCMalloc = tcmalloc::TCMalloc;

#[cfg(feature = "bench_snmalloc")]
#[global_allocator]
static RREDIS_SNMALLOC: snmalloc_rs::SnMalloc = snmalloc_rs::SnMalloc;

#[cfg(any(feature = "bench_ptmalloc", feature = "bench_scudo"))]
#[global_allocator]
static RREDIS_SYSTEM_ALLOCATOR: std::alloc::System = std::alloc::System;
"""
    text = main_rs.read_text(encoding="utf-8")
    if RREDIS_MAIN_OVERLAY_MARKER in text:
        record["prepared"] = True
        return record
    anchor = "pub mod release;\n"
    if anchor in text:
        next_text = text.replace(anchor, anchor + overlay, 1)
    else:
        next_text = overlay + "\n" + text
    main_rs.write_text(next_text, encoding="utf-8")
    record.update({"changed": True, "prepared": True})
    return record


def ensure_allocator_overlay(real_workload_dir: Path) -> Dict[str, Any]:
    repo_root = repo_root_from_runner()
    records = [
        ensure_allocator_cargo_overlay(real_workload_dir, repo_root),
        ensure_allocator_main_overlay(real_workload_dir),
    ]
    failed = [record for record in records if not record.get("prepared")]
    return {
        "schema_version": 1,
        "source": "paper-external-rredis-runner-allocator-overlay",
        "real_workload_dir": str(real_workload_dir),
        "changed": any(record.get("changed") for record in records),
        "prepared": not failed,
        "preparations": records,
        "claim_grade": False,
        "not_claim_grade_reason": (
            "compile-time allocator hook surface only; claim-grade RRedis evidence still "
            "requires per-allocator builds, benchmark parity, repetitions, and raw provenance"
        ),
    }


def inject_cargo_feature(wrapper_args: List[str], feature: str) -> tuple[List[str], Dict[str, Any]]:
    record: Dict[str, Any] = {
        "schema_version": 1,
        "source": "paper-external-rredis-runner-cargo-feature-routing",
        "requested_feature": feature or None,
        "changed": False,
        "routed": False,
    }
    if not feature:
        record["reason"] = "no allocator feature selected"
        return list(wrapper_args), record
    args = list(wrapper_args)
    try:
        separator = args.index("--")
    except ValueError:
        record["reason"] = "wrapper args do not contain server command separator"
        return args, record
    server = args[separator + 1 :]
    if not server or Path(server[0]).name != "cargo" or "run" not in server[:3]:
        record["reason"] = "server command is not a cargo run invocation"
        record["server_command"] = server
        return args, record
    if "--features" in server:
        feature_index = server.index("--features")
        if feature_index + 1 < len(server):
            existing = [part for part in re.split(r"[,\s]+", server[feature_index + 1]) if part]
            if feature not in existing:
                existing.append(feature)
                server[feature_index + 1] = ",".join(existing)
                record["changed"] = True
        else:
            server.append(feature)
            record["changed"] = True
    else:
        try:
            cargo_program_separator = server.index("--", 1)
        except ValueError:
            cargo_program_separator = len(server)
        server[cargo_program_separator:cargo_program_separator] = ["--features", feature]
        record["changed"] = True
    record.update({"routed": True, "server_command": server})
    return args[: separator + 1] + server, record


def prepare_rsedis_host_target_dependencies(real_workload_dir: Path) -> Dict[str, Any]:
    host_triple = detect_host_triple(real_workload_dir)
    targets = [
        (real_workload_dir / "logger" / "Cargo.toml", {"syslog": "2.2.0"}),
        (real_workload_dir / "compat" / "Cargo.toml", {"libc": "0.1.8"}),
        (real_workload_dir / "networking" / "Cargo.toml", {"fork": "0.1", "unix_socket": "0.4.3"}),
    ]
    records = [
        ensure_target_dependencies(cargo_toml, host_triple, dependencies)
        for cargo_toml, dependencies in targets
    ]
    changed = [record for record in records if record.get("changed")]
    failed = [record for record in records if not record.get("prepared")]
    return {
        "schema_version": 1,
        "source": "paper-external-rredis-runner-prepare",
        "real_workload_dir": str(real_workload_dir),
        "host_triple": host_triple,
        "changed": bool(changed),
        "changed_count": len(changed),
        "prepared": not failed,
        "preparations": records,
        "claim_grade": False,
        "not_claim_grade_reason": (
            "checkout compatibility preparation only; timing evidence is emitted "
            "by the delegated Redis load bridge after the real server surface runs"
        ),
    }


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--real-workload-dir", required=True)
    parser.add_argument(
        "--redis-wrapper",
        default=str(Path(__file__).resolve().parent / "paper_external_redis_load_json.py"),
    )
    parser.add_argument("--skip-prepare", action="store_true")
    parser.add_argument("--prepare-only", action="store_true")
    known, wrapper_args = parser.parse_known_args(argv)

    real_workload_dir = Path(known.real_workload_dir).expanduser().resolve()
    redis_wrapper = Path(known.redis_wrapper).expanduser().resolve()
    allocator = selected_allocator(wrapper_args)
    feature = allocator_feature(allocator)
    if not known.skip_prepare:
        try:
            prep = prepare_rsedis_host_target_dependencies(real_workload_dir)
            allocator_overlay = ensure_allocator_overlay(real_workload_dir)
            prep["allocator_selector"] = allocator or None
            prep["allocator_feature"] = feature or None
            prep["allocator_overlay"] = allocator_overlay
            prep["prepared"] = bool(prep.get("prepared")) and bool(allocator_overlay.get("prepared"))
            prep["changed"] = bool(prep.get("changed")) or bool(allocator_overlay.get("changed"))
        except Exception as exc:
            prep = {
                "schema_version": 1,
                "source": "paper-external-rredis-runner-prepare",
                "real_workload_dir": str(real_workload_dir),
                "success": False,
                "error": f"{type(exc).__name__}: {exc}",
            }
        emit(prep)
        if not prep.get("prepared"):
            return 2
    if known.prepare_only:
        return 0
    wrapper_args, feature_route = inject_cargo_feature(wrapper_args, feature)
    emit({
        "schema_version": 1,
        "source": "paper-external-rredis-runner-selector-route",
        "real_workload_dir": str(real_workload_dir),
        "allocator_selector": allocator or None,
        "allocator_feature": feature or None,
        "route": feature_route,
        "claim_grade": False,
        "not_claim_grade_reason": (
            "selector routing/build identity evidence only; delegated Redis load bridge remains "
            "non-claim-grade until full paper methodology is satisfied"
        ),
    })

    command = [sys.executable, str(redis_wrapper), *wrapper_args]
    return subprocess.run(command, cwd=str(real_workload_dir), env=external_workload_env()).returncode


if __name__ == "__main__":
    raise SystemExit(main())
