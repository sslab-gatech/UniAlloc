#!/usr/bin/env python3
"""Build and measure a source-bound real-world Rust allocator matrix.

The performance variants omit statistics.  The separate ``typeiso_coverage``
variant enables UniAlloc statistics and is used only for dynamic coverage
evidence.  All Type Isolation variants use the actual MIR rewrite driver and
fail closed when no semantic rewrite was applied.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import pathlib
import re
import shutil
import signal
import statistics
import subprocess
import sys
import time
from dataclasses import dataclass
from typing import Any, Iterable, Sequence


ROOT = pathlib.Path(__file__).resolve().parents[2]
DEFAULT_RAW_DIR = ROOT / "evaluation" / "raw" / "realworld-type-isolation-matrix"
DEFAULT_CHECKOUT_ROOT = ROOT / "evaluation" / "external" / "_checkouts"
DEFAULT_WRAPPER = DEFAULT_RAW_DIR / "tools" / "unialloc-rustc-wrapper"
PASS_SOURCE = (
    ROOT
    / "tools"
    / "unialloc-rustc-pass"
    / "unialloc-rustc-mir-rewrite-dry-run.rs"
)
STATS_PREFIX = "UNIALLOC_REALWORLD_STATS="
INSTRUMENTATION_MARKER = "// UniAlloc real-world allocator matrix instrumentation."
FORCE_LOAD_MARKER = "// UniAlloc real-world Type Isolation force-load."
FORCE_LOAD_WRAPPER_SOURCE = r'''#!/usr/bin/env python3
"""Selectively force-load UniAlloc before invoking the MIR rewrite driver."""

import os
import pathlib
import sys


def fail(message: str) -> "None":
    print(f"unialloc force-load wrapper: {message}", file=sys.stderr)
    raise SystemExit(2)


def normalized(value: str) -> str:
    return value.strip().replace("-", "_")


def crate_name(arguments: list[str]) -> str:
    for index, value in enumerate(arguments):
        if value == "--crate-name" and index + 1 < len(arguments):
            return normalized(arguments[index + 1])
        if value.startswith("--crate-name="):
            return normalized(value.split("=", 1)[1])
    return ""


def extern_specs(arguments: list[str]) -> list[str]:
    specs: list[str] = []
    for index, value in enumerate(arguments):
        if value == "--extern" and index + 1 < len(arguments):
            specs.append(arguments[index + 1])
        elif value.startswith("--extern="):
            specs.append(value.split("=", 1)[1])
    return specs


arguments = sys.argv[1:]
if not arguments:
    fail("missing Cargo rustc invocation")

driver = pathlib.Path(os.environ.get("UNIALLOC_FORCE_LOAD_DRIVER", ""))
rlib = pathlib.Path(os.environ.get("UNIALLOC_FORCE_LOAD_RLIB", ""))
dependency_dir = pathlib.Path(
    os.environ.get("UNIALLOC_FORCE_LOAD_DEPENDENCY_DIR", "")
)
targets = {
    normalized(value)
    for value in os.environ.get("UNIALLOC_RUSTC_TARGET_CRATES", "").split(",")
    if normalized(value)
}
if not driver.is_file():
    fail(f"missing MIR rewrite driver: {driver}")
if not targets:
    fail("empty selected-crate allowlist")

selected = crate_name(arguments) in targets
if selected:
    if not rlib.is_file() or rlib.suffix != ".rlib":
        fail(f"missing force-load rlib: {rlib}")
    if not dependency_dir.is_dir():
        fail(f"missing force-load dependency directory: {dependency_dir}")
    for spec in extern_specs(arguments):
        name = spec.split("=", 1)[0].split(":")[-1]
        if normalized(name) == "unialloc":
            fail("selected crate already has a UniAlloc --extern entry")
    arguments.extend(
        [
            "-Z",
            "unstable-options",
            "--extern",
            f"force:unialloc={rlib}",
            "-L",
            f"dependency={dependency_dir}",
        ]
    )

os.execv(str(driver), [str(driver), *arguments])
'''
VARIANTS = (
    "native",
    "jemalloc",
    "mimalloc",
    "unialloc",
    "typed_plain",
    "typeiso_perf",
    "typeiso_coverage",
)
TYPEISO_VARIANTS = frozenset(("typed_plain", "typeiso_perf", "typeiso_coverage"))


class MatrixError(RuntimeError):
    """A fail-closed matrix setup, build, or measurement error."""


@dataclass(frozen=True)
class AppSpec:
    name: str
    checkout: str
    head: str
    main_source: str
    binary: str
    target_crates: tuple[str, ...]
    original_allocator: str
    force_load_lock_packages: tuple[str, ...] = ()


APP_SPECS = {
    "ripgrep": AppSpec(
        name="ripgrep",
        checkout="realworld-ripgrep",
        head="4649aa9700619f94cf9c66876e9549d83420e16c",
        main_source="crates/core/main.rs",
        binary="rg",
        target_crates=(
            "rg",
            "globset",
            "grep",
            "grep_cli",
            "grep_matcher",
            "grep_printer",
            "grep_searcher",
            "grep_regex",
            "ignore",
        ),
        original_allocator="system",
    ),
    "fd": AppSpec(
        name="fd",
        checkout="realworld-fd",
        head="b19136871310b01500b4f09eadd7387b8476be47",
        main_source="src/main.rs",
        binary="fd",
        target_crates=("fd", "ignore", "walkdir", "same_file", "globset"),
        original_allocator="jemalloc-0.5.4",
        force_load_lock_packages=(
            "fd-find",
            "ignore",
            "walkdir",
            "same-file",
            "globset",
        ),
    ),
    "oxipng": AppSpec(
        name="oxipng",
        checkout="realworld-oxipng",
        head="dea23211ae6259007e068c59ab16929798d00d96",
        main_source="src/main.rs",
        binary="oxipng",
        target_crates=("oxipng",),
        original_allocator="system",
    ),
}


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_file(path: pathlib.Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def implementation_digest() -> str:
    roots = (ROOT / "unialloc", ROOT / "alloc_macros")
    files = [
        ROOT / "Cargo.toml",
        ROOT / "Cargo.lock",
        PASS_SOURCE,
        pathlib.Path(__file__).resolve(),
    ]
    for source_root in roots:
        files.extend(
            path
            for path in source_root.rglob("*")
            if path.is_file()
            and "target" not in path.parts
            and (path.suffix in {".rs", ".toml"} or path.name == "build.rs")
        )
    digest = hashlib.sha256()
    for path in sorted(set(files)):
        digest.update(path.relative_to(ROOT).as_posix().encode())
        digest.update(b"\0")
        digest.update(path.read_bytes())
    return digest.hexdigest()


def parse_csv(raw: str, allowed: Iterable[str], label: str) -> tuple[str, ...]:
    values = tuple(dict.fromkeys(part.strip() for part in raw.split(",") if part.strip()))
    unknown = sorted(set(values) - set(allowed))
    if not values:
        raise argparse.ArgumentTypeError(f"{label} must contain at least one value")
    if unknown:
        raise argparse.ArgumentTypeError(f"unknown {label}: {', '.join(unknown)}")
    return values


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apps", default=",".join(APP_SPECS))
    parser.add_argument("--variants", default=",".join(VARIANTS))
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--quick", dest="quick", action="store_true")
    mode.add_argument("--full", dest="quick", action="store_false")
    parser.set_defaults(quick=True)
    parser.add_argument("--warmups", type=int)
    parser.add_argument("--repetitions", type=int)
    parser.add_argument("--raw-dir", type=pathlib.Path, default=DEFAULT_RAW_DIR)
    parser.add_argument("--checkout-root", type=pathlib.Path, default=DEFAULT_CHECKOUT_ROOT)
    parser.add_argument("--wrapper", type=pathlib.Path)
    parser.add_argument("--toolchain", default="nightly-2026-06-11")
    parser.add_argument("--time-binary", type=pathlib.Path, default=pathlib.Path("/usr/bin/time"))
    parser.add_argument("--jobs", type=int, default=max(1, os.cpu_count() or 1))
    parser.add_argument("--build-timeout", type=int, default=1800)
    parser.add_argument("--run-timeout", type=int, default=300)
    parser.add_argument("--reuse-binaries", action="store_true")
    parser.add_argument(
        "--disable-glibc-rseq",
        action="store_true",
        help=(
            "set glibc.pthread.rseq=0 for measured processes; the default "
            "keeps libc-managed rseq registration"
        ),
    )
    args = parser.parse_args(argv)
    try:
        args.apps = parse_csv(args.apps, APP_SPECS, "app")
        args.variants = parse_csv(args.variants, VARIANTS, "variant")
    except argparse.ArgumentTypeError as error:
        parser.error(str(error))
    if args.warmups is not None and args.warmups < 0:
        parser.error("--warmups must be non-negative")
    if args.repetitions is not None and args.repetitions < 1:
        parser.error("--repetitions must be positive")
    if args.jobs < 1:
        parser.error("--jobs must be positive")
    return args


def measurement_counts(args: argparse.Namespace) -> tuple[int, int]:
    warmups = args.warmups if args.warmups is not None else (1 if args.quick else 2)
    repetitions = args.repetitions if args.repetitions is not None else (3 if args.quick else 7)
    return warmups, repetitions


def rotated_order(values: tuple[str, ...], offset: int) -> tuple[str, ...]:
    if not values:
        return values
    pivot = offset % len(values)
    return values[pivot:] + values[:pivot]


def execute(
    argv: Sequence[str | os.PathLike[str]],
    *,
    cwd: pathlib.Path,
    env: dict[str, str],
    timeout: int,
) -> dict[str, Any]:
    command = [str(value) for value in argv]
    started = time.perf_counter_ns()
    process = subprocess.Popen(
        command,
        cwd=str(cwd),
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        start_new_session=True,
    )
    timed_out = False
    try:
        stdout, stderr = process.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        timed_out = True
        os.killpg(process.pid, signal.SIGKILL)
        stdout, stderr = process.communicate()
    elapsed = (time.perf_counter_ns() - started) / 1_000_000_000
    return {
        "command": command,
        "exit_code": process.returncode,
        "stdout": stdout,
        "stderr": stderr,
        "wall_seconds": elapsed,
        "timed_out": timed_out,
    }


def git_text(checkout: pathlib.Path, args: Sequence[str]) -> str:
    result = execute(
        ["git", *args], cwd=checkout, env=os.environ.copy(), timeout=60
    )
    if result["exit_code"] != 0:
        raise MatrixError(result["stderr"].decode("utf-8", errors="replace"))
    return result["stdout"].decode("utf-8", errors="replace").strip()


def verify_checkout(path: pathlib.Path, expected_head: str) -> dict[str, str]:
    if not (path / ".git").exists():
        raise MatrixError(f"missing Git checkout: {path}")
    head = git_text(path, ("rev-parse", "HEAD"))
    if head != expected_head:
        raise MatrixError(f"checkout HEAD mismatch for {path}: got {head}, expected {expected_head}")
    status = git_text(path, ("status", "--short"))
    if status:
        raise MatrixError(f"checkout must be clean before copying: {path}\n{status}")
    return {"path": str(path.resolve()), "head": head, "status": status}


def locked_package_names(lock_path: pathlib.Path) -> set[str]:
    if not lock_path.is_file():
        raise MatrixError(f"missing Cargo lockfile: {lock_path}")
    text = lock_path.read_text(encoding="utf-8")
    return set(re.findall(r'(?m)^name\s*=\s*"([^"]+)"\s*$', text))


def validate_force_load_lock_targets(spec: AppSpec, lock_path: pathlib.Path) -> None:
    required = set(spec.force_load_lock_packages)
    if not required:
        return
    missing = sorted(required - locked_package_names(lock_path))
    if missing:
        raise MatrixError(
            f"{spec.name} force-load targets are absent from Cargo.lock: "
            + ",".join(missing)
        )


def copy_checkout(source: pathlib.Path, destination: pathlib.Path) -> None:
    if destination.exists():
        shutil.rmtree(destination)
    shutil.copytree(
        source,
        destination,
        ignore=shutil.ignore_patterns(".git", "target"),
    )


def cargo_path_dependency(path: pathlib.Path, features: Sequence[str] = ()) -> str:
    quoted_path = str(path.resolve()).replace("\\", "\\\\").replace('"', '\\"')
    feature_text = ""
    if features:
        feature_text = ", features = [" + ", ".join(json.dumps(value) for value in features) + "]"
    return f'unialloc = {{ path = "{quoted_path}"{feature_text} }}'


def add_dependency(manifest: pathlib.Path, dependency: str) -> None:
    text = manifest.read_text(encoding="utf-8")
    name = dependency.split("=", 1)[0].strip()
    if re.search(rf"(?m)^{re.escape(name)}\s*=", text):
        return
    match = re.search(r"(?m)^\[dependencies\]\s*$", text)
    if match:
        insert_at = match.end()
        text = text[:insert_at] + "\n" + dependency + text[insert_at:]
    else:
        text = text.rstrip() + "\n\n[dependencies]\n" + dependency + "\n"
    manifest.write_text(text, encoding="utf-8")


def ensure_standalone_workspace(manifest: pathlib.Path) -> None:
    text = manifest.read_text(encoding="utf-8")
    if re.search(r"(?m)^\[workspace\]\s*$", text):
        return
    manifest.write_text(text.rstrip() + "\n\n[workspace]\n", encoding="utf-8")


def section_names(text: str) -> set[str]:
    names: set[str] = set()
    section = ""
    package_name = ""
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith("[") and stripped.endswith("]"):
            section = stripped
            continue
        match = re.match(r'^name\s*=\s*"([^"]+)"', stripped)
        if not match or section not in {"[package]", "[lib]", "[[bin]]"}:
            continue
        normalized = match.group(1).replace("-", "_")
        names.add(normalized)
        if section == "[package]":
            package_name = normalized
    if package_name and ("[lib]" in text or "src/lib.rs" in text):
        names.add(package_name)
    return names


def lib_source_for_manifest(manifest: pathlib.Path) -> pathlib.Path | None:
    text = manifest.read_text(encoding="utf-8")
    section = ""
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith("[") and stripped.endswith("]"):
            section = stripped
            continue
        if section == "[lib]":
            match = re.match(r'^path\s*=\s*"([^"]+)"', stripped)
            if match:
                candidate = manifest.parent / match.group(1)
                return candidate if candidate.exists() else None
    candidate = manifest.parent / "src" / "lib.rs"
    return candidate if candidate.exists() else None


def force_load_unialloc(manifest: pathlib.Path) -> None:
    source = lib_source_for_manifest(manifest)
    if source is None:
        return
    text = source.read_text(encoding="utf-8")
    if FORCE_LOAD_MARKER in text:
        return
    text = text.rstrip() + (
        "\n\n"
        + FORCE_LOAD_MARKER
        + "\n#[allow(unused_extern_crates)]\nextern crate unialloc;\n"
    )
    source.write_text(text, encoding="utf-8")


def inject_unialloc_workspace_crates(
    checkout: pathlib.Path,
    *,
    target_crates: Sequence[str],
    dependency: str,
) -> list[pathlib.Path]:
    targets = {name.replace("-", "_") for name in target_crates}
    patched: list[pathlib.Path] = []
    for manifest in sorted(checkout.rglob("Cargo.toml")):
        if "target" in manifest.parts:
            continue
        names = section_names(manifest.read_text(encoding="utf-8"))
        if not names.intersection(targets):
            continue
        add_dependency(manifest, dependency)
        force_load_unialloc(manifest)
        patched.append(manifest)
    if not patched:
        raise MatrixError(
            "no selected workspace manifest matched Type Isolation target crates: "
            + ",".join(target_crates)
        )
    return patched


def find_cached_spin() -> pathlib.Path:
    roots = sorted((pathlib.Path.home() / ".cargo" / "registry" / "src").glob("*/spin-0.9.0"))
    if not roots:
        raise MatrixError("spin 0.9.0 is absent from the Cargo source cache")
    indexed = [path for path in roots if path.parent.name.startswith("index.crates.io-")]
    return (indexed or roots)[0].resolve()


def add_spin_patch(manifest: pathlib.Path, spin_path: pathlib.Path) -> None:
    text = manifest.read_text(encoding="utf-8")
    if re.search(r"(?m)^spin\s*=\s*\{\s*path\s*=", text):
        return
    dependency = f'spin = {{ path = "{spin_path}" }}'
    match = re.search(r"(?m)^\[patch\.crates-io\]\s*$", text)
    if match:
        insert_at = match.end()
        text = text[:insert_at] + "\n" + dependency + text[insert_at:]
    else:
        text = text.rstrip() + "\n\n[patch.crates-io]\n" + dependency + "\n"
    manifest.write_text(text, encoding="utf-8")


def allocator_source(variant: str) -> str:
    if variant == "jemalloc":
        body = "static ALLOCATOR: jemallocator::Jemalloc = jemallocator::Jemalloc;"
    elif variant == "mimalloc":
        body = "static ALLOCATOR: mimalloc::MiMalloc = mimalloc::MiMalloc;"
    elif variant in {"unialloc", "typed_plain", "typeiso_perf"}:
        body = "static ALLOCATOR: unialloc::UniAlloc = unialloc::UniAlloc;"
    elif variant == "typeiso_coverage":
        return f'''\n\n{INSTRUMENTATION_MARKER}
mod unialloc_realworld_instrumentation {{
    #[global_allocator]
    static ALLOCATOR: unialloc::UniAlloc = unialloc::UniAlloc;

    unsafe extern "C" {{
        fn atexit(callback: extern "C" fn()) -> i32;
    }}

    extern "C" fn report() {{
        let stats = unialloc::semantic_stats_snapshot();
        let fallback = unialloc::semantic_fallback_attribution_snapshot();
        let validation = unialloc::semantic_metadata_validation_snapshot();
        let side_cache = unialloc::type_isolation_side_cache_snapshot();
        eprintln!(
            "{STATS_PREFIX}{{{{\\\"total_allocations\\\":{{}},\\\"typed_allocations\\\":{{}},\\\"fallback_allocations\\\":{{}},\\\"typed_deallocations\\\":{{}},\\\"fallback_deallocations\\\":{{}},\\\"coverage_basis_points\\\":{{}},\\\"raw_alloc_no_metadata\\\":{{}},\\\"raw_dealloc_no_metadata\\\":{{}},\\\"raw_realloc_no_metadata\\\":{{}},\\\"cache_hits\\\":{{}},\\\"cache_inserts\\\":{{}},\\\"cache_bypasses\\\":{{}},\\\"recovery_matches\\\":{{}},\\\"recovery_mismatches\\\":{{}},\\\"type_stats_dropped_events\\\":{{}},\\\"side_cache_entries\\\":{{}},\\\"side_cache_corrupt_slots\\\":{{}}}}}}",
            stats.total_allocations,
            stats.typed_allocations,
            stats.fallback_allocations,
            stats.typed_deallocations,
            stats.fallback_deallocations,
            stats.coverage_basis_points,
            fallback.raw_alloc_no_metadata,
            fallback.raw_dealloc_no_metadata,
            fallback.raw_realloc_no_metadata,
            stats.typed_cache_hits,
            stats.typed_cache_inserts,
            stats.typed_cache_bypasses,
            validation.recovery_identity_matches,
            validation.recovery_identity_mismatches,
            stats.semantic_type_stats_dropped_events,
            side_cache.occupied_entries,
            side_cache.corrupt_slots,
        );
    }}

    extern "C" fn initialize() {{
        unialloc::semantic_stats_reset();
        unialloc::semantic_stats_recording_enable();
        unialloc::semantic_type_stats_recording_enable();
        unsafe {{ atexit(report); }}
    }}

    #[used]
    #[cfg_attr(target_family = "unix", unsafe(link_section = ".init_array"))]
    static INITIALIZER: extern "C" fn() = initialize;
}}
'''
    else:
        raise MatrixError(f"allocator source requested for unsupported variant: {variant}")
    return f'''\n\n{INSTRUMENTATION_MARKER}
mod unialloc_realworld_instrumentation {{
    #[global_allocator]
    {body}
}}
'''


def inject_allocator_main(main_source: pathlib.Path, variant: str) -> None:
    text = main_source.read_text(encoding="utf-8")
    if INSTRUMENTATION_MARKER in text:
        raise MatrixError(f"allocator instrumentation already exists in {main_source}")
    main_source.write_text(text.rstrip() + allocator_source(variant), encoding="utf-8")


def merge_glibc_tunable(current: str) -> str:
    values = [value for value in current.split(":") if value and not value.startswith("glibc.pthread.rseq=")]
    values.append("glibc.pthread.rseq=0")
    return ":".join(values)


def runtime_environment(
    base: dict[str, str], *, disable_glibc_rseq: bool
) -> dict[str, str]:
    env = dict(base)
    if disable_glibc_rseq:
        env["GLIBC_TUNABLES"] = merge_glibc_tunable(env.get("GLIBC_TUNABLES", ""))
    return env


def typeiso_environment(
    base: dict[str, str],
    *,
    wrapper: pathlib.Path,
    audit_dir: pathlib.Path,
    pass_log_dir: pathlib.Path,
    sysroot: pathlib.Path,
    target_crates: Sequence[str],
    policy_flags: int = 1,
) -> dict[str, str]:
    env = dict(base)
    library_dir = str((sysroot / "lib").resolve())
    existing = env.get("LD_LIBRARY_PATH", "")
    env.update(
        {
            "RUSTC_WRAPPER": str(wrapper.resolve()),
            "UNIALLOC_RUSTC_SYSROOT": str(sysroot.resolve()),
            "UNIALLOC_RUSTC_TARGET_CRATES": ",".join(target_crates),
            "UNIALLOC_REWRITE_AUDIT_DIR": str(audit_dir.resolve()),
            "UNIALLOC_PASS_LOG_DIR": str(pass_log_dir.resolve()),
            "UNIALLOC_ACTUAL_MIR_REWRITE": "1",
            "UNIALLOC_ACTUAL_SEMANTIC_SCOPE_REWRITE": "1",
            "UNIALLOC_DIRECT_LOCAL_METADATA_ABI": "1",
            "UNIALLOC_DIRECT_LOCAL_SIZE_ALIGN_WITH_SEMANTIC_DROP": "1",
            "UNIALLOC_CONTINUE_COMPILATION": "1",
            "UNIALLOC_LOWERING_POLICY_FLAGS": str(policy_flags),
            "CARGO_INCREMENTAL": "0",
            "LD_LIBRARY_PATH": library_dir + ((os.pathsep + existing) if existing else ""),
        }
    )
    return env


def force_load_environment(
    base: dict[str, str],
    *,
    driver_wrapper: pathlib.Path,
    force_load: dict[str, Any],
) -> dict[str, str]:
    driver = driver_wrapper.resolve()
    rlib = pathlib.Path(str(force_load.get("rlib", ""))).resolve()
    dependency_dir = pathlib.Path(
        str(force_load.get("dependency_dir", ""))
    ).resolve()
    if not driver.is_file():
        raise MatrixError(f"missing MIR rewrite driver: {driver}")
    if not rlib.is_file() or rlib.suffix != ".rlib":
        raise MatrixError(f"missing force-load rlib: {rlib}")
    if not dependency_dir.is_dir():
        raise MatrixError(f"missing force-load dependency directory: {dependency_dir}")
    env = dict(base)
    env.update(
        {
            "UNIALLOC_FORCE_LOAD_DRIVER": str(driver),
            "UNIALLOC_FORCE_LOAD_RLIB": str(rlib),
            "UNIALLOC_FORCE_LOAD_DEPENDENCY_DIR": str(dependency_dir),
        }
    )
    return env


def workload_compatibility_environment(
    spec: AppSpec, base: dict[str, str]
) -> tuple[dict[str, str], dict[str, Any]]:
    env = dict(base)
    if spec.name != "oxipng":
        return env, {"rustc_bootstrap": None, "rustflags_added": []}
    compatibility_flag = "-Zcrate-attr=feature(custom_inner_attributes)"
    existing = env.get("RUSTFLAGS", "").strip()
    values = existing.split() if existing else []
    added: list[str] = []
    if compatibility_flag not in values:
        values.append(compatibility_flag)
        added.append(compatibility_flag)
    env["RUSTFLAGS"] = " ".join(values)
    env["RUSTC_BOOTSTRAP"] = "1"
    return env, {
        "rustc_bootstrap": "1",
        "rustflags_added": added,
        "reason": "semver-parser-0.10.1-generated-custom-inner-attribute",
    }


def parse_stats_json(stderr: str) -> dict[str, Any] | None:
    parsed: dict[str, Any] | None = None
    for line in stderr.splitlines():
        if not line.startswith(STATS_PREFIX):
            continue
        try:
            candidate = json.loads(line[len(STATS_PREFIX) :])
        except json.JSONDecodeError:
            continue
        if isinstance(candidate, dict):
            parsed = candidate
    return parsed


def summarize_audits(audit_dir: pathlib.Path) -> dict[str, Any]:
    files = sorted(audit_dir.glob("*.json")) if audit_dir.exists() else []
    semantic_rewrites = 0
    scope_rewrites = 0
    drop_rewrites = 0
    unresolved_files = 0
    claim_grade_files = 0
    crate_names: set[str] = set()
    for path in files:
        try:
            record = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        summary = record.get("summary") if isinstance(record.get("summary"), dict) else {}
        compiler = record.get("compiler_pass") if isinstance(record.get("compiler_pass"), dict) else {}
        rustc_args = record.get("rustc_args")
        if isinstance(rustc_args, list):
            for index, value in enumerate(rustc_args):
                if value == "--crate-name" and index + 1 < len(rustc_args):
                    crate_names.add(str(rustc_args[index + 1]).replace("-", "_"))
                    break
                if isinstance(value, str) and value.startswith("--crate-name="):
                    crate_names.add(value.split("=", 1)[1].replace("-", "_"))
                    break
        scope = int(summary.get("semantic_scope_rewrite_applied_count", compiler.get("semantic_scope_rewrite_applied_count", 0)) or 0)
        drops = int(summary.get("semantic_scope_drop_rewrite_applied_count", compiler.get("semantic_scope_drop_rewrite_applied_count", 0)) or 0)
        scope_rewrites += scope
        drop_rewrites += drops
        semantic_rewrites += scope + drops
        resolution = str(summary.get("semantic_scope_replacement_resolution_status", compiler.get("semantic_scope_replacement_resolution_status", "")))
        if "not_resolved" in resolution:
            unresolved_files += 1
        if bool(summary.get("claim_grade", compiler.get("claim_grade", False))):
            claim_grade_files += 1
    return {
        "audit_file_count": len(files),
        "semantic_scope_rewrites_applied": scope_rewrites,
        "semantic_drop_rewrites_applied": drop_rewrites,
        "semantic_rewrites_applied": semantic_rewrites,
        "unresolved_file_count": unresolved_files,
        "claim_grade_file_count": claim_grade_files,
        "crate_names": sorted(crate_names),
        "audit_sha256": sha256_bytes(
            b"".join(path.name.encode() + b"\0" + path.read_bytes() for path in files)
        ),
    }


def validate_typeiso_audits(
    audit: dict[str, Any], target_crates: Sequence[str] = ()
) -> None:
    if int(audit.get("audit_file_count", 0)) <= 0:
        raise MatrixError("Type Isolation build emitted no compiler audit files")
    if int(audit.get("semantic_rewrites_applied", 0)) <= 0:
        raise MatrixError("Type Isolation build applied zero semantic rewrites")
    expected = {name.replace("-", "_") for name in target_crates}
    observed = {
        str(name).replace("-", "_") for name in audit.get("crate_names", [])
    }
    missing = sorted(expected - observed)
    if missing:
        raise MatrixError(
            "Type Isolation build emitted no audit for selected crates: "
            + ",".join(missing)
        )


def validate_typeiso_coverage(audit: dict[str, Any], stats: dict[str, Any] | None) -> None:
    validate_typeiso_audits(audit)
    if not isinstance(stats, dict):
        raise MatrixError("typeiso_coverage run emitted no complete stats JSON")
    typed = int(stats.get("typed_allocations", 0) or 0)
    total = int(stats.get("total_allocations", 0) or 0)
    if typed <= 0 or total < typed:
        raise MatrixError(
            f"typeiso_coverage runtime evidence is invalid: typed={typed}, total={total}"
        )


def ensure_wrapper(path: pathlib.Path, toolchain: str, timeout: int) -> pathlib.Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    record_path = path.with_name(path.name + ".build.json")
    pass_source_sha256 = sha256_file(PASS_SOURCE)
    if path.is_file() and record_path.is_file():
        try:
            record = json.loads(record_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            record = {}
        if (
            record.get("success") is True
            and record.get("toolchain") == toolchain
            and record.get("pass_source_sha256") == pass_source_sha256
            and record.get("wrapper_sha256") == sha256_file(path)
        ):
            return path.resolve()

    temporary = path.with_name(path.name + ".tmp")
    temporary.unlink(missing_ok=True)
    env = os.environ.copy()
    env["RUSTC_BOOTSTRAP"] = "1"
    result = execute(
        [
            "rustc",
            f"+{toolchain}",
            "--cfg",
            "unialloc_rustc_current",
            PASS_SOURCE,
            "-O",
            "-o",
            temporary,
        ],
        cwd=ROOT,
        env=env,
        timeout=timeout,
    )
    if result["exit_code"] != 0 or result["timed_out"] or not temporary.is_file():
        temporary.unlink(missing_ok=True)
        raise MatrixError(
            "rustc wrapper build failed:\n"
            + result["stderr"].decode("utf-8", errors="replace")
        )
    temporary.replace(path)
    record = {
        "schema_version": 1,
        "success": True,
        "toolchain": toolchain,
        "pass_source": str(PASS_SOURCE.resolve()),
        "pass_source_sha256": pass_source_sha256,
        "wrapper_sha256": sha256_file(path),
        "command": result["command"],
    }
    record_path.write_text(
        json.dumps(record, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    return path.resolve()


def ensure_force_load_wrapper(path: pathlib.Path) -> pathlib.Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    if not path.exists() or path.read_text(encoding="utf-8") != FORCE_LOAD_WRAPPER_SOURCE:
        path.write_text(FORCE_LOAD_WRAPPER_SOURCE, encoding="utf-8")
    path.chmod(0o755)
    return path.resolve()


def rustc_sysroot(toolchain: str) -> pathlib.Path:
    result = execute(
        ["rustc", f"+{toolchain}", "--print", "sysroot"],
        cwd=ROOT,
        env=os.environ.copy(),
        timeout=60,
    )
    if result["exit_code"] != 0:
        raise MatrixError(result["stderr"].decode("utf-8", errors="replace"))
    return pathlib.Path(result["stdout"].decode().strip()).resolve()


def dependency_for_variant(variant: str) -> tuple[str, tuple[str, ...]]:
    if variant == "jemalloc":
        return 'jemallocator = "=0.5.4"', ()
    if variant == "mimalloc":
        return 'mimalloc = { version = "=0.1.25", default-features = false }', ()
    if variant == "unialloc":
        return cargo_path_dependency(ROOT / "unialloc"), ()
    if variant in {"typed_plain", "typeiso_perf"}:
        return cargo_path_dependency(ROOT / "unialloc", ("type_isolation",)), ("type_isolation",)
    if variant == "typeiso_coverage":
        features = ("stats", "type_isolation")
        return cargo_path_dependency(ROOT / "unialloc", features), features
    raise MatrixError(f"no dependency is required for variant {variant}")


def force_load_features_for_variant(variant: str) -> tuple[str, ...]:
    if variant not in TYPEISO_VARIANTS:
        raise MatrixError(f"force-load rlib requested for unsupported variant: {variant}")
    _dependency, features = dependency_for_variant(variant)
    return features


def build_force_load_rlib(
    variant: str,
    *,
    raw_dir: pathlib.Path,
    toolchain: str,
    jobs: int,
    timeout: int,
) -> dict[str, Any]:
    features = force_load_features_for_variant(variant)
    feature_key = "-".join(features) if features else "default"
    build_root = raw_dir.resolve() / "force-load" / feature_key
    target_dir = build_root / "target"
    record_path = build_root / "build.json"
    implementation_sha256 = implementation_digest()
    if record_path.is_file():
        try:
            cached = json.loads(record_path.read_text(encoding="utf-8"))
            rlib = pathlib.Path(str(cached.get("rlib", "")))
            if (
                cached.get("success")
                and cached.get("toolchain") == toolchain
                and cached.get("features") == list(features)
                and cached.get("panic_strategy") == "unwind"
                and cached.get("implementation_sha256") == implementation_sha256
                and rlib.is_file()
                and cached.get("rlib_sha256") == sha256_file(rlib)
            ):
                return cached
        except (OSError, ValueError, json.JSONDecodeError):
            pass

    if build_root.exists():
        shutil.rmtree(build_root)
    build_root.mkdir(parents=True)
    env = os.environ.copy()
    env["CARGO_INCREMENTAL"] = "0"
    env["CARGO_PROFILE_RELEASE_PANIC"] = "unwind"
    command: list[str | os.PathLike[str]] = [
        "cargo",
        f"+{toolchain}",
        "build",
        "--release",
        "--locked",
        "--manifest-path",
        ROOT / "unialloc" / "Cargo.toml",
        "--lib",
        "--target-dir",
        target_dir,
        "--jobs",
        str(jobs),
    ]
    if features:
        command.extend(["--features", ",".join(features)])
    result = execute(command, cwd=ROOT, env=env, timeout=timeout)
    (build_root / "build.stdout").write_bytes(result["stdout"])
    (build_root / "build.stderr").write_bytes(result["stderr"])
    candidates = sorted((target_dir / "release" / "deps").glob("libunialloc-*.rlib"))
    success = (
        result["exit_code"] == 0
        and not result["timed_out"]
        and len(candidates) == 1
    )
    record: dict[str, Any] = {
        "mode": "selected-crate-rustc-force-extern",
        "success": success,
        "toolchain": toolchain,
        "features": list(features),
        "default_features_enabled": True,
        "default_features": ["pthread_dtor", "rseq"],
        "panic_strategy": "unwind",
        "implementation_sha256": implementation_sha256,
        "build_exit_code": result["exit_code"],
        "build_timed_out": result["timed_out"],
        "build_wall_seconds": result["wall_seconds"],
        "command": result["command"],
        "candidate_count": len(candidates),
    }
    if len(candidates) == 1:
        rlib = candidates[0].resolve()
        record.update(
            {
                "rlib": str(rlib),
                "rlib_sha256": sha256_file(rlib),
                "rlib_size_bytes": rlib.stat().st_size,
                "dependency_dir": str(rlib.parent),
                "force_extern": f"force:unialloc={rlib}",
            }
        )
    record_path.write_text(
        json.dumps(record, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    if not success:
        stderr = result["stderr"].decode("utf-8", errors="replace")
        detail = (
            f"expected one libunialloc rlib, found {len(candidates)}"
            if result["exit_code"] == 0 and not result["timed_out"]
            else stderr[-8000:]
        )
        raise MatrixError(f"force-load UniAlloc rlib build failed: {detail}")
    return record


def uses_original_jemalloc(spec: AppSpec, variant: str) -> bool:
    return spec.name == "fd" and variant == "jemalloc"


def uses_unialloc(variant: str) -> bool:
    return variant == "unialloc" or variant in TYPEISO_VARIANTS


def reset_target_dir_before_rebuild(target_dir: pathlib.Path, variant: str) -> None:
    if variant in TYPEISO_VARIANTS and target_dir.exists():
        # Selected dependency crates embed the exact force-loaded UniAlloc rlib.
        # Once that rlib changes, Cargo can otherwise reuse an apparently fresh
        # dependency whose transitive crate metadata points at the removed hash.
        shutil.rmtree(target_dir)


def cargo_feature_args(spec: AppSpec, variant: str) -> list[str]:
    if spec.name != "fd" or variant == "native":
        return []
    features = "use-jemalloc,completions" if variant == "jemalloc" else "completions"
    return ["--no-default-features", "--features", features]


def is_reusable_binary_record(
    record: dict[str, Any],
    *,
    spec: AppSpec,
    variant: str,
    output_binary: pathlib.Path,
    toolchain: str,
    implementation_sha256: str,
    compiler_wrapper_sha256: str | None,
    force_load_wrapper_sha256: str | None,
    force_load: dict[str, Any] | None,
) -> bool:
    if record.get("success") is not True or not output_binary.is_file():
        return False
    try:
        binary_sha256 = sha256_file(output_binary)
    except OSError:
        return False
    if variant in TYPEISO_VARIANTS:
        recorded_force_load = record.get("force_load")
        if not isinstance(recorded_force_load, dict) or not isinstance(force_load, dict):
            return False
        if (
            recorded_force_load.get("rlib_sha256") != force_load.get("rlib_sha256")
            or recorded_force_load.get("features") != force_load.get("features")
        ):
            return False
    return (
        record.get("toolchain") == toolchain
        and record.get("app") == spec.name
        and record.get("variant") == variant
        and record.get("source_head") == spec.head
        and record.get("implementation_sha256") == implementation_sha256
        and record.get("compiler_wrapper_sha256") == compiler_wrapper_sha256
        and record.get("force_load_wrapper_sha256") == force_load_wrapper_sha256
        and record.get("binary") == str(output_binary.resolve())
        and record.get("binary_sha256") == binary_sha256
    )


def build_variant(
    spec: AppSpec,
    variant: str,
    *,
    source_checkout: pathlib.Path,
    raw_dir: pathlib.Path,
    wrapper: pathlib.Path,
    sysroot: pathlib.Path,
    toolchain: str,
    jobs: int,
    timeout: int,
    reuse_binary: bool,
) -> dict[str, Any]:
    output_binary = raw_dir / "binaries" / spec.name / variant / spec.binary
    record_path = output_binary.parent / "build.json"
    target_dir = raw_dir / "targets" / spec.name / variant
    implementation_sha256 = implementation_digest()
    compiler_wrapper_sha256 = sha256_file(wrapper) if variant in TYPEISO_VARIANTS else None
    force_load: dict[str, Any] | None = None
    force_load_wrapper: pathlib.Path | None = None
    force_load_wrapper_sha256: str | None = None
    if variant in TYPEISO_VARIANTS:
        validate_force_load_lock_targets(spec, source_checkout / "Cargo.lock")
        force_load = build_force_load_rlib(
            variant,
            raw_dir=raw_dir,
            toolchain=toolchain,
            jobs=jobs,
            timeout=timeout,
        )
        force_load_wrapper = ensure_force_load_wrapper(
            raw_dir / "tools" / "unialloc-force-load-wrapper"
        )
        force_load_wrapper_sha256 = sha256_file(force_load_wrapper)
    if reuse_binary and record_path.is_file():
        try:
            cached = json.loads(record_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError):
            cached = {}
        if isinstance(cached, dict) and is_reusable_binary_record(
            cached,
            spec=spec,
            variant=variant,
            output_binary=output_binary,
            toolchain=toolchain,
            implementation_sha256=implementation_sha256,
            compiler_wrapper_sha256=compiler_wrapper_sha256,
            force_load_wrapper_sha256=force_load_wrapper_sha256,
            force_load=force_load,
        ):
            return cached

    reset_target_dir_before_rebuild(target_dir, variant)
    worktree = raw_dir / "build-work" / f"{spec.name}-{variant}"
    copy_checkout(source_checkout, worktree)
    ensure_standalone_workspace(worktree / "Cargo.toml")
    patched_manifests: list[pathlib.Path] = []
    if variant != "native" and not uses_original_jemalloc(spec, variant):
        root_manifest = worktree / "Cargo.toml"
        if variant in TYPEISO_VARIANTS:
            inject_allocator_main(worktree / spec.main_source, variant)
        else:
            dependency, _features = dependency_for_variant(variant)
            add_dependency(root_manifest, dependency)
            patched_manifests = [root_manifest]
            if uses_unialloc(variant):
                add_spin_patch(root_manifest, find_cached_spin())
            inject_allocator_main(worktree / spec.main_source, variant)

    audit_dir = raw_dir / "audits" / spec.name / variant
    pass_log_dir = raw_dir / "pass-logs" / spec.name / variant
    if audit_dir.exists():
        shutil.rmtree(audit_dir)
    if pass_log_dir.exists():
        shutil.rmtree(pass_log_dir)
    audit_dir.mkdir(parents=True, exist_ok=True)
    pass_log_dir.mkdir(parents=True, exist_ok=True)
    env = os.environ.copy()
    env["CARGO_TARGET_DIR"] = str(target_dir.resolve())
    env["CARGO_INCREMENTAL"] = "0"
    if variant in TYPEISO_VARIANTS:
        assert force_load is not None and force_load_wrapper is not None
        env = typeiso_environment(
            env,
            wrapper=force_load_wrapper,
            audit_dir=audit_dir,
            pass_log_dir=pass_log_dir,
            sysroot=sysroot,
            target_crates=spec.target_crates,
            policy_flags=0 if variant == "typed_plain" else 1,
        )
        env = force_load_environment(
            env,
            driver_wrapper=wrapper,
            force_load=force_load,
        )
    env, compatibility = workload_compatibility_environment(spec, env)
    command = [
        "cargo",
        f"+{toolchain}",
        "build",
        "--release",
        "--bin",
        spec.binary,
        "--jobs",
        str(jobs),
    ]
    command.extend(cargo_feature_args(spec, variant))
    result = execute(command, cwd=worktree, env=env, timeout=timeout)
    output_binary.parent.mkdir(parents=True, exist_ok=True)
    (output_binary.parent / "build.stdout").write_bytes(result["stdout"])
    (output_binary.parent / "build.stderr").write_bytes(result["stderr"])
    built_binary = target_dir / "release" / spec.binary
    audit = summarize_audits(audit_dir) if variant in TYPEISO_VARIANTS else None
    success = result["exit_code"] == 0 and built_binary.exists() and not result["timed_out"]
    if success and audit is not None:
        validate_typeiso_audits(audit, spec.target_crates)
    record = {
        "app": spec.name,
        "variant": variant,
        "toolchain": toolchain,
        "source_head": spec.head,
        "implementation_sha256": implementation_sha256,
        "compiler_wrapper_sha256": compiler_wrapper_sha256,
        "force_load_wrapper_sha256": force_load_wrapper_sha256,
        "force_load": force_load,
        "original_allocator": (
            spec.original_allocator
            if variant == "native" or uses_original_jemalloc(spec, variant)
            else None
        ),
        "allocator_route": (
            "original-native"
            if variant == "native"
            else (
                "original-fd-use-jemalloc"
                if uses_original_jemalloc(spec, variant)
                else (
                    "injected-selected-crate-force-load"
                    if variant in TYPEISO_VARIANTS
                    else "injected"
                )
            )
        ),
        "success": success,
        "build_exit_code": result["exit_code"],
        "build_wall_seconds": result["wall_seconds"],
        "build_timed_out": result["timed_out"],
        "command": result["command"],
        "patched_manifests": [
            path.relative_to(worktree).as_posix() for path in patched_manifests
        ],
        "target_crates": list(spec.target_crates) if variant in TYPEISO_VARIANTS else [],
        "force_load_lock_packages": (
            list(spec.force_load_lock_packages) if variant in TYPEISO_VARIANTS else []
        ),
        "stats_enabled": variant == "typeiso_coverage",
        "performance_eligible": variant != "typeiso_coverage",
        "actual_mir_rewrite": variant in TYPEISO_VARIANTS,
        "compatibility": compatibility,
        "lowering_policy_flags": (
            0 if variant == "typed_plain" else (1 if variant in TYPEISO_VARIANTS else None)
        ),
        "audit": audit,
    }
    if success:
        shutil.copy2(built_binary, output_binary)
        record.update(
            {
                "binary": str(output_binary.resolve()),
                "binary_sha256": sha256_file(output_binary),
                "binary_size_bytes": output_binary.stat().st_size,
            }
        )
    record_path.write_text(json.dumps(record, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    if not success:
        stderr = result["stderr"].decode("utf-8", errors="replace")
        raise MatrixError(f"build failed for {spec.name}/{variant}:\n{stderr[-8000:]}")
    return record


def corpus_digest(root: pathlib.Path) -> str:
    digest = hashlib.sha256()
    for path in sorted(root.rglob("*.txt")):
        digest.update(path.relative_to(root).as_posix().encode())
        digest.update(b"\0")
        digest.update(path.read_bytes())
    return digest.hexdigest()


def fd_tree_file_count(quick: bool) -> int:
    return 20_480 if quick else 204_800


def prepare_metadata_tree(root: pathlib.Path, *, file_count: int) -> dict[str, Any]:
    if file_count < 1:
        raise MatrixError("metadata tree file count must be positive")
    if root.exists():
        shutil.rmtree(root)
    root.mkdir(parents=True)
    bucket_count = min(256, file_count)
    digest = hashlib.sha256()
    for index in range(file_count):
        relative = pathlib.Path(f"bucket-{index % bucket_count:03d}") / f"file-{index:06d}.txt"
        path = root / relative
        path.parent.mkdir(exist_ok=True)
        path.touch()
        digest.update(relative.as_posix().encode())
        digest.update(b"\0")
        digest.update(b"0\0")
    return {
        "path": str(root.resolve()),
        "file_count": file_count,
        "file_bytes": 0,
        "bucket_count": bucket_count,
        "tree_sha256": digest.hexdigest(),
    }


def prepare_fd_tree(root: pathlib.Path, quick: bool) -> dict[str, Any]:
    return prepare_metadata_tree(root, file_count=fd_tree_file_count(quick))


def prepare_corpus(root: pathlib.Path, quick: bool) -> dict[str, Any]:
    file_count = 1024 if quick else 4096
    file_bytes = 32 * 1024 if quick else 64 * 1024
    if root.exists():
        shutil.rmtree(root)
    root.mkdir(parents=True)
    filler = b"allocator matrix deterministic corpus line 0123456789abcdef\n"
    for index in range(file_count):
        directory = root / f"bucket-{index % 32:02d}"
        directory.mkdir(exist_ok=True)
        header = f"file={index:06d}\n".encode()
        if index % 8 == 0:
            header += f"UNIALLOC_MATRIX_NEEDLE_{index:06d}\n".encode()
        repeats = (file_bytes - len(header) + len(filler) - 1) // len(filler)
        data = (header + filler * repeats)[:file_bytes]
        (directory / f"file-{index:06d}.txt").write_bytes(data)
    return {
        "path": str(root.resolve()),
        "file_count": file_count,
        "file_bytes": file_bytes,
        "tree_sha256": corpus_digest(root),
    }


def workload_command(
    spec: AppSpec,
    binary: pathlib.Path,
    *,
    corpus: pathlib.Path,
    source_checkout: pathlib.Path,
    run_dir: pathlib.Path,
    quick: bool,
) -> tuple[list[str], pathlib.Path, pathlib.Path | None]:
    if spec.name == "ripgrep":
        return (
            [
                str(binary),
                "--threads",
                "1",
                "--sort",
                "path",
                "--color",
                "never",
                "--no-ignore",
                "--no-heading",
                "--line-number",
                r"UNIALLOC_MATRIX_NEEDLE_[0-9]+",
                ".",
            ],
            corpus,
            None,
        )
    if spec.name == "fd":
        return (
            [
                str(binary),
                "--threads",
                "1",
                "--no-ignore",
                "--type",
                "f",
                "--glob",
                "*.txt",
                ".",
            ],
            corpus,
            None,
        )
    input_name = "issue-141.png" if quick else "issue-167.png"
    input_path = source_checkout / "tests" / "files" / input_name
    output_path = run_dir / "output.png"
    return (
        [
            str(binary),
            "--opt",
            "2",
            "--threads",
            "1",
            "--force",
            "--quiet",
            "--out",
            str(output_path),
            str(input_path),
        ],
        run_dir,
        output_path,
    )


def run_measured(
    command: Sequence[str | os.PathLike[str]],
    *,
    cwd: pathlib.Path,
    env: dict[str, str],
    time_binary: pathlib.Path,
    rss_path: pathlib.Path,
    timeout: int,
) -> dict[str, Any]:
    if not time_binary.exists():
        raise MatrixError(f"GNU time binary is missing: {time_binary}")
    rss_path.parent.mkdir(parents=True, exist_ok=True)
    result = execute(
        [time_binary, "-f", "%M", "-o", rss_path, "--", *command],
        cwd=cwd,
        env=env,
        timeout=timeout,
    )
    try:
        peak_rss_kib = int(rss_path.read_text(encoding="utf-8").strip().splitlines()[-1])
    except (OSError, ValueError, IndexError) as error:
        raise MatrixError(f"unable to parse GNU time RSS output: {rss_path}") from error
    return {
        "command": [str(value) for value in command],
        "exit_code": result["exit_code"],
        "timed_out": result["timed_out"],
        "wall_seconds": result["wall_seconds"],
        "peak_rss_kib": peak_rss_kib,
        "stdout": result["stdout"],
        "stderr": result["stderr"],
        "stdout_sha256": sha256_bytes(result["stdout"]),
        "stderr_sha256": sha256_bytes(result["stderr"]),
    }


def persist_result(path: pathlib.Path, record: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(record, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    temporary.replace(path)


def summarize_measurements(measurements: Sequence[dict[str, Any]]) -> list[dict[str, Any]]:
    summaries: list[dict[str, Any]] = []
    keys = sorted({(row["app"], row["variant"]) for row in measurements if not row["warmup"]})
    for app, variant in keys:
        rows = [
            row
            for row in measurements
            if row["app"] == app and row["variant"] == variant and not row["warmup"]
        ]
        summaries.append(
            {
                "app": app,
                "variant": variant,
                "sample_count": len(rows),
                "median_wall_seconds": statistics.median(row["wall_seconds"] for row in rows),
                "median_peak_rss_kib": statistics.median(row["peak_rss_kib"] for row in rows),
                "output_sha256": rows[0]["output_sha256"],
                "stats": rows[-1].get("stats"),
                "performance_eligible": variant != "typeiso_coverage",
            }
        )
    by_app = {name: [row for row in summaries if row["app"] == name] for name in APP_SPECS}
    for rows in by_app.values():
        baseline = next((row for row in rows if row["variant"] == "native"), None)
        typed_plain = next((row for row in rows if row["variant"] == "typed_plain"), None)
        for row in rows:
            if row["performance_eligible"]:
                if baseline is not None:
                    row["wall_ratio_vs_native"] = row["median_wall_seconds"] / baseline["median_wall_seconds"]
                    row["rss_ratio_vs_native"] = row["median_peak_rss_kib"] / baseline["median_peak_rss_kib"]
                if typed_plain is not None:
                    row["wall_ratio_vs_typed_plain"] = row["median_wall_seconds"] / typed_plain["median_wall_seconds"]
                    row["rss_ratio_vs_typed_plain"] = row["median_peak_rss_kib"] / typed_plain["median_peak_rss_kib"]
    return summaries


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    runtime_env = runtime_environment(
        os.environ.copy(), disable_glibc_rseq=args.disable_glibc_rseq
    )
    raw_dir = args.raw_dir.resolve()
    raw_dir.mkdir(parents=True, exist_ok=True)
    wrapper = ensure_wrapper((args.wrapper or (raw_dir / "tools" / "unialloc-rustc-wrapper")).resolve(), args.toolchain, args.build_timeout)
    sysroot = rustc_sysroot(args.toolchain)
    warmups, repetitions = measurement_counts(args)
    result_path = raw_dir / "results.json"
    result: dict[str, Any] = {
        "schema_version": 1,
        "source": "unialloc-realworld-type-isolation-matrix",
        "success": False,
        "quick": args.quick,
        "warmups": warmups,
        "repetitions": repetitions,
        "apps": list(args.apps),
        "variants": list(args.variants),
        "toolchain": args.toolchain,
        "wrapper": {"path": str(wrapper), "sha256": sha256_file(wrapper)},
        "implementation_sha256": implementation_digest(),
        "pass_source_sha256": sha256_file(PASS_SOURCE),
        "sysroot": str(sysroot),
        "glibc_rseq_mode": (
            "disabled_for_self_registration_testing"
            if args.disable_glibc_rseq
            else "libc_default"
        ),
        "glibc_tunable": runtime_env.get("GLIBC_TUNABLES"),
        "performance_stats_enabled": False,
        "coverage_variant_performance_eligible": False,
        "checkouts": {},
        "builds": [],
        "measurements": [],
        "summaries": [],
    }
    checkouts: dict[str, pathlib.Path] = {}
    for app in args.apps:
        spec = APP_SPECS[app]
        checkout = (args.checkout_root / spec.checkout).resolve()
        result["checkouts"][app] = verify_checkout(checkout, spec.head)
        checkouts[app] = checkout
    result["corpus"] = prepare_corpus(raw_dir / "corpus", args.quick)
    if "fd" in args.apps:
        result["fd_tree"] = prepare_fd_tree(raw_dir / "fd-tree", args.quick)
    persist_result(result_path, result)

    builds: dict[tuple[str, str], dict[str, Any]] = {}
    for app in args.apps:
        for variant in args.variants:
            build = build_variant(
                APP_SPECS[app],
                variant,
                source_checkout=checkouts[app],
                raw_dir=raw_dir,
                wrapper=wrapper,
                sysroot=sysroot,
                toolchain=args.toolchain,
                jobs=args.jobs,
                timeout=args.build_timeout,
                reuse_binary=args.reuse_binaries,
            )
            builds[(app, variant)] = build
            result["builds"].append(build)
            persist_result(result_path, result)

    corpus_path = pathlib.Path(result["corpus"]["path"])
    fd_tree_path = (
        pathlib.Path(result["fd_tree"]["path"])
        if isinstance(result.get("fd_tree"), dict)
        else None
    )
    total_rounds = warmups + repetitions
    expected_outputs: dict[str, str] = {}
    for round_index in range(total_rounds):
        warmup = round_index < warmups
        measured_index = None if warmup else round_index - warmups
        for app_offset, app in enumerate(args.apps):
            spec = APP_SPECS[app]
            for variant in rotated_order(args.variants, round_index + app_offset):
                build = builds[(app, variant)]
                run_dir = raw_dir / "runs" / app / f"round-{round_index:02d}" / variant
                if run_dir.exists():
                    shutil.rmtree(run_dir)
                run_dir.mkdir(parents=True)
                command, cwd, output_file = workload_command(
                    spec,
                    pathlib.Path(build["binary"]),
                    corpus=(
                        fd_tree_path
                        if spec.name == "fd" and fd_tree_path is not None
                        else corpus_path
                    ),
                    source_checkout=checkouts[app],
                    run_dir=run_dir,
                    quick=args.quick,
                )
                measured = run_measured(
                    command,
                    cwd=cwd,
                    env=runtime_env,
                    time_binary=args.time_binary,
                    rss_path=run_dir / "peak-rss-kib.txt",
                    timeout=args.run_timeout,
                )
                (run_dir / "stdout.bin").write_bytes(measured.pop("stdout"))
                stderr = measured.pop("stderr")
                (run_dir / "stderr.bin").write_bytes(stderr)
                if measured["exit_code"] != 0 or measured["timed_out"]:
                    raise MatrixError(f"workload failed for {app}/{variant}: {measured}")
                output_sha = sha256_file(output_file) if output_file is not None else measured["stdout_sha256"]
                stats = parse_stats_json(stderr.decode("utf-8", errors="replace"))
                if variant == "typeiso_coverage":
                    validate_typeiso_coverage(build["audit"], stats)
                elif stats is not None:
                    raise MatrixError(f"performance variant unexpectedly emitted stats: {app}/{variant}")
                expected = expected_outputs.setdefault(app, output_sha)
                if output_sha != expected:
                    raise MatrixError(
                        f"output mismatch for {app}/{variant}: got {output_sha}, expected {expected}"
                    )
                row = {
                    **measured,
                    "app": app,
                    "variant": variant,
                    "round": round_index,
                    "warmup": warmup,
                    "measurement_index": measured_index,
                    "output_sha256": output_sha,
                    "stats": stats,
                    "performance_eligible": variant != "typeiso_coverage",
                }
                result["measurements"].append(row)
                persist_result(result_path, result)

    result["summaries"] = summarize_measurements(result["measurements"])
    result["success"] = True
    persist_result(result_path, result)
    print(json.dumps({"success": True, "results": str(result_path), "summaries": result["summaries"]}, sort_keys=True))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except MatrixError as error:
        print(f"error: {error}", file=sys.stderr)
        raise SystemExit(1)
