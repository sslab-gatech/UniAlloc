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
import importlib.util
import json
import os
import pathlib
import re
import shutil
import signal
import statistics
import subprocess
import sys
import tempfile
import time
import tomllib
from dataclasses import dataclass
from typing import Any, Iterable, Sequence


ROOT = pathlib.Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from evaluation.scripts import type_isolation_suite_contract as suite_contract  # noqa: E402


def _load_google_tcmalloc_support() -> Any:
    try:
        import google_tcmalloc_support as support

        return support
    except ModuleNotFoundError:
        support_path = pathlib.Path(__file__).resolve().with_name(
            "google_tcmalloc_support.py"
        )
        spec = importlib.util.spec_from_file_location(
            "_unialloc_realworld_google_tcmalloc_support", support_path
        )
        if spec is None or spec.loader is None:
            raise ImportError(f"could not load google/tcmalloc support from {support_path}")
        support = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(support)
        return support


GOOGLE_TCMALLOC = _load_google_tcmalloc_support()
GOOGLE_TCMALLOC_PREFIX_ENV = "UNIALLOC_GOOGLE_TCMALLOC_PREFIX"
GOOGLE_TCMALLOC_RUNTIME_IDENTITY_MARKER = GOOGLE_TCMALLOC.RUNTIME_IDENTITY_MARKER
DEFAULT_RAW_DIR = ROOT / "evaluation" / "raw" / "realworld-type-isolation-matrix"
DEFAULT_CHECKOUT_ROOT = ROOT / "evaluation" / "external" / "_checkouts"
DEFAULT_WRAPPER = DEFAULT_RAW_DIR / "tools" / "unialloc-rustc-wrapper"
PASS_SOURCE = (
    ROOT / "tools" / "unialloc-rustc-pass" / "unialloc-rustc-mir-rewrite-dry-run.rs"
)
STATS_PREFIX = "UNIALLOC_REALWORLD_STATS="
TYPE_STATS_PREFIX = "UNIALLOC_REALWORLD_TYPE_STATS="
DEPOT_STATS_PREFIX = "UNIALLOC_REALWORLD_DEPOT_STATS="
DEPOT_EVENT_COUNTERS = (
    "attempts",
    "inserts",
    "rescued_overflow",
    "hits",
    "hits_after_recorded_l1_bypass",
    "evictions",
    "rejected_capacity",
    "rejected_policy",
    "rejected_registry_headroom",
    "from_probe_window_full",
    "from_slot_depth",
    "from_slot_byte_budget",
    "from_aggregate_byte_budget",
    "from_aggregate_entry_budget",
    "from_segregated_capacity",
    "from_local_eviction",
)
DEPOT_STATS_REQUIRED_FIELDS = tuple(
    f"cache_admission_depot_{counter}_{metric}"
    for counter in DEPOT_EVENT_COUNTERS
    for metric in ("events", "rounded_bytes")
) + (
    "cache_admission_depot_current_entries",
    "cache_admission_depot_peak_entries",
    "cache_admission_depot_current_rounded_bytes",
    "cache_admission_depot_peak_rounded_bytes",
)
GNU_TIME_PREFIX = "UNIALLOC_GNU_TIME"
GNU_TIME_FORMAT = GNU_TIME_PREFIX + "\t%U\t%S\t%P\t%M\t%F\t%R\t%c\t%w\t%x"
MIMALLOC_SYS_DEPENDENCY = 'libmimalloc-sys = "=0.1.49"'
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
BASE_VARIANTS = (
    "native",
    "system",
    "jemalloc",
    "mimalloc",
    "tcmalloc",
    "gperftools_legacy",
    "unialloc",
    "typed_plain",
    "typeiso_perf",
    "typeiso_coverage",
)
UNIALLOC_FEATURE_VARIANTS = {
    "unialloc_no_optional": (),
    "unialloc_rseq": ("rseq",),
    "unialloc_pthread_dtor": ("pthread_dtor",),
    "unialloc_hugepage": ("hugepage",),
    "unialloc_separate_sc": ("separate_sc_backend",),
    "unialloc_reclaim_checks": ("reclaim_checks",),
    "unialloc_metadata_segregation": ("metadata_segregation",),
    "unialloc_type_isolation": ("type_isolation",),
    "unialloc_pac": ("pac",),
}
VARIANTS = (
    "native",
    "system",
    "jemalloc",
    "mimalloc",
    "mimalloc_no_thp",
    "tcmalloc",
    "gperftools_legacy",
    "unialloc",
    *UNIALLOC_FEATURE_VARIANTS,
    "typed_plain",
    "typeiso_perf",
    "typeiso_coverage",
)
# Feature-isolation and runtime-policy variants are opt-in so existing invocations
# retain the same default matrix and cost.
DEFAULT_VARIANTS = tuple(
    variant
    for variant in BASE_VARIANTS
    if variant not in {"tcmalloc", "gperftools_legacy"}
)
TYPEISO_VARIANTS = frozenset(("typed_plain", "typeiso_perf", "typeiso_coverage"))
MIMALLOC_VARIANTS = frozenset(("mimalloc", "mimalloc_no_thp"))
UNIALLOC_VARIANTS = frozenset(
    ("unialloc", *UNIALLOC_FEATURE_VARIANTS, *TYPEISO_VARIANTS)
)
MIMALLOC_COMMON_RUNTIME_OVERRIDES = {
    "MIMALLOC_ALLOW_LARGE_OS_PAGES": "0",
    "MIMALLOC_RESERVE_HUGE_OS_PAGES": "0",
}


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

# fd remains available as an explicit compiler-path diagnostic. Its typed
# negative control is not equivalent to the uninstrumented UniAlloc route on
# the pinned directory-walk workload, so it is excluded from default campaigns.
DEFAULT_APPS = ("ripgrep", "oxipng")


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
    values = tuple(
        dict.fromkeys(part.strip() for part in raw.split(",") if part.strip())
    )
    unknown = sorted(set(values) - set(allowed))
    if not values:
        raise argparse.ArgumentTypeError(f"{label} must contain at least one value")
    if unknown:
        raise argparse.ArgumentTypeError(f"unknown {label}: {', '.join(unknown)}")
    return values


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apps", default=",".join(DEFAULT_APPS))
    parser.add_argument("--variants", default=",".join(DEFAULT_VARIANTS))
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--quick", dest="quick", action="store_true")
    mode.add_argument("--full", dest="quick", action="store_false")
    parser.set_defaults(quick=True)
    parser.add_argument("--warmups", type=int)
    parser.add_argument("--repetitions", type=int)
    parser.add_argument("--raw-dir", type=pathlib.Path, default=DEFAULT_RAW_DIR)
    parser.add_argument(
        "--checkout-root", type=pathlib.Path, default=DEFAULT_CHECKOUT_ROOT
    )
    parser.add_argument("--wrapper", type=pathlib.Path)
    parser.add_argument("--toolchain", default="nightly-2026-06-11")
    parser.add_argument(
        "--time-binary", type=pathlib.Path, default=pathlib.Path("/usr/bin/time")
    )
    parser.add_argument("--jobs", type=int, default=max(1, os.cpu_count() or 1))
    parser.add_argument(
        "--cpu-list",
        help="physical CPU list passed to numactl for measured commands",
    )
    parser.add_argument(
        "--numa-node",
        type=int,
        help="NUMA memory node passed to numactl for measured commands",
    )
    parser.add_argument(
        "--ripgrep-path-repetitions",
        type=int,
        default=1,
        help="repeat the ripgrep corpus path within one process",
    )
    parser.add_argument(
        "--fd-path-repetitions",
        type=int,
        default=1,
        help="repeat the fd search path within one process",
    )
    parser.add_argument(
        "--oxipng-threads",
        type=int,
        default=1,
        help="worker threads used by the Oxipng workload",
    )
    parser.add_argument(
        "--google-tcmalloc-prefix",
        type=pathlib.Path,
        default=os.environ.get(GOOGLE_TCMALLOC_PREFIX_ENV),
        help=(
            "prefix produced by build_google_tcmalloc.py; required for the modern "
            "tcmalloc variant"
        ),
    )
    parser.add_argument(
        "--tcmalloc-library",
        type=pathlib.Path,
        help=(
            "deprecated modern compatibility input; its containing lib directory "
            "must pass google/tcmalloc provenance and HPAA validation"
        ),
    )
    parser.add_argument(
        "--gperftools-legacy-library",
        type=pathlib.Path,
        help="explicit historical gperftools shared library for gperftools_legacy",
    )
    parser.add_argument("--build-timeout", type=int, default=1800)
    parser.add_argument("--run-timeout", type=int, default=300)
    parser.add_argument("--reuse-binaries", action="store_true")
    parser.add_argument(
        "--discard-run-output",
        action="store_true",
        help="retain hashes and metrics without persisting per-run stdout/stderr",
    )
    args = parser.parse_args(argv)
    args.rseq_policy = suite_contract.PRODUCTION_RSEQ_POLICY
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
    if args.numa_node is not None and args.numa_node < 0:
        parser.error("--numa-node must be non-negative")
    if args.ripgrep_path_repetitions < 1:
        parser.error("--ripgrep-path-repetitions must be positive")
    if args.fd_path_repetitions < 1:
        parser.error("--fd-path-repetitions must be positive")
    if args.oxipng_threads < 1:
        parser.error("--oxipng-threads must be positive")
    if (
        "tcmalloc" in args.variants
        and args.google_tcmalloc_prefix is None
        and args.tcmalloc_library is None
    ):
        parser.error(
            "--google-tcmalloc-prefix is required for the modern tcmalloc variant"
        )
    if (
        "gperftools_legacy" in args.variants
        and args.gperftools_legacy_library is None
    ):
        parser.error(
            "--gperftools-legacy-library is required for gperftools_legacy"
        )
    return args


def measurement_counts(args: argparse.Namespace) -> tuple[int, int]:
    warmups = args.warmups if args.warmups is not None else (1 if args.quick else 2)
    repetitions = (
        args.repetitions if args.repetitions is not None else (3 if args.quick else 7)
    )
    return warmups, repetitions


def rotated_order(values: tuple[str, ...], offset: int) -> tuple[str, ...]:
    if not values:
        return values
    pivot = offset % len(values)
    return values[pivot:] + values[:pivot]


def terminate_process_group(process: subprocess.Popen[bytes]) -> None:
    if process.poll() is not None:
        return
    try:
        os.killpg(process.pid, signal.SIGTERM)
    except ProcessLookupError:
        return
    try:
        process.communicate(timeout=2)
    except subprocess.TimeoutExpired:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        process.communicate()


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
    previous_sigterm: Any = None
    sigterm_installed = False

    def interrupt_on_sigterm(signum: int, frame: Any) -> None:
        del signum, frame
        raise KeyboardInterrupt("received SIGTERM while a child process was active")

    try:
        try:
            previous_sigterm = signal.getsignal(signal.SIGTERM)
            signal.signal(signal.SIGTERM, interrupt_on_sigterm)
            sigterm_installed = True
        except ValueError:
            pass
        stdout, stderr = process.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        timed_out = True
        os.killpg(process.pid, signal.SIGKILL)
        stdout, stderr = process.communicate()
    except BaseException:
        terminate_process_group(process)
        raise
    finally:
        if process.poll() is None:
            terminate_process_group(process)
        if sigterm_installed:
            signal.signal(signal.SIGTERM, previous_sigterm)
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
    result = execute(["git", *args], cwd=checkout, env=os.environ.copy(), timeout=60)
    if result["exit_code"] != 0:
        raise MatrixError(result["stderr"].decode("utf-8", errors="replace"))
    return result["stdout"].decode("utf-8", errors="replace").strip()


def verify_checkout(path: pathlib.Path, expected_head: str) -> dict[str, str]:
    if not (path / ".git").exists():
        raise MatrixError(f"missing Git checkout: {path}")
    head = git_text(path, ("rev-parse", "HEAD"))
    if head != expected_head:
        raise MatrixError(
            f"checkout HEAD mismatch for {path}: got {head}, expected {expected_head}"
        )
    status = git_text(path, ("status", "--short"))
    if status:
        raise MatrixError(f"checkout must be clean before copying: {path}\n{status}")
    return {"path": str(path.resolve()), "head": head, "status": status}


def locked_package_names(lock_path: pathlib.Path) -> set[str]:
    if not lock_path.is_file():
        raise MatrixError(f"missing Cargo lockfile: {lock_path}")
    text = lock_path.read_text(encoding="utf-8")
    return set(re.findall(r'(?m)^name\s*=\s*"([^"]+)"\s*$', text))


def locked_package(lock_path: pathlib.Path, name: str) -> dict[str, Any] | None:
    if not lock_path.is_file():
        return None
    try:
        record = tomllib.loads(lock_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, tomllib.TOMLDecodeError):
        return None
    packages = record.get("package")
    if not isinstance(packages, list):
        return None
    matches = [package for package in packages if package.get("name") == name]
    return dict(matches[0]) if len(matches) == 1 else None


def mimalloc_build_provenance(
    lock_path: pathlib.Path, target_dir: pathlib.Path
) -> dict[str, Any]:
    wrapper = locked_package(lock_path, "mimalloc")
    sys_package = locked_package(lock_path, "libmimalloc-sys")
    if not isinstance(wrapper, dict) or not isinstance(sys_package, dict):
        raise MatrixError("mimalloc build lockfile has incomplete allocator provenance")
    if sys_package.get("version") != "0.1.49":
        raise MatrixError(
            f"unexpected libmimalloc-sys version: {sys_package.get('version')}"
        )
    source_roots = sorted(
        (pathlib.Path.home() / ".cargo" / "registry" / "src").glob(
            "*/libmimalloc-sys-0.1.49"
        )
    )
    headers = [
        root / "c_src" / "mimalloc" / "v3" / "include" / "mimalloc.h"
        for root in source_roots
    ]
    header = next((path for path in headers if path.is_file()), None)
    if header is None:
        raise MatrixError("mimalloc v3 header is absent from the Cargo source cache")
    header_text = header.read_text(encoding="utf-8")
    version_match = re.search(r"(?m)^#define MI_MALLOC_VERSION\s+(\d+)", header_text)
    if version_match is None:
        raise MatrixError(f"mimalloc core version is absent from {header}")
    archives = sorted(
        (target_dir / "release" / "build").glob("libmimalloc-sys-*/out/libmimalloc.a")
    )
    return {
        "route": "rust-global-allocator-native-api",
        "wrapper_package": wrapper,
        "sys_package": sys_package,
        "core_generation": "v3",
        "core_version_number": int(version_match.group(1)),
        "core_header": str(header.resolve()),
        "core_header_sha256": sha256_file(header),
        "lockfile_sha256": sha256_file(lock_path),
        "static_archives": [
            {
                "path": str(path.resolve()),
                "size_bytes": path.stat().st_size,
                "sha256": sha256_file(path),
            }
            for path in archives
        ],
        "secure_feature_enabled": False,
    }


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


def cargo_path_dependency(
    path: pathlib.Path,
    features: Sequence[str] = (),
    *,
    default_features_enabled: bool = True,
) -> str:
    quoted_path = str(path.resolve()).replace("\\", "\\\\").replace('"', '\\"')
    options = [f'path = "{quoted_path}"']
    if not default_features_enabled:
        options.append("default-features = false")
    if features:
        options.append(
            "features = [" + ", ".join(json.dumps(value) for value in features) + "]"
        )
    return "unialloc = { " + ", ".join(options) + " }"


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
    roots = sorted(
        (pathlib.Path.home() / ".cargo" / "registry" / "src").glob("*/spin-0.9.0")
    )
    if not roots:
        raise MatrixError("spin 0.9.0 is absent from the Cargo source cache")
    indexed = [
        path for path in roots if path.parent.name.startswith("index.crates.io-")
    ]
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
    elif variant in MIMALLOC_VARIANTS:
        body = "static ALLOCATOR: mimalloc::MiMalloc = mimalloc::MiMalloc;"
    elif variant in UNIALLOC_VARIANTS - {"typeiso_coverage"}:
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
        let metadata_side_cache =
            unialloc::metadata_segregation_side_cache_snapshot();
        let admission = unialloc::type_cache_admission_stats_snapshot();
        let mut type_rows = [unialloc::SemanticTypeStatsSnapshot::empty(); 512];
        let type_row_count =
            unialloc::semantic_type_stats_snapshot(&mut type_rows).min(type_rows.len());
        eprintln!(
            "{STATS_PREFIX}{{{{\\\"total_allocations\\\":{{}},\\\"typed_allocations\\\":{{}},\\\"fallback_allocations\\\":{{}},\\\"typed_deallocations\\\":{{}},\\\"fallback_deallocations\\\":{{}},\\\"coverage_basis_points\\\":{{}},\\\"raw_alloc_no_metadata\\\":{{}},\\\"raw_dealloc_no_metadata\\\":{{}},\\\"raw_realloc_no_metadata\\\":{{}},\\\"cache_hits\\\":{{}},\\\"cache_inserts\\\":{{}},\\\"cache_bypasses\\\":{{}},\\\"typed_cache_wrong_identity_denials\\\":{{}},\\\"last_wrong_identity_requested_type_id\\\":{{}},\\\"last_wrong_identity_retained_type_id\\\":{{}},\\\"last_wrong_identity_requested_module_id\\\":{{}},\\\"last_wrong_identity_retained_module_id\\\":{{}},\\\"last_wrong_identity_requested_callsite\\\":{{}},\\\"last_wrong_identity_size\\\":{{}},\\\"last_wrong_identity_align\\\":{{}},\\\"recovery_matches\\\":{{}},\\\"recovery_mismatches\\\":{{}},\\\"type_stats_dropped_events\\\":{{}},\\\"side_cache_entries\\\":{{}},\\\"side_cache_retained_bytes\\\":{{}},\\\"side_cache_corrupt_slots\\\":{{}},\\\"metadata_segregation_side_cache_entries\\\":{{}},\\\"metadata_segregation_side_cache_retained_bytes\\\":{{}},\\\"metadata_segregation_side_cache_corrupt_buckets\\\":{{}},\\\"cache_admission_admitted_events\\\":{{}},\\\"cache_admission_admitted_rounded_bytes\\\":{{}},\\\"cache_admission_rejected_too_small_events\\\":{{}},\\\"cache_admission_rejected_too_small_rounded_bytes\\\":{{}},\\\"cache_admission_rejected_too_large_events\\\":{{}},\\\"cache_admission_rejected_too_large_rounded_bytes\\\":{{}},\\\"cache_admission_rejected_metadata_ineligible_events\\\":{{}},\\\"cache_admission_rejected_metadata_ineligible_rounded_bytes\\\":{{}},\\\"cache_admission_rejected_probe_window_full_events\\\":{{}},\\\"cache_admission_rejected_probe_window_full_rounded_bytes\\\":{{}},\\\"cache_admission_rejected_slot_depth_events\\\":{{}},\\\"cache_admission_rejected_slot_depth_rounded_bytes\\\":{{}},\\\"cache_admission_rejected_slot_byte_budget_events\\\":{{}},\\\"cache_admission_rejected_slot_byte_budget_rounded_bytes\\\":{{}},\\\"cache_admission_rejected_aggregate_byte_budget_events\\\":{{}},\\\"cache_admission_rejected_aggregate_byte_budget_rounded_bytes\\\":{{}},\\\"cache_admission_rejected_aggregate_entry_budget_events\\\":{{}},\\\"cache_admission_rejected_aggregate_entry_budget_rounded_bytes\\\":{{}},\\\"cache_admission_rejected_structural_or_alignment_events\\\":{{}},\\\"cache_admission_rejected_structural_or_alignment_rounded_bytes\\\":{{}},\\\"cache_admission_rejected_segregated_capacity_events\\\":{{}},\\\"cache_admission_rejected_segregated_capacity_rounded_bytes\\\":{{}},\\\"cache_admission_rejected_registry_pressure_events\\\":{{}},\\\"cache_admission_rejected_registry_pressure_rounded_bytes\\\":{{}},\\\"cache_admission_ownership_registry_full_failstop_events\\\":{{}},\\\"cache_admission_ownership_registry_full_failstop_rounded_bytes\\\":{{}},\\\"cache_admission_tiny_side_inserts_events\\\":{{}},\\\"cache_admission_tiny_side_inserts_rounded_bytes\\\":{{}},\\\"cache_admission_tiny_side_hits_events\\\":{{}},\\\"cache_admission_tiny_side_hits_rounded_bytes\\\":{{}},\\\"cache_admission_terminal_events\\\":{{}}}}}}",
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
            stats.typed_cache_wrong_identity_denials,
            stats.last_wrong_identity_requested_type_id,
            stats.last_wrong_identity_retained_type_id,
            stats.last_wrong_identity_requested_module_id,
            stats.last_wrong_identity_retained_module_id,
            stats.last_wrong_identity_requested_callsite,
            stats.last_wrong_identity_size,
            stats.last_wrong_identity_align,
            validation.recovery_identity_matches,
            validation.recovery_identity_mismatches,
            stats.semantic_type_stats_dropped_events,
            side_cache.occupied_entries,
            side_cache.retained_bytes,
            side_cache.corrupt_slots,
            metadata_side_cache.occupied_entries,
            metadata_side_cache.retained_bytes,
            metadata_side_cache.corrupt_buckets,
            admission.admitted.events,
            admission.admitted.rounded_bytes,
            admission.rejected_too_small.events,
            admission.rejected_too_small.rounded_bytes,
            admission.rejected_too_large.events,
            admission.rejected_too_large.rounded_bytes,
            admission.rejected_metadata_ineligible.events,
            admission.rejected_metadata_ineligible.rounded_bytes,
            admission.rejected_probe_window_full.events,
            admission.rejected_probe_window_full.rounded_bytes,
            admission.rejected_slot_depth.events,
            admission.rejected_slot_depth.rounded_bytes,
            admission.rejected_slot_byte_budget.events,
            admission.rejected_slot_byte_budget.rounded_bytes,
            admission.rejected_aggregate_byte_budget.events,
            admission.rejected_aggregate_byte_budget.rounded_bytes,
            admission.rejected_aggregate_entry_budget.events,
            admission.rejected_aggregate_entry_budget.rounded_bytes,
            admission.rejected_structural_or_alignment.events,
            admission.rejected_structural_or_alignment.rounded_bytes,
            admission.rejected_segregated_capacity.events,
            admission.rejected_segregated_capacity.rounded_bytes,
            admission.rejected_registry_pressure.events,
            admission.rejected_registry_pressure.rounded_bytes,
            admission.ownership_registry_full_failstop.events,
            admission.ownership_registry_full_failstop.rounded_bytes,
            admission.tiny_side_inserts.events,
            admission.tiny_side_inserts.rounded_bytes,
            admission.tiny_side_hits.events,
            admission.tiny_side_hits.rounded_bytes,
            admission.terminal_events(),
        );
        eprintln!(
            "{DEPOT_STATS_PREFIX}{{{{\\\"cache_admission_depot_attempts_events\\\":{{}},\\\"cache_admission_depot_attempts_rounded_bytes\\\":{{}},\\\"cache_admission_depot_inserts_events\\\":{{}},\\\"cache_admission_depot_inserts_rounded_bytes\\\":{{}},\\\"cache_admission_depot_rescued_overflow_events\\\":{{}},\\\"cache_admission_depot_rescued_overflow_rounded_bytes\\\":{{}},\\\"cache_admission_depot_hits_events\\\":{{}},\\\"cache_admission_depot_hits_rounded_bytes\\\":{{}},\\\"cache_admission_depot_hits_after_recorded_l1_bypass_events\\\":{{}},\\\"cache_admission_depot_hits_after_recorded_l1_bypass_rounded_bytes\\\":{{}},\\\"cache_admission_depot_evictions_events\\\":{{}},\\\"cache_admission_depot_evictions_rounded_bytes\\\":{{}},\\\"cache_admission_depot_rejected_capacity_events\\\":{{}},\\\"cache_admission_depot_rejected_capacity_rounded_bytes\\\":{{}},\\\"cache_admission_depot_rejected_policy_events\\\":{{}},\\\"cache_admission_depot_rejected_policy_rounded_bytes\\\":{{}},\\\"cache_admission_depot_rejected_registry_headroom_events\\\":{{}},\\\"cache_admission_depot_rejected_registry_headroom_rounded_bytes\\\":{{}},\\\"cache_admission_depot_from_probe_window_full_events\\\":{{}},\\\"cache_admission_depot_from_probe_window_full_rounded_bytes\\\":{{}},\\\"cache_admission_depot_from_slot_depth_events\\\":{{}},\\\"cache_admission_depot_from_slot_depth_rounded_bytes\\\":{{}},\\\"cache_admission_depot_from_slot_byte_budget_events\\\":{{}},\\\"cache_admission_depot_from_slot_byte_budget_rounded_bytes\\\":{{}},\\\"cache_admission_depot_from_aggregate_byte_budget_events\\\":{{}},\\\"cache_admission_depot_from_aggregate_byte_budget_rounded_bytes\\\":{{}},\\\"cache_admission_depot_from_aggregate_entry_budget_events\\\":{{}},\\\"cache_admission_depot_from_aggregate_entry_budget_rounded_bytes\\\":{{}},\\\"cache_admission_depot_from_segregated_capacity_events\\\":{{}},\\\"cache_admission_depot_from_segregated_capacity_rounded_bytes\\\":{{}},\\\"cache_admission_depot_from_local_eviction_events\\\":{{}},\\\"cache_admission_depot_from_local_eviction_rounded_bytes\\\":{{}},\\\"cache_admission_depot_current_entries\\\":{{}},\\\"cache_admission_depot_peak_entries\\\":{{}},\\\"cache_admission_depot_current_rounded_bytes\\\":{{}},\\\"cache_admission_depot_peak_rounded_bytes\\\":{{}}}}}}",
            admission.depot_attempts.events,
            admission.depot_attempts.rounded_bytes,
            admission.depot_inserts.events,
            admission.depot_inserts.rounded_bytes,
            admission.depot_rescued_overflow.events,
            admission.depot_rescued_overflow.rounded_bytes,
            admission.depot_hits.events,
            admission.depot_hits.rounded_bytes,
            admission.depot_hits_after_recorded_l1_bypass.events,
            admission.depot_hits_after_recorded_l1_bypass.rounded_bytes,
            admission.depot_evictions.events,
            admission.depot_evictions.rounded_bytes,
            admission.depot_rejected_capacity.events,
            admission.depot_rejected_capacity.rounded_bytes,
            admission.depot_rejected_policy.events,
            admission.depot_rejected_policy.rounded_bytes,
            admission.depot_rejected_registry_headroom.events,
            admission.depot_rejected_registry_headroom.rounded_bytes,
            admission.depot_from_probe_window_full.events,
            admission.depot_from_probe_window_full.rounded_bytes,
            admission.depot_from_slot_depth.events,
            admission.depot_from_slot_depth.rounded_bytes,
            admission.depot_from_slot_byte_budget.events,
            admission.depot_from_slot_byte_budget.rounded_bytes,
            admission.depot_from_aggregate_byte_budget.events,
            admission.depot_from_aggregate_byte_budget.rounded_bytes,
            admission.depot_from_aggregate_entry_budget.events,
            admission.depot_from_aggregate_entry_budget.rounded_bytes,
            admission.depot_from_segregated_capacity.events,
            admission.depot_from_segregated_capacity.rounded_bytes,
            admission.depot_from_local_eviction.events,
            admission.depot_from_local_eviction.rounded_bytes,
            admission.depot_current_entries,
            admission.depot_peak_entries,
            admission.depot_current_rounded_bytes,
            admission.depot_peak_rounded_bytes,
        );
        for row in type_rows.iter().take(type_row_count) {{
            eprintln!(
                "{TYPE_STATS_PREFIX}{{{{\\\"type_id\\\":{{}},\\\"module_id\\\":{{}},\\\"callsite\\\":{{}},\\\"allocations\\\":{{}},\\\"allocated_bytes\\\":{{}},\\\"deallocations\\\":{{}},\\\"cache_hits\\\":{{}},\\\"cache_inserts\\\":{{}},\\\"cache_bypasses\\\":{{}},\\\"observed_alloc_size\\\":{{}},\\\"observed_alloc_align\\\":{{}},\\\"observed_dealloc_size\\\":{{}},\\\"observed_dealloc_align\\\":{{}},\\\"policy_flags_seen\\\":{{}}}}}}",
                row.type_id,
                row.module_id,
                row.callsite,
                row.allocations,
                row.allocated_bytes,
                row.deallocations,
                row.cache_hits,
                row.cache_inserts,
                row.cache_bypasses,
                row.observed_alloc_size,
                row.observed_alloc_align,
                row.observed_dealloc_size,
                row.observed_dealloc_align,
                row.policy_flags_seen,
            );
        }}
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
        raise MatrixError(
            f"allocator source requested for unsupported variant: {variant}"
        )
    return f"""\n\n{INSTRUMENTATION_MARKER}
mod unialloc_realworld_instrumentation {{
    #[global_allocator]
    {body}
}}
"""


def inject_allocator_main(main_source: pathlib.Path, variant: str) -> None:
    text = main_source.read_text(encoding="utf-8")
    if INSTRUMENTATION_MARKER in text:
        raise MatrixError(f"allocator instrumentation already exists in {main_source}")
    main_source.write_text(text.rstrip() + allocator_source(variant), encoding="utf-8")


def runtime_environment(
    base: dict[str, str], *, temporary_dir: pathlib.Path
) -> dict[str, str]:
    return suite_contract.clean_runtime_environment(temporary_dir, source=base)


def allocator_runtime_environment_overrides(variant: str) -> dict[str, str]:
    if variant not in MIMALLOC_VARIANTS:
        return {}
    return {
        **MIMALLOC_COMMON_RUNTIME_OVERRIDES,
        "MIMALLOC_ALLOW_THP": "0" if variant == "mimalloc_no_thp" else "1",
    }


def allocator_runtime_environment(base: dict[str, str], variant: str) -> dict[str, str]:
    env = dict(base)
    env.update(allocator_runtime_environment_overrides(variant))
    return env


def allocator_thp_mode(variant: str) -> str | None:
    if variant == "mimalloc":
        return "mimalloc-thp-explicitly-allowed"
    if variant == "mimalloc_no_thp":
        # mimalloc v3 maps MIMALLOC_ALLOW_THP=0 to Linux PR_SET_THP_DISABLE.
        return "process-wide-pr-set-thp-disable"
    return None


def measurement_command_prefix(
    cpu_list: str | None, numa_node: int | None
) -> list[str]:
    if cpu_list is None and numa_node is None:
        return []
    if not shutil.which("numactl"):
        raise MatrixError("numactl is required for measurement affinity")
    prefix = ["numactl"]
    if cpu_list is not None:
        prefix.append(f"--physcpubind={cpu_list}")
    if numa_node is not None:
        prefix.append(f"--membind={numa_node}")
    return prefix


def tcmalloc_runtime_evidence(
    prefix: pathlib.Path | None = None,
    library: pathlib.Path | None = None,
) -> dict[str, Any]:
    """Authenticate the exact modern google/tcmalloc DSO used by LD_PRELOAD."""

    if prefix is not None:
        lib_dir = prefix.expanduser().resolve(strict=False) / "lib"
    elif library is not None:
        lib_dir = library.expanduser().resolve(strict=False).parent
    else:
        raise MatrixError("missing modern google/tcmalloc prefix")
    try:
        identity = dict(GOOGLE_TCMALLOC.validate_library_dir(lib_dir))
    except (
        OSError,
        RuntimeError,
        ValueError,
        subprocess.SubprocessError,
    ) as exc:
        raise MatrixError(f"modern google/tcmalloc authentication failed: {exc}") from exc
    resolved = pathlib.Path(str(identity["realpath"])).resolve(strict=True)
    if library is not None and library.expanduser().resolve(strict=True) != resolved:
        raise MatrixError(
            "deprecated --tcmalloc-library does not name the authenticated modern DSO"
        )
    return {
        "variant": "tcmalloc",
        "label": "google-tcmalloc-modern-hpaa-adaptive-subrelease",
        "library": str(resolved),
        "library_name": resolved.name,
        "library_size_bytes": resolved.stat().st_size,
        "library_sha256": identity["sha256"],
        "activation": "system-api-ld-preload-with-runtime-identity-guard",
        "identity": identity,
        "runtime_requirements": {
            "revision": GOOGLE_TCMALLOC.UPSTREAM_REVISION,
            "hpaa_active": 1,
            "malloc_provider_is_self": 1,
            "exact_mapped_path": str(resolved),
        },
    }


def gperftools_legacy_runtime_evidence(library: pathlib.Path) -> dict[str, Any]:
    resolved = library.expanduser().resolve()
    if not resolved.is_file():
        raise MatrixError(f"missing legacy gperftools shared library: {resolved}")
    if ".so" not in resolved.name:
        raise MatrixError(f"legacy gperftools runtime must be a shared library: {resolved}")
    evidence: dict[str, Any] = {
        "variant": "gperftools_legacy",
        "label": "gperftools-tcmalloc-full-preload",
        "library": str(resolved),
        "library_name": resolved.name,
        "library_size_bytes": resolved.stat().st_size,
        "library_sha256": sha256_file(resolved),
        "activation": "system-api-ld-preload",
    }
    receipt = resolved.parent.parent / "receipt.json"
    if receipt.is_file():
        try:
            record = json.loads(receipt.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError):
            record = None
        if isinstance(record, dict):
            built = record.get("built_library")
            source = record.get("dependency_source")
            if (
                isinstance(built, dict)
                and built.get("sha256") == evidence["library_sha256"]
            ):
                evidence["soname"] = built.get("soname")
            if isinstance(source, dict):
                evidence["source_version"] = source.get("version")
                evidence["source_archive_sha256"] = source.get("archive_sha256")
                version = source.get("version")
                if isinstance(version, str) and version:
                    evidence["label"] = f"{version}-full-preload"
            configure = record.get("configure")
            if isinstance(configure, dict):
                evidence["configure_flags"] = configure.get("flags")
    return evidence


def allocator_runtime_prefix(
    variant: str, tcmalloc_runtime: dict[str, Any] | None
) -> list[str]:
    runtime_overrides = allocator_runtime_environment_overrides(variant)
    if runtime_overrides:
        return [
            "env",
            *(f"{name}={value}" for name, value in runtime_overrides.items()),
        ]
    if variant not in {"tcmalloc", "gperftools_legacy"}:
        return []
    if not isinstance(tcmalloc_runtime, dict):
        raise MatrixError("missing TCMalloc runtime evidence")
    library = str(tcmalloc_runtime.get("library", ""))
    if not library:
        raise MatrixError("missing TCMalloc preload library path")
    return [
        "env",
        "-u",
        "HEAPPROFILE",
        "-u",
        "CPUPROFILE",
        "-u",
        "MALLOCSTATS",
        f"LD_PRELOAD={library}",
    ]


def mimalloc_no_thp_build_record(mimalloc_build: dict[str, Any]) -> dict[str, Any]:
    if mimalloc_build.get("variant") != "mimalloc" or not mimalloc_build.get("success"):
        raise MatrixError("mimalloc_no_thp requires a successful mimalloc build")
    return {
        **mimalloc_build,
        "variant": "mimalloc_no_thp",
        "base_build_variant": "mimalloc",
        "runtime_environment_overrides": allocator_runtime_environment_overrides(
            "mimalloc_no_thp"
        ),
        "thp_mode": allocator_thp_mode("mimalloc_no_thp"),
    }


def tcmalloc_build_record(
    system_build: dict[str, Any], tcmalloc_runtime: dict[str, Any]
) -> dict[str, Any]:
    if system_build.get("variant") != "system" or not system_build.get("success"):
        raise MatrixError("TCMalloc requires a successful System allocator build")
    variant = str(tcmalloc_runtime.get("variant") or "")
    if variant not in {"tcmalloc", "gperftools_legacy"}:
        raise MatrixError(f"unknown TCMalloc runtime variant: {variant}")
    return {
        **system_build,
        "variant": variant,
        "base_build_variant": "system",
        "original_allocator": "system",
        "allocator_route": "system-api-ld-preload",
        "tcmalloc_runtime": dict(tcmalloc_runtime),
    }


def prove_tcmalloc_preload(
    binary: pathlib.Path,
    tcmalloc_runtime: dict[str, Any],
    *,
    timeout: int,
    temporary_dir: pathlib.Path | None = None,
) -> dict[str, Any]:
    binary = binary.expanduser().resolve(strict=True)
    if not binary.is_file() or not os.access(binary, os.X_OK):
        raise MatrixError(f"TCMalloc target binary is not executable: {binary}")
    library = pathlib.Path(str(tcmalloc_runtime["library"])).resolve()
    runtime_tmp = temporary_dir or (
        pathlib.Path(tempfile.gettempdir()) / "unialloc-tcmalloc-runtime-proof"
    )
    runtime_overrides = {"LD_PRELOAD": str(library)}
    modern = tcmalloc_runtime.get("variant") == "tcmalloc"
    compile_record: dict[str, Any] | None = None
    if modern:
        runtime_overrides["UNIALLOC_GOOGLE_TCMALLOC_LIBRARY"] = str(library)
        runtime_overrides["UNIALLOC_GOOGLE_TCMALLOC_REVISION"] = (
            GOOGLE_TCMALLOC.UPSTREAM_REVISION
        )
        compiler = shutil.which("cc")
        if compiler is None:
            raise MatrixError("cc is required for the google/tcmalloc runtime proof")
        runtime_probe = r'''
#define _GNU_SOURCE
#include <dlfcn.h>
#include <limits.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
typedef const char* (*revision_fn)(void);
typedef int (*check_fn)(void);
int main(void) {
  const char* expected = getenv("UNIALLOC_GOOGLE_TCMALLOC_LIBRARY");
  const char* expected_revision = getenv("UNIALLOC_GOOGLE_TCMALLOC_REVISION");
  Dl_info provider = {0};
  char provider_real[PATH_MAX] = {0};
  char expected_real[PATH_MAX] = {0};
  void* active_malloc = dlsym(RTLD_DEFAULT, "malloc");
  void* handle = expected ? dlopen(expected, RTLD_NOW | RTLD_NOLOAD) : NULL;
  revision_fn revision = handle ? (revision_fn)dlsym(handle, "unialloc_google_tcmalloc_revision") : NULL;
  check_fn hpaa = handle ? (check_fn)dlsym(handle, "unialloc_google_tcmalloc_hpaa_active") : NULL;
  check_fn provider_check = handle ? (check_fn)dlsym(handle, "unialloc_google_tcmalloc_malloc_provider_is_self") : NULL;
  int mapped = active_malloc && dladdr(active_malloc, &provider) && provider.dli_fname &&
      realpath(provider.dli_fname, provider_real) && expected && realpath(expected, expected_real) &&
      strcmp(provider_real, expected_real) == 0;
  const char* actual_revision = revision ? revision() : "";
  int hpaa_active = hpaa ? hpaa() : 0;
  int provider_is_self = provider_check ? provider_check() : 0;
  printf("{\"revision\":\"%s\",\"hpaa_active\":%d,\"malloc_provider_is_self\":%d,\"exact_library_mapped\":%s}\n",
         actual_revision, hpaa_active, provider_is_self, mapped ? "true" : "false");
  return mapped && hpaa_active == 1 && provider_is_self == 1 && expected_revision &&
         strcmp(actual_revision, expected_revision) == 0 ? 0 : 86;
}
'''
        with tempfile.TemporaryDirectory(prefix="unialloc-google-tcmalloc-probe-") as tmp:
            probe_source = pathlib.Path(tmp) / "probe.c"
            probe_binary = pathlib.Path(tmp) / "probe"
            probe_source.write_text(runtime_probe, encoding="utf-8")
            env = suite_contract.clean_runtime_environment(
                runtime_tmp, overrides=runtime_overrides
            )
            compile_env = dict(env)
            compile_env.pop("LD_PRELOAD", None)
            compiled = execute(
                [compiler, str(probe_source), "-ldl", "-o", str(probe_binary)],
                cwd=pathlib.Path(tmp),
                env=compile_env,
                timeout=timeout,
            )
            compile_record = {
                "command": compiled["command"],
                "exit_code": compiled["exit_code"],
                "timed_out": compiled["timed_out"],
                "stdout_sha256": sha256_bytes(compiled["stdout"]),
                "stderr_sha256": sha256_bytes(compiled["stderr"]),
            }
            if compiled["exit_code"] != 0 or compiled["timed_out"]:
                raise MatrixError(
                    f"google/tcmalloc runtime proof compilation failed: {compile_record}"
                )
            result = execute(
                [probe_binary],
                cwd=pathlib.Path(tmp),
                env=env,
                timeout=timeout,
            )
    else:
        env = suite_contract.clean_runtime_environment(
            runtime_tmp, overrides=runtime_overrides
        )
        dynamic_linker = pathlib.Path("/lib64/ld-linux-x86-64.so.2")
        if not dynamic_linker.is_file():
            raise MatrixError(
                f"missing dynamic linker for legacy preload proof: {dynamic_linker}"
            )
        result = execute(
            [dynamic_linker, "--list", binary],
            cwd=binary.parent,
            env=env,
            timeout=timeout,
        )
    stdout = result["stdout"].decode("utf-8", errors="replace")
    runtime_identity: dict[str, Any] | None = None
    if modern:
        try:
            runtime_identity = json.loads(stdout.splitlines()[-1])
        except (IndexError, json.JSONDecodeError):
            runtime_identity = None
    success = result["exit_code"] == 0 and not result["timed_out"]
    if modern:
        success = success and runtime_identity == {
            "revision": GOOGLE_TCMALLOC.UPSTREAM_REVISION,
            "hpaa_active": 1,
            "malloc_provider_is_self": 1,
            "exact_library_mapped": True,
        }
    else:
        success = success and str(library) in stdout
    proof = {
        "success": success,
        "command": result["command"],
        "exit_code": result["exit_code"],
        "timed_out": result["timed_out"],
        "stdout_sha256": sha256_bytes(result["stdout"]),
        "stderr_sha256": sha256_bytes(result["stderr"]),
        "resolved_library": str(library),
        "target_binary": str(binary),
        "target_binary_sha256": sha256_file(binary),
        "target_runtime_guard": (
            "every measured target process must emit the DSO constructor identity marker"
            if modern
            else "dynamic linker maps the explicit legacy preload library"
        ),
        "artifact_preflight_only": modern,
        "runtime_identity": runtime_identity,
        "runtime_probe_compile": compile_record,
        "runtime_environment": suite_contract.runtime_environment_record(env),
        "rseq_policy": suite_contract.PRODUCTION_RSEQ_POLICY,
    }
    if not success:
        raise MatrixError(f"TCMalloc preload proof failed for {binary}: {proof}")
    return proof


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
            "LD_LIBRARY_PATH": library_dir
            + ((os.pathsep + existing) if existing else ""),
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
    dependency_dir = pathlib.Path(str(force_load.get("dependency_dir", ""))).resolve()
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


def exact_identity_marker_count(stderr: bytes, marker: str) -> int:
    lines = stderr.decode("utf-8", errors="replace").splitlines(keepends=True)
    return sum(line == marker for line in lines)


def google_tcmalloc_target_identity_evidence(
    variant: str, stderr: bytes
) -> dict[str, Any]:
    count = exact_identity_marker_count(
        stderr, GOOGLE_TCMALLOC_RUNTIME_IDENTITY_MARKER
    )
    expected = 1 if variant == "tcmalloc" else 0
    return {
        "google_tcmalloc_identity_marker_count": count,
        "google_tcmalloc_expected_identity_marker_count": expected,
        "google_tcmalloc_target_identity_verified": count == expected,
    }


def parse_type_stats_json(stderr: str) -> list[dict[str, Any]]:
    parsed: list[dict[str, Any]] = []
    for line in stderr.splitlines():
        if not line.startswith(TYPE_STATS_PREFIX):
            continue
        try:
            candidate = json.loads(line[len(TYPE_STATS_PREFIX) :])
        except json.JSONDecodeError:
            continue
        if isinstance(candidate, dict):
            parsed.append(candidate)
    return parsed


def parse_depot_stats_json(stderr: str) -> dict[str, Any] | None:
    parsed: dict[str, Any] | None = None
    for line in stderr.splitlines():
        if not line.startswith(DEPOT_STATS_PREFIX):
            continue
        try:
            candidate = json.loads(line[len(DEPOT_STATS_PREFIX) :])
        except json.JSONDecodeError:
            continue
        if isinstance(candidate, dict):
            parsed = candidate
    return parsed


def validate_depot_stats(stats: dict[str, Any] | None, label: str) -> None:
    if not isinstance(stats, dict):
        raise MatrixError(f"{label} emitted no complete depot stats JSON")
    missing = sorted(set(DEPOT_STATS_REQUIRED_FIELDS) - stats.keys())
    if missing:
        raise MatrixError(
            f"{label} emitted incomplete depot stats JSON; missing: "
            + ",".join(missing)
        )
    invalid = sorted(
        field
        for field in DEPOT_STATS_REQUIRED_FIELDS
        if type(stats[field]) is not int or stats[field] < 0
    )
    if invalid:
        raise MatrixError(
            f"{label} emitted invalid depot stats JSON fields: "
            + ",".join(invalid)
        )


def summarize_audits(audit_dir: pathlib.Path) -> dict[str, Any]:
    files = sorted(audit_dir.glob("*.json")) if audit_dir.exists() else []
    semantic_rewrites = 0
    scope_rewrites = 0
    drop_rewrites = 0
    semantic_ownership_rewrites = 0
    semantic_deallocation_like_rewrites = 0
    direct_allocator_rewrites = 0
    direct_layout_allocator_rewrites = 0
    unresolved_files = 0
    claim_grade_files = 0
    crate_names: set[str] = set()
    for path in files:
        try:
            record = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        summary = (
            record.get("summary") if isinstance(record.get("summary"), dict) else {}
        )
        compiler = (
            record.get("compiler_pass")
            if isinstance(record.get("compiler_pass"), dict)
            else {}
        )
        rustc_args = record.get("rustc_args")
        if isinstance(rustc_args, list):
            for index, value in enumerate(rustc_args):
                if value == "--crate-name" and index + 1 < len(rustc_args):
                    crate_names.add(str(rustc_args[index + 1]).replace("-", "_"))
                    break
                if isinstance(value, str) and value.startswith("--crate-name="):
                    crate_names.add(value.split("=", 1)[1].replace("-", "_"))
                    break
        direct_allocator = int(
            summary.get(
                "rewrite_applied_count", compiler.get("rewrite_applied_count", 0)
            )
            or 0
        )
        direct_layout = int(
            summary.get(
                "direct_layout_allocator_rewrite_applied_count",
                compiler.get("direct_layout_allocator_rewrite_applied_count", 0),
            )
            or 0
        )
        scope = int(
            summary.get(
                "semantic_scope_rewrite_applied_count",
                compiler.get("semantic_scope_rewrite_applied_count", 0),
            )
            or 0
        )
        drops = int(
            summary.get(
                "semantic_scope_drop_rewrite_applied_count",
                compiler.get("semantic_scope_drop_rewrite_applied_count", 0),
            )
            or 0
        )
        ownership = int(
            summary.get(
                "semantic_ownership_transfer_rewrite_applied_count",
                compiler.get("semantic_ownership_transfer_rewrite_applied_count", 0),
            )
            or 0
        )
        deallocation_like = int(
            summary.get(
                "semantic_scope_deallocation_like_rewrite_applied_count",
                compiler.get(
                    "semantic_scope_deallocation_like_rewrite_applied_count", 0
                ),
            )
            or 0
        )
        direct_allocator_rewrites += direct_allocator
        direct_layout_allocator_rewrites += direct_layout
        scope_rewrites += scope
        drop_rewrites += drops
        semantic_ownership_rewrites += ownership
        semantic_deallocation_like_rewrites += deallocation_like
        semantic_rewrites += scope + drops
        resolution = str(
            summary.get(
                "semantic_scope_replacement_resolution_status",
                compiler.get("semantic_scope_replacement_resolution_status", ""),
            )
        )
        if "not_resolved" in resolution:
            unresolved_files += 1
        if bool(summary.get("claim_grade", compiler.get("claim_grade", False))):
            claim_grade_files += 1
    return {
        "audit_file_count": len(files),
        "direct_allocator_rewrites_applied": direct_allocator_rewrites,
        "direct_layout_allocator_rewrites_applied": direct_layout_allocator_rewrites,
        "semantic_scope_rewrites_applied": scope_rewrites,
        "semantic_drop_rewrites_applied": drop_rewrites,
        "semantic_ownership_transfer_rewrites_applied": semantic_ownership_rewrites,
        "semantic_deallocation_like_rewrites_applied": semantic_deallocation_like_rewrites,
        "semantic_rewrites_applied": semantic_rewrites,
        "total_compiler_rewrites_applied": (
            direct_allocator_rewrites
            + scope_rewrites
            + drop_rewrites
            + semantic_ownership_rewrites
        ),
        "unresolved_file_count": unresolved_files,
        "claim_grade_file_count": claim_grade_files,
        "crate_names": sorted(crate_names),
        "audit_sha256": sha256_bytes(
            b"".join(path.name.encode() + b"\0" + path.read_bytes() for path in files)
        ),
    }


def validate_typeiso_audit_presence(
    audit: dict[str, Any], target_crates: Sequence[str] = ()
) -> None:
    if int(audit.get("audit_file_count", 0)) <= 0:
        raise MatrixError("Type Isolation build emitted no compiler audit files")
    expected = {name.replace("-", "_") for name in target_crates}
    observed = {str(name).replace("-", "_") for name in audit.get("crate_names", [])}
    missing = sorted(expected - observed)
    if missing:
        raise MatrixError(
            "Type Isolation build emitted no audit for selected crates: "
            + ",".join(missing)
        )


def validate_typeiso_audits(
    audit: dict[str, Any], target_crates: Sequence[str] = ()
) -> None:
    validate_typeiso_audit_presence(audit, target_crates)
    if int(audit.get("semantic_rewrites_applied", 0)) <= 0:
        raise MatrixError("Type Isolation build applied zero semantic rewrites")


def validate_typeiso_coverage(
    audit: dict[str, Any], stats: dict[str, Any] | None
) -> None:
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
    if (
        not path.exists()
        or path.read_text(encoding="utf-8") != FORCE_LOAD_WRAPPER_SOURCE
    ):
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


def unialloc_configuration_for_variant(
    variant: str,
) -> tuple[tuple[str, ...], bool]:
    if variant == "unialloc":
        return (), True
    if variant in UNIALLOC_FEATURE_VARIANTS:
        return UNIALLOC_FEATURE_VARIANTS[variant], False
    if variant in {"typed_plain", "typeiso_perf"}:
        return ("type_isolation",), True
    if variant == "typeiso_coverage":
        return ("stats", "type_isolation"), True
    raise MatrixError(f"UniAlloc configuration requested for {variant}")


def unialloc_build_evidence(variant: str) -> dict[str, Any]:
    if variant not in UNIALLOC_VARIANTS:
        return {
            "unialloc_features": None,
            "default_features_enabled": None,
        }
    features, default_features_enabled = unialloc_configuration_for_variant(variant)
    return {
        "unialloc_features": list(features),
        "default_features_enabled": default_features_enabled,
    }


def dependency_for_variant(variant: str) -> tuple[str, tuple[str, ...]]:
    if variant == "jemalloc":
        return 'jemallocator = "=0.5.4"', ()
    if variant in MIMALLOC_VARIANTS:
        return 'mimalloc = { version = "=0.1.25", default-features = false }', ()
    if variant in UNIALLOC_VARIANTS:
        features, default_features_enabled = unialloc_configuration_for_variant(variant)
        return (
            cargo_path_dependency(
                ROOT / "unialloc",
                features,
                default_features_enabled=default_features_enabled,
            ),
            features,
        )
    raise MatrixError(f"no dependency is required for variant {variant}")


def force_load_features_for_variant(variant: str) -> tuple[str, ...]:
    if variant not in TYPEISO_VARIANTS:
        raise MatrixError(
            f"force-load rlib requested for unsupported variant: {variant}"
        )
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
        result["exit_code"] == 0 and not result["timed_out"] and len(candidates) == 1
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
    return variant in UNIALLOC_VARIANTS


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
        if not isinstance(recorded_force_load, dict) or not isinstance(
            force_load, dict
        ):
            return False
        if recorded_force_load.get("rlib_sha256") != force_load.get(
            "rlib_sha256"
        ) or recorded_force_load.get("features") != force_load.get("features"):
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
    compiler_wrapper_sha256 = (
        sha256_file(wrapper) if variant in TYPEISO_VARIANTS else None
    )
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
    if variant not in {"native", "system"} and not uses_original_jemalloc(
        spec, variant
    ):
        root_manifest = worktree / "Cargo.toml"
        if variant in TYPEISO_VARIANTS:
            inject_allocator_main(worktree / spec.main_source, variant)
        else:
            dependency, _features = dependency_for_variant(variant)
            add_dependency(root_manifest, dependency)
            if variant in MIMALLOC_VARIANTS:
                add_dependency(root_manifest, MIMALLOC_SYS_DEPENDENCY)
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
    success = (
        result["exit_code"] == 0 and built_binary.exists() and not result["timed_out"]
    )
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
        **unialloc_build_evidence(variant),
        "runtime_environment_overrides": allocator_runtime_environment_overrides(
            variant
        ),
        "thp_mode": allocator_thp_mode(variant),
        "original_allocator": (
            "system"
            if variant == "system"
            else (
                spec.original_allocator
                if variant == "native" or uses_original_jemalloc(spec, variant)
                else None
            )
        ),
        "allocator_route": (
            "rust-system"
            if variant == "system"
            else (
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
        "target_crates": list(spec.target_crates)
        if variant in TYPEISO_VARIANTS
        else [],
        "force_load_lock_packages": (
            list(spec.force_load_lock_packages) if variant in TYPEISO_VARIANTS else []
        ),
        "stats_enabled": variant == "typeiso_coverage",
        "performance_eligible": variant != "typeiso_coverage",
        "actual_mir_rewrite": variant in TYPEISO_VARIANTS,
        "compatibility": compatibility,
        "lowering_policy_flags": (
            0
            if variant == "typed_plain"
            else (1 if variant in TYPEISO_VARIANTS else None)
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
        if variant in MIMALLOC_VARIANTS:
            record["allocator_provenance"] = mimalloc_build_provenance(
                worktree / "Cargo.lock", target_dir
            )
    record_path.write_text(
        json.dumps(record, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
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
        relative = (
            pathlib.Path(f"bucket-{index % bucket_count:03d}") / f"file-{index:06d}.txt"
        )
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
    path_repetitions: int = 1,
    oxipng_threads: int = 1,
) -> tuple[list[str], pathlib.Path, pathlib.Path | None]:
    if path_repetitions < 1:
        raise MatrixError("path repetitions must be positive")
    if oxipng_threads < 1:
        raise MatrixError("Oxipng thread count must be positive")
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
                *(["."] * path_repetitions),
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
                *(["."] * path_repetitions),
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
            str(oxipng_threads),
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
    command_prefix: Sequence[str | os.PathLike[str]] = (),
) -> dict[str, Any]:
    if not time_binary.exists():
        raise MatrixError(f"GNU time binary is missing: {time_binary}")
    rss_path.parent.mkdir(parents=True, exist_ok=True)
    measured_command = [*command_prefix, *command]
    result = execute(
        [time_binary, "-f", GNU_TIME_FORMAT, "-o", rss_path, "--", *measured_command],
        cwd=cwd,
        env=env,
        timeout=timeout,
    )
    try:
        metrics = parse_gnu_time_metrics(rss_path.read_text(encoding="utf-8"))
    except OSError as error:
        raise MatrixError(f"unable to read GNU time output: {rss_path}") from error
    return {
        "command": [str(value) for value in command],
        "measured_command": [str(value) for value in measured_command],
        "exit_code": result["exit_code"],
        "timed_out": result["timed_out"],
        "wall_seconds": result["wall_seconds"],
        **metrics,
        "stdout": result["stdout"],
        "stderr": result["stderr"],
        "stdout_sha256": sha256_bytes(result["stdout"]),
        "stderr_sha256": sha256_bytes(result["stderr"]),
    }


def parse_gnu_time_metrics(text: str) -> dict[str, Any]:
    record = next(
        (
            line
            for line in reversed(text.splitlines())
            if line.startswith(GNU_TIME_PREFIX + "\t")
        ),
        None,
    )
    if record is None:
        raise MatrixError("GNU time emitted no complete metric record")
    fields = record.split("\t")
    if len(fields) != 10:
        raise MatrixError(
            f"GNU time metric record has {len(fields) - 1} fields, expected 9"
        )
    try:
        return {
            "user_cpu_seconds": float(fields[1]),
            "system_cpu_seconds": float(fields[2]),
            "cpu_percent": float(fields[3].removesuffix("%")),
            "peak_rss_kib": int(fields[4]),
            "major_page_faults": int(fields[5]),
            "minor_page_faults": int(fields[6]),
            "involuntary_context_switches": int(fields[7]),
            "voluntary_context_switches": int(fields[8]),
            "gnu_time_exit_status": int(fields[9]),
        }
    except ValueError as error:
        raise MatrixError(f"invalid GNU time metric record: {record}") from error


def persist_result(path: pathlib.Path, record: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(record, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    temporary.replace(path)


def median_absolute_deviation(values: Sequence[float]) -> float:
    center = statistics.median(values)
    return statistics.median(abs(value - center) for value in values)


def throughput_value(
    work_amount: int | float, work_unit: str, wall_seconds: float
) -> tuple[float, str]:
    if wall_seconds <= 0:
        raise MatrixError("wall time must be positive for throughput")
    if work_unit == "bytes":
        return float(work_amount) / (1024 * 1024) / wall_seconds, "MiB/s"
    return float(work_amount) / wall_seconds, f"{work_unit}/s"


def paired_median_ratio(
    rows: Sequence[dict[str, Any]],
    reference_rows: Sequence[dict[str, Any]],
    field: str,
) -> float | None:
    reference = {
        row.get("measurement_index"): float(row[field]) for row in reference_rows
    }
    ratios = [
        float(row[field]) / reference[row.get("measurement_index")]
        for row in rows
        if row.get("measurement_index") in reference
        and reference[row.get("measurement_index")] != 0
    ]
    return statistics.median(ratios) if ratios else None


def summarize_measurements(
    measurements: Sequence[dict[str, Any]],
) -> list[dict[str, Any]]:
    summaries: list[dict[str, Any]] = []
    keys = sorted(
        {(row["app"], row["variant"]) for row in measurements if not row["warmup"]}
    )
    for app, variant in keys:
        rows = [
            row
            for row in measurements
            if row["app"] == app and row["variant"] == variant and not row["warmup"]
        ]
        wall_values = [float(row["wall_seconds"]) for row in rows]
        rss_values = [float(row["peak_rss_kib"]) for row in rows]
        total_cpu_values = [
            float(row["user_cpu_seconds"]) + float(row["system_cpu_seconds"])
            for row in rows
        ]
        throughput_values = [
            throughput_value(row["work_amount"], row["work_unit"], row["wall_seconds"])[
                0
            ]
            for row in rows
        ]
        _, throughput_unit = throughput_value(
            rows[0]["work_amount"], rows[0]["work_unit"], rows[0]["wall_seconds"]
        )
        summaries.append(
            {
                "app": app,
                "variant": variant,
                "sample_count": len(rows),
                "median_wall_seconds": statistics.median(wall_values),
                "wall_mad_seconds": median_absolute_deviation(wall_values),
                "min_wall_seconds": min(wall_values),
                "max_wall_seconds": max(wall_values),
                "median_user_cpu_seconds": statistics.median(
                    float(row["user_cpu_seconds"]) for row in rows
                ),
                "median_system_cpu_seconds": statistics.median(
                    float(row["system_cpu_seconds"]) for row in rows
                ),
                "median_total_cpu_seconds": statistics.median(total_cpu_values),
                "median_cpu_percent": statistics.median(
                    float(row["cpu_percent"]) for row in rows
                ),
                "median_peak_rss_kib": statistics.median(rss_values),
                "min_peak_rss_kib": min(rss_values),
                "max_peak_rss_kib": max(rss_values),
                "rss_mad_kib": median_absolute_deviation(rss_values),
                "median_major_page_faults": statistics.median(
                    int(row["major_page_faults"]) for row in rows
                ),
                "median_minor_page_faults": statistics.median(
                    int(row["minor_page_faults"]) for row in rows
                ),
                "median_involuntary_context_switches": statistics.median(
                    int(row["involuntary_context_switches"]) for row in rows
                ),
                "median_voluntary_context_switches": statistics.median(
                    int(row["voluntary_context_switches"]) for row in rows
                ),
                "median_throughput_per_second": statistics.median(throughput_values),
                "throughput_unit": throughput_unit,
                "work_amount": rows[0]["work_amount"],
                "work_unit": rows[0]["work_unit"],
                "output_sha256": rows[0]["output_sha256"],
                "stats": rows[-1].get("stats"),
                "runtime_environment_overrides": rows[0].get(
                    "runtime_environment_overrides", {}
                ),
                "thp_mode": rows[0].get("thp_mode"),
                "performance_eligible": variant != "typeiso_coverage",
            }
        )
    by_app = {
        name: [row for row in summaries if row["app"] == name] for name in APP_SPECS
    }
    for app, summary_rows in by_app.items():
        baseline = next(
            (row for row in summary_rows if row["variant"] == "system"),
            next((row for row in summary_rows if row["variant"] == "native"), None),
        )
        typed_plain = next(
            (row for row in summary_rows if row["variant"] == "typed_plain"), None
        )
        measurement_rows = [
            row for row in measurements if row["app"] == app and not row["warmup"]
        ]
        rows_by_variant = {
            variant: [row for row in measurement_rows if row["variant"] == variant]
            for variant in {row["variant"] for row in measurement_rows}
        }
        for row in summary_rows:
            if row["performance_eligible"]:
                if baseline is not None:
                    baseline_name = str(baseline["variant"])
                    row["baseline_variant"] = baseline_name
                    row[f"wall_ratio_vs_{baseline_name}"] = (
                        row["median_wall_seconds"] / baseline["median_wall_seconds"]
                    )
                    row[f"rss_ratio_vs_{baseline_name}"] = (
                        row["median_peak_rss_kib"] / baseline["median_peak_rss_kib"]
                    )
                if typed_plain is not None:
                    row["wall_ratio_vs_typed_plain"] = (
                        row["median_wall_seconds"] / typed_plain["median_wall_seconds"]
                    )
                    row["rss_ratio_vs_typed_plain"] = (
                        row["median_peak_rss_kib"] / typed_plain["median_peak_rss_kib"]
                    )
                current_rows = rows_by_variant.get(str(row["variant"]), [])
                for comparator in (
                    "system",
                    "native",
                    "mimalloc",
                    "tcmalloc",
                    "gperftools_legacy",
                    "unialloc",
                    "typed_plain",
                ):
                    reference_rows = rows_by_variant.get(comparator)
                    if comparator == row["variant"] or not reference_rows:
                        continue
                    wall_ratio = paired_median_ratio(
                        current_rows, reference_rows, "wall_seconds"
                    )
                    rss_ratio = paired_median_ratio(
                        current_rows, reference_rows, "peak_rss_kib"
                    )
                    if wall_ratio is not None:
                        row[f"paired_wall_ratio_vs_{comparator}"] = wall_ratio
                    if rss_ratio is not None:
                        row[f"paired_rss_ratio_vs_{comparator}"] = rss_ratio
    return summaries


def path_repetitions_for_app(spec: AppSpec, args: argparse.Namespace) -> int:
    if spec.name == "ripgrep":
        return args.ripgrep_path_repetitions
    if spec.name == "fd":
        return args.fd_path_repetitions
    return 1


def workload_evidence(
    spec: AppSpec,
    result: dict[str, Any],
    source_checkout: pathlib.Path,
    args: argparse.Namespace,
) -> dict[str, Any]:
    repetitions = path_repetitions_for_app(spec, args)
    if spec.name == "ripgrep":
        corpus = result["corpus"]
        total_bytes = (
            int(corpus["file_count"]) * int(corpus["file_bytes"]) * repetitions
        )
        return {
            "description": "single-thread regex search over repeated deterministic paths",
            "path_repetitions": repetitions,
            "work_amount": total_bytes,
            "work_unit": "bytes",
        }
    if spec.name == "fd":
        tree = result["fd_tree"]
        return {
            "description": "single-thread metadata traversal over repeated deterministic paths",
            "path_repetitions": repetitions,
            "work_amount": int(tree["file_count"]) * repetitions,
            "work_unit": "files",
        }
    input_name = "issue-141.png" if args.quick else "issue-167.png"
    input_path = source_checkout / "tests" / "files" / input_name
    return {
        "description": "lossless PNG optimization",
        "threads": args.oxipng_threads,
        "input": str(input_path.resolve()),
        "input_sha256": sha256_file(input_path),
        "work_amount": input_path.stat().st_size,
        "work_unit": "bytes",
    }


def host_snapshot() -> dict[str, Any]:
    model_name = None
    cpuinfo = pathlib.Path("/proc/cpuinfo")
    if cpuinfo.is_file():
        match = re.search(
            r"(?m)^model name\s*:\s*(.+)$", cpuinfo.read_text(encoding="utf-8")
        )
        model_name = match.group(1).strip() if match else None
    load = os.getloadavg()
    return {
        "kernel": os.uname().release,
        "machine": os.uname().machine,
        "cpu_model": model_name,
        "logical_cpu_count": os.cpu_count(),
        "process_affinity": sorted(os.sched_getaffinity(0)),
        "load_average": {
            "one_minute": load[0],
            "five_minutes": load[1],
            "fifteen_minutes": load[2],
        },
    }


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    raw_dir = args.raw_dir.resolve()
    raw_dir.mkdir(parents=True, exist_ok=True)
    runtime_env = runtime_environment(
        os.environ.copy(), temporary_dir=raw_dir / "tmp" / "runtime"
    )
    wrapper = ensure_wrapper(
        (args.wrapper or (raw_dir / "tools" / "unialloc-rustc-wrapper")).resolve(),
        args.toolchain,
        args.build_timeout,
    )
    sysroot = rustc_sysroot(args.toolchain)
    warmups, repetitions = measurement_counts(args)
    affinity_prefix = measurement_command_prefix(args.cpu_list, args.numa_node)
    preload_runtimes: dict[str, dict[str, Any]] = {}
    if "tcmalloc" in args.variants:
        preload_runtimes["tcmalloc"] = tcmalloc_runtime_evidence(
            args.google_tcmalloc_prefix,
            args.tcmalloc_library,
        )
    if "gperftools_legacy" in args.variants:
        preload_runtimes["gperftools_legacy"] = gperftools_legacy_runtime_evidence(
            args.gperftools_legacy_library
        )
    result_path = raw_dir / "results.json"
    result: dict[str, Any] = {
        "schema_version": 2,
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
        "host": host_snapshot(),
        "measurement_affinity": {
            "cpu_list": args.cpu_list,
            "numa_node": args.numa_node,
            "command_prefix": affinity_prefix,
        },
        "tcmalloc_runtime": preload_runtimes.get("tcmalloc"),
        "gperftools_legacy_runtime": preload_runtimes.get("gperftools_legacy"),
        "allocator_runtime_configurations": {
            variant: {
                "environment_overrides": allocator_runtime_environment_overrides(
                    variant
                ),
                "thp_mode": allocator_thp_mode(variant),
            }
            for variant in args.variants
        },
        "runtime_environment": suite_contract.runtime_environment_record(runtime_env),
        "glibc_rseq_mode": suite_contract.PRODUCTION_RSEQ_POLICY,
        "glibc_tunable": None,
        "performance_stats_enabled": False,
        "coverage_variant_performance_eligible": False,
        "run_output_retained": not args.discard_run_output,
        "checkouts": {},
        "workloads": {},
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
    for app in args.apps:
        result["workloads"][app] = workload_evidence(
            APP_SPECS[app], result, checkouts[app], args
        )
    persist_result(result_path, result)

    builds: dict[tuple[str, str], dict[str, Any]] = {}
    base_builds: dict[tuple[str, str], dict[str, Any]] = {}
    for app in args.apps:
        for variant in args.variants:
            base_variant = {
                "tcmalloc": "system",
                "gperftools_legacy": "system",
                "mimalloc_no_thp": "mimalloc",
            }.get(variant, variant)
            base_key = (app, base_variant)
            if base_key not in base_builds:
                base_builds[base_key] = build_variant(
                    APP_SPECS[app],
                    base_variant,
                    source_checkout=checkouts[app],
                    raw_dir=raw_dir,
                    wrapper=wrapper,
                    sysroot=sysroot,
                    toolchain=args.toolchain,
                    jobs=args.jobs,
                    timeout=args.build_timeout,
                    reuse_binary=args.reuse_binaries,
                )
            build = base_builds[base_key]
            if variant in {"tcmalloc", "gperftools_legacy"}:
                runtime = preload_runtimes[variant]
                build = tcmalloc_build_record(build, runtime)
                build["preload_proof"] = prove_tcmalloc_preload(
                    pathlib.Path(build["binary"]),
                    runtime,
                    timeout=args.run_timeout,
                    temporary_dir=raw_dir / "tmp" / "tcmalloc-preload-proof",
                )
                persist_result(
                    raw_dir / "binaries" / app / variant / "build.json", build
                )
            elif variant == "mimalloc_no_thp":
                build = mimalloc_no_thp_build_record(build)
                persist_result(
                    raw_dir / "binaries" / app / "mimalloc_no_thp" / "build.json",
                    build,
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
    with suite_contract.primary_measurement_lock(
        runner=pathlib.Path(__file__).name,
        raw_root=raw_dir,
        phase="warmup-and-measured",
    ) as measurement_lock:
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
                        path_repetitions=path_repetitions_for_app(spec, args),
                        oxipng_threads=args.oxipng_threads,
                    )
                    command_prefix = [
                        *affinity_prefix,
                        *allocator_runtime_prefix(
                            variant, preload_runtimes.get(variant)
                        ),
                    ]
                    child_environment = allocator_runtime_environment(
                        runtime_env, variant
                    )
                    measured = run_measured(
                        command,
                        cwd=cwd,
                        env=child_environment,
                        time_binary=args.time_binary,
                        rss_path=run_dir / "peak-rss-kib.txt",
                        timeout=args.run_timeout,
                        command_prefix=command_prefix,
                    )
                    stdout = measured.pop("stdout")
                    stderr = measured.pop("stderr")
                    if not args.discard_run_output:
                        (run_dir / "stdout.bin").write_bytes(stdout)
                        (run_dir / "stderr.bin").write_bytes(stderr)
                    if measured["exit_code"] != 0 or measured["timed_out"]:
                        raise MatrixError(
                            f"workload failed for {app}/{variant}: {measured}"
                        )
                    target_identity = google_tcmalloc_target_identity_evidence(
                        variant, stderr
                    )
                    measured.update(target_identity)
                    if target_identity["google_tcmalloc_target_identity_verified"] is not True:
                        raise MatrixError(
                            f"workload {app}/{variant} emitted "
                            f"{target_identity['google_tcmalloc_identity_marker_count']} "
                            "modern google/tcmalloc identity markers; expected "
                            f"{target_identity['google_tcmalloc_expected_identity_marker_count']}"
                        )
                    output_sha = (
                        sha256_file(output_file)
                        if output_file is not None
                        else measured["stdout_sha256"]
                    )
                    stats = parse_stats_json(stderr.decode("utf-8", errors="replace"))
                    depot_stats = parse_depot_stats_json(
                        stderr.decode("utf-8", errors="replace")
                    )
                    if variant == "typeiso_coverage":
                        validate_depot_stats(depot_stats, f"{app}/{variant}")
                    if stats is not None and depot_stats is not None:
                        stats.update(depot_stats)
                    type_stats = parse_type_stats_json(
                        stderr.decode("utf-8", errors="replace")
                    )
                    if variant == "typeiso_coverage":
                        validate_typeiso_coverage(build["audit"], stats)
                    elif stats is not None:
                        raise MatrixError(
                            f"performance variant unexpectedly emitted stats: {app}/{variant}"
                        )
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
                        "type_stats": type_stats,
                        "runtime_environment_overrides": (
                            allocator_runtime_environment_overrides(variant)
                        ),
                        "runtime_environment": (
                            suite_contract.runtime_environment_record(child_environment)
                        ),
                        "rseq_policy": suite_contract.PRODUCTION_RSEQ_POLICY,
                        "thp_mode": allocator_thp_mode(variant),
                        "performance_eligible": variant != "typeiso_coverage",
                        "work_amount": result["workloads"][app]["work_amount"],
                        "work_unit": result["workloads"][app]["work_unit"],
                    }
                    result["measurements"].append(row)
                    persist_result(result_path, result)

    result["measurement_lock"] = dict(measurement_lock)
    result["summaries"] = summarize_measurements(result["measurements"])
    result["success"] = True
    persist_result(result_path, result)
    print(
        json.dumps(
            {
                "success": True,
                "results": str(result_path),
                "summaries": result["summaries"],
            },
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except MatrixError as error:
        print(f"error: {error}", file=sys.stderr)
        raise SystemExit(1)
