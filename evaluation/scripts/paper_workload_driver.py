#!/usr/bin/env python3
"""Run one supported paper performance workload cell and emit timing JSON.

This is intentionally narrow: it only drives the in-tree std_bench Collections
row that exists in this repository. Paper macrobenchmarks and allocator wrappers
that are not implemented locally fail loudly instead of emitting synthetic
timing.
"""

from __future__ import annotations

import argparse
import ctypes.util
import glob
import hashlib
import json
import math
import os
import platform
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

ROOT = Path(__file__).resolve().parents[2]

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

TCMALLOC_LIBRARY_NAMES = (
    "libtcmalloc.dylib",
    "libtcmalloc.so",
    "libtcmalloc.so.4",
    "libtcmalloc.a",
    "libtcmalloc_minimal.dylib",
    "libtcmalloc_minimal.so",
    "libtcmalloc_minimal.so.4",
    "libtcmalloc_minimal.a",
)

TCMALLOC_LIBRARY_DIR_GLOBS = (
    "evaluation/deps/tcmalloc/lib",
    "evaluation/raw/local-tcmalloc-dep-build-*/prefix/lib",
    "evaluation/raw/local-tcmalloc-dep-build-*/lib",
)

CMAKE_BIN_GLOBS = (
    "evaluation/deps/cmake/bin/cmake",
    "evaluation/raw/local-cmake-pip-*/bin/cmake",
    "evaluation/raw/local-cmake-pip-*/cmake/data/bin/cmake",
)

SCUDO_SANITIZER_RUSTFLAGS = "-Z sanitizer=scudo"
SCUDO_DEFAULT_MODE = "auto"
SCUDO_MODES = ("auto", "rust-sanitizer", "ld-preload")
SCUDO_RUNTIME_LIBRARY_ENVS = (
    "UNIALLOC_SCUDO_RUNTIME_LIBRARY",
    "SCUDO_RUNTIME_LIBRARY",
    "SCUDO_STANDALONE_LIBRARY",
)
SCUDO_RUNTIME_LIBRARY_GLOBS = (
    "/usr/lib/llvm-*/lib/clang/*/lib/linux/libclang_rt.scudo_standalone-*.so",
    "/usr/lib/clang/*/lib/linux/libclang_rt.scudo_standalone-*.so",
    "evaluation/deps/scudo/lib/libclang_rt.scudo_standalone-*.so",
    "evaluation/raw/local-scudo-*/**/libclang_rt.scudo_standalone-*.so",
)

BENCH_LIST_CACHE_SCHEMA = 1
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


def resolve_tcmalloc_library_dir(value: Optional[str]) -> Optional[Path]:
    if not value:
        return None
    path = Path(value).expanduser()
    if not path.is_absolute():
        path = (ROOT / path).resolve()
    if not path.is_dir():
        return None
    for name in TCMALLOC_LIBRARY_NAMES:
        if (path / name).exists():
            return path
    return None


def library_files_in_dir(path: Optional[Path]) -> List[str]:
    if not path:
        return []
    return sorted(name for name in TCMALLOC_LIBRARY_NAMES if (path / name).exists())


def path_mtime(path: Path) -> float:
    try:
        return path.stat().st_mtime
    except OSError:
        return 0.0


def discover_tcmalloc_library_dir() -> Optional[Path]:
    candidates: List[Path] = []
    for pattern in TCMALLOC_LIBRARY_DIR_GLOBS:
        candidates.extend(ROOT.glob(pattern))
    for path in sorted(candidates, key=lambda item: (path_mtime(item), str(item)), reverse=True):
        resolved = resolve_tcmalloc_library_dir(str(path))
        if resolved:
            return resolved
    return None


def tcmalloc_library_probe(args: argparse.Namespace) -> Dict[str, Any]:
    explicit = args.tcmalloc_lib_dir or os.environ.get("UNIALLOC_TCMALLOC_LIB_DIR") or os.environ.get("TCMALLOC_LIB_DIR")
    explicit_resolved = resolve_tcmalloc_library_dir(explicit)
    discovered = discover_tcmalloc_library_dir()
    resolved = explicit_resolved or discovered
    return {
        "ctypes_find_library_tcmalloc": ctypes.util.find_library("tcmalloc"),
        "ctypes_find_library_tcmalloc_minimal": ctypes.util.find_library("tcmalloc_minimal"),
        "configured_library_dir": str(resolved) if resolved else None,
        "configured_library_dir_input": explicit,
        "configured_library_dir_source": (
            "explicit" if explicit_resolved else "repo_auto_discovery" if discovered else None
        ),
        "configured_library_files": library_files_in_dir(resolved),
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
            key=lambda item: (path_mtime(item), str(item)),
            reverse=True,
        )
        return candidates[0] if candidates else None
    return None


def discover_scudo_runtime_library() -> Optional[Path]:
    candidates: List[Path] = []
    for pattern in SCUDO_RUNTIME_LIBRARY_GLOBS:
        if pattern.startswith("/"):
            candidates.extend(Path(path) for path in glob.glob(pattern, recursive=True))
        else:
            candidates.extend(ROOT.glob(pattern))
    for path in sorted(candidates, key=lambda item: (path_mtime(item), str(item)), reverse=True):
        resolved = resolve_scudo_runtime_library(str(path))
        if resolved:
            return resolved
    return None


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
    explicit_resolved = resolve_scudo_runtime_library(explicit)
    discovered = discover_scudo_runtime_library()
    resolved = explicit_resolved or discovered
    return {
        "ok": bool(resolved),
        "configured_runtime_library": str(resolved) if resolved else None,
        "configured_runtime_library_input": explicit,
        "configured_runtime_library_source": (
            "explicit" if explicit_resolved else "repo_or_system_auto_discovery" if discovered else None
        ),
        "library_globs": list(SCUDO_RUNTIME_LIBRARY_GLOBS),
        "runtime_env_names": list(SCUDO_RUNTIME_LIBRARY_ENVS),
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


def toolchain_claim_grade_blockers(provenance: Dict[str, Any]) -> List[str]:
    if provenance.get("paper_exact_toolchain") is not True:
        return [
            "effective Rust toolchain is not nightly-2022-07-01; current/newer validation is not paper-exact toolchain evidence"
        ]
    return []


def scudo_toolchain_probe(args: Optional[argparse.Namespace] = None) -> Dict[str, Any]:
    rustc = shutil.which("rustc") or "rustc"
    provenance = rust_toolchain_provenance(args)
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
        proc = subprocess.run(
            cmd,
            input="fn main() {}\n",
            cwd=str(ROOT),
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=20,
        )
        return {
            "command": cmd,
            "ok": proc.returncode == 0,
            "exit_code": proc.returncode,
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
            "rust_toolchain_provenance": provenance,
            "rustflags": SCUDO_SANITIZER_RUSTFLAGS,
            "stdout_tail": subprocess_text(exc.stdout)[-2000:],
            "stderr_tail": subprocess_text(exc.stderr)[-4000:],
        }


def scudo_requested_mode(args: argparse.Namespace) -> str:
    mode = str(getattr(args, "scudo_mode", SCUDO_DEFAULT_MODE) or SCUDO_DEFAULT_MODE).strip()
    return mode if mode in SCUDO_MODES else SCUDO_DEFAULT_MODE


def scudo_execution_probe(args: argparse.Namespace) -> Dict[str, Any]:
    cached = getattr(args, "_scudo_execution_probe", None)
    if isinstance(cached, dict):
        return cached

    requested = scudo_requested_mode(args)
    toolchain = scudo_toolchain_probe(args)
    runtime = scudo_runtime_probe(args)
    selected_mode: Optional[str] = None
    blockers: List[str] = []

    if requested == "rust-sanitizer":
        if toolchain.get("ok"):
            selected_mode = "rust-sanitizer"
        else:
            blockers.append("requested rust-sanitizer mode but rustc rejected -Z sanitizer=scudo")
    elif requested == "ld-preload":
        if runtime.get("ok"):
            selected_mode = "ld-preload"
        else:
            blockers.append("requested ld-preload mode but no Scudo standalone runtime library was found")
    else:
        if toolchain.get("ok"):
            selected_mode = "rust-sanitizer"
        elif runtime.get("ok"):
            selected_mode = "ld-preload"
        else:
            blockers.extend(
                [
                    "rustc rejected -Z sanitizer=scudo",
                    "no Scudo standalone runtime library was found for LD_PRELOAD",
                ]
            )

    result = {
        "ok": bool(selected_mode),
        "requested_mode": requested,
        "selected_mode": selected_mode,
        "rust_sanitizer_rustflags": SCUDO_SANITIZER_RUSTFLAGS,
        "toolchain_probe": toolchain,
        "runtime_probe": runtime,
        "runtime_library": runtime.get("configured_runtime_library") if selected_mode == "ld-preload" else None,
        "paper_allocator_equivalent": bool(selected_mode),
        "equivalence_reason": (
            "Rust Scudo sanitizer runtime selected"
            if selected_mode == "rust-sanitizer"
            else "bench_scudo uses System allocator and LD_PRELOAD routes malloc/free to Scudo standalone runtime"
            if selected_mode == "ld-preload"
            else None
        ),
        "blockers": blockers,
    }
    setattr(args, "_scudo_execution_probe", result)
    return result


def cargo_subprocess_env(args: argparse.Namespace) -> Dict[str, str]:
    env = os.environ.copy()
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
        lib_dir = (
            resolve_tcmalloc_library_dir(
                args.tcmalloc_lib_dir or env.get("UNIALLOC_TCMALLOC_LIB_DIR") or env.get("TCMALLOC_LIB_DIR")
            )
            or discover_tcmalloc_library_dir()
        )
        if lib_dir:
            env["UNIALLOC_TCMALLOC_LIB_DIR"] = str(lib_dir)
            env["LIBRARY_PATH"] = prepend_path(env.get("LIBRARY_PATH"), lib_dir)
            env["DYLD_LIBRARY_PATH"] = prepend_path(env.get("DYLD_LIBRARY_PATH"), lib_dir)
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
        "UNIALLOC_TCMALLOC_LIB_DIR",
        "UNIALLOC_CMAKE_BIN",
        "UNIALLOC_SCUDO_RUNTIME_LIBRARY",
        "LIBRARY_PATH",
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
    proc = subprocess.run(
        cmd,
        cwd=str(ROOT),
        env=env or bench_env,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=min(max(timeout, 1), 300),
    )
    if proc.returncode != 0:
        raise RuntimeError(
            "failed to list std_bench benchmarks: "
            f"exit={proc.returncode}\n{proc.stderr[-2000:]}"
        )
    benches = parse_bench_list(proc.stdout)
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
        bench_list = collect_bench_list(cargo, features, args.timeout, cargo_subprocess_env(args), toolchain)
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
                    "or install/provide a Scudo standalone runtime such as "
                    "libclang_rt.scudo_standalone-<arch>.so via --scudo-runtime-library "
                    "or UNIALLOC_SCUDO_RUNTIME_LIBRARY"
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
        if not probe.get("ctypes_find_library_tcmalloc") and not probe.get("configured_library_dir"):
            return emit_error(
                "tcmalloc link library was not found on this host",
                code=2,
                allocator=args.allocator,
                host_system=platform.system(),
                library_probe=probe,
                hint=(
                    "install/provide libtcmalloc, or set UNIALLOC_TCMALLOC_LIB_DIR / "
                    "--tcmalloc-lib-dir to a directory containing libtcmalloc"
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
    try:
        semantic_selection = semantic_harness_selection(args, features)
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
        "source_accepted_newer_toolchain": toolchain_provenance.get("source_accepted_newer_toolchain"),
        "source_accepted_non_repo_toolchain": toolchain_provenance.get("source_accepted_non_repo_toolchain"),
        "command": cmd,
        "cwd": str(ROOT),
        "host_system": platform.system(),
        "bench_filter": args.bench_filter,
        "claim_grade_scope": "collections-full" if not args.bench_filter else "collections-subset",
        "claim_grade": (
            False
            if semantic_selection.get("required") or toolchain_blockers
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
    if args.allocator == "tcmalloc":
        metadata["tcmalloc_library_probe"] = tcmalloc_library_probe(args)
    if args.allocator == "scudo":
        metadata["scudo_execution_probe"] = scudo_execution_probe(args)
        metadata["scudo_toolchain_probe"] = metadata["scudo_execution_probe"].get("toolchain_probe")
        metadata["scudo_runtime_probe"] = metadata["scudo_execution_probe"].get("runtime_probe")
        metadata["scudo_runtime_mode"] = metadata["scudo_execution_probe"].get("selected_mode")
    if args.dry_run:
        dry_run_blockers = ["dry-run is not timing evidence", *toolchain_blockers]
        if args.bench_filter:
            dry_run_blockers.append("filtered Collections subset is not claim-grade full-row evidence")
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
    start = time.perf_counter()
    try:
        proc = subprocess.run(
            cmd,
            cwd=str(ROOT),
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=args.timeout,
            env=env,
        )
    except subprocess.TimeoutExpired as exc:
        stdout_text = subprocess_text(exc.stdout)
        stderr_text = subprocess_text(exc.stderr)
        return emit_error(
            "cargo bench timed out",
            code=124,
            timeout_seconds=args.timeout,
            command=cmd,
            stdout_tail=stdout_text[-4000:],
            stderr_tail=stderr_text[-4000:],
            **bench_failure_details(stdout_text),
        )
    elapsed = time.perf_counter() - start
    if proc.returncode != 0:
        return emit_error(
            "cargo bench failed",
            code=proc.returncode or 1,
            command=cmd,
            stdout_tail=proc.stdout[-4000:],
            stderr_tail=proc.stderr[-4000:],
            **bench_failure_details(proc.stdout),
        )
    parsed_rows = parse_bench_rows(proc.stdout)
    semantic_events = parse_semantic_events(proc.stdout)
    selected = select_timing_rows(parsed_rows, args)
    rows = selected["timing_rows"]
    excluded_harness_rows = selected["excluded_harness_rows"]
    if not parsed_rows:
        return emit_error(
            "no libtest bench timing rows were parsed",
            code=1,
            command=cmd,
            stdout_tail=proc.stdout[-4000:],
            stderr_tail=proc.stderr[-4000:],
            parsed_bench_rows=0,
            benchmarks_before_failure=[],
            last_bench_rows=[],
        )
    if not rows:
        return emit_error(
            "no non-harness timing rows remained after semantic/filter selection",
            code=1,
            command=cmd,
            stdout_tail=proc.stdout[-4000:],
            stderr_tail=proc.stderr[-4000:],
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
    final_claim_grade = (
        not bool(args.bench_filter)
        and bool(semantic_policy.get("ready"))
        and not claim_grade_blockers
    )

    print(
        json.dumps(
            {
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
            },
            sort_keys=True,
        )
    )
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
        "--tcmalloc-lib-dir",
        default=os.environ.get("UNIALLOC_TCMALLOC_LIB_DIR") or os.environ.get("TCMALLOC_LIB_DIR"),
        help="Directory containing libtcmalloc; also configurable via UNIALLOC_TCMALLOC_LIB_DIR",
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
