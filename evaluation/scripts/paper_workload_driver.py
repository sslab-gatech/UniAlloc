#!/usr/bin/env python3
"""Run one supported paper performance workload cell and emit timing JSON.

This is intentionally narrow: it only drives the in-tree std_bench Collections
row that exists in this repository. Paper macrobenchmarks and allocator wrappers
that are not implemented locally fail loudly instead of emitting synthetic
timing.
"""

from __future__ import annotations

import argparse
import copy
import glob
import hashlib
import importlib.util
import json
import math
import os
import platform
import re
import signal
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

ROOT = Path(__file__).resolve().parents[2]


def _load_google_tcmalloc_support() -> Any:
    """Load the local modern google/tcmalloc authenticator by repository path."""

    try:
        import google_tcmalloc_support as support

        return support
    except ModuleNotFoundError:
        support_path = Path(__file__).resolve().with_name("google_tcmalloc_support.py")
        spec = importlib.util.spec_from_file_location(
            "_unialloc_paper_driver_google_tcmalloc_support", support_path
        )
        if spec is None or spec.loader is None:
            raise ImportError(f"could not load modern google/tcmalloc support from {support_path}")
        support = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(support)
        return support


GOOGLE_TCMALLOC = _load_google_tcmalloc_support()

BENCH_LINE = re.compile(
    r"^test (?P<name>\S+)\s+\.\.\. bench:\s+"
    r"(?P<ns>[0-9][0-9,]*(?:\.[0-9]+)?)\s+ns/iter"
    r"(?:\s+\(\+/-\s+(?P<dev>[0-9][0-9,]*(?:\.[0-9]+)?)\))?"
)

ALLOCATOR_FEATURES = {
    "unialloc": "bench_ourself",
    "jemalloc": "bench_jemalloc",
    "mimalloc": "bench_mimalloc",
    "tcmalloc": "bench_tcmalloc",
    "gperftools_legacy": "bench_gperftools_legacy",
    "snmalloc": "bench_snmalloc",
    "ptmalloc": "bench_ptmalloc",
    "scudo": "bench_scudo",
}

DATASET_VARIANT_FEATURES = {
    "default_performance": None,
    "type_isolation": "type_isolation",
    "metadata_segregation": "metadata_segregation",
    "hugepage_metadata": "hugepage",
    "pac_authentication": "pac",
}

SEMANTIC_VARIANT_FEATURES = {
    "type_isolation",
    "metadata_segregation",
    "hugepage",
    "pac",
}

SEMANTIC_HARNESS_SENTINEL_PREFIXES = (
    "aaa_semantic_auto_metadata_",
    "zzz_semantic_auto_metadata_",
)

GOOGLE_TCMALLOC_PREFIX_ENV = "UNIALLOC_GOOGLE_TCMALLOC_PREFIX"
GOOGLE_TCMALLOC_LEGACY_DIR_ENVS = (
    "UNIALLOC_TCMALLOC_LIB_DIR",
    "TCMALLOC_LIB_DIR",
)
DEFAULT_GOOGLE_TCMALLOC_PREFIX = ROOT / "evaluation/deps/tcmalloc"

CMAKE_BIN_GLOBS = (
    "evaluation/deps/cmake/bin/cmake",
    "evaluation/raw/local-cmake-pip-*/bin/cmake",
    "evaluation/raw/local-cmake-pip-*/cmake/data/bin/cmake",
)

SCUDO_SANITIZER_RUSTFLAGS = "-Zsanitizer=scudo"
SCUDO_DEFAULT_MODE = "auto"
SCUDO_MODES = ("auto", "rust-sanitizer", "ld-preload")
SCUDO_RUNTIME_IDENTITY_MARKER = "unialloc: verified Scudo runtime identity\n"
SCUDO_RUNTIME_LIBRARY_ENVS = (
    "UNIALLOC_SCUDO_RUNTIME_LIBRARY",
    "SCUDO_RUNTIME_LIBRARY",
    "SCUDO_STANDALONE_LIBRARY",
)
SCUDO_SANITIZER_CONFLICTING_ENV_NAMES = (
    *SCUDO_RUNTIME_LIBRARY_ENVS,
    "LD_PRELOAD",
    "CARGO_ENCODED_RUSTFLAGS",
)
SCUDO_RUNTIME_LIBRARY_GLOBS = (
    "/usr/lib/llvm-*/lib/clang/*/lib/linux/libclang_rt.scudo_standalone-*.so",
    "/usr/lib/clang/*/lib/linux/libclang_rt.scudo_standalone-*.so",
    "evaluation/deps/scudo/lib/libclang_rt.scudo_standalone-*.so",
    "evaluation/raw/local-scudo-*/**/libclang_rt.scudo_standalone-*.so",
)
SCUDO_TRUSTED_SYSTEM_RUNTIME_RE = re.compile(
    r"^/usr/lib/(?:llvm-[0-9]+/lib/clang/[0-9.]+|clang/[0-9.]+)/lib/linux/"
    r"libclang_rt\.scudo_standalone-(?P<arch>[A-Za-z0-9_+-]+)\.so$"
)
SCUDO_DPKG_PACKAGE_RE = re.compile(
    r"^(?:libclang-rt-[0-9]+-dev|compiler-rt(?:-[0-9]+)?(?:-dev)?)(?::[A-Za-z0-9_-]+)?$"
)
SCUDO_BEHAVIOR_REQUIRED_PATTERNS = (
    re.compile(r"^Stats: SizeClassAllocator", re.MULTILINE),
    re.compile(r"^Stats: MapAllocator: allocated [1-9][0-9]* times", re.MULTILINE),
)
_SCUDO_RUNTIME_AUTHENTICITY_CACHE: Dict[Tuple[str, int, int, str], Dict[str, Any]] = {}

BENCH_LIST_CACHE_SCHEMA = 1
INTERRUPTED_OUTPUT_TAIL_BYTES = 64 * 1024
INTERRUPTED_CARGO_TERM_GRACE_SECONDS = 0.5
RUST_TOOLCHAIN_ENV = "UNIALLOC_RUST_TOOLCHAIN"
REPO_TOOLCHAIN_ALIASES = {"", "repo", "pinned", "rust-toolchain", "rust_toolchain", "default"}
SYSTEM_TOOLCHAIN_ALIASES = {"none", "system", "host", "path"}
LATEST_TOOLCHAIN_ALIASES = {"latest", "current-nightly", "current_nightly"}
STABLE_TOOLCHAIN_ALIASES = {"current-stable", "current_stable"}
COMPILER_SITE_TYPE_IDS_ENV = "UNIALLOC_COMPILER_SITE_TYPE_IDS"
COMPILER_SITE_TYPE_ID_MODE_ENV = "UNIALLOC_COMPILER_SITE_TYPE_ID_MODE"
COMPILER_SITE_RECOVERY_SCOPE_ENV = "UNIALLOC_COMPILER_SITE_RECOVERY_SCOPE"
COMPILER_SITE_ID_MODES = ("cyclic-replay", "consuming-stream")
COMPILER_SITE_RECOVERY_SCOPES = ("thread-local", "global")
COMPILER_SITE_REPLAY_DEFAULT_LIMIT = 4096
TYPE_MAPPING_TYPE_ID_KEYS = [
    "type_id",
    "compiler_type_id",
    "object_type_id",
    "semantic_type_id",
]
TYPE_MAPPING_COLLECTION_KEYS = [
    "type_mappings",
    "type_mapping",
    "mappings",
    "entries",
    "allocation_sites",
    "allocations",
    "sites",
]
TYPE_MAPPING_NESTED_CONTAINERS = [
    "allocation_site",
    "site",
    "type",
    "object",
    "metadata",
    "compiler",
]


def emit_error(message: str, *, code: int, **details: Any) -> int:
    payload = {
        "ok": False,
        "source": "paper-workload-driver",
        "error": message,
        "code": code,
        **details,
    }
    print(json.dumps(payload, sort_keys=True), file=sys.stderr)
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


def relay_interrupted_benchmark_output(result: Dict[str, Any]) -> Dict[str, Any]:
    """Relay only bounded Cargo tails so an outer supervisor can persist them."""

    stdout_tail, stdout_bytes, stdout_truncated = bounded_text_tail(
        result.get("stdout"), INTERRUPTED_OUTPUT_TAIL_BYTES
    )
    stderr_tail, stderr_bytes, stderr_truncated = bounded_text_tail(
        result.get("stderr"), INTERRUPTED_OUTPUT_TAIL_BYTES
    )
    if stdout_tail:
        sys.stdout.write(stdout_tail)
        if not stdout_tail.endswith("\n"):
            sys.stdout.write("\n")
        sys.stdout.flush()
    if stderr_tail:
        sys.stderr.write(stderr_tail)
        if not stderr_tail.endswith("\n"):
            sys.stderr.write("\n")
        sys.stderr.flush()
    return {
        "stdout_bytes": stdout_bytes,
        "stderr_bytes": stderr_bytes,
        "stdout_retained_bytes": len(stdout_tail.encode("utf-8", errors="replace")),
        "stderr_retained_bytes": len(stderr_tail.encode("utf-8", errors="replace")),
        "stdout_truncated": stdout_truncated,
        "stderr_truncated": stderr_truncated,
        "max_relay_bytes_per_stream": INTERRUPTED_OUTPUT_TAIL_BYTES,
    }


def process_group_exists(process_group_id: int) -> bool:
    if os.name != "posix":
        return False
    try:
        os.killpg(process_group_id, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return False


def terminate_process_group(
    proc: subprocess.Popen[Any],
    *,
    grace_seconds: float = 2.0,
) -> bool:
    """Terminate Cargo and every benchmark process in its isolated group."""

    if os.name != "posix":  # pragma: no cover - Windows runner fallback.
        if proc.poll() is not None:
            return False
        proc.terminate()
        try:
            proc.wait(timeout=grace_seconds)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=grace_seconds)
        return True

    process_group_id = proc.pid
    try:
        os.killpg(process_group_id, signal.SIGTERM)
    except (ProcessLookupError, PermissionError):
        return False
    deadline = time.monotonic() + grace_seconds
    while process_group_exists(process_group_id) and time.monotonic() < deadline:
        time.sleep(0.05)
    if process_group_exists(process_group_id):
        try:
            os.killpg(process_group_id, signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            pass
    if proc.poll() is None:
        try:
            proc.wait(timeout=grace_seconds)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=grace_seconds)
    return True


def run_benchmark_command(
    command: List[str],
    *,
    cwd: Path,
    env: Dict[str, str],
    timeout_seconds: int,
) -> Dict[str, Any]:
    """Run Cargo in an isolated process group and retain timeout diagnostics."""

    started = time.perf_counter()
    proc = subprocess.Popen(
        command,
        cwd=str(cwd),
        env=env,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        start_new_session=(os.name == "posix"),
    )
    try:
        stdout, stderr = proc.communicate(timeout=timeout_seconds)
        return {
            "exit_code": int(proc.returncode or 0),
            "stdout": stdout or "",
            "stderr": stderr or "",
            "timed_out": False,
            "elapsed_seconds": time.perf_counter() - started,
            "process_group_pid": proc.pid,
            "process_group_terminated": False,
            "process_group_absent_after_cleanup": (
                os.name != "posix" or not process_group_exists(proc.pid)
            ),
            "child_returncode_after_cleanup": proc.poll(),
        }
    except subprocess.TimeoutExpired:
        process_group_terminated = terminate_process_group(proc)
        try:
            stdout, stderr = proc.communicate(timeout=5)
        except subprocess.TimeoutExpired:  # pragma: no cover - defensive fallback.
            terminate_process_group(proc, grace_seconds=0.1)
            stdout, stderr = proc.communicate()
        return {
            "exit_code": 124,
            "stdout": stdout or "",
            "stderr": stderr or "",
            "timed_out": True,
            "elapsed_seconds": time.perf_counter() - started,
            "process_group_pid": proc.pid,
            "process_group_terminated": process_group_terminated,
            "process_group_absent_after_cleanup": (
                os.name != "posix" or not process_group_exists(proc.pid)
            ),
            "child_returncode_after_cleanup": proc.poll(),
        }
    except KeyboardInterrupt as exc:
        # The outer plan runner gives this wrapper a bounded SIGINT window.
        # Escalate a TERM-resistant Cargo promptly so the wrapper can finish
        # cleanup before that outer window expires.
        process_group_terminated = terminate_process_group(
            proc, grace_seconds=INTERRUPTED_CARGO_TERM_GRACE_SECONDS
        )
        try:
            stdout, stderr = proc.communicate(timeout=5)
        except subprocess.TimeoutExpired:  # pragma: no cover - defensive fallback.
            terminate_process_group(proc, grace_seconds=0.1)
            stdout, stderr = proc.communicate()
        result = {
            "exit_code": 130,
            "stdout": stdout or "",
            "stderr": stderr or "",
            "timed_out": False,
            "interrupted": True,
            "status": "interrupted",
            "interrupt_signal": "SIGINT",
            "process_group_pid": proc.pid,
            "process_group_terminated": process_group_terminated,
            "process_group_absent_after_cleanup": (
                os.name != "posix" or not process_group_exists(proc.pid)
            ),
            "child_returncode_after_cleanup": proc.poll(),
        }
        setattr(exc, "_unialloc_bounded_child_result", result)
        raise


class BenchListCommandError(RuntimeError):
    """Structured failure from the filtered std_bench discovery phase."""

    def __init__(self, message: str, *, code: int, record: Dict[str, Any]) -> None:
        super().__init__(message)
        self.code = code
        self.record = record


def bench_failure_details(stdout_text: str) -> Dict[str, Any]:
    rows = parse_bench_rows(stdout_text)
    return {
        "parsed_bench_rows": len(rows),
        "benchmarks_before_failure": [row["benchmark"] for row in rows],
        "last_bench_rows": rows[-10:],
    }


def nonpositive_bench_failure_details(rows: List[Dict[str, Any]]) -> Dict[str, Any]:
    nonpositive = [row for row in rows if float(row.get("ns_per_iter") or 0) <= 0]
    return {
        "parsed_bench_rows": len(rows),
        "benchmarks_before_failure": [row["benchmark"] for row in rows],
        "nonpositive_bench_row_count": len(nonpositive),
        "nonpositive_bench_rows": nonpositive[-10:],
        "last_bench_rows": rows[-10:],
        "hint": (
            "The libtest filter matched benchmark rows, but every parsed timing was "
            "zero or non-positive. Use a heavier filter such as "
            "'vec::bench_with_capacity_1000' for smoke runs, or run the full "
            "Collections row for claim-grade evidence."
        ),
    }


def unique_preserve_order(values: Iterable[Optional[str]]) -> List[str]:
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


def read_json_or_jsonl(path: Path) -> Any:
    """Read machine-auditable mapping evidence without importing evaluate.py.

    The workload driver is intentionally standalone so a focused smoke run does
    not load the large evaluator module.  This helper supports the two evidence
    formats produced by the compiler probes: one JSON object/list, or JSONL
    where each non-empty line is a mapping row.
    """

    text = path.read_text(encoding="utf-8")
    if path.suffix.lower() == ".jsonl":
        rows: List[Any] = []
        for line_no, line in enumerate(text.splitlines(), start=1):
            stripped = line.strip()
            if not stripped:
                continue
            try:
                rows.append(json.loads(stripped))
            except json.JSONDecodeError as exc:
                raise ValueError(f"{path}:{line_no}: {exc}") from exc
        return rows
    return json.loads(text)


def value_is_present(value: Any) -> bool:
    if value in (None, "", [], {}):
        return False
    if isinstance(value, str) and not value.strip():
        return False
    return True


def mapping_rows_from_json(data: Any) -> List[Dict[str, Any]]:
    """Normalize common compiler type-mapping JSON shapes into row dicts."""

    if isinstance(data, list):
        return [row for row in data if isinstance(row, dict)]
    if not isinstance(data, dict):
        return []
    for key in TYPE_MAPPING_COLLECTION_KEYS:
        value = data.get(key)
        if isinstance(value, list):
            return [row for row in value if isinstance(row, dict)]
        if isinstance(value, dict):
            return [dict({"mapping_key": k}, **v) for k, v in value.items() if isinstance(v, dict)]
    dict_values = [value for value in data.values() if isinstance(value, dict)]
    if dict_values and any(any(key in value for key in TYPE_MAPPING_TYPE_ID_KEYS) for value in dict_values):
        return dict_values
    return [data]


def find_mapping_field(row: Dict[str, Any], keys: List[str]) -> Tuple[Optional[Any], Optional[str]]:
    for key in keys:
        value = row.get(key)
        if value_is_present(value):
            return value, key
    for container in TYPE_MAPPING_NESTED_CONTAINERS:
        nested = row.get(container)
        if not isinstance(nested, dict):
            continue
        for key in keys:
            value = nested.get(key)
            if value_is_present(value):
                return value, f"{container}.{key}"
    return None, None


def compiler_site_replay_type_ids_from_mapping(mapping_path: Path, *, limit: int) -> List[int]:
    """Extract stable, non-zero compiler type ids for std_bench replay."""

    data = read_json_or_jsonl(mapping_path)
    rows = mapping_rows_from_json(data)
    ids: List[int] = []
    seen: set[int] = set()
    for row in rows:
        type_id, _field = find_mapping_field(row, TYPE_MAPPING_TYPE_ID_KEYS)
        if not value_is_present(type_id):
            continue
        try:
            parsed = int(str(type_id), 0)
        except (TypeError, ValueError):
            continue
        if parsed <= 0 or parsed in seen:
            continue
        ids.append(parsed)
        seen.add(parsed)
        if limit > 0 and len(ids) >= limit:
            break
    return ids


def compiler_site_type_ids_env_summary(raw: str) -> Dict[str, str]:
    tokens = [
        token
        for token in re.split(r"[,;\s]+", raw.strip())
        if token
    ]
    return {
        f"{COMPILER_SITE_TYPE_IDS_ENV}_COUNT": str(len(tokens)),
        f"{COMPILER_SITE_TYPE_IDS_ENV}_SAMPLE": ",".join(tokens[:8]),
        f"{COMPILER_SITE_TYPE_IDS_ENV}_SHA256": hashlib.sha256(raw.encode("utf-8")).hexdigest(),
    }


def compiler_site_replay_record(config: Dict[str, Any]) -> Dict[str, Any]:
    """Public metadata for replay config, excluding the potentially long env."""

    return {
        key: value
        for key, value in config.items()
        if key not in {"_type_ids", "type_ids_env"}
    }


def compiler_site_replay_config(args: argparse.Namespace) -> Dict[str, Any]:
    """Resolve optional compiler-site type-id replay for semantic std_bench.

    The benchmark harness already accepts a runtime stream via
    UNIALLOC_COMPILER_SITE_TYPE_IDS.  This driver bridges audited MIR
    type_mapping evidence into that ABI so local smoke timing can exercise
    compiler-assigned ids rather than layout-derived ids.
    """

    cached = getattr(args, "_compiler_site_replay_config", None)
    if isinstance(cached, dict):
        return cached

    raw_mapping = getattr(args, "compiler_site_replay_type_mapping", None)
    mode = str(getattr(args, "compiler_site_id_mode", "cyclic-replay") or "cyclic-replay")
    recovery_scope = str(
        getattr(args, "compiler_site_recovery_scope", "thread-local") or "thread-local"
    )
    limit = int(getattr(args, "compiler_site_replay_limit", COMPILER_SITE_REPLAY_DEFAULT_LIMIT) or 0)
    blockers: List[str] = []
    type_ids: List[int] = []
    mapping_path: Optional[Path] = None

    if mode not in COMPILER_SITE_ID_MODES:
        blockers.append(f"unsupported compiler-site id mode: {mode}")
    if recovery_scope not in COMPILER_SITE_RECOVERY_SCOPES:
        blockers.append(f"unsupported compiler-site recovery scope: {recovery_scope}")
    if raw_mapping:
        mapping_path = Path(str(raw_mapping)).expanduser()
        if not mapping_path.is_absolute():
            mapping_path = (ROOT / mapping_path).resolve()
        if not mapping_path.exists():
            blockers.append(f"compiler-site replay type_mapping not found: {mapping_path}")
        else:
            try:
                type_ids = compiler_site_replay_type_ids_from_mapping(mapping_path, limit=limit)
            except (OSError, json.JSONDecodeError, ValueError) as exc:
                blockers.append(f"failed to read compiler-site replay type_mapping: {exc}")
            if not type_ids and not blockers:
                blockers.append(f"compiler-site replay type_mapping contains no usable type_id rows: {mapping_path}")

    type_ids_env = ",".join(str(value) for value in type_ids)
    config = {
        "requested": bool(raw_mapping),
        "enabled": bool(raw_mapping) and bool(type_ids) and not blockers,
        "type_mapping": str(mapping_path) if mapping_path else None,
        "type_id_mode": mode,
        "recovery_scope": recovery_scope,
        "type_id_limit": limit,
        "type_id_count": len(type_ids),
        "type_id_sample": type_ids[:8],
        "type_ids_sha256": hashlib.sha256(type_ids_env.encode("utf-8")).hexdigest() if type_ids_env else None,
        "env_names": [
            COMPILER_SITE_TYPE_IDS_ENV,
            COMPILER_SITE_TYPE_ID_MODE_ENV,
            COMPILER_SITE_RECOVERY_SCOPE_ENV,
        ],
        "blockers": blockers,
        "_type_ids": type_ids,
        "type_ids_env": type_ids_env,
    }
    setattr(args, "_compiler_site_replay_config", config)
    return config


def prepend_path(existing: Optional[str], path: Path) -> str:
    prefix = str(path)
    if not existing:
        return prefix
    parts = [part for part in str(existing).split(os.pathsep) if part]
    if prefix in parts:
        return str(existing)
    return os.pathsep.join([prefix, *parts])


def append_env_flags(existing: Optional[str], flags: str) -> str:
    parts = [part for part in str(existing or "").split() if part]
    for flag in flags.split():
        if flag not in parts:
            parts.append(flag)
    return " ".join(parts)


def prepend_preload_library(existing: Optional[str], library: Path) -> str:
    """Return LD_PRELOAD with a concrete library prepended once."""

    lib = str(library)
    parts = [part for part in re.split(r"[\s:]+", str(existing or "")) if part]
    parts = [part for part in parts if part != lib]
    return " ".join([lib, *parts])


def normalize_repository_path(value: str) -> Path:
    path = Path(value).expanduser()
    if not path.is_absolute():
        path = ROOT / path
    return path.resolve(strict=False)


def google_tcmalloc_library_dirs(value: str, *, prefix: bool) -> List[Path]:
    """Translate canonical prefixes and deprecated lib-dir inputs to candidates."""

    path = normalize_repository_path(value)
    if prefix:
        return [path / "lib"]
    candidates = [path, path / "lib"]
    return list(dict.fromkeys(candidates))


def authenticate_google_tcmalloc_input(
    value: str, *, prefix: bool
) -> Tuple[Optional[Path], Optional[Dict[str, Any]], List[str]]:
    errors: List[str] = []
    for lib_dir in google_tcmalloc_library_dirs(value, prefix=prefix):
        try:
            identity = GOOGLE_TCMALLOC.validate_library_dir(lib_dir)
        except (
            OSError,
            RuntimeError,
            ValueError,
            subprocess.SubprocessError,
        ) as exc:
            errors.append(f"{lib_dir}: {exc}")
            continue
        return lib_dir.resolve(strict=True), dict(identity), errors
    return None, None, errors


def resolve_tcmalloc_library_dir(value: Optional[str]) -> Optional[Path]:
    """Compatibility resolver that accepts only authenticated modern artifacts."""

    if not value:
        return None
    lib_dir, _identity, _errors = authenticate_google_tcmalloc_input(
        value, prefix=False
    )
    return lib_dir


def path_mtime(path: Path) -> float:
    try:
        return path.stat().st_mtime
    except OSError:
        return 0.0


def discover_tcmalloc_library_dir() -> Optional[Path]:
    """Discover only the build helper's canonical, provenance-bound prefix."""

    if not DEFAULT_GOOGLE_TCMALLOC_PREFIX.exists():
        return None
    lib_dir, _identity, _errors = authenticate_google_tcmalloc_input(
        str(DEFAULT_GOOGLE_TCMALLOC_PREFIX), prefix=True
    )
    return lib_dir


def tcmalloc_library_probe(args: argparse.Namespace) -> Dict[str, Any]:
    canonical = getattr(args, "google_tcmalloc_prefix", None) or os.environ.get(
        GOOGLE_TCMALLOC_PREFIX_ENV
    )
    source: Optional[str] = None
    value: Optional[str] = None
    prefix = True
    if canonical:
        source = GOOGLE_TCMALLOC_PREFIX_ENV
        value = str(canonical)
    else:
        deprecated_arg = getattr(args, "tcmalloc_lib_dir", None)
        if deprecated_arg:
            source = "--tcmalloc-lib-dir"
            value = str(deprecated_arg)
            prefix = False
        else:
            for env_name in GOOGLE_TCMALLOC_LEGACY_DIR_ENVS:
                if os.environ.get(env_name):
                    source = env_name
                    value = str(os.environ[env_name])
                    prefix = False
                    break
    if value is None and DEFAULT_GOOGLE_TCMALLOC_PREFIX.exists():
        source = "canonical_repo_prefix"
        value = str(DEFAULT_GOOGLE_TCMALLOC_PREFIX)
        prefix = True

    resolved: Optional[Path] = None
    identity: Optional[Dict[str, Any]] = None
    errors: List[str] = []
    if value is not None:
        resolved, identity, errors = authenticate_google_tcmalloc_input(
            value, prefix=prefix
        )
    return {
        "ok": bool(resolved and identity),
        "allocator_family_required": "google/tcmalloc",
        "variant_required": "modern-hpaa-adaptive-subrelease",
        "configured_prefix": str(resolved.parent) if resolved else None,
        "configured_library_dir": str(resolved) if resolved else None,
        "configured_input": value,
        "configured_input_source": source,
        "deprecated_input": bool(source in {"--tcmalloc-lib-dir", *GOOGLE_TCMALLOC_LEGACY_DIR_ENVS}),
        "identity": identity,
        "validation_errors": errors,
    }


def resolve_scudo_runtime_library(value: Optional[str]) -> Optional[Path]:
    if not value:
        return None
    path = Path(value).expanduser()
    if not path.is_absolute():
        path = (ROOT / path).resolve()
    if path.is_file():
        return path
    if path.is_dir():
        candidates = sorted(
            path.glob("**/libclang_rt.scudo_standalone-*.so"),
            key=scudo_runtime_candidate_order,
        )
        return candidates[0] if candidates else None
    return None


def scudo_runtime_candidate_order(path: Path) -> Tuple[int, float, str]:
    resolved_text = str(path.resolve(strict=False))
    path_match = SCUDO_TRUSTED_SYSTEM_RUNTIME_RE.fullmatch(resolved_text)
    system_tier = 0 if path_match else 1
    host_arch = {"amd64": "x86_64", "arm64": "aarch64"}.get(
        platform.machine().lower(), platform.machine().lower()
    )
    runtime_arch = (
        {"amd64": "x86_64", "arm64": "aarch64"}.get(
            path_match.group("arch").lower(), path_match.group("arch").lower()
        )
        if path_match
        else ""
    )
    arch_tier = 0 if runtime_arch == host_arch else 1
    return (system_tier * 2 + arch_tier, -path_mtime(path), resolved_text)


def discover_scudo_runtime_library() -> Optional[Path]:
    candidates: List[Path] = []
    for pattern in SCUDO_RUNTIME_LIBRARY_GLOBS:
        if pattern.startswith("/"):
            candidates.extend(Path(path) for path in glob.glob(pattern, recursive=True))
        else:
            candidates.extend(ROOT.glob(pattern))
    # Prefer packaged system compiler-rt runtimes over mutable repository/raw
    # artifacts. Authenticity verification below remains authoritative; this
    # ordering also prevents a recently-created local lookalike from shadowing
    # a valid system Scudo runtime during auto-discovery.
    for path in sorted(candidates, key=scudo_runtime_candidate_order):
        resolved = resolve_scudo_runtime_library(str(path))
        if resolved:
            return resolved
    return None


def scudo_runtime_library_identity(path: Path) -> Dict[str, Any]:
    """Return immutable identity evidence for a discovered Scudo runtime."""

    realpath = path.resolve(strict=True)
    stat = realpath.stat()
    hasher = hashlib.sha256()
    with realpath.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            hasher.update(chunk)
    return {
        "path": str(path),
        "realpath": str(realpath),
        "size_bytes": stat.st_size,
        "sha256": hasher.hexdigest(),
        "platform": platform.system(),
        "architecture": platform.machine(),
    }


def file_digest(path: Path, algorithm: str) -> str:
    hasher = hashlib.new(algorithm)
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            hasher.update(chunk)
    return hasher.hexdigest()


def scudo_runtime_dpkg_provenance(path: Path) -> Dict[str, Any]:
    """Verify that ``path`` is an intact compiler-rt file owned by dpkg.

    Symbol names alone are forgeable. The package database is the local trust
    anchor on Debian-family hosts: the runtime path must be canonical, owned by
    a compiler-rt package, match that package's md5sums entry, and pass dpkg's
    package verification.
    """

    realpath = path.resolve(strict=True)
    blockers: List[str] = []
    path_match = SCUDO_TRUSTED_SYSTEM_RUNTIME_RE.fullmatch(str(realpath))
    host_arch = platform.machine().lower()
    runtime_arch = path_match.group("arch").lower() if path_match else ""
    arch_aliases = {
        "amd64": "x86_64",
        "arm64": "aarch64",
    }
    normalized_host_arch = arch_aliases.get(host_arch, host_arch)
    normalized_runtime_arch = arch_aliases.get(runtime_arch, runtime_arch)
    if path_match is None:
        blockers.append("runtime is outside the canonical system compiler-rt Scudo path")
    elif normalized_runtime_arch != normalized_host_arch:
        blockers.append(
            f"runtime architecture {runtime_arch} does not match host architecture {host_arch}"
        )

    dpkg_query = shutil.which("dpkg-query")
    dpkg = shutil.which("dpkg")
    if not dpkg_query or not dpkg:
        blockers.append("dpkg package verification tools are unavailable")
        return {
            "ok": False,
            "trust_anchor": "debian-dpkg",
            "runtime_path": str(realpath),
            "runtime_architecture": runtime_arch or None,
            "host_architecture": host_arch,
            "blockers": blockers,
        }

    owner_proc = subprocess.run(
        [dpkg_query, "-S", str(realpath)],
        cwd=str(ROOT),
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=20,
    )
    owners: List[str] = []
    if owner_proc.returncode == 0:
        suffix = ": " + str(realpath)
        for line in owner_proc.stdout.splitlines():
            if line.endswith(suffix):
                owners.append(line[: -len(suffix)].strip())
    trusted_owners = [owner for owner in owners if SCUDO_DPKG_PACKAGE_RE.fullmatch(owner)]
    if len(trusted_owners) != 1:
        blockers.append("runtime is not uniquely owned by a trusted compiler-rt dpkg package")
        return {
            "ok": False,
            "trust_anchor": "debian-dpkg",
            "runtime_path": str(realpath),
            "runtime_architecture": runtime_arch or None,
            "host_architecture": host_arch,
            "owner_query_exit_code": owner_proc.returncode,
            "owner_query_stdout_tail": owner_proc.stdout[-2000:],
            "owner_query_stderr_tail": owner_proc.stderr[-2000:],
            "owners": owners,
            "trusted_owners": trusted_owners,
            "blockers": blockers,
        }

    package = trusted_owners[0]
    status_proc = subprocess.run(
        [dpkg_query, "-W", "-f=${Status}\t${Version}\t${Architecture}\n", package],
        cwd=str(ROOT),
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=20,
    )
    status_fields = status_proc.stdout.strip().split("\t") if status_proc.returncode == 0 else []
    installed_status = status_fields[0] if len(status_fields) >= 1 else ""
    package_version = status_fields[1] if len(status_fields) >= 2 else ""
    package_arch = status_fields[2] if len(status_fields) >= 3 else ""
    if installed_status != "install ok installed":
        blockers.append("compiler-rt package is not in the installed-ok state")

    md5sums_path = Path("/var/lib/dpkg/info") / f"{package}.md5sums"
    relative_path = str(realpath).lstrip("/")
    expected_md5 = ""
    if md5sums_path.is_file():
        for raw_line in md5sums_path.read_text(encoding="utf-8", errors="replace").splitlines():
            fields = raw_line.strip().split(None, 1)
            if len(fields) == 2 and fields[1].lstrip("./") == relative_path:
                expected_md5 = fields[0].lower()
                break
    if not expected_md5:
        blockers.append("compiler-rt package md5sums does not attest the runtime file")
    actual_md5 = file_digest(realpath, "md5")
    if expected_md5 and actual_md5 != expected_md5:
        blockers.append("runtime content does not match the compiler-rt package md5sums entry")

    verify_proc = subprocess.run(
        [dpkg, "-V", package],
        cwd=str(ROOT),
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=60,
    )
    if verify_proc.returncode != 0 or verify_proc.stdout.strip() or verify_proc.stderr.strip():
        blockers.append("dpkg reported a modified or unverifiable compiler-rt package")

    return {
        "ok": not blockers,
        "trust_anchor": "debian-dpkg",
        "runtime_path": str(realpath),
        "runtime_architecture": runtime_arch or None,
        "host_architecture": host_arch,
        "owner_query_exit_code": owner_proc.returncode,
        "owners": owners,
        "trusted_owner": package,
        "package_status": installed_status,
        "package_version": package_version or None,
        "package_architecture": package_arch or None,
        "md5sums_path": str(md5sums_path),
        "expected_md5": expected_md5 or None,
        "actual_md5": actual_md5,
        "dpkg_verify_exit_code": verify_proc.returncode,
        "dpkg_verify_stdout": verify_proc.stdout,
        "dpkg_verify_stderr": verify_proc.stderr,
        "blockers": blockers,
    }


def scudo_runtime_behavioral_probe(path: Path) -> Dict[str, Any]:
    """Exercise allocator entry points and require genuine Scudo stats output."""

    realpath = path.resolve(strict=True)
    compiler = shutil.which("cc") or shutil.which("clang")
    if not compiler:
        return {
            "ok": False,
            "runtime_path": str(realpath),
            "blockers": ["a C compiler is required for the Scudo behavioral probe"],
        }
    source = r"""
#include <dlfcn.h>
#include <stdlib.h>
#include <string.h>
typedef void (*stats_fn)(void);
int main(void) {
  stats_fn stats = (stats_fn)dlsym(RTLD_DEFAULT, "__scudo_print_stats");
  if (!stats) return 3;
  void *p = malloc(1048576);
  if (!p) return 2;
  memset(p, 0x5a, 1048576);
  free(p);
  stats();
  return 0;
}
""".lstrip()
    source_sha256 = hashlib.sha256(source.encode("utf-8")).hexdigest()
    with tempfile.TemporaryDirectory(prefix="unialloc-scudo-probe-") as td:
        temp_dir = Path(td)
        source_path = temp_dir / "probe.c"
        binary_path = temp_dir / "probe"
        source_path.write_text(source, encoding="utf-8")
        compile_cmd = [compiler, str(source_path), "-O0", "-ldl", "-o", str(binary_path)]
        compile_proc = subprocess.run(
            compile_cmd,
            cwd=str(temp_dir),
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=30,
        )
        if compile_proc.returncode != 0:
            return {
                "ok": False,
                "runtime_path": str(realpath),
                "compiler": compiler,
                "source_sha256": source_sha256,
                "compile_exit_code": compile_proc.returncode,
                "compile_stdout_tail": compile_proc.stdout[-2000:],
                "compile_stderr_tail": compile_proc.stderr[-4000:],
                "blockers": ["failed to compile the Scudo behavioral probe"],
            }
        env = os.environ.copy()
        for name in (
            "LD_PRELOAD",
            "UNIALLOC_SCUDO_RUNTIME_LIBRARY",
            "SCUDO_RUNTIME_LIBRARY",
            "SCUDO_STANDALONE_LIBRARY",
            "SCUDO_OPTIONS",
        ):
            env.pop(name, None)
        env["LD_PRELOAD"] = str(realpath)
        run_proc = subprocess.run(
            [str(binary_path)],
            cwd=str(temp_dir),
            env=env,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=30,
        )
    combined = run_proc.stdout + "\n" + run_proc.stderr
    required_stats_present = [bool(pattern.search(combined)) for pattern in SCUDO_BEHAVIOR_REQUIRED_PATTERNS]
    blockers = []
    if run_proc.returncode != 0:
        blockers.append(f"Scudo behavioral probe exited with status {run_proc.returncode}")
    if not all(required_stats_present):
        blockers.append("runtime did not emit the required Scudo allocator statistics")
    return {
        "ok": not blockers,
        "runtime_path": str(realpath),
        "compiler": compiler,
        "source_sha256": source_sha256,
        "compile_exit_code": compile_proc.returncode,
        "run_exit_code": run_proc.returncode,
        "required_stats_present": required_stats_present,
        "stdout_sha256": hashlib.sha256(run_proc.stdout.encode("utf-8")).hexdigest(),
        "stderr_sha256": hashlib.sha256(run_proc.stderr.encode("utf-8")).hexdigest(),
        "stdout_tail": run_proc.stdout[-2000:],
        "stderr_tail": run_proc.stderr[-4000:],
        "blockers": blockers,
    }


def scudo_runtime_authenticity_probe(path: Path) -> Dict[str, Any]:
    """Authenticate a standalone Scudo artifact before it can be executed."""

    try:
        identity = scudo_runtime_library_identity(path)
        realpath = Path(str(identity["realpath"]))
        stat = realpath.stat()
    except OSError as exc:
        return {
            "ok": False,
            "runtime_authenticity_verified": False,
            "blockers": [f"failed to identify Scudo runtime: {exc}"],
        }
    cache_key = (
        str(realpath),
        int(identity["size_bytes"]),
        int(stat.st_mtime_ns),
        str(identity["sha256"]),
    )
    cached = _SCUDO_RUNTIME_AUTHENTICITY_CACHE.get(cache_key)
    if isinstance(cached, dict):
        return copy.deepcopy(cached)

    try:
        package = scudo_runtime_dpkg_provenance(realpath)
    except (OSError, subprocess.SubprocessError) as exc:
        package = {
            "ok": False,
            "trust_anchor": "debian-dpkg",
            "runtime_path": str(realpath),
            "blockers": [f"compiler-rt package verification failed: {exc}"],
        }
    behavior: Dict[str, Any]
    if package.get("ok") is True:
        try:
            behavior = scudo_runtime_behavioral_probe(realpath)
        except (OSError, subprocess.SubprocessError) as exc:
            behavior = {
                "ok": False,
                "runtime_path": str(realpath),
                "blockers": [f"Scudo behavioral verification failed: {exc}"],
            }
    else:
        behavior = {
            "ok": False,
            "runtime_path": str(realpath),
            "skipped": True,
            "blockers": ["behavioral probe skipped because package authenticity failed"],
        }
    blockers = list(
        dict.fromkeys(
            [
                *(str(item) for item in package.get("blockers", []) if str(item).strip()),
                *(str(item) for item in behavior.get("blockers", []) if str(item).strip()),
            ]
        )
    )
    verified = package.get("ok") is True and behavior.get("ok") is True
    result = {
        "ok": verified,
        "runtime_authenticity_verified": verified,
        "verification_scheme": "dpkg-compiler-rt-content-and-scudo-stats-v1",
        "runtime_library_identity": identity,
        "package_provenance": package,
        "behavioral_probe": behavior,
        "blockers": blockers,
    }
    _SCUDO_RUNTIME_AUTHENTICITY_CACHE[cache_key] = copy.deepcopy(result)
    return result


def scudo_runtime_probe(args: Optional[argparse.Namespace] = None) -> Dict[str, Any]:
    explicit = None
    if args is not None:
        explicit = getattr(args, "scudo_runtime_library", None)
    if not explicit:
        for env_name in SCUDO_RUNTIME_LIBRARY_ENVS:
            value = os.environ.get(env_name)
            if value:
                explicit = value
                break
    explicit_requested = bool(str(explicit or "").strip())
    explicit_resolved = resolve_scudo_runtime_library(explicit)
    # An explicit path is a contract, not a hint. A missing/untrusted explicit
    # artifact must fail instead of silently evaluating a different discovered
    # runtime.
    discovered = None if explicit_requested else discover_scudo_runtime_library()
    resolved = explicit_resolved if explicit_requested else discovered
    identity = None
    identity_error = None
    authenticity: Optional[Dict[str, Any]] = None
    if resolved:
        try:
            identity = scudo_runtime_library_identity(resolved)
            authenticity = scudo_runtime_authenticity_probe(resolved)
        except OSError as exc:
            identity_error = str(exc)
    authenticity_verified = bool(
        isinstance(authenticity, dict)
        and authenticity.get("ok") is True
        and authenticity.get("runtime_authenticity_verified") is True
    )
    return {
        "ok": identity is not None and authenticity_verified,
        "identity_ok": identity is not None,
        "runtime_authenticity_verified": authenticity_verified,
        "configured_runtime_library": str(resolved) if resolved else None,
        "configured_runtime_library_input": explicit,
        "configured_runtime_library_input_resolved": bool(explicit_resolved),
        "configured_runtime_library_source": (
            "explicit" if explicit_resolved else "repo_or_system_auto_discovery" if discovered else None
        ),
        "runtime_library_identity": identity,
        "runtime_library_identity_error": identity_error,
        "runtime_authenticity": authenticity,
        "host_platform": platform.system(),
        "host_architecture": platform.machine(),
        "library_globs": list(SCUDO_RUNTIME_LIBRARY_GLOBS),
        "runtime_env_names": list(SCUDO_RUNTIME_LIBRARY_ENVS),
        "blockers": (
            ["explicit Scudo runtime library path could not be resolved"]
            if explicit_requested and explicit_resolved is None
            else list(authenticity.get("blockers", []))
            if isinstance(authenticity, dict)
            else []
        ),
    }


def resolve_cmake_bin(value: Optional[str]) -> Optional[Path]:
    if not value:
        return None
    path = Path(value).expanduser()
    if not path.is_absolute():
        path = (ROOT / path).resolve()
    if path.is_file() and os.access(path, os.X_OK):
        return path
    return None


def discover_cmake_bin() -> Optional[Path]:
    candidates: List[Path] = []
    for pattern in CMAKE_BIN_GLOBS:
        candidates.extend(ROOT.glob(pattern))
    for path in sorted(candidates, key=lambda item: (path_mtime(item), str(item)), reverse=True):
        resolved = resolve_cmake_bin(str(path))
        if resolved:
            return resolved
    return None


def read_repo_rust_toolchain() -> str:
    """Return the repository-pinned rustup toolchain label, if any."""

    toolchain_file = ROOT / "rust-toolchain"
    if not toolchain_file.exists():
        return ""
    return toolchain_file.read_text(encoding="utf-8").strip()


PAPER_EXACT_RUST_TOOLCHAIN = "nightly-2022-07-01"


def raw_requested_rust_toolchain(args: Optional[argparse.Namespace]) -> Dict[str, Optional[str]]:
    """Return the raw user/env request before alias normalization.

    The repo default remains the historical `rust-toolchain` file.  Explicit
    requests allow newer validation without editing the source tree:
    `--rust-toolchain latest`/`nightly` selects the installed/current rustup
    nightly, while `system`/`none` intentionally drops the rustup `+toolchain`
    prefix for host-path cargo/rustc probes.
    """

    if args is not None:
        value = getattr(args, "rust_toolchain", None)
        if value is not None and str(value).strip():
            return {"value": str(value).strip(), "source": "cli"}
    env_value = os.environ.get(RUST_TOOLCHAIN_ENV)
    if env_value is not None and str(env_value).strip():
        return {"value": str(env_value).strip(), "source": "env"}
    return {"value": None, "source": "repo-rust-toolchain"}


def normalize_rust_toolchain(value: Optional[str], repo_toolchain: str) -> str:
    """Normalize user-facing aliases to rustup labels.

    The returned empty string means "do not pass a rustup +toolchain override".
    """

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
    """Describe the effective Rust toolchain and paper-equivalence status."""

    repo_toolchain = read_repo_rust_toolchain()
    requested = raw_requested_rust_toolchain(args)
    raw_value = requested.get("value")
    effective = normalize_rust_toolchain(raw_value, repo_toolchain)
    source = str(requested.get("source") or "repo-rust-toolchain")
    explicit_non_repo = source in {"cli", "env"} and effective != repo_toolchain
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


def rust_toolchain_arg(toolchain: Optional[str] = None) -> List[str]:
    if toolchain is None:
        effective = str(rust_toolchain_provenance().get("effective_toolchain") or "")
    else:
        effective = str(toolchain).strip()
        if effective.startswith("+"):
            effective = effective[1:]
    return [f"+{effective}"] if effective else []


def explicit_cargo_rust_toolchain(command: Iterable[Any]) -> str:
    """Return the rustup override from an actual `cargo +toolchain ...` command."""

    tokens = [str(token) for token in command]
    if len(tokens) >= 2 and Path(tokens[0]).name == "cargo" and tokens[1].startswith("+"):
        return tokens[1][1:].strip()
    return ""


def scudo_child_probe_args(
    args: argparse.Namespace,
    *,
    command: Iterable[Any],
    cwd: Path,
) -> argparse.Namespace:
    """Bind a Scudo compiler probe to the delegated Cargo command and cwd."""

    command_toolchain = explicit_cargo_rust_toolchain(command)
    requested = (
        command_toolchain
        or str(getattr(args, "rust_toolchain", "") or "").strip()
        or str(os.environ.get(RUST_TOOLCHAIN_ENV) or "").strip()
        or str(os.environ.get("RUSTUP_TOOLCHAIN") or "").strip()
        or "system"
    )
    return argparse.Namespace(
        scudo_mode=str(getattr(args, "scudo_mode", "auto") or "auto"),
        scudo_runtime_library=str(getattr(args, "scudo_runtime_library", "") or "") or None,
        rust_toolchain=requested,
        scudo_probe_cwd=str(Path(cwd).expanduser().resolve()),
        scudo_command_toolchain=command_toolchain or None,
    )


def align_scudo_child_toolchain_env(
    env: Dict[str, str],
    probe_args: argparse.Namespace,
) -> Dict[str, Any]:
    """Make delegated Cargo resolve the same rustc identity as the Scudo probe."""

    provenance = rust_toolchain_provenance(probe_args)
    effective = str(provenance.get("effective_toolchain") or "").strip()
    removed: List[str] = []
    if provenance.get("uses_system_toolchain") is True:
        for name in (RUST_TOOLCHAIN_ENV, "RUSTUP_TOOLCHAIN"):
            if name in env:
                removed.append(name)
            env.pop(name, None)
    else:
        env[RUST_TOOLCHAIN_ENV] = effective
        env["RUSTUP_TOOLCHAIN"] = effective
    return {
        "rust_toolchain_provenance": provenance,
        "probe_cwd": str(getattr(probe_args, "scudo_probe_cwd", "") or ""),
        "command_toolchain": getattr(probe_args, "scudo_command_toolchain", None),
        "subprocess_env_removed": removed,
        "subprocess_env_effective": {
            name: env[name]
            for name in (RUST_TOOLCHAIN_ENV, "RUSTUP_TOOLCHAIN")
            if name in env
        },
        "subprocess_env_effective_absence": {
            name: name not in env for name in (RUST_TOOLCHAIN_ENV, "RUSTUP_TOOLCHAIN")
        },
    }


def toolchain_claim_grade_blockers(provenance: Dict[str, Any]) -> List[str]:
    if provenance.get("paper_exact_toolchain") is not True:
        return [
            "effective Rust toolchain is not nightly-2022-07-01; current/newer validation is not paper-exact toolchain evidence"
        ]
    return []


def paper_toolchain_workspace_probe(
    args: argparse.Namespace,
    features: List[str],
) -> Dict[str, Any]:
    """Verify that the paper Cargo can resolve the requested locked feature graph.

    Dry-run plan audits can otherwise reuse a cached bench list and report a
    command as ready even when Cargo 1.64 cannot parse the workspace lockfile.
    `cargo tree` is intentionally resolution-only: it is fast enough for plan
    probes while still exercising the exact Cargo frontend and locked graph.
    """

    provenance = rust_toolchain_provenance(args)
    cargo = args.cargo or shutil.which("cargo") or "cargo"
    cmd = [
        cargo,
        *rust_toolchain_arg(provenance.get("effective_toolchain")),
        "tree",
        "-p",
        "unialloc",
        "--features",
        ",".join(features),
        "--locked",
        "--offline",
    ]
    try:
        proc = subprocess.run(
            cmd,
            cwd=str(ROOT),
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=min(max(int(getattr(args, "timeout", 60)), 1), 120),
        )
        return {
            "command": cmd,
            "ok": proc.returncode == 0,
            "exit_code": proc.returncode,
            "rust_toolchain_provenance": provenance,
            "stdout_tail": proc.stdout[-2000:],
            "stderr_tail": proc.stderr[-4000:],
        }
    except subprocess.TimeoutExpired as exc:
        return {
            "command": cmd,
            "ok": False,
            "exit_code": None,
            "timed_out": True,
            "rust_toolchain_provenance": provenance,
            "stdout_tail": subprocess_text(exc.stdout)[-2000:],
            "stderr_tail": subprocess_text(exc.stderr)[-4000:],
        }
    except OSError as exc:
        return {
            "command": cmd,
            "ok": False,
            "exit_code": None,
            "rust_toolchain_provenance": provenance,
            "stdout_tail": "",
            "stderr_tail": str(exc),
        }


def scudo_toolchain_probe(args: Optional[argparse.Namespace] = None) -> Dict[str, Any]:
    rustc = shutil.which("rustc") or "rustc"
    provenance = rust_toolchain_provenance(args)
    probe_cwd = Path(str(getattr(args, "scudo_probe_cwd", ROOT) or ROOT)).expanduser().resolve()
    probe_env = os.environ.copy()
    if provenance.get("uses_system_toolchain") is True:
        probe_env.pop(RUST_TOOLCHAIN_ENV, None)
        probe_env.pop("RUSTUP_TOOLCHAIN", None)
    cmd = [
        rustc,
        *rust_toolchain_arg(provenance.get("effective_toolchain")),
        "-",
        "--crate-name",
        "___unialloc_scudo_probe",
        "--print=cfg",
        "-Z",
        "sanitizer=scudo",
    ]
    try:
        version_proc = subprocess.run(
            [rustc, *rust_toolchain_arg(provenance.get("effective_toolchain")), "--version", "--verbose"],
            cwd=str(probe_cwd),
            env=probe_env,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=20,
        )
        proc = subprocess.run(
            cmd,
            input="fn main() {}\n",
            cwd=str(probe_cwd),
            env=probe_env,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=20,
        )
        return {
            "command": cmd,
            "ok": proc.returncode == 0 and version_proc.returncode == 0,
            "exit_code": proc.returncode,
            "probe_cwd": str(probe_cwd),
            "rustc_verbose_version": version_proc.stdout.strip(),
            "rustc_version_exit_code": version_proc.returncode,
            "rust_toolchain_provenance": provenance,
            "rustflags": SCUDO_SANITIZER_RUSTFLAGS,
            "stdout_tail": proc.stdout[-2000:],
            "stderr_tail": proc.stderr[-4000:],
        }
    except subprocess.TimeoutExpired as exc:
        return {
            "command": cmd,
            "ok": False,
            "exit_code": None,
            "timed_out": True,
            "probe_cwd": str(probe_cwd),
            "rust_toolchain_provenance": provenance,
            "rustflags": SCUDO_SANITIZER_RUSTFLAGS,
            "stdout_tail": subprocess_text(exc.stdout)[-2000:],
            "stderr_tail": subprocess_text(exc.stderr)[-4000:],
        }


def scudo_requested_mode(args: argparse.Namespace) -> str:
    mode = str(getattr(args, "scudo_mode", SCUDO_DEFAULT_MODE) or SCUDO_DEFAULT_MODE).strip()
    return mode if mode in SCUDO_MODES else SCUDO_DEFAULT_MODE


def scudo_runtime_identity_marker_present(stderr_text: Any) -> bool:
    """Require the benchmark constructor's exact, newline-terminated marker."""

    return SCUDO_RUNTIME_IDENTITY_MARKER in subprocess_text(stderr_text).splitlines(keepends=True)


def retain_scudo_stderr_evidence(stderr_text: str, *, label: str) -> Dict[str, Any]:
    evidence_dir = Path(tempfile.mkdtemp(prefix="unialloc-scudo-evidence-"))
    safe_label = re.sub(r"[^A-Za-z0-9_.-]+", "-", label).strip("-.") or "benchmark"
    path = evidence_dir / f"{safe_label}.stderr.txt"
    path.write_text(stderr_text, encoding="utf-8")
    return {
        "kind": "scudo_benchmark_stderr",
        "path": str(path),
        "sha256": file_digest(path, "sha256"),
        "bytes": path.stat().st_size,
    }


def retain_scudo_stdout_evidence(stdout_text: str, *, label: str) -> Dict[str, Any]:
    evidence_dir = Path(tempfile.mkdtemp(prefix="unialloc-scudo-evidence-"))
    safe_label = re.sub(r"[^A-Za-z0-9_.-]+", "-", label).strip("-.") or "benchmark"
    path = evidence_dir / f"{safe_label}.stdout.txt"
    path.write_text(stdout_text, encoding="utf-8")
    return {
        "kind": "scudo_benchmark_stdout",
        "path": str(path),
        "sha256": file_digest(path, "sha256"),
        "bytes": path.stat().st_size,
    }


SCUDO_TIMING_BINDING_FIELDS = (
    "source",
    "dataset",
    "benchmark",
    "allocator",
    "variant_feature",
    "run_index",
    "command",
    "server_command",
    "server_cwd",
    "bench_filter",
    "claim_grade",
    "claim_grade_scope",
    "semantic_harness",
    "semantic_policy",
    "allocator_semantics",
    "scudo_execution_probe",
    "scudo_toolchain_alignment",
    "subprocess_env_delta",
    "subprocess_env_effective",
    "subprocess_env_removed",
    "subprocess_env_effective_absence",
    "success",
    "exit_code",
    "child_returncode",
    "measurement_source",
    "measurement_sources",
    "seconds",
    "time_seconds",
    "wall_seconds",
    "elapsed_seconds",
    "ns_per_iter",
    "time_ns",
    "nanoseconds",
    "ms",
    "milliseconds",
    "benchmarks",
    "bench_results",
    "bench_rows",
    "parsed_bench_rows",
    "operation_count",
    "successful_operations",
    "failed_operations",
    "operations_per_second",
    "redis_benchmark_rows",
)


def scudo_timing_binding_payload(record: Dict[str, Any]) -> Dict[str, Any]:
    """Canonical wrapper-owned timing identity bound to its existing raw evidence."""

    fields = {
        key: copy.deepcopy(record[key])
        for key in SCUDO_TIMING_BINDING_FIELDS
        if key in record
    }
    raw_evidence = sorted(
        [
            {
                key: entry.get(key)
                for key in ("kind", "path", "sha256", "bytes")
                if key in entry
            }
            for entry in (record.get("evidence") or [])
            if isinstance(entry, dict)
            and entry.get("kind") != "scudo_timing_binding_json"
        ],
        key=lambda item: (
            str(item.get("kind") or ""),
            str(item.get("path") or ""),
            str(item.get("sha256") or ""),
        ),
    )
    return {
        "schema_version": 1,
        "source": "unialloc-scudo-timing-binding",
        "producer_source": record.get("source"),
        "fields": fields,
        "raw_evidence": raw_evidence,
    }


def attach_scudo_timing_binding(
    record: Dict[str, Any],
    *,
    evidence_dir: Optional[Path] = None,
) -> Dict[str, Any]:
    """Persist and attach the canonical timing binding as the wrapper's final step."""

    directory = evidence_dir or Path(tempfile.mkdtemp(prefix="unialloc-scudo-timing-binding-"))
    directory.mkdir(parents=True, exist_ok=True)
    safe_label = re.sub(
        r"[^A-Za-z0-9_.-]+",
        "-",
        "-".join(
            str(record.get(key) or "")
            for key in ("source", "dataset", "benchmark", "run_index")
        ),
    ).strip("-.") or "scudo-timing"
    path = directory / f"{safe_label}.scudo-timing.json"
    payload = scudo_timing_binding_payload(record)
    path.write_text(
        json.dumps(payload, sort_keys=True, separators=(",", ":")) + "\n",
        encoding="utf-8",
    )
    entry = {
        "kind": "scudo_timing_binding_json",
        "path": str(path),
        "sha256": file_digest(path, "sha256"),
        "bytes": path.stat().st_size,
    }
    evidence = [
        item for item in (record.get("evidence") or []) if isinstance(item, dict)
    ]
    evidence.append(entry)
    record["evidence"] = evidence
    record["raw_evidence"] = copy.deepcopy(evidence)
    record["scudo_timing_binding"] = copy.deepcopy(entry)
    return record


def scudo_execution_route_authenticity_verified(execution: Dict[str, Any]) -> bool:
    mode = execution.get("selected_mode")
    if mode == "rust-sanitizer":
        toolchain = execution.get("toolchain_probe")
        return isinstance(toolchain, dict) and toolchain.get("ok") is True
    if mode == "ld-preload":
        runtime = execution.get("runtime_probe")
        return bool(
            isinstance(runtime, dict)
            and runtime.get("ok") is True
            and runtime.get("runtime_authenticity_verified") is True
            and isinstance(runtime.get("runtime_authenticity"), dict)
            and runtime["runtime_authenticity"].get("ok") is True
            and runtime["runtime_authenticity"].get("runtime_authenticity_verified") is True
        )
    return False


def scudo_allocator_semantics(
    execution: Dict[str, Any],
    *,
    runtime_verified: bool,
    verification_status: str,
) -> Dict[str, Any]:
    runtime_probe = execution.get("runtime_probe")
    runtime_probe = runtime_probe if isinstance(runtime_probe, dict) else {}
    toolchain_probe = execution.get("toolchain_probe")
    toolchain_probe = toolchain_probe if isinstance(toolchain_probe, dict) else {}
    route_authenticity_verified = scudo_execution_route_authenticity_verified(execution)
    effective_runtime_verified = bool(runtime_verified and route_authenticity_verified)
    return {
        "requested_allocator": "scudo",
        "allocator_feature": ALLOCATOR_FEATURES["scudo"],
        "implementation_kind": (
            "verified_external_scudo_runtime"
            if effective_runtime_verified
            else "planned_scudo_runtime_route"
            if verification_status == "planned"
            else "unverified_scudo_runtime_route"
        ),
        "paper_allocator_equivalent": effective_runtime_verified,
        "runtime_identity_verified": effective_runtime_verified,
        "runtime_authenticity_verified": route_authenticity_verified,
        "runtime_identity_status": verification_status,
        "runtime_identity_marker": SCUDO_RUNTIME_IDENTITY_MARKER,
        "scudo_runtime_mode": execution.get("selected_mode"),
        "scudo_runtime_library": execution.get("runtime_library"),
        "scudo_runtime_library_identity": runtime_probe.get("runtime_library_identity"),
        "scudo_runtime_authenticity": runtime_probe.get("runtime_authenticity"),
        "runtime_provenance": {
            "selected_mode": execution.get("selected_mode"),
            "runtime_library": execution.get("runtime_library"),
            "runtime_library_identity": runtime_probe.get("runtime_library_identity"),
            "runtime_authenticity_verified": route_authenticity_verified,
            "runtime_authenticity": runtime_probe.get("runtime_authenticity"),
            "rust_toolchain_provenance": toolchain_probe.get("rust_toolchain_provenance"),
        },
    }


def scudo_execution_runtime_record(
    execution: Dict[str, Any],
    *,
    runtime_verified: bool,
    verification_status: str,
) -> Dict[str, Any]:
    route_authenticity_verified = scudo_execution_route_authenticity_verified(execution)
    effective_runtime_verified = bool(runtime_verified and route_authenticity_verified)
    record = dict(execution)
    record.update(
        {
            "execution_state": verification_status,
            "runtime_verified": effective_runtime_verified,
            "runtime_authenticity_verified": route_authenticity_verified,
            "paper_allocator_equivalent": effective_runtime_verified,
            "equivalence_reason": (
                "trusted Scudo artifact and benchmark runtime identity marker were both verified"
                if effective_runtime_verified
                else "benchmark marker was present but the selected Scudo artifact was not authenticated"
                if runtime_verified
                else "execution route is planned; benchmark runtime identity marker has not been verified"
                if verification_status == "planned"
                else "benchmark did not emit the exact Scudo runtime identity marker on stderr"
            ),
        }
    )
    return record


def scudo_execution_probe(args: argparse.Namespace) -> Dict[str, Any]:
    cached = getattr(args, "_scudo_execution_probe", None)
    if isinstance(cached, dict):
        return cached

    requested = scudo_requested_mode(args)
    toolchain = scudo_toolchain_probe(args)
    runtime = scudo_runtime_probe(args)
    runtime_authentic = bool(
        runtime.get("ok") is True
        and runtime.get("runtime_authenticity_verified") is True
        and isinstance(runtime.get("runtime_authenticity"), dict)
        and runtime["runtime_authenticity"].get("ok") is True
    )
    explicit_runtime_requested = bool(
        str(runtime.get("configured_runtime_library_input") or "").strip()
    )
    selected_mode: Optional[str] = None
    blockers: List[str] = []

    if requested == "rust-sanitizer":
        if toolchain.get("ok"):
            selected_mode = "rust-sanitizer"
        else:
            blockers.append("requested rust-sanitizer mode but rustc rejected -Z sanitizer=scudo")
    elif requested == "ld-preload":
        if runtime_authentic:
            selected_mode = "ld-preload"
        else:
            blockers.append("requested ld-preload mode but no authenticated Scudo standalone runtime was found")
    else:
        if explicit_runtime_requested and runtime_authentic:
            selected_mode = "ld-preload"
        elif explicit_runtime_requested:
            blockers.append(
                "explicit Scudo runtime library was requested but did not pass resolution and authenticity checks"
            )
        elif toolchain.get("ok"):
            selected_mode = "rust-sanitizer"
        elif runtime_authentic:
            selected_mode = "ld-preload"
        else:
            blockers.extend(
                [
                    "rustc rejected -Z sanitizer=scudo",
                    "no authenticated Scudo standalone runtime was found for LD_PRELOAD",
                ]
            )

    runtime_identity = runtime.get("runtime_library_identity")
    runtime_realpath = (
        runtime_identity.get("realpath")
        if isinstance(runtime_identity, dict)
        else None
    )
    result = {
        "ok": bool(selected_mode),
        "requested_mode": requested,
        "selected_mode": selected_mode,
        "rust_sanitizer_rustflags": SCUDO_SANITIZER_RUSTFLAGS,
        "toolchain_probe": toolchain,
        "runtime_probe": runtime,
        "runtime_library": (
            runtime_realpath or runtime.get("configured_runtime_library")
            if selected_mode == "ld-preload"
            else None
        ),
        "execution_state": "planned",
        "runtime_verified": False,
        "runtime_authenticity_verified": (
            runtime_authentic if selected_mode == "ld-preload" else bool(toolchain.get("ok"))
        ),
        "paper_allocator_equivalent": False,
        "equivalence_reason": (
            "execution route selected; exact benchmark stderr identity marker is still required"
            if selected_mode
            else None
        ),
        "runtime_identity_marker_required": SCUDO_RUNTIME_IDENTITY_MARKER,
        "blockers": blockers,
    }
    setattr(args, "_scudo_execution_probe", result)
    return result


def cargo_subprocess_env(args: argparse.Namespace) -> Dict[str, str]:
    env = os.environ.copy()
    if rust_toolchain_provenance(args).get("uses_system_toolchain") is True:
        env.pop(RUST_TOOLCHAIN_ENV, None)
        env.pop("RUSTUP_TOOLCHAIN", None)
    env.setdefault("GLIBC_TUNABLES", "glibc.pthread.rseq=0")
    # Paper timing runs do not need semantic coverage counters: counting every
    # measured allocation would charge evidence instrumentation to the allocator
    # policy.  When callers explicitly request the `stats` feature, treat the
    # run as validation and leave counters enabled.
    explicit_stats = "stats" in (args.extra_feature or [])
    if not explicit_stats:
        env.setdefault("UNIALLOC_STD_BENCH_DISABLE_TYPE_STATS", "1")
        env.setdefault("UNIALLOC_STD_BENCH_DISABLE_AGGREGATE_STATS", "1")
    compiler_site_replay = compiler_site_replay_config(args)
    if compiler_site_replay.get("enabled"):
        env[COMPILER_SITE_TYPE_IDS_ENV] = str(compiler_site_replay.get("type_ids_env") or "")
        env[COMPILER_SITE_TYPE_ID_MODE_ENV] = str(compiler_site_replay.get("type_id_mode") or "cyclic-replay")
        env[COMPILER_SITE_RECOVERY_SCOPE_ENV] = str(
            compiler_site_replay.get("recovery_scope") or "thread-local"
        )
    if args.allocator == "tcmalloc":
        probe = tcmalloc_library_probe(args)
        lib_dir_value = probe.get("configured_library_dir")
        prefix_value = probe.get("configured_prefix")
        if probe.get("ok") and lib_dir_value and prefix_value:
            lib_dir = Path(str(lib_dir_value))
            env[GOOGLE_TCMALLOC_PREFIX_ENV] = str(prefix_value)
            env["LIBRARY_PATH"] = prepend_path(env.get("LIBRARY_PATH"), lib_dir)
            env["LD_LIBRARY_PATH"] = prepend_path(
                env.get("LD_LIBRARY_PATH"), lib_dir
            )
            env["DYLD_LIBRARY_PATH"] = prepend_path(
                env.get("DYLD_LIBRARY_PATH"), lib_dir
            )
    if args.allocator == "gperftools_legacy":
        cmake_bin = (
            resolve_cmake_bin(args.cmake_bin or env.get("UNIALLOC_CMAKE_BIN") or env.get("CMAKE_BIN"))
            or discover_cmake_bin()
        )
        if cmake_bin:
            env["UNIALLOC_CMAKE_BIN"] = str(cmake_bin)
            env["PATH"] = prepend_path(env.get("PATH"), cmake_bin.parent)
    if args.allocator == "scudo":
        scudo = scudo_execution_probe(args)
        mode = scudo.get("selected_mode")
        if mode == "rust-sanitizer":
            for name in SCUDO_SANITIZER_CONFLICTING_ENV_NAMES:
                env.pop(name, None)
            env["RUSTFLAGS"] = append_env_flags(env.get("RUSTFLAGS"), SCUDO_SANITIZER_RUSTFLAGS)
        elif mode == "ld-preload":
            runtime = scudo.get("runtime_library")
            if runtime:
                runtime_path = Path(str(runtime))
                env["UNIALLOC_SCUDO_RUNTIME_LIBRARY"] = str(runtime_path)
                env["LD_PRELOAD"] = prepend_preload_library(env.get("LD_PRELOAD"), runtime_path)
    return env


def env_delta_for_record(env: Dict[str, str]) -> Dict[str, str]:
    keys = (
        "UNIALLOC_STD_BENCH_DISABLE_TYPE_STATS",
        "UNIALLOC_STD_BENCH_DISABLE_AGGREGATE_STATS",
        GOOGLE_TCMALLOC_PREFIX_ENV,
        "UNIALLOC_CMAKE_BIN",
        "UNIALLOC_SCUDO_RUNTIME_LIBRARY",
        "LIBRARY_PATH",
        "LD_LIBRARY_PATH",
        "DYLD_LIBRARY_PATH",
        "LD_PRELOAD",
        "RUSTFLAGS",
        RUST_TOOLCHAIN_ENV,
        COMPILER_SITE_TYPE_ID_MODE_ENV,
        COMPILER_SITE_RECOVERY_SCOPE_ENV,
    )
    out = {key: env[key] for key in keys if key in env and env.get(key) != os.environ.get(key)}
    if COMPILER_SITE_TYPE_IDS_ENV in env and env.get(COMPILER_SITE_TYPE_IDS_ENV) != os.environ.get(COMPILER_SITE_TYPE_IDS_ENV):
        out.update(compiler_site_type_ids_env_summary(str(env.get(COMPILER_SITE_TYPE_IDS_ENV) or "")))
    cmake_bin = env.get("UNIALLOC_CMAKE_BIN")
    if env.get("PATH") != os.environ.get("PATH") and cmake_bin:
        out["PATH_PREPEND"] = str(Path(cmake_bin).parent)
    return out


def geomean(values: Iterable[float]) -> Optional[float]:
    finite = [float(v) for v in values if v and math.isfinite(float(v)) and float(v) > 0]
    if not finite:
        return None
    return math.exp(sum(math.log(v) for v in finite) / len(finite))


def parse_bench_rows(stdout_text: str) -> List[Dict[str, Any]]:
    rows: List[Dict[str, Any]] = []
    for line in stdout_text.splitlines():
        match = BENCH_LINE.match(line.strip())
        if not match:
            continue
        ns = float(match.group("ns").replace(",", ""))
        dev = match.group("dev")
        rows.append(
            {
                "benchmark": match.group("name"),
                "ns_per_iter": ns,
                "deviation_ns": float(dev.replace(",", "")) if dev else None,
            }
        )
    return rows


def parse_bench_list(output: str) -> List[str]:
    benches: List[str] = []
    for raw in output.splitlines():
        text = raw.strip()
        if text.endswith(": benchmark"):
            benches.append(text[: -len(": benchmark")])
    return benches


def bench_list_fingerprint(features: List[str], toolchain: str) -> Dict[str, Any]:
    """Return a cache key for the std_bench target surface.

    A filtered smoke run should not spend minutes rebuilding just to learn that
    a libtest filter names a real benchmark.  The cache is still derived from a
    real `cargo bench -- --list` run and is invalidated when the bench sources,
    Cargo manifest/lockfile, rust-toolchain, feature set, or explicit
    newer/system toolchain selection changes.
    """

    hasher = hashlib.sha256()
    files: List[Path] = []
    for path in [ROOT / "Cargo.lock", ROOT / "rust-toolchain", ROOT / "unialloc" / "Cargo.toml"]:
        if path.exists():
            files.append(path)
    benches_dir = ROOT / "unialloc" / "benches"
    if benches_dir.exists():
        files.extend(sorted(benches_dir.rglob("*.rs")))
    normalized_features = list(features or [])
    hasher.update(("features\0" + "\0".join(normalized_features)).encode("utf-8"))
    hasher.update(("toolchain\0" + str(toolchain or "system")).encode("utf-8"))
    source_files: List[Dict[str, Any]] = []
    for path in sorted(files, key=lambda item: str(item.relative_to(ROOT))):
        rel = str(path.relative_to(ROOT))
        try:
            data = path.read_bytes()
        except OSError:
            continue
        hasher.update(rel.encode("utf-8"))
        hasher.update(b"\0")
        hasher.update(data)
        source_files.append({"path": rel, "bytes": len(data)})
    digest = hasher.hexdigest()
    return {
        "schema": BENCH_LIST_CACHE_SCHEMA,
        "features": normalized_features,
        "rust_toolchain": toolchain,
        "source_digest": digest,
        "source_file_count": len(source_files),
        "source_files": source_files,
        "cache_key": digest[:24],
    }


def bench_list_cache_paths(fingerprint: Dict[str, Any]) -> List[str]:
    key = str(fingerprint.get("cache_key") or fingerprint.get("source_digest") or "unknown")
    feature_key = hashlib.sha256(
        "\0".join(str(item) for item in fingerprint.get("features", [])).encode("utf-8")
    ).hexdigest()[:12]
    filename = f"std_bench-{feature_key}-{key}.json"
    candidates: List[Path] = []
    explicit = os.environ.get("UNIALLOC_BENCH_LIST_CACHE_DIR")
    if explicit:
        candidates.append(Path(explicit).expanduser() / filename)
    cargo_target = os.environ.get("CARGO_TARGET_DIR")
    if cargo_target:
        candidates.append(Path(cargo_target).expanduser() / "unialloc-bench-list-cache" / filename)
    candidates.append(ROOT / "evaluation" / "raw" / "bench-list-cache" / filename)
    return unique_preserve_order([str(path) for path in candidates])


def read_cached_bench_list(fingerprint: Dict[str, Any]) -> Optional[List[str]]:
    for raw_path in bench_list_cache_paths(fingerprint):
        path = Path(raw_path)
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if not isinstance(payload, dict):
            continue
        if payload.get("schema") != BENCH_LIST_CACHE_SCHEMA:
            continue
        if payload.get("source_digest") != fingerprint.get("source_digest"):
            continue
        if payload.get("features") != fingerprint.get("features"):
            continue
        if payload.get("rust_toolchain") != fingerprint.get("rust_toolchain"):
            continue
        benches = payload.get("benches")
        if isinstance(benches, list) and all(isinstance(item, str) for item in benches):
            return list(benches)
    return None


def write_cached_bench_list(fingerprint: Dict[str, Any], benches: List[str]) -> None:
    payload = {
        **fingerprint,
        "benches": list(benches),
        "bench_count": len(benches),
        "source": "paper-workload-driver-std-bench-list-cache",
    }
    for raw_path in bench_list_cache_paths(fingerprint):
        path = Path(raw_path)
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
        except OSError:
            continue


def collect_bench_list(
    cargo: str,
    features: List[str],
    timeout: int,
    env: Optional[Dict[str, str]] = None,
    toolchain: Optional[str] = None,
) -> List[str]:
    effective_toolchain = (
        str(toolchain or "").strip().lstrip("+")
        if toolchain is not None
        else str(rust_toolchain_provenance().get("effective_toolchain") or "")
    )
    fingerprint = bench_list_fingerprint(features, effective_toolchain)
    cached = read_cached_bench_list(fingerprint)
    if cached is not None:
        return cached
    cmd = [cargo, *rust_toolchain_arg(effective_toolchain), "bench", "-p", "unialloc", "--bench", "std_bench"]
    if features:
        cmd.extend(["--features", ",".join(features)])
    cmd.extend(["--", "--list"])
    bench_env = os.environ.copy()
    bench_env.setdefault("GLIBC_TUNABLES", "glibc.pthread.rseq=0")
    timeout_seconds = min(max(timeout, 1), 300)
    try:
        result = run_benchmark_command(
            cmd,
            cwd=ROOT,
            env=env or bench_env,
            timeout_seconds=timeout_seconds,
        )
    except OSError as exc:
        raise BenchListCommandError(
            "failed to start std_bench benchmark listing",
            code=2,
            record={
                "status": "failed-to-start",
                "command": cmd,
                "timeout_seconds": timeout_seconds,
                "exit_code": None,
                "timed_out": False,
                "stdout_tail": "",
                "stderr_tail": str(exc)[-4000:],
                "process_group_pid": None,
                "process_group_terminated": False,
                "process_group_absent_after_cleanup": True,
                "child_returncode_after_cleanup": None,
            },
        ) from exc
    status = (
        "timed-out"
        if result.get("timed_out") is True
        else "passed"
        if int(result.get("exit_code") or 0) == 0
        else "failed"
    )
    record = bounded_command_record(
        result,
        command=cmd,
        timeout_seconds=timeout_seconds,
        status=status,
    )
    if result.get("timed_out") is True:
        raise BenchListCommandError(
            "std_bench benchmark listing timed out",
            code=124,
            record=record,
        )
    if int(result.get("exit_code") or 0) != 0:
        raise BenchListCommandError(
            "failed to list std_bench benchmarks",
            code=int(result.get("exit_code") or 1),
            record=record,
        )
    benches = parse_bench_list(str(result.get("stdout") or ""))
    write_cached_bench_list(fingerprint, benches)
    return benches

def is_semantic_harness_benchmark(name: str) -> bool:
    return str(name).startswith(SEMANTIC_HARNESS_SENTINEL_PREFIXES)


def semantic_variant_requires_runtime_harness(args: argparse.Namespace, features: List[str]) -> bool:
    """Return true when timing would be fake without std_bench semantic setup.

    The policy variants are activated in std_bench by the
    `aaa_semantic_auto_metadata_enable` sentinel.  A libtest name filter hides
    that sentinel, so the driver must either run the full row or select the
    requested leaf with sentinels via `--skip` rather than passing the filter
    directly.  Stats are intentionally optional so performance timing can run
    the real policy without mixing coverage-counter overhead into C003-C006
    measurements.
    """

    variant = args.variant_feature or DATASET_VARIANT_FEATURES.get(args.dataset)
    return args.allocator == "unialloc" and variant in SEMANTIC_VARIANT_FEATURES


def semantic_harness_selection(
    args: argparse.Namespace,
    features: List[str],
) -> Dict[str, Any]:
    required = semantic_variant_requires_runtime_harness(args, features)
    selection: Dict[str, Any] = {
        "required": required,
        "strategy": "libtest-filter" if args.bench_filter else "full-row",
        "bench_filter": args.bench_filter,
        "selected_benches": [],
        "selected_target_benches": [],
        "derived_skips": [],
        "bench_list_count": None,
        "deferred_for_dry_run": False,
    }
    cargo = args.cargo or shutil.which("cargo") or "cargo"
    if args.bench_filter:
        toolchain = str(rust_toolchain_provenance(args).get("effective_toolchain") or "")
        list_timeout = getattr(args, "build_timeout", None) or args.timeout
        bench_list = collect_bench_list(
            cargo,
            features,
            list_timeout,
            cargo_subprocess_env(args),
            toolchain,
        )
        selected_targets = [
            bench
            for bench in bench_list
            if args.bench_filter in bench and not is_semantic_harness_benchmark(bench)
        ]
        if not selected_targets:
            raise ValueError(
                f"bench filter matched no non-sentinel std_bench target: {args.bench_filter}"
            )
        if not required:
            selection.update(
                {
                    "strategy": "validated-libtest-filter",
                    "selected_benches": sorted(selected_targets),
                    "selected_target_benches": sorted(selected_targets),
                    "bench_list_count": len(bench_list),
                }
            )
            return selection

    if not required:
        return selection
    if not args.bench_filter:
        selection["strategy"] = "full-row-with-sentinels"
        return selection

    selected_set = {
        bench
        for bench in bench_list
        if is_semantic_harness_benchmark(bench) or args.bench_filter in bench
    }
    selected_targets = [
        bench
        for bench in bench_list
        if args.bench_filter in bench and not is_semantic_harness_benchmark(bench)
    ]
    if not selected_targets:
        raise ValueError(
            f"bench filter matched no non-sentinel std_bench target: {args.bench_filter}"
        )
    selection.update(
        {
            "strategy": "sentinel-plus-target-skip-selection",
            "selected_benches": sorted(selected_set),
            "selected_target_benches": sorted(selected_targets),
            "derived_skips": [bench for bench in bench_list if bench not in selected_set],
            "bench_list_count": len(bench_list),
        }
    )
    return selection


def build_cargo_command(
    args: argparse.Namespace,
    features: List[str],
    semantic_selection: Optional[Dict[str, Any]] = None,
) -> List[str]:
    cargo = args.cargo or shutil.which("cargo") or "cargo"
    toolchain = str(rust_toolchain_provenance(args).get("effective_toolchain") or "")
    cmd = [cargo, *rust_toolchain_arg(toolchain), "bench", "-p", "unialloc", "--bench", "std_bench"]
    if features:
        cmd.extend(["--features", ",".join(features)])
    semantic_selection = semantic_selection or {}
    uses_skip_selection = bool(semantic_selection.get("derived_skips"))
    deferred_skip_selection = bool(semantic_selection.get("deferred_for_dry_run"))
    if args.bench_filter and not uses_skip_selection and not deferred_skip_selection:
        # Cargo arguments and libtest benchmark arguments are separated by
        # `--`.  Passing the filter as a cargo-level trailing argument can
        # compile the bench target but run zero libtest benchmark rows, which
        # makes subset smoke plans look like timing failures instead of a valid
        # narrow benchmark selection.
        cmd.extend(["--", args.bench_filter])
    elif uses_skip_selection:
        libtest_args = ["--test-threads=1"]
        # Keep sentinel stdout available for raw evidence while the timing
        # parser below excludes those harness rows from the geomean.
        libtest_args.append("--nocapture")
        for skip in semantic_selection.get("derived_skips", []):
            libtest_args.extend(["--skip", str(skip)])
        cmd.extend(["--", *libtest_args])
    return cmd


def build_cargo_prebuild_command(
    args: argparse.Namespace,
    features: List[str],
) -> List[str]:
    """Build std_bench without running it, using the timing command inputs."""

    cargo = args.cargo or shutil.which("cargo") or "cargo"
    toolchain = str(rust_toolchain_provenance(args).get("effective_toolchain") or "")
    cmd = [
        cargo,
        *rust_toolchain_arg(toolchain),
        "bench",
        "-p",
        "unialloc",
        "--bench",
        "std_bench",
        "--no-run",
    ]
    if features:
        cmd.extend(["--features", ",".join(features)])
    return cmd


def bounded_command_record(
    result: Dict[str, Any],
    *,
    command: List[str],
    timeout_seconds: int,
    status: str,
) -> Dict[str, Any]:
    """Keep phase evidence useful without embedding unbounded Cargo output."""

    return {
        "status": status,
        "command": command,
        "timeout_seconds": timeout_seconds,
        "elapsed_seconds": result.get("elapsed_seconds"),
        "exit_code": result.get("exit_code"),
        "timed_out": result.get("timed_out"),
        "stdout_tail": str(result.get("stdout") or "")[-4000:],
        "stderr_tail": str(result.get("stderr") or "")[-4000:],
        "process_group_pid": result.get("process_group_pid"),
        "process_group_terminated": result.get("process_group_terminated"),
        "process_group_absent_after_cleanup": result.get(
            "process_group_absent_after_cleanup"
        ),
        "child_returncode_after_cleanup": result.get("child_returncode_after_cleanup"),
    }


def resolve_features(args: argparse.Namespace) -> Optional[List[str]]:
    allocator = args.allocator.strip()
    allocator_feature = ALLOCATOR_FEATURES.get(allocator)
    if not allocator_feature:
        return None
    dataset_variant = DATASET_VARIANT_FEATURES.get(args.dataset)
    variant = args.variant_feature or dataset_variant
    return unique_preserve_order([allocator_feature, variant, *(args.extra_feature or [])])


def select_timing_rows(
    rows: List[Dict[str, Any]],
    args: argparse.Namespace,
) -> Dict[str, Any]:
    excluded_harness_rows = [
        row for row in rows if is_semantic_harness_benchmark(str(row.get("benchmark") or ""))
    ]
    timing_rows = [
        row for row in rows if not is_semantic_harness_benchmark(str(row.get("benchmark") or ""))
    ]
    if args.bench_filter:
        timing_rows = [
            row for row in timing_rows if args.bench_filter in str(row.get("benchmark") or "")
        ]
    return {
        "timing_rows": timing_rows,
        "excluded_harness_rows": excluded_harness_rows,
    }


def json_objects_from_mixed_line(text: str) -> Iterable[Dict[str, Any]]:
    """Yield JSON objects even when libtest prefixes the line with bench text."""

    decoder = json.JSONDecoder()
    start = text.find("{")
    while start != -1:
        try:
            payload, end = decoder.raw_decode(text[start:])
        except json.JSONDecodeError:
            start = text.find("{", start + 1)
            continue
        if isinstance(payload, dict):
            yield payload
        start = text.find("{", start + max(end, 1))


def parse_semantic_events(stdout_text: str) -> List[Dict[str, Any]]:
    events: List[Dict[str, Any]] = []
    for raw in stdout_text.splitlines():
        text = raw.strip()
        if "std_bench_auto_metadata" not in text:
            continue
        for payload in json_objects_from_mixed_line(text):
            if payload.get("source") == "std_bench_auto_metadata":
                events.append(payload)
    return events


def semantic_policy_claim_grade_status(
    semantic_selection: Dict[str, Any],
    semantic_events: List[Dict[str, Any]],
) -> Dict[str, Any]:
    """Audit whether a semantic timing row exercised real compiler type IDs."""

    if not semantic_selection.get("required"):
        return {"ready": True, "type_id_bases": [], "blockers": []}

    bases = [
        str(event.get("type_id_basis") or "")
        for event in semantic_events
        if event.get("type_id_basis")
    ]
    stream_modes = [
        str(event.get("compiler_site_id_stream_mode") or "")
        for event in semantic_events
        if event.get("compiler_site_id_stream_mode")
    ]
    compiler_assigned = [
        basis
        for basis in bases
        if "compiler-assigned" in basis and "layout-derived" not in basis
    ]
    blockers: List[str] = []
    if not semantic_events:
        blockers.append("semantic sentinel/report event was not observed in stdout")
    if not bases:
        blockers.append("semantic timing did not report a type-id basis")
    if bases and not compiler_assigned:
        blockers.append(
            "semantic timing used layout-derived or non-compiler type ids; "
            "performance evidence is real smoke data but not claim-grade type-isolation evidence"
        )

    return {
        "ready": bool(compiler_assigned) and not blockers,
        "type_id_bases": sorted(set(bases)),
        "compiler_site_id_stream_modes": sorted(set(stream_modes)),
        "blockers": blockers,
    }


def validate_args(args: argparse.Namespace) -> Optional[int]:
    build_timeout = getattr(args, "build_timeout", None)
    if args.timeout <= 0 or (build_timeout is not None and build_timeout <= 0):
        return emit_error(
            "benchmark and build timeouts must be positive",
            code=2,
            timeout_seconds=args.timeout,
            build_timeout_seconds=build_timeout,
        )
    if args.benchmark != "Collections":
        return emit_error(
            "unsupported paper row; only the in-tree Collections row is runnable by this driver",
            code=2,
            dataset=args.dataset,
            benchmark=args.benchmark,
            allocator=args.allocator,
        )
    if args.dataset not in DATASET_VARIANT_FEATURES:
        return emit_error(
            "unsupported dataset for the local Collections driver",
            code=2,
            dataset=args.dataset,
            supported=sorted(DATASET_VARIANT_FEATURES),
        )
    expected_variant = DATASET_VARIANT_FEATURES[args.dataset]
    if args.variant_feature and args.variant_feature != expected_variant:
        return emit_error(
            "variant feature does not match dataset",
            code=2,
            dataset=args.dataset,
            variant_feature=args.variant_feature,
            expected_variant_feature=expected_variant,
        )
    compiler_site_replay = compiler_site_replay_config(args)
    if compiler_site_replay.get("blockers"):
        return emit_error(
            "compiler-site replay type mapping is not usable",
            code=2,
            compiler_site_replay=compiler_site_replay_record(compiler_site_replay),
        )
    if args.allocator == "scudo":
        probe = scudo_execution_probe(args)
        if not probe.get("ok"):
            return emit_error(
                "scudo allocator runtime is not supported by the active Rust toolchain/target or external runtime",
                code=2,
                allocator=args.allocator,
                host_system=platform.system(),
                scudo_execution_probe=probe,
                hint=(
                    "run on a Rust toolchain/target that accepts '-Z sanitizer=scudo', "
                    "or use the host-architecture Scudo standalone runtime from an installed "
                    "compiler-rt package; standalone authentication currently uses the "
                    "Debian/Ubuntu dpkg trust path"
                ),
            )
    if args.allocator == "ptmalloc" and platform.system() != "Linux" and not args.allow_host_allocator_mismatch:
        return emit_error(
            "ptmalloc paper evidence requires a Linux/glibc host; refusing to label this host as ptmalloc",
            code=2,
            allocator=args.allocator,
            host_system=platform.system(),
            hint="pass --allow-host-allocator-mismatch only for non-claim-grade smoke runs",
        )
    if args.allocator == "tcmalloc":
        probe = tcmalloc_library_probe(args)
        if probe.get("ok") is not True:
            return emit_error(
                "modern google/tcmalloc artifact failed provenance and HPAA authentication",
                code=2,
                allocator=args.allocator,
                host_system=platform.system(),
                library_probe=probe,
                hint=(
                    "run evaluation/scripts/build_google_tcmalloc.py and set "
                    "UNIALLOC_GOOGLE_TCMALLOC_PREFIX to its --prefix directory"
                ),
            )
    if args.allocator not in ALLOCATOR_FEATURES:
        return emit_error(
            "unknown allocator for the local Collections driver",
            code=2,
            allocator=args.allocator,
            supported=sorted([*ALLOCATOR_FEATURES, "scudo"]),
        )
    return None


def run(args: argparse.Namespace) -> int:
    args.dataset = args.dataset.strip()
    args.benchmark = args.benchmark.strip()
    args.allocator = args.allocator.strip()
    validation = validate_args(args)
    if validation is not None:
        return validation
    features = resolve_features(args)
    if not features:
        return emit_error("could not resolve cargo features", code=2)
    toolchain_provenance = rust_toolchain_provenance(args)
    toolchain_blockers = toolchain_claim_grade_blockers(toolchain_provenance)
    compiler_site_replay = compiler_site_replay_config(args)
    workspace_probe = None
    if args.dry_run and toolchain_provenance.get("paper_exact_toolchain"):
        workspace_probe = paper_toolchain_workspace_probe(args, features)
        if workspace_probe.get("ok") is not True:
            return emit_error(
                "paper toolchain could not resolve the locked UniAlloc workspace graph",
                code=2,
                dataset=args.dataset,
                allocator=args.allocator,
                paper_toolchain_workspace_probe=workspace_probe,
            )
    try:
        semantic_selection = semantic_harness_selection(args, features)
    except BenchListCommandError as exc:
        return emit_error(
            "failed to validate std_bench filter/harness selection",
            code=exc.code,
            phase="benchmark-list",
            dataset=args.dataset,
            allocator=args.allocator,
            bench_filter=args.bench_filter,
            benchmark_list=exc.record,
            error=str(exc),
        )
    except (RuntimeError, ValueError, subprocess.TimeoutExpired) as exc:
        return emit_error(
            "failed to validate std_bench filter/harness selection",
            code=2,
            dataset=args.dataset,
            allocator=args.allocator,
            bench_filter=args.bench_filter,
            error=str(exc),
        )
    cmd = build_cargo_command(args, features, semantic_selection)
    build_timeout = getattr(args, "build_timeout", None)
    build_cmd = (
        build_cargo_prebuild_command(args, features)
        if build_timeout is not None
        else None
    )
    env = cargo_subprocess_env(args)
    metadata = {
        "ok": True,
        "source": "paper-workload-driver",
        "dataset": args.dataset,
        "benchmark": args.benchmark,
        "allocator": args.allocator,
        "variant_feature": args.variant_feature or DATASET_VARIANT_FEATURES.get(args.dataset),
        "features": features,
        "rust_toolchain": toolchain_provenance.get("effective_toolchain"),
        "rust_toolchain_provenance": toolchain_provenance,
        "paper_toolchain_workspace_probe": workspace_probe,
        "source_accepted_newer_toolchain": toolchain_provenance.get("source_accepted_newer_toolchain"),
        "source_accepted_non_repo_toolchain": toolchain_provenance.get("source_accepted_non_repo_toolchain"),
        "command": cmd,
        "cwd": str(ROOT),
        "host_system": platform.system(),
        "bench_filter": args.bench_filter,
        "claim_grade_scope": "collections-full" if not args.bench_filter else "collections-subset",
        "claim_grade": (
            False
            if args.allocator == "scudo" or semantic_selection.get("required") or toolchain_blockers
            else not bool(args.bench_filter)
        ),
        "compiler_site_replay": compiler_site_replay_record(compiler_site_replay),
        "semantic_harness": {
            **semantic_selection,
            "stats_feature_enabled": "stats" in features,
            "timing_mode": (
                "stats-validation"
                if semantic_selection.get("required") and "stats" in features
                else "stats-free-policy"
                if semantic_selection.get("required")
                else "plain"
            ),
            "derived_skip_count": len(semantic_selection.get("derived_skips") or []),
            # The raw skip list can be hundreds of entries; keep it in the
            # command/evidence while making the top-level metadata compact.
            "derived_skips": (
                semantic_selection.get("derived_skips")
                if len(semantic_selection.get("derived_skips") or []) <= 20
                else [
                    *(semantic_selection.get("derived_skips") or [])[:10],
                    "...",
                    *(semantic_selection.get("derived_skips") or [])[-10:],
                ]
            ),
        },
        "subprocess_env_delta": env_delta_for_record(env),
    }
    if toolchain_provenance.get("uses_system_toolchain") is True:
        metadata["subprocess_env_removed"] = [RUST_TOOLCHAIN_ENV, "RUSTUP_TOOLCHAIN"]
        metadata["subprocess_env_effective_absence"] = {
            RUST_TOOLCHAIN_ENV: RUST_TOOLCHAIN_ENV not in env,
            "RUSTUP_TOOLCHAIN": "RUSTUP_TOOLCHAIN" not in env,
        }
    if build_cmd is not None:
        metadata.update(
            {
                "build_command": build_cmd,
                "build_timeout_seconds": build_timeout,
                "benchmark_timeout_seconds": args.timeout,
            }
        )
    if args.allocator == "tcmalloc":
        probe = tcmalloc_library_probe(args)
        metadata["google_tcmalloc_library_probe"] = probe
        metadata["tcmalloc_library_probe"] = probe
    if args.allocator == "scudo":
        planned_execution = scudo_execution_runtime_record(
            scudo_execution_probe(args),
            runtime_verified=False,
            verification_status="planned",
        )
        metadata["scudo_execution_probe"] = planned_execution
        metadata["scudo_toolchain_probe"] = metadata["scudo_execution_probe"].get("toolchain_probe")
        metadata["scudo_runtime_probe"] = metadata["scudo_execution_probe"].get("runtime_probe")
        metadata["scudo_runtime_mode"] = metadata["scudo_execution_probe"].get("selected_mode")
        metadata["subprocess_env_effective"] = {
            key: env[key]
            for key in (
                "UNIALLOC_SCUDO_RUNTIME_LIBRARY",
                "LD_PRELOAD",
                "RUSTFLAGS",
            )
            if key in env
        }
        if metadata["scudo_runtime_mode"] == "rust-sanitizer":
            removed = list(SCUDO_SANITIZER_CONFLICTING_ENV_NAMES)
            metadata["subprocess_env_removed"] = sorted(
                set(metadata.get("subprocess_env_removed", [])) | set(removed)
            )
            absence = dict(metadata.get("subprocess_env_effective_absence", {}))
            absence.update({name: name not in env for name in removed})
            metadata["subprocess_env_effective_absence"] = absence
        metadata["allocator_semantics"] = scudo_allocator_semantics(
            planned_execution,
            runtime_verified=False,
            verification_status="planned",
        )
    if args.dry_run:
        dry_run_blockers = ["dry-run is not timing evidence", *toolchain_blockers]
        if args.bench_filter:
            dry_run_blockers.append("filtered Collections subset is not claim-grade full-row evidence")
        if args.allocator == "scudo":
            dry_run_blockers.append(
                "Scudo execution route is planned; runtime identity marker has not been observed"
            )
        print(
            json.dumps(
                {
                    **metadata,
                    "dry_run": True,
                    "claim_grade": False,
                    "claim_grade_blockers": dry_run_blockers,
                },
                sort_keys=True,
            )
        )
        return 0
    total_start = time.perf_counter()
    if build_cmd is not None and build_timeout is not None:
        try:
            build_proc = run_benchmark_command(
                build_cmd,
                cwd=ROOT,
                env=env,
                timeout_seconds=build_timeout,
            )
        except KeyboardInterrupt as exc:
            interrupted_result = getattr(exc, "_unialloc_bounded_child_result", None)
            if isinstance(interrupted_result, dict):
                relay = relay_interrupted_benchmark_output(interrupted_result)
                diagnostic = {
                    "ok": False,
                    "source": "paper-workload-driver-interruption",
                    "error": "cargo bench build interrupted",
                    "code": 130,
                    "phase": "build",
                    "interrupted": True,
                    "interrupt_signal": "SIGINT",
                    "dataset": args.dataset,
                    "benchmark": args.benchmark,
                    "allocator": args.allocator,
                    "bench_filter": args.bench_filter,
                    "process_group_pid": interrupted_result.get("process_group_pid"),
                    "process_group_terminated": interrupted_result.get(
                        "process_group_terminated"
                    ),
                    "process_group_absent_after_cleanup": interrupted_result.get(
                        "process_group_absent_after_cleanup"
                    ),
                    **relay,
                }
                print(json.dumps(diagnostic, sort_keys=True), file=sys.stderr, flush=True)
            raise
        build_status = (
            "timed-out"
            if build_proc.get("timed_out") is True
            else "passed"
            if int(build_proc.get("exit_code") or 0) == 0
            else "failed"
        )
        build_record = bounded_command_record(
            build_proc,
            command=build_cmd,
            timeout_seconds=build_timeout,
            status=build_status,
        )
        if build_proc.get("timed_out") is True:
            return emit_error(
                "cargo bench build timed out",
                code=124,
                phase="build",
                dataset=args.dataset,
                allocator=args.allocator,
                bench_filter=args.bench_filter,
                build=build_record,
            )
        if int(build_proc.get("exit_code") or 0) != 0:
            return emit_error(
                "cargo bench build failed",
                code=int(build_proc.get("exit_code") or 1),
                phase="build",
                dataset=args.dataset,
                allocator=args.allocator,
                bench_filter=args.bench_filter,
                build=build_record,
            )
        metadata["build"] = build_record
    start = time.perf_counter()
    try:
        proc = run_benchmark_command(
            cmd,
            cwd=ROOT,
            env=env,
            timeout_seconds=args.timeout,
        )
    except KeyboardInterrupt as exc:
        interrupted_result = getattr(exc, "_unialloc_bounded_child_result", None)
        if isinstance(interrupted_result, dict):
            relay = relay_interrupted_benchmark_output(interrupted_result)
            diagnostic = {
                "ok": False,
                "source": "paper-workload-driver-interruption",
                "error": "cargo bench interrupted",
                "code": 130,
                "interrupted": True,
                "interrupt_signal": "SIGINT",
                "dataset": args.dataset,
                "benchmark": args.benchmark,
                "allocator": args.allocator,
                "bench_filter": args.bench_filter,
                "process_group_pid": interrupted_result.get("process_group_pid"),
                "process_group_terminated": interrupted_result.get(
                    "process_group_terminated"
                ),
                "process_group_absent_after_cleanup": interrupted_result.get(
                    "process_group_absent_after_cleanup"
                ),
                **relay,
            }
            print(json.dumps(diagnostic, sort_keys=True), file=sys.stderr, flush=True)
        raise
    if proc.get("timed_out") is True:
        stdout_text = str(proc.get("stdout") or "")
        stderr_text = str(proc.get("stderr") or "")
        return emit_error(
            "cargo bench timed out",
            code=124,
            phase="benchmark",
            timeout_seconds=args.timeout,
            benchmark_timeout_seconds=args.timeout,
            elapsed_seconds=proc.get("elapsed_seconds"),
            command=cmd,
            stdout_tail=stdout_text[-4000:],
            stderr_tail=stderr_text[-4000:],
            process_group_pid=proc.get("process_group_pid"),
            process_group_terminated=proc.get("process_group_terminated"),
            process_group_absent_after_cleanup=proc.get(
                "process_group_absent_after_cleanup"
            ),
            child_returncode_after_cleanup=proc.get("child_returncode_after_cleanup"),
            **bench_failure_details(stdout_text),
        )
    elapsed = time.perf_counter() - start
    stdout_text = str(proc.get("stdout") or "")
    stderr_text = str(proc.get("stderr") or "")
    if int(proc.get("exit_code") or 0) != 0:
        return emit_error(
            "cargo bench failed",
            code=int(proc.get("exit_code") or 1),
            command=cmd,
            stdout_tail=stdout_text[-4000:],
            stderr_tail=stderr_text[-4000:],
            **bench_failure_details(stdout_text),
        )
    if args.allocator == "scudo":
        planned_execution = scudo_execution_probe(args)
        if not scudo_runtime_identity_marker_present(stderr_text):
            failed_execution = scudo_execution_runtime_record(
                planned_execution,
                runtime_verified=False,
                verification_status="verification-failed",
            )
            return emit_error(
                "Scudo benchmark exited successfully without the required runtime identity marker",
                code=1,
                command=cmd,
                stdout_tail=stdout_text[-4000:],
                stderr_tail=stderr_text[-4000:],
                scudo_execution_probe=failed_execution,
                allocator_semantics=scudo_allocator_semantics(
                    failed_execution,
                    runtime_verified=False,
                    verification_status="verification-failed",
                ),
            )
        verified_execution = scudo_execution_runtime_record(
            planned_execution,
            runtime_verified=True,
            verification_status="runtime-verified",
        )
        if verified_execution.get("runtime_verified") is not True:
            return emit_error(
                "Scudo benchmark marker was present but the runtime artifact was not authenticated",
                code=1,
                command=cmd,
                stdout_tail=stdout_text[-4000:],
                stderr_tail=stderr_text[-4000:],
                scudo_execution_probe=verified_execution,
                allocator_semantics=scudo_allocator_semantics(
                    verified_execution,
                    runtime_verified=False,
                    verification_status="verification-failed",
                ),
            )
        metadata["scudo_execution_probe"] = verified_execution
        metadata["allocator_semantics"] = scudo_allocator_semantics(
            verified_execution,
            runtime_verified=True,
            verification_status="runtime-verified",
        )
        stderr_evidence = retain_scudo_stderr_evidence(
            stderr_text,
            label=f"{args.dataset}-{args.benchmark}-{args.allocator}",
        )
        stdout_evidence = retain_scudo_stdout_evidence(
            stdout_text,
            label=f"{args.dataset}-{args.benchmark}-{args.allocator}",
        )
        metadata["stdout"] = stdout_evidence["path"]
        metadata["stdout_sha256"] = stdout_evidence["sha256"]
        metadata["stderr"] = stderr_evidence["path"]
        metadata["stderr_sha256"] = stderr_evidence["sha256"]
        metadata["evidence"] = [stdout_evidence, stderr_evidence]
        metadata["raw_evidence"] = [stdout_evidence, stderr_evidence]
    parsed_rows = parse_bench_rows(stdout_text)
    semantic_events = parse_semantic_events(stdout_text)
    selected = select_timing_rows(parsed_rows, args)
    rows = selected["timing_rows"]
    excluded_harness_rows = selected["excluded_harness_rows"]
    if not parsed_rows:
        return emit_error(
            "no libtest bench timing rows were parsed",
            code=1,
            command=cmd,
            stdout_tail=stdout_text[-4000:],
            stderr_tail=stderr_text[-4000:],
            parsed_bench_rows=0,
            benchmarks_before_failure=[],
            last_bench_rows=[],
        )
    if not rows:
        return emit_error(
            "no non-harness timing rows remained after semantic/filter selection",
            code=1,
            command=cmd,
            stdout_tail=stdout_text[-4000:],
            stderr_tail=stderr_text[-4000:],
            parsed_bench_rows=len(parsed_rows),
            benchmarks_before_failure=[row["benchmark"] for row in parsed_rows],
            excluded_harness_benchmarks=[row["benchmark"] for row in excluded_harness_rows],
            bench_filter=args.bench_filter,
            semantic_harness=metadata.get("semantic_harness"),
        )
    ns_per_iter = geomean(row["ns_per_iter"] for row in rows)
    if ns_per_iter is None:
        return emit_error(
            "parsed rows did not contain finite positive timings",
            code=1,
            command=cmd,
            **nonpositive_bench_failure_details(rows),
        )
    semantic_policy = semantic_policy_claim_grade_status(semantic_selection, semantic_events)
    claim_grade_blockers: List[str] = []
    if args.bench_filter:
        claim_grade_blockers.append("filtered Collections subset is not claim-grade full-row evidence")
    claim_grade_blockers.extend(toolchain_blockers)
    claim_grade_blockers.extend(semantic_policy.get("blockers", []))
    scudo_allocator_equivalent = (
        args.allocator != "scudo"
        or metadata.get("allocator_semantics", {}).get("paper_allocator_equivalent") is True
    )
    if not scudo_allocator_equivalent:
        claim_grade_blockers.append("Scudo runtime identity was not verified by the benchmark")
    final_claim_grade = (
        not bool(args.bench_filter)
        and bool(semantic_policy.get("ready"))
        and scudo_allocator_equivalent
        and not claim_grade_blockers
    )

    final_record = {
        **metadata,
        "dry_run": False,
        "claim_grade": final_claim_grade,
        "claim_grade_blockers": claim_grade_blockers,
        "semantic_policy": semantic_policy,
        "seconds": ns_per_iter / 1_000_000_000.0,
        "ns_per_iter": ns_per_iter,
        "parsed_bench_rows": len(rows),
        "parsed_bench_rows_total": len(parsed_rows),
        "benchmarks": [row["benchmark"] for row in rows],
        "excluded_harness_benchmarks": [
            row["benchmark"] for row in excluded_harness_rows
        ],
        "semantic_event_count": len(semantic_events),
        "semantic_events": semantic_events,
        "wall_seconds": elapsed,
        **(
            {"total_wall_seconds": time.perf_counter() - total_start}
            if build_cmd is not None
            else {}
        ),
    }
    if args.allocator == "scudo":
        # Bind the wrapper-owned selection contract (command/filter/selected
        # rows) to the same raw stdout/stderr evidence before emitting timing.
        # This prevents a valid full-row execution from being relabeled as a
        # filtered subset after the fact.
        attach_scudo_timing_binding(final_record)
    print(json.dumps(final_record, sort_keys=True))
    return 0


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dataset", required=True)
    parser.add_argument("--benchmark", required=True)
    parser.add_argument("--allocator", required=True)
    parser.add_argument("--variant-feature")
    parser.add_argument("--bench-filter", help="Optional libtest filter; subset runs are not claim-grade")
    parser.add_argument("--extra-feature", action="append", help="Extra cargo feature for smoke/debug runs")
    parser.add_argument("--timeout", type=int, default=1800)
    parser.add_argument(
        "--build-timeout",
        type=int,
        help=(
            "Optional separate timeout for filtered benchmark listing and an explicit "
            "cargo bench --no-run build. When omitted, --timeout keeps its legacy "
            "single-command behavior."
        ),
    )
    parser.add_argument("--cargo", help="Cargo executable; default resolves from PATH")
    parser.add_argument(
        "--rust-toolchain",
        help=(
            "Rustup toolchain override for validation runs. Defaults to the repo "
            "rust-toolchain file. Use 'latest'/'nightly' for the installed/current "
            "nightly, 'stable' for stable, or 'system'/'none' to omit rustup +toolchain. "
            f"Also configurable via {RUST_TOOLCHAIN_ENV}. Non-repo selections are "
            "recorded as accepted-newer/non-paper-exact evidence."
        ),
    )
    parser.add_argument("--allow-host-allocator-mismatch", action="store_true")
    parser.add_argument(
        "--compiler-site-replay-type-mapping",
        help=(
            "JSON/JSONL MIR type_mapping artifact whose type_id rows are replayed "
            "through std_bench's compiler-site auto-metadata ABI"
        ),
    )
    parser.add_argument(
        "--compiler-site-replay-limit",
        type=int,
        default=COMPILER_SITE_REPLAY_DEFAULT_LIMIT,
        help="maximum unique compiler-site type ids to replay; 0 means no limit",
    )
    parser.add_argument(
        "--compiler-site-id-mode",
        choices=COMPILER_SITE_ID_MODES,
        default="cyclic-replay",
        help=(
            "how std_bench consumes compiler-site type ids: cyclic-replay preserves "
            "high-coverage smoke replay; consuming-stream consumes each id once and "
            "exposes short/misaligned streams"
        ),
    )
    parser.add_argument(
        "--compiler-site-recovery-scope",
        choices=COMPILER_SITE_RECOVERY_SCOPES,
        default="thread-local",
        help=(
            "recovery table used for compiler-site replay metadata. thread-local "
            "keeps single-thread std_bench smoke on the TLS fast table; global "
            "preserves conservative cross-thread deallocation recovery."
        ),
    )
    parser.add_argument(
        "--google-tcmalloc-prefix",
        default=os.environ.get(GOOGLE_TCMALLOC_PREFIX_ENV),
        help=(
            "Prefix produced by build_google_tcmalloc.py; also configurable via "
            "UNIALLOC_GOOGLE_TCMALLOC_PREFIX"
        ),
    )
    parser.add_argument(
        "--tcmalloc-lib-dir",
        default=(
            os.environ.get("UNIALLOC_TCMALLOC_LIB_DIR")
            or os.environ.get("TCMALLOC_LIB_DIR")
        ),
        help=(
            "Deprecated compatibility input. The directory is accepted only when "
            "modern google/tcmalloc provenance and HPAA authentication succeeds."
        ),
    )
    parser.add_argument(
        "--cmake-bin",
        default=os.environ.get("UNIALLOC_CMAKE_BIN") or os.environ.get("CMAKE_BIN"),
        help="cmake executable to prepend to PATH for allocator dependency builds",
    )
    parser.add_argument(
        "--scudo-mode",
        choices=SCUDO_MODES,
        default=os.environ.get("UNIALLOC_SCUDO_MODE", SCUDO_DEFAULT_MODE),
        help=(
            "How bench_scudo obtains a real Scudo allocator: rust-sanitizer uses "
            "-Z sanitizer=scudo; ld-preload uses a standalone libclang_rt.scudo "
            "runtime; auto prefers rust-sanitizer and falls back to ld-preload."
        ),
    )
    parser.add_argument(
        "--scudo-runtime-library",
        default=os.environ.get("UNIALLOC_SCUDO_RUNTIME_LIBRARY")
        or os.environ.get("SCUDO_RUNTIME_LIBRARY")
        or os.environ.get("SCUDO_STANDALONE_LIBRARY"),
        help="Path or directory for libclang_rt.scudo_standalone-*.so used by --scudo-mode ld-preload",
    )
    parser.add_argument("--dry-run", action="store_true", help="Print the resolved command without running it")
    return run(parser.parse_args(argv))


if __name__ == "__main__":
    raise SystemExit(main())
