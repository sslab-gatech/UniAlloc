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
import hashlib
import importlib.util
import json
import os
import platform
import re
import subprocess
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

try:
    import tomllib
except ModuleNotFoundError:  # pragma: no cover - Python <3.11 fail-closed path.
    tomllib = None  # type: ignore[assignment]


ALLOCATOR_FEATURES = {
    "default": "bench_ourself",
    "ourself": "bench_ourself",
    "unialloc": "bench_ourself",
    "jemalloc": "bench_jemalloc",
    "ptmalloc": "bench_ptmalloc",
    "mimalloc": "bench_mimalloc",
    "tcmalloc": "bench_tcmalloc",
    "gperftools-legacy": "bench_gperftools_legacy",
    "snmalloc": "bench_snmalloc",
    "scudo": "bench_scudo",
}

RREDIS_CARGO_OVERLAY_MARKER = "# UniAlloc evaluation allocator feature overlay."
RREDIS_MAIN_OVERLAY_MARKER = "// UniAlloc evaluation allocator feature overlay."
RREDIS_MAIN_OVERLAY_END_MARKER = "// End UniAlloc evaluation allocator feature overlay."
RREDIS_SCUDO_GUARD_MARKER = "// UniAlloc evaluation verified Scudo runtime guard."
SCUDO_CONFIGURATION_ERROR = 78
SCUDO_RUNNER_ATTESTATION_OPTION = "--scudo-runner-attestation-json"
GOOGLE_TCMALLOC_PREFIX_ENV = "UNIALLOC_GOOGLE_TCMALLOC_PREFIX"


def _load_google_tcmalloc_support() -> tuple[Optional[Any], Optional[str]]:
    try:
        import google_tcmalloc_support as support

        return support, None
    except Exception as direct_exc:
        support_path = Path(__file__).resolve().with_name("google_tcmalloc_support.py")
        try:
            spec = importlib.util.spec_from_file_location(
                "_unialloc_rredis_google_tcmalloc_support",
                support_path,
            )
            if spec is None or spec.loader is None:
                raise ImportError(f"could not create module spec for {support_path}")
            support = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(support)
            return support, None
        except Exception as fallback_exc:
            return None, f"direct import failed: {direct_exc}; local import failed: {fallback_exc}"


_GOOGLE_TCMALLOC, _GOOGLE_TCMALLOC_IMPORT_ERROR = _load_google_tcmalloc_support()


def _load_paper_workload_driver() -> tuple[Optional[Any], Optional[str]]:
    """Load the canonical Scudo probe without relying on the caller's cwd/sys.path."""

    try:
        import paper_workload_driver as helper

        return helper, None
    except Exception as direct_exc:
        helper_path = Path(__file__).resolve().with_name("paper_workload_driver.py")
        try:
            spec = importlib.util.spec_from_file_location(
                "_unialloc_rredis_runner_paper_workload_driver",
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
    normalized = normalize_allocator_name(allocator)
    if normalized == "bench-scudo":
        return "bench_scudo"
    return ALLOCATOR_FEATURES.get(normalized, "")


def scudo_allocator_requested(allocator: str) -> bool:
    return allocator_feature(allocator) == "bench_scudo"


def google_tcmalloc_allocator_requested(allocator: str) -> bool:
    return allocator_feature(allocator) == "bench_tcmalloc"


def google_tcmalloc_preflight() -> Dict[str, Any]:
    if _GOOGLE_TCMALLOC is None:
        raise RuntimeError(
            f"google/tcmalloc support is unavailable: {_GOOGLE_TCMALLOC_IMPORT_ERROR}"
        )
    repo_root = repo_root_from_runner()
    configured = str(os.environ.get(GOOGLE_TCMALLOC_PREFIX_ENV) or "").strip()
    default_prefix = repo_root / "evaluation" / "deps" / "tcmalloc"
    if configured:
        prefix = Path(configured).expanduser()
        source = GOOGLE_TCMALLOC_PREFIX_ENV
    elif default_prefix.exists():
        prefix = default_prefix
        source = "canonical_repo_prefix"
    else:
        raise RuntimeError(
            "modern google/tcmalloc requires UNIALLOC_GOOGLE_TCMALLOC_PREFIX; "
            "build it with evaluation/scripts/build_google_tcmalloc.py"
        )
    prefix = prefix.resolve(strict=True)
    identity = dict(_GOOGLE_TCMALLOC.validate_library_dir(prefix / "lib"))
    return {
        "schema_version": 1,
        "source": "paper-external-rredis-runner-google-tcmalloc-preflight",
        "ok": True,
        "configured_input_source": source,
        "configured_prefix": str(prefix),
        "configured_library_dir": str((prefix / "lib").resolve(strict=True)),
        "identity": identity,
        "runtime_identity_marker": _GOOGLE_TCMALLOC.RUNTIME_IDENTITY_MARKER,
    }


def delegated_server_command(wrapper_args: List[str]) -> List[str]:
    try:
        separator = wrapper_args.index("--")
    except ValueError:
        return []
    return list(wrapper_args[separator + 1 :])


def scudo_execution_preflight(
    wrapper_args: List[str],
    *,
    real_workload_dir: Path,
) -> Dict[str, Any]:
    """Select a canonical route before any delegated server timing can start."""

    if _PAPER_WORKLOAD_DRIVER is None:
        return {
            "requested": True,
            "ok": False,
            "error": f"canonical Scudo workload driver is unavailable: {_PAPER_WORKLOAD_DRIVER_IMPORT_ERROR}",
            "execution": None,
        }
    raw_args = argparse.Namespace(
        scudo_mode=selected_option_value(wrapper_args, "--scudo-mode")
        or os.environ.get("UNIALLOC_SCUDO_MODE", "auto"),
        scudo_runtime_library=selected_option_value(wrapper_args, "--scudo-runtime-library")
        or os.environ.get("UNIALLOC_SCUDO_RUNTIME_LIBRARY")
        or os.environ.get("SCUDO_RUNTIME_LIBRARY")
        or os.environ.get("SCUDO_STANDALONE_LIBRARY"),
        rust_toolchain=selected_option_value(wrapper_args, "--rust-toolchain")
        or os.environ.get("UNIALLOC_RUST_TOOLCHAIN"),
    )
    probe_args = _PAPER_WORKLOAD_DRIVER.scudo_child_probe_args(
        raw_args,
        command=delegated_server_command(wrapper_args),
        cwd=real_workload_dir,
    )
    try:
        execution = _PAPER_WORKLOAD_DRIVER.scudo_execution_probe(probe_args)
    except Exception as exc:
        return {
            "requested": True,
            "ok": False,
            "error": f"canonical Scudo execution probe failed: {type(exc).__name__}: {exc}",
            "execution": None,
        }
    if not execution.get("ok"):
        blockers = "; ".join(str(item) for item in execution.get("blockers", []) if str(item).strip())
        return {
            "requested": True,
            "ok": False,
            "error": blockers or "no canonical Scudo execution route is available",
            "execution": execution,
        }
    planned = _PAPER_WORKLOAD_DRIVER.scudo_execution_runtime_record(
        execution,
        runtime_verified=False,
        verification_status="planned",
    )
    return {"requested": True, "ok": True, "execution": planned}


def canonical_scudo_wrapper(redis_wrapper: Path) -> bool:
    scripts_dir = Path(__file__).resolve().parent
    allowed = {
        (scripts_dir / "paper_external_redis_load_json.py").resolve(),
        (scripts_dir / "paper_external_redis_benchmark_json.py").resolve(),
    }
    return redis_wrapper.resolve() in allowed


def prepend_env_path(env: Dict[str, str], key: str, values: List[Path]) -> None:
    existing = env.get(key, "")
    prefixes = [str(path) for path in values if path.exists()]
    if not prefixes:
        return
    env[key] = os.pathsep.join(prefixes + ([existing] if existing else []))


def external_workload_env(
    scudo_preflight: Optional[Dict[str, Any]] = None,
    *,
    allocator: str = "",
    google_tcmalloc: Optional[Dict[str, Any]] = None,
) -> Dict[str, str]:
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
    if allocator_feature(allocator) == "bench_gperftools_legacy":
        prepend_env_path(env, "LIBRARY_PATH", candidate_libs)
        prepend_env_path(env, "DYLD_FALLBACK_LIBRARY_PATH", candidate_libs)
        include_flags = " ".join(f"-I{path}" for path in candidate_includes if path.exists())
        library_flags = " ".join(f"-L{path}" for path in candidate_libs if path.exists())
        if include_flags:
            env["CFLAGS"] = (include_flags + " " + env.get("CFLAGS", "")).strip()
            env["CXXFLAGS"] = (include_flags + " " + env.get("CXXFLAGS", "")).strip()
        if library_flags:
            env["LDFLAGS"] = (library_flags + " " + env.get("LDFLAGS", "")).strip()
    if allocator_feature(allocator) == "bench_tcmalloc":
        if not isinstance(google_tcmalloc, dict) or google_tcmalloc.get("ok") is not True:
            raise RuntimeError("modern google/tcmalloc environment requires an authenticated artifact")
        lib_dir = Path(str(google_tcmalloc["configured_library_dir"]))
        prefix = Path(str(google_tcmalloc["configured_prefix"]))
        prepend_env_path(env, "LIBRARY_PATH", [lib_dir])
        prepend_env_path(env, "LD_LIBRARY_PATH", [lib_dir])
        env[GOOGLE_TCMALLOC_PREFIX_ENV] = str(prefix)
    execution = scudo_preflight.get("execution") if isinstance(scudo_preflight, dict) else None
    if isinstance(execution, dict) and scudo_preflight.get("ok"):
        mode = str(execution.get("selected_mode") or "")
        env["UNIALLOC_SCUDO_MODE"] = mode
        runtime = str(execution.get("runtime_library") or "").strip()
        if runtime:
            env["UNIALLOC_SCUDO_RUNTIME_LIBRARY"] = runtime
        toolchain_probe = execution.get("toolchain_probe")
        provenance = (
            toolchain_probe.get("rust_toolchain_provenance")
            if isinstance(toolchain_probe, dict)
            else None
        )
        if isinstance(provenance, dict):
            effective = str(provenance.get("effective_toolchain") or "").strip()
            if provenance.get("uses_system_toolchain") is True:
                env.pop("UNIALLOC_RUST_TOOLCHAIN", None)
                env.pop("RUSTUP_TOOLCHAIN", None)
            elif effective:
                env["UNIALLOC_RUST_TOOLCHAIN"] = effective
                env["RUSTUP_TOOLCHAIN"] = effective
        # The wrapper applies LD_PRELOAD/RUSTFLAGS to the server child only.
        # Preloading the Python orchestration process would broaden the timed
        # allocator route and still would not prove the server's identity.
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

[dependencies.gperftools-tcmalloc]
package = "tcmalloc"
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
bench_tcmalloc = []
bench_gperftools_legacy = ["gperftools-tcmalloc"]
bench_snmalloc = ["snmalloc-rs"]
bench_scudo = []
"""
    text = cargo_toml.read_text(encoding="utf-8")
    if RREDIS_CARGO_OVERLAY_MARKER in text:
        next_text = text
        legacy_tcmalloc_block = (
            '[dependencies.tcmalloc]\nversion = "0.3.0"\noptional = true\n'
        )
        modern_legacy_block = (
            '[dependencies.gperftools-tcmalloc]\npackage = "tcmalloc"\n'
            'version = "0.3.0"\noptional = true\n'
        )
        if legacy_tcmalloc_block in next_text:
            next_text = next_text.replace(legacy_tcmalloc_block, modern_legacy_block, 1)
        elif "[dependencies.gperftools-tcmalloc]" not in next_text:
            next_text = next_text.rstrip() + "\n\n" + modern_legacy_block
        next_text = re.sub(
            r'(?m)^\s*bench_tcmalloc\s*=.*$',
            'bench_tcmalloc = []',
            next_text,
            count=1,
        )
        if not re.search(r"(?m)^\s*bench_gperftools_legacy\s*=", next_text):
            features = section_bounds(next_text, "[features]")
            if features is None:
                next_text = next_text.rstrip() + '\n\n[features]\nbench_gperftools_legacy = ["gperftools-tcmalloc"]\n'
            else:
                insert_at = features[0] + len("[features]")
                next_text = (
                    next_text[:insert_at]
                    + '\nbench_gperftools_legacy = ["gperftools-tcmalloc"]'
                    + next_text[insert_at:]
                )
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


def rredis_scudo_runtime_guard_overlay() -> str:
    """Constructor guard proving System's process-wide ABI resolves to Scudo."""

    return r'''
// UniAlloc evaluation verified Scudo runtime guard.
#[cfg(all(feature = "bench_scudo", not(target_os = "linux")))]
compile_error!("bench_scudo requires Linux and a verifiable Scudo runtime");

#[cfg(all(feature = "bench_scudo", target_os = "linux"))]
mod unialloc_scudo_runtime_guard {
    use std::os::raw::{c_char, c_int, c_void};
    use std::ptr;

    const EXIT_SCUDO_UNVERIFIED: c_int = 86;
    const SUCCESS: &[u8] = b"unialloc: verified Scudo runtime identity\n";
    const FAILURE: &[u8] = b"unialloc: bench_scudo requires a verified Scudo runtime\n";
    const SCUDO_IDENTITY_SYMBOL: &[u8] = b"__scudo_print_stats\0";
    const SCUDO_RUNTIME_LIBRARY_ENV: &[u8] = b"UNIALLOC_SCUDO_RUNTIME_LIBRARY\0";
    const PATH_CAPACITY: usize = 4096;

    #[repr(C)]
    struct DlInfo {
        dli_fname: *const c_char,
        dli_fbase: *mut c_void,
        dli_sname: *const c_char,
        dli_saddr: *mut c_void,
    }

    #[link(name = "dl")]
    extern "C" {
        fn dlsym(handle: *mut c_void, symbol: *const c_char) -> *mut c_void;
        fn dladdr(address: *const c_void, info: *mut DlInfo) -> c_int;
    }

    extern "C" {
        fn write(fd: c_int, buffer: *const c_void, count: usize) -> isize;
        fn _exit(status: c_int) -> !;
        fn getenv(name: *const c_char) -> *mut c_char;
        fn realpath(path: *const c_char, resolved: *mut c_char) -> *mut c_char;
    }

    fn emit(message: &[u8]) {
        unsafe {
            write(2, message.as_ptr() as *const c_void, message.len());
        }
    }

    fn fail_closed() -> ! {
        emit(FAILURE);
        unsafe { _exit(EXIT_SCUDO_UNVERIFIED) }
    }

    unsafe fn symbol_provider(symbol: &[u8]) -> Option<DlInfo> {
        let address = dlsym(ptr::null_mut(), symbol.as_ptr() as *const c_char);
        if address.is_null() {
            return None;
        }
        let mut info = DlInfo {
            dli_fname: ptr::null(),
            dli_fbase: ptr::null_mut(),
            dli_sname: ptr::null(),
            dli_saddr: ptr::null_mut(),
        };
        if dladdr(address as *const c_void, &mut info) == 0 {
            return None;
        }
        Some(info)
    }

    fn same_c_path(left: &[c_char], right: &[c_char]) -> bool {
        for index in 0..std::cmp::min(left.len(), right.len()) {
            if left[index] != right[index] {
                return false;
            }
            if left[index] == 0 {
                return true;
            }
        }
        false
    }

    unsafe fn provider_matches_configured_runtime(provider: &DlInfo) -> bool {
        let configured = getenv(SCUDO_RUNTIME_LIBRARY_ENV.as_ptr() as *const c_char);
        if configured.is_null() || *configured == 0 {
            return true;
        }
        if provider.dli_fname.is_null() {
            return false;
        }
        let mut configured_realpath = [0 as c_char; PATH_CAPACITY];
        let mut provider_realpath = [0 as c_char; PATH_CAPACITY];
        if realpath(configured, configured_realpath.as_mut_ptr()).is_null()
            || realpath(provider.dli_fname, provider_realpath.as_mut_ptr()).is_null()
        {
            return false;
        }
        same_c_path(&configured_realpath, &provider_realpath)
    }

    unsafe fn verify_allocator_symbol(scudo_provider: &DlInfo, symbol: &[u8]) {
        let provider = symbol_provider(symbol).unwrap_or_else(|| fail_closed());
        if provider.dli_fbase != scudo_provider.dli_fbase
            || !provider_matches_configured_runtime(&provider)
        {
            fail_closed();
        }
    }

    extern "C" fn verify_scudo_runtime_identity() {
        unsafe {
            let scudo_provider = symbol_provider(SCUDO_IDENTITY_SYMBOL)
                .unwrap_or_else(|| fail_closed());
            if !provider_matches_configured_runtime(&scudo_provider) {
                fail_closed();
            }
            verify_allocator_symbol(&scudo_provider, b"malloc\0");
            verify_allocator_symbol(&scudo_provider, b"calloc\0");
            verify_allocator_symbol(&scudo_provider, b"realloc\0");
            verify_allocator_symbol(&scudo_provider, b"free\0");
            verify_allocator_symbol(&scudo_provider, b"posix_memalign\0");
        }
        emit(SUCCESS);
    }

    #[used]
    #[link_section = ".init_array"]
static VERIFY_SCUDO_RUNTIME_IDENTITY: extern "C" fn() = verify_scudo_runtime_identity;
}
'''.lstrip()


def crate_root_overlay_insertion_index(text: str) -> int:
    """Return the byte position after leading crate-level inner attributes."""

    offset = 0
    saw_inner_attr = False
    for line in text.splitlines(keepends=True):
        stripped = line.strip()
        if stripped.startswith("#!["):
            offset += len(line)
            saw_inner_attr = True
            continue
        if saw_inner_attr and not stripped:
            offset += len(line)
            continue
        break
    return offset


def scudo_guard_at_crate_root(text: str, guard: Optional[str] = None) -> bool:
    """Require exact active guard bytes at the crate-root insertion point."""

    expected = guard if guard is not None else rredis_scudo_runtime_guard_overlay()
    insert_at = crate_root_overlay_insertion_index(text)
    return text[insert_at : insert_at + len(expected)] == expected


def rredis_allocator_main_overlay() -> str:
    if _GOOGLE_TCMALLOC is None:
        raise RuntimeError(
            f"google/tcmalloc support is unavailable: {_GOOGLE_TCMALLOC_IMPORT_ERROR}"
        )
    google_tcmalloc_adapter = _GOOGLE_TCMALLOC.rust_allocator_adapter_source(
        feature="bench_tcmalloc",
        static_name="RREDIS_TCMALLOC",
        conflicting_features=(
            "bench_ourself",
            "bench_jemalloc",
            "bench_ptmalloc",
            "bench_mimalloc",
            "bench_gperftools_legacy",
            "bench_snmalloc",
            "bench_scudo",
        ),
    ).rstrip()
    return f'''{RREDIS_MAIN_OVERLAY_MARKER}
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

{google_tcmalloc_adapter}

#[cfg(feature = "bench_gperftools_legacy")]
#[global_allocator]
static RREDIS_GPERFTOOLS_LEGACY: gperftools_tcmalloc::TCMalloc = gperftools_tcmalloc::TCMalloc;

#[cfg(feature = "bench_snmalloc")]
#[global_allocator]
static RREDIS_SNMALLOC: snmalloc_rs::SnMalloc = snmalloc_rs::SnMalloc;

#[cfg(feature = "bench_ptmalloc")]
#[global_allocator]
static RREDIS_PTMALLOC_SYSTEM_ALLOCATOR: std::alloc::System = std::alloc::System;

#[cfg(feature = "bench_scudo")]
#[global_allocator]
static RREDIS_SCUDO_SYSTEM_ALLOCATOR: std::alloc::System = std::alloc::System;
{RREDIS_MAIN_OVERLAY_END_MARKER}
'''


def replace_rredis_allocator_main_overlay(text: str, overlay: str) -> tuple[str, Dict[str, Any]]:
    count = text.count(RREDIS_MAIN_OVERLAY_MARKER)
    record: Dict[str, Any] = {"replaced": False, "marker_count": count}
    if count != 1:
        record["error"] = f"expected exactly one RRedis allocator overlay marker, found {count}"
        return text, record
    start = text.find(RREDIS_MAIN_OVERLAY_MARKER)
    end_marker = text.find(RREDIS_MAIN_OVERLAY_END_MARKER, start)
    if end_marker >= 0:
        end = end_marker + len(RREDIS_MAIN_OVERLAY_END_MARKER)
        record["previous_overlay_kind"] = "versioned"
    else:
        legacy_tail = (
            "static RREDIS_SCUDO_SYSTEM_ALLOCATOR: std::alloc::System = "
            "std::alloc::System;"
        )
        tail = text.find(legacy_tail, start)
        if tail < 0:
            record["error"] = "unrecognized RRedis allocator overlay; refusing ambiguous replacement"
            return text, record
        end = tail + len(legacy_tail)
        record["previous_overlay_kind"] = "crates-io-tcmalloc-legacy"
    while end < len(text) and text[end] in "\r\n":
        end += 1
    next_text = text[:start] + overlay + text[end:]
    record["replaced"] = True
    return next_text, record


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
    overlay = rredis_allocator_main_overlay()
    text = main_rs.read_text(encoding="utf-8")
    guard = rredis_scudo_runtime_guard_overlay()
    next_text = text
    stale_guard_occurrences = 0
    if not scudo_guard_at_crate_root(next_text, guard):
        stale_guard_occurrences = next_text.count(guard)
        if stale_guard_occurrences:
            next_text = next_text.replace(guard, "")
        insert_at = crate_root_overlay_insertion_index(next_text)
        next_text = next_text[:insert_at] + guard + "\n" + next_text[insert_at:]
    if not scudo_guard_at_crate_root(next_text, guard):
        record["error"] = "canonical Scudo runtime guard could not be installed at the active crate root"
        return record
    if RREDIS_MAIN_OVERLAY_MARKER in next_text:
        if overlay not in next_text:
            next_text, upgrade = replace_rredis_allocator_main_overlay(next_text, overlay)
            record["overlay_upgrade"] = upgrade
            if not upgrade.get("replaced"):
                # A marker inside a comment/string is an inactive forgery, not
                # an owned overlay range. Retire only that marker token, then
                # install the exact current block through the normal path.
                next_text = next_text.replace(
                    RREDIS_MAIN_OVERLAY_MARKER,
                    "// Inactive UniAlloc allocator overlay marker.",
                )
                record["inactive_overlay_markers_retired"] = upgrade.get("marker_count", 0)
            else:
                if next_text != text:
                    main_rs.write_text(next_text, encoding="utf-8")
                    record["changed"] = True
                record.update(
                    {
                        "prepared": True,
                        "exact_google_tcmalloc_overlay": overlay in next_text,
                        "scudo_runtime_identity_guard": True,
                        "scudo_guard_at_crate_root": True,
                        "stale_scudo_guard_occurrences_removed": stale_guard_occurrences,
                    }
                )
                return record
        else:
            if next_text != text:
                main_rs.write_text(next_text, encoding="utf-8")
                record["changed"] = True
            record.update(
                {
                    "prepared": True,
                    "exact_google_tcmalloc_overlay": True,
                    "scudo_runtime_identity_guard": True,
                    "scudo_guard_at_crate_root": True,
                    "stale_scudo_guard_occurrences_removed": stale_guard_occurrences,
                }
            )
            return record
    anchor = "pub mod release;\n"
    if anchor in next_text:
        next_text = next_text.replace(anchor, anchor + overlay, 1)
    else:
        insert_at = crate_root_overlay_insertion_index(next_text) + len(guard) + 1
        next_text = next_text[:insert_at] + overlay + "\n" + next_text[insert_at:]
    if not scudo_guard_at_crate_root(next_text, guard):
        record["error"] = "allocator overlay insertion displaced the canonical Scudo crate-root guard"
        return record
    main_rs.write_text(next_text, encoding="utf-8")
    record.update(
        {
            "changed": True,
            "prepared": True,
            "scudo_runtime_identity_guard": True,
            "scudo_guard_at_crate_root": True,
            "stale_scudo_guard_occurrences_removed": stale_guard_occurrences,
        }
    )
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
    try:
        cargo_program_separator = server.index("--", 1)
    except ValueError:
        cargo_program_separator = len(server)
    cargo_arguments = server[:cargo_program_separator]
    cargo_subcommand_args = cargo_arguments[1:]
    run_position = 1 if cargo_subcommand_args and cargo_subcommand_args[0].startswith("+") else 0
    if (
        not cargo_arguments
        or Path(cargo_arguments[0]).name != "cargo"
        or run_position >= len(cargo_subcommand_args)
        or cargo_subcommand_args[run_position] != "run"
        or sum(token == "run" for token in cargo_subcommand_args) != 1
    ):
        record["reason"] = "server command is not a cargo run invocation"
        record["server_command"] = server
        return args, record
    if "--features" in cargo_arguments:
        feature_index = cargo_arguments.index("--features")
        if feature_index + 1 >= len(cargo_arguments):
            record["reason"] = "cargo --features is missing its value before the program separator"
            record["server_command"] = server
            return args, record
        existing = [part for part in re.split(r"[,\s]+", server[feature_index + 1]) if part]
        if feature not in existing:
            existing.append(feature)
            server[feature_index + 1] = ",".join(existing)
            record["changed"] = True
    else:
        feature_equals_indexes = [
            index for index, token in enumerate(cargo_arguments) if token.startswith("--features=")
        ]
        if len(feature_equals_indexes) > 1:
            record["reason"] = "cargo run has multiple --features selectors"
            record["server_command"] = server
            return args, record
        if feature_equals_indexes:
            feature_index = feature_equals_indexes[0]
            existing = [
                part
                for part in re.split(r"[,\s]+", server[feature_index].partition("=")[2])
                if part
            ]
            if feature not in existing:
                existing.append(feature)
                server[feature_index] = "--features=" + ",".join(existing)
                record["changed"] = True
        else:
            server[cargo_program_separator:cargo_program_separator] = ["--features", feature]
            record["changed"] = True
    record.update({"routed": True, "server_command": server})
    return args[: separator + 1] + server, record


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _cargo_option_values(arguments: List[str], long_name: str, short_name: Optional[str] = None) -> tuple[List[str], List[str]]:
    values: List[str] = []
    blockers: List[str] = []
    index = 0
    while index < len(arguments):
        token = arguments[index]
        if token == long_name or (short_name and token == short_name):
            if index + 1 >= len(arguments):
                blockers.append(f"missing value for {token}")
                index += 1
                continue
            values.append(arguments[index + 1])
            index += 2
            continue
        if token.startswith(long_name + "="):
            values.append(token.partition("=")[2])
        elif short_name and token.startswith(short_name) and token != short_name:
            values.append(token[len(short_name) :])
        index += 1
    return values, blockers


def scudo_cargo_target_contract(real_workload_dir: Path, server_command: List[str]) -> Dict[str, Any]:
    """Resolve cargo run to the guarded cwd/src/main.rs target or reject it."""

    blockers: List[str] = []
    resolved_dir = real_workload_dir.resolve()
    manifest = (resolved_dir / "Cargo.toml").resolve()
    guarded_entrypoint = (resolved_dir / "src" / "main.rs").resolve()
    if not server_command or Path(server_command[0]).name != "cargo":
        blockers.append("Scudo server command must invoke cargo")
        cargo_arguments: List[str] = []
    else:
        try:
            program_separator = server_command.index("--", 1)
        except ValueError:
            program_separator = len(server_command)
        cargo_arguments = server_command[1:program_separator]

    run_position = 1 if cargo_arguments and cargo_arguments[0].startswith("+") else 0
    if (
        run_position >= len(cargo_arguments)
        or cargo_arguments[run_position] != "run"
        or sum(token == "run" for token in cargo_arguments) != 1
    ):
        blockers.append("Scudo server command must use run as the actual cargo subcommand")

    forbidden_target_options = (
        "--manifest-path",
        "--example",
        "--bench",
        "--test",
        "--lib",
        "--workspace",
        "--all",
        "--all-targets",
        "--bins",
        "--examples",
        "--tests",
        "--benches",
    )
    for token in cargo_arguments:
        option = token.partition("=")[0]
        if option in forbidden_target_options:
            blockers.append(f"Scudo attestation rejects cargo target/manifest selector {option}")

    package_values, package_blockers = _cargo_option_values(cargo_arguments, "--package", "-p")
    bin_values, bin_blockers = _cargo_option_values(cargo_arguments, "--bin")
    blockers.extend(package_blockers)
    blockers.extend(bin_blockers)
    if len(package_values) > 1:
        blockers.append("Scudo cargo run may select at most one root package")
    if len(bin_values) > 1:
        blockers.append("Scudo cargo run may select at most one guarded binary")

    package_name: Optional[str] = None
    default_run: Optional[str] = None
    explicit_bins: List[Dict[str, Any]] = []
    if not manifest.is_file():
        blockers.append(f"missing default cwd manifest: {manifest}")
    elif tomllib is None:
        blockers.append("Python tomllib is required to attest the Scudo cargo target")
    else:
        try:
            with manifest.open("rb") as handle:
                manifest_data = tomllib.load(handle)
        except (OSError, ValueError) as exc:
            blockers.append(f"failed to parse default cwd manifest: {exc}")
            manifest_data = {}
        package = manifest_data.get("package") if isinstance(manifest_data, dict) else None
        package = package if isinstance(package, dict) else {}
        package_name = str(package.get("name") or "").strip() or None
        default_run = str(package.get("default-run") or "").strip() or None
        bins = manifest_data.get("bin") if isinstance(manifest_data, dict) else None
        explicit_bins = [item for item in bins if isinstance(item, dict)] if isinstance(bins, list) else []
        if not package_name:
            blockers.append("default cwd manifest has no root [package].name")

    selected_package = package_values[0] if len(package_values) == 1 else package_name
    if package_name and selected_package != package_name:
        blockers.append(
            f"Scudo cargo run selected package {selected_package!r}; guarded root package is {package_name!r}"
        )
    selected_bin = bin_values[0] if len(bin_values) == 1 else default_run or package_name
    if package_name and selected_bin != package_name:
        blockers.append(
            f"Scudo cargo run selected binary {selected_bin!r}; guarded default binary is {package_name!r}"
        )

    for entry in explicit_bins:
        if str(entry.get("name") or "").strip() != str(selected_bin or ""):
            continue
        raw_path = str(entry.get("path") or "").strip()
        if not raw_path:
            selected_path = guarded_entrypoint if selected_bin == package_name else None
            if selected_path is None:
                blockers.append("alternate explicit [[bin]] entry has no path bound to the guarded entrypoint")
                continue
        else:
            selected_path = (resolved_dir / raw_path).resolve()
        if selected_path != guarded_entrypoint:
            blockers.append(
                f"Scudo cargo run resolves binary {selected_bin!r} to {selected_path}, not guarded {guarded_entrypoint}"
            )

    if not guarded_entrypoint.is_file():
        blockers.append(f"missing guarded default binary entrypoint: {guarded_entrypoint}")
    return {
        "ok": not blockers,
        "manifest_path": str(manifest),
        "manifest_sha256": file_sha256(manifest) if manifest.is_file() else None,
        "package_name": package_name,
        "selected_package": selected_package,
        "selected_binary": selected_bin,
        "guarded_entrypoint": str(guarded_entrypoint),
        "blockers": blockers,
    }


def google_tcmalloc_server_contract(
    real_workload_dir: Path,
    server_command: List[str],
) -> Dict[str, Any]:
    """Recompute source, feature, and artifact identity before Redis timing."""

    blockers: List[str] = []
    target = scudo_cargo_target_contract(real_workload_dir, server_command)
    blockers.extend(str(item) for item in target.get("blockers", []) if str(item).strip())
    try:
        separator = server_command.index("--", 1)
    except ValueError:
        separator = len(server_command)
    cargo_arguments = server_command[:separator]
    features: List[str] = []
    for index, token in enumerate(cargo_arguments):
        if token == "--features" and index + 1 < len(cargo_arguments):
            features.extend(cargo_arguments[index + 1].replace(",", " ").split())
        elif token.startswith("--features="):
            features.extend(token.partition("=")[2].replace(",", " ").split())
    if "bench_tcmalloc" not in features:
        blockers.append("modern google/tcmalloc cargo run does not enable bench_tcmalloc")
    if "bench_gperftools_legacy" in features:
        blockers.append("modern google/tcmalloc cargo run also enables bench_gperftools_legacy")

    main_rs = real_workload_dir.resolve() / "src" / "main.rs"
    overlay = rredis_allocator_main_overlay()
    overlay_sha256 = hashlib.sha256(overlay.encode("utf-8")).hexdigest()
    main_sha256: Optional[str] = None
    exact_overlay = False
    if not main_rs.is_file():
        blockers.append(f"missing RRedis entrypoint: {main_rs}")
    else:
        text = main_rs.read_text(encoding="utf-8")
        main_sha256 = file_sha256(main_rs)
        exact_overlay = text.count(overlay) == 1
        if not exact_overlay:
            blockers.append("RRedis entrypoint lacks the exact revision-bound google/tcmalloc overlay")
    try:
        artifact = google_tcmalloc_preflight()
    except (OSError, RuntimeError, ValueError, json.JSONDecodeError) as exc:
        artifact = {"ok": False, "error": str(exc)}
        blockers.append(f"modern google/tcmalloc artifact authentication failed: {exc}")
    return {
        "schema_version": 1,
        "source": "paper-external-rredis-google-tcmalloc-server-contract",
        "ok": not blockers,
        "allocator_feature": "bench_tcmalloc",
        "cargo_target_contract": target,
        "features": sorted(set(features)),
        "main_rs": str(main_rs),
        "main_rs_sha256": main_sha256,
        "overlay_sha256": overlay_sha256,
        "exact_overlay_verified": exact_overlay,
        "artifact_preflight": artifact,
        "runtime_identity_marker": (
            _GOOGLE_TCMALLOC.RUNTIME_IDENTITY_MARKER if _GOOGLE_TCMALLOC is not None else None
        ),
        "blockers": blockers,
    }


def build_scudo_runner_attestation(
    real_workload_dir: Path,
    wrapper_args: List[str],
    feature_route: Dict[str, Any],
) -> Dict[str, Any]:
    """Bind the wrapper run to the generated guard and routed cargo command."""

    resolved_dir = real_workload_dir.resolve()
    main_rs = (resolved_dir / "src" / "main.rs").resolve()
    blockers: List[str] = []
    try:
        separator = wrapper_args.index("--")
        server_command = [str(item) for item in wrapper_args[separator + 1 :]]
    except ValueError:
        server_command = []
        blockers.append("wrapper args do not contain a server command separator")
    routed_command = feature_route.get("server_command")
    if feature_route.get("routed") is not True:
        blockers.append("Scudo server command was not routed through cargo run")
    if not isinstance(routed_command, list) or server_command != [str(item) for item in routed_command]:
        blockers.append("Scudo routed server command does not match the delegated wrapper command")
    target_contract = scudo_cargo_target_contract(resolved_dir, server_command)
    blockers.extend(str(item) for item in target_contract.get("blockers", []) if str(item).strip())

    try:
        cargo_program_separator = server_command.index("--", 1)
    except ValueError:
        cargo_program_separator = len(server_command)
    cargo_arguments = server_command[:cargo_program_separator]

    feature_values: List[str] = []
    for index, token in enumerate(cargo_arguments):
        if token == "--features" and index + 1 < len(cargo_arguments):
            feature_values.extend(cargo_arguments[index + 1].replace(",", " ").split())
        elif token.startswith("--features="):
            feature_values.extend(token.partition("=")[2].replace(",", " ").split())
    if "bench_scudo" not in feature_values:
        blockers.append("routed cargo run command does not enable bench_scudo")

    expected_guard = rredis_scudo_runtime_guard_overlay()
    expected_guard_sha256 = hashlib.sha256(expected_guard.encode("utf-8")).hexdigest()
    main_rs_sha256: Optional[str] = None
    guard_occurrences = 0
    guard_at_crate_root = False
    guard_offset: Optional[int] = None
    if not main_rs.is_file():
        blockers.append(f"missing attested RRedis entrypoint: {main_rs}")
    else:
        text = main_rs.read_text(encoding="utf-8")
        main_rs_sha256 = file_sha256(main_rs)
        guard_occurrences = text.count(expected_guard)
        guard_offset = crate_root_overlay_insertion_index(text)
        guard_at_crate_root = scudo_guard_at_crate_root(text, expected_guard)
        if not guard_at_crate_root:
            blockers.append("RRedis entrypoint lacks the canonical active Scudo guard at the crate root")

    return {
        "schema_version": 1,
        "source": "paper-external-rredis-runner-scudo-attestation",
        "ok": not blockers,
        "allocator_feature": "bench_scudo",
        "real_workload_dir": str(resolved_dir),
        "main_rs": str(main_rs),
        "main_rs_sha256": main_rs_sha256,
        "guard_block_sha256": expected_guard_sha256,
        "guard_block_occurrences": guard_occurrences,
        "guard_block_at_crate_root": guard_at_crate_root,
        "guard_block_offset": guard_offset,
        "server_command": server_command,
        "server_command_sha256": hashlib.sha256(
            json.dumps(server_command, separators=(",", ":"), ensure_ascii=True).encode("utf-8")
        ).hexdigest(),
        "cargo_run_routed": feature_route.get("routed") is True,
        "bench_scudo_feature_routed": "bench_scudo" in feature_values,
        "cargo_target_contract": target_contract,
        "blockers": blockers,
    }


def inject_scudo_runner_attestation(
    wrapper_args: List[str],
    attestation: Dict[str, Any],
) -> List[str]:
    """Replace any caller value with the runner's canonical attestation JSON."""

    args = list(wrapper_args)
    separator = args.index("--") if "--" in args else len(args)
    prefix = args[:separator]
    suffix = args[separator:]
    cleaned: List[str] = []
    index = 0
    while index < len(prefix):
        token = prefix[index]
        if token == SCUDO_RUNNER_ATTESTATION_OPTION:
            index += 2
            continue
        if token.startswith(SCUDO_RUNNER_ATTESTATION_OPTION + "="):
            index += 1
            continue
        cleaned.append(token)
        index += 1
    encoded = json.dumps(attestation, sort_keys=True, separators=(",", ":"))
    return [*cleaned, SCUDO_RUNNER_ATTESTATION_OPTION, encoded, *suffix]


def verify_scudo_runner_attestation(
    raw_attestation: str,
    *,
    cwd: Path,
    server_command: List[str],
) -> Dict[str, Any]:
    """Recompute every attested input before a wrapper may launch Scudo."""

    blockers: List[str] = []
    try:
        parsed = json.loads(str(raw_attestation or ""))
    except json.JSONDecodeError as exc:
        parsed = None
        blockers.append(f"invalid Scudo runner attestation JSON: {exc}")
    if not isinstance(parsed, dict):
        if not blockers:
            blockers.append("missing Scudo runner attestation JSON object")
        return {
            "schema_version": 1,
            "source": "paper-external-rredis-wrapper-scudo-attestation-verification",
            "ok": False,
            "blockers": blockers,
        }

    resolved_cwd = cwd.resolve()
    main_rs = (resolved_cwd / "src" / "main.rs").resolve()
    if parsed.get("source") != "paper-external-rredis-runner-scudo-attestation":
        blockers.append("Scudo attestation source is not the canonical RRedis runner")
    if parsed.get("ok") is not True:
        blockers.append("Scudo runner attestation was not complete")
    if parsed.get("allocator_feature") != "bench_scudo":
        blockers.append("Scudo runner attestation does not bind allocator_feature=bench_scudo")
    if str(parsed.get("real_workload_dir") or "") != str(resolved_cwd):
        blockers.append("Scudo runner attestation cwd does not match the wrapper cwd")
    if str(parsed.get("main_rs") or "") != str(main_rs):
        blockers.append("Scudo runner attestation does not bind cwd/src/main.rs")
    if parsed.get("server_command") != server_command:
        blockers.append("Scudo runner attestation server command does not match the wrapper command")

    command_sha256 = hashlib.sha256(
        json.dumps(server_command, separators=(",", ":"), ensure_ascii=True).encode("utf-8")
    ).hexdigest()
    if parsed.get("server_command_sha256") != command_sha256:
        blockers.append("Scudo runner attestation server command hash does not match")
    target_contract = scudo_cargo_target_contract(resolved_cwd, server_command)
    blockers.extend(str(item) for item in target_contract.get("blockers", []) if str(item).strip())
    if parsed.get("cargo_target_contract") != target_contract:
        blockers.append("Scudo runner attestation cargo target contract no longer matches")

    try:
        cargo_program_separator = server_command.index("--", 1)
    except ValueError:
        cargo_program_separator = len(server_command)
    cargo_arguments = server_command[:cargo_program_separator]
    feature_values: List[str] = []
    for index, token in enumerate(cargo_arguments):
        if token == "--features" and index + 1 < len(cargo_arguments):
            feature_values.extend(cargo_arguments[index + 1].replace(",", " ").split())
        elif token.startswith("--features="):
            feature_values.extend(token.partition("=")[2].replace(",", " ").split())
    if "bench_scudo" not in feature_values:
        blockers.append("attested cargo run server command does not enable bench_scudo")
    if parsed.get("cargo_run_routed") is not True or parsed.get("bench_scudo_feature_routed") is not True:
        blockers.append("Scudo runner attestation does not record complete cargo feature routing")

    expected_guard = rredis_scudo_runtime_guard_overlay()
    expected_guard_sha256 = hashlib.sha256(expected_guard.encode("utf-8")).hexdigest()
    if parsed.get("guard_block_sha256") != expected_guard_sha256:
        blockers.append("Scudo runner attestation guard block hash is not canonical")
    current_main_sha256: Optional[str] = None
    guard_occurrences = 0
    guard_at_crate_root = False
    guard_offset: Optional[int] = None
    if not main_rs.is_file():
        blockers.append(f"missing attested RRedis entrypoint: {main_rs}")
    else:
        text = main_rs.read_text(encoding="utf-8")
        current_main_sha256 = file_sha256(main_rs)
        if parsed.get("main_rs_sha256") != current_main_sha256:
            blockers.append("Scudo runner attestation main.rs hash no longer matches")
        guard_occurrences = text.count(expected_guard)
        guard_offset = crate_root_overlay_insertion_index(text)
        guard_at_crate_root = scudo_guard_at_crate_root(text, expected_guard)
        if not guard_at_crate_root:
            blockers.append("attested RRedis entrypoint lacks the canonical active crate-root Scudo guard")
        if parsed.get("guard_block_at_crate_root") is not True:
            blockers.append("Scudo runner attestation did not bind an active crate-root guard")
        if parsed.get("guard_block_offset") != guard_offset:
            blockers.append("Scudo runner attestation guard offset no longer matches")
        if parsed.get("guard_block_occurrences") != guard_occurrences:
            blockers.append("Scudo runner attestation guard occurrence count no longer matches")

    return {
        "schema_version": 1,
        "source": "paper-external-rredis-wrapper-scudo-attestation-verification",
        "ok": not blockers,
        "allocator_feature": parsed.get("allocator_feature"),
        "real_workload_dir": str(resolved_cwd),
        "main_rs": str(main_rs),
        "main_rs_sha256": current_main_sha256,
        "guard_block_sha256": expected_guard_sha256,
        "guard_block_occurrences": guard_occurrences,
        "guard_block_at_crate_root": guard_at_crate_root,
        "guard_block_offset": guard_offset,
        "server_command_sha256": command_sha256,
        "cargo_target_contract": target_contract,
        "blockers": blockers,
    }


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
    scudo_preflight: Optional[Dict[str, Any]] = None
    google_preflight: Optional[Dict[str, Any]] = None
    if google_tcmalloc_allocator_requested(allocator) and not known.prepare_only:
        if not canonical_scudo_wrapper(redis_wrapper):
            emit(
                {
                    "schema_version": 1,
                    "source": "paper-external-rredis-runner-google-tcmalloc-preflight",
                    "success": False,
                    "allocator_selector": allocator,
                    "allocator_feature": feature,
                    "error": (
                        "modern google/tcmalloc evaluation requires the canonical Redis load or "
                        "redis-benchmark wrapper so the constructor marker is verified before timing"
                    ),
                }
            )
            return SCUDO_CONFIGURATION_ERROR
        try:
            google_preflight = google_tcmalloc_preflight()
        except (OSError, RuntimeError, ValueError, json.JSONDecodeError) as exc:
            google_preflight = {
                "schema_version": 1,
                "source": "paper-external-rredis-runner-google-tcmalloc-preflight",
                "ok": False,
                "error": str(exc),
            }
        emit(
            {
                **google_preflight,
                "success": google_preflight.get("ok") is True,
                "allocator_selector": allocator,
                "allocator_feature": feature,
                "workload_timing_started": False,
            }
        )
        if google_preflight.get("ok") is not True:
            return SCUDO_CONFIGURATION_ERROR
    if scudo_allocator_requested(allocator) and not known.prepare_only:
        if not canonical_scudo_wrapper(redis_wrapper):
            emit(
                {
                    "schema_version": 1,
                    "source": "paper-external-rredis-runner-scudo-preflight",
                    "success": False,
                    "allocator_selector": allocator,
                    "allocator_feature": feature,
                    "error": (
                        "Scudo evaluation requires the canonical Redis load or redis-benchmark wrapper "
                        "so runtime identity is verified before workload timing"
                    ),
                }
            )
            return SCUDO_CONFIGURATION_ERROR
        scudo_preflight = scudo_execution_preflight(
            wrapper_args,
            real_workload_dir=real_workload_dir,
        )
        emit(
            {
                "schema_version": 1,
                "source": "paper-external-rredis-runner-scudo-preflight",
                "success": bool(scudo_preflight.get("ok")),
                "allocator_selector": allocator,
                "allocator_feature": feature,
                "scudo_execution_probe": scudo_preflight.get("execution"),
                "error": scudo_preflight.get("error"),
                "workload_timing_started": False,
            }
        )
        if not scudo_preflight.get("ok"):
            return SCUDO_CONFIGURATION_ERROR
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

    if scudo_allocator_requested(allocator):
        attestation = build_scudo_runner_attestation(
            real_workload_dir,
            wrapper_args,
            feature_route,
        )
        emit(attestation)
        if not attestation.get("ok"):
            return SCUDO_CONFIGURATION_ERROR
        wrapper_args = inject_scudo_runner_attestation(wrapper_args, attestation)

    command = [sys.executable, str(redis_wrapper), *wrapper_args]
    return subprocess.run(
        command,
        cwd=str(real_workload_dir),
        env=external_workload_env(
            scudo_preflight,
            allocator=allocator,
            google_tcmalloc=google_preflight,
        ),
    ).returncode


if __name__ == "__main__":
    raise SystemExit(main())
