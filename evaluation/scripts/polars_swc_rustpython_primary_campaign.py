#!/usr/bin/env python3
"""Run current-source Polars, SWC, and RustPython Type Isolation campaigns.

The campaign is intentionally fail closed.  It freezes one allocator snapshot,
builds all three variants from that snapshot, retains source/patch/compiler
audits, runs one warmup plus three paired measured rounds, reports median point
estimates, and only enables a gate when the corresponding evidence is present.
"""

from __future__ import annotations

import argparse
import contextlib
import difflib
import fcntl
import hashlib
import io
import json
import math
import os
import pathlib
import re
import shutil
import statistics
import sys
import tarfile
import time
from dataclasses import dataclass
from typing import Any, Iterable, Mapping, Sequence


ROOT = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "evaluation" / "scripts"))

import realworld_type_isolation_matrix as matrix  # noqa: E402


SUITE_PATH = ROOT / "evaluation" / "config" / "type_isolation_primary_suite.json"
PREREGISTRATION_SUITE_MANIFEST_SHA256 = (
    "96fa64009550d52f89549dd2124e3b1a402a42b7f1fd1750888600dd67bb8a6f"
)
ANALYSIS_SUITE_MANIFEST_SHA256 = hashlib.sha256(SUITE_PATH.read_bytes()).hexdigest()
# Retain the historical field as an analysis-manifest alias for consumers that
# predate the explicit preregistration/amendment split.
SUITE_MANIFEST_SHA256 = ANALYSIS_SUITE_MANIFEST_SHA256
_SUITE_MANIFEST = json.loads(SUITE_PATH.read_text(encoding="utf-8"))
SUITE_IMPLEMENTATION_REVISION = str(_SUITE_MANIFEST["implementation"]["git_revision"])
SUITE_IMPLEMENTATION_SHA256 = str(_SUITE_MANIFEST["implementation"]["canonical_sha256"])
DEFAULT_RAW_DIR = ROOT / "evaluation" / "raw" / "polars-swc-rustpython-primary-f5d0c19"
DEFAULT_CHECKOUT_ROOT = ROOT / "evaluation" / "external" / "_checkouts"
ASSEMBLER_TARGET_DIR = (
    ROOT / "evaluation" / "raw" / "type-isolation-primary-v1" / "targets"
)
MEASUREMENT_LOCK = (
    ROOT
    / "evaluation"
    / "raw"
    / "type-isolation-primary-v1"
    / "primary-measurement.lock"
)
TOOLCHAIN = "nightly-2026-06-11"
MEASUREMENT_CPU_LIST = "32-63"
MEASUREMENT_NUMA_NODE = "1"
VARIANTS = ("unialloc", "typed_plain", "typeiso_perf")
BUILD_ORDER = ("typed_plain", "typeiso_perf", "unialloc")
REQUIRED_GATES = (
    "correctness",
    "build_success",
    "allocator_activation",
    "actual_mir_provenance",
    "stats_disabled",
    "compiler_route_equivalent",
    "source_audit_retained",
)
DATA_ELIGIBILITY_GATES = tuple(
    gate for gate in REQUIRED_GATES if gate != "compiler_route_equivalent"
)
ROUTE_RATIO_MIN = 0.85
ROUTE_RATIO_MAX = 1.15
PRIMARY_ROUNDS = 3
# Artifact readers retain compatibility with completed five-round campaigns;
# every scheduling path below accepts PRIMARY_ROUNDS only.
LEGACY_PRIMARY_ROUNDS = 5
PUBLISHABLE_ROUND_COUNTS = frozenset((PRIMARY_ROUNDS, LEGACY_PRIMARY_ROUNDS))
ALLOCATOR_MARKER = "// UniAlloc Polars/SWC/RustPython primary campaign allocator."
GNU_TIME_FORMAT = "UNIALLOC_PRIMARY_TIME\t%U\t%S\t%P\t%M\t%F\t%R\t%c\t%w\t%x"
FORCE_LOAD_WRAPPER_SOURCE = r'''#!/usr/bin/env python3
"""Expose UniAlloc metadata globally and force-load it for selected crates."""

import os
import pathlib
import sys


def fail(message: str) -> "None":
    print(f"unialloc primary force-load wrapper: {message}", file=sys.stderr)
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
if not dependency_dir.is_dir():
    fail(f"missing force-load dependency directory: {dependency_dir}")
if not targets:
    fail("empty selected-crate allowlist")

# A rewritten crate's metadata names UniAlloc.  Every downstream invocation
# therefore needs the dependency search path while loading that metadata.
arguments.extend(["-L", f"dependency={dependency_dir}"])

selected = crate_name(arguments) in targets
if selected:
    if not rlib.is_file() or rlib.suffix != ".rlib":
        fail(f"missing force-load rlib: {rlib}")
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
        ]
    )

os.execv(str(driver), [str(driver), *arguments])
'''
DIRECT_LOAD_WRAPPER_SOURCE = r'''#!/usr/bin/env python3
"""Expose a prebuilt baseline UniAlloc rlib to one Cargo rustc target."""

import os
import pathlib
import sys


def fail(message: str) -> "None":
    print(f"unialloc primary direct-load wrapper: {message}", file=sys.stderr)
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


arguments = sys.argv[1:]
if not arguments:
    fail("missing Cargo rustc invocation")
rustc = pathlib.Path(arguments[0])
rustc_arguments = arguments[1:]
rlib = pathlib.Path(os.environ.get("UNIALLOC_DIRECT_LOAD_RLIB", ""))
dependency_dir = pathlib.Path(
    os.environ.get("UNIALLOC_DIRECT_LOAD_DEPENDENCY_DIR", "")
)
target = normalized(os.environ.get("UNIALLOC_DIRECT_LOAD_TARGET_CRATE", ""))
if not rustc.is_file():
    fail(f"missing rustc: {rustc}")
if not rlib.is_file() or rlib.suffix != ".rlib":
    fail(f"missing baseline rlib: {rlib}")
if not dependency_dir.is_dir():
    fail(f"missing baseline dependency directory: {dependency_dir}")
if not target:
    fail("empty direct-load target crate")

rustc_arguments.extend(["-L", f"dependency={dependency_dir}"])
if crate_name(rustc_arguments) == target:
    rustc_arguments.extend(
        [
            "-Z",
            "unstable-options",
            "--extern",
            f"force:unialloc={rlib}",
        ]
    )

os.execv(str(rustc), [str(rustc), *rustc_arguments])
'''


class CampaignError(RuntimeError):
    """A source, build, provenance, or measurement contract failed."""


@dataclass(frozen=True)
class TargetSpec:
    target_id: str
    checkout: str
    commit: str
    source_ref: str
    upstream_toolchain: str
    mode: str
    package: str
    bench_name: str | None
    allocator_source: str | None
    target_crates: tuple[str, ...]
    harness_filters: Mapping[str, str]


TARGET_SPECS: dict[str, TargetSpec] = {
    "polars": TargetSpec(
        target_id="polars",
        checkout="external-R-Polars-Polars",
        commit="0df0c25d4db895ec8cad773bedc4e98400e3135e",
        source_ref="py-1.42.1",
        upstream_toolchain="nightly-2026-04-01",
        mode="polars-driver",
        package="polars-unialloc-primary",
        bench_name=None,
        allocator_source=None,
        target_crates=(
            "polars_unialloc_primary",
            "polars",
            "polars_core",
            "polars_lazy",
            "polars_mem_engine",
            "polars_expr",
            "polars_io",
            "polars_plan",
            "polars_stream",
            "polars_compute",
        ),
        harness_filters={
            "groupby_low_cardinality": "groupby_low_cardinality",
            "groupby_high_cardinality": "groupby_high_cardinality",
            "groupby_multikey": "groupby_multikey",
            "filter_retain": "filter_retain",
            "csv_scan": "csv_scan",
        },
    ),
    "swc": TargetSpec(
        target_id="swc",
        checkout="external-RJS-Compiler-SWC",
        commit="73f0f386da64fe432975183f9ccf28429b52638a",
        source_ref="v1.15.43",
        upstream_toolchain="nightly-2026-04-10",
        mode="criterion",
        package="swc",
        bench_name="typescript",
        allocator_source="crates/swc/benches/typescript.rs",
        target_crates=(
            "typescript",
            "swc",
            "swc_common",
            "swc_ecma_parser",
            "swc_ecma_codegen",
            "swc_ecma_transforms",
            "swc_ecma_transforms_base",
            "swc_ecma_transforms_compat",
        ),
        harness_filters={
            "large_parser": "es/large/parser",
            "large_fixer": "es/large/base/fixer",
            "large_codegen_es2020": "es/large/codegen/es2020",
            "large_all_es2020": "es/large/all/es2020",
            "large_all_es5": "es/large/all/es5",
        },
    ),
    "rustpython": TargetSpec(
        target_id="rustpython",
        checkout="external-RPython-RustPython",
        commit="a9c2c529b14199a7ae7893b9d82c8bebe6b17418",
        source_ref="main@2026-07-13",
        upstream_toolchain="1.95.0",
        mode="criterion",
        package="rustpython",
        bench_name="execution",
        allocator_source="benches/execution.rs",
        target_crates=(
            "execution",
            "rustpython_vm",
            "rustpython_compiler",
            "rustpython_codegen",
            "rustpython_common",
            "rustpython_ruff_python_parser",
        ),
        harness_filters={
            "parse_mandelbrot": "parse_to_ast/rustpython/mandelbrot.py",
            "execute_mandelbrot": "execution/mandelbrot.py/rustpython",
            "execute_nbody": "execution/nbody.py/rustpython",
            "execute_fannkuch": "execution/fannkuch.py/rustpython",
            "pystone_30000": "pystone/rustpython/30000",
        },
    ),
}


POLARS_MANIFEST = """[package]
name = "polars-unialloc-primary"
version = "0.0.0"
edition = "2024"
publish = false

[dependencies]
polars = { path = "../polars", default-features = false, features = ["lazy", "csv", "strings", "fmt", "temporal"] }
"""


POLARS_SOURCE = r"""use polars::prelude::*;
use std::collections::hash_map::DefaultHasher;
use std::hash::{Hash, Hasher};
use std::time::Instant;

const ROWS: usize = 1_000_000;
const GROUPS: u64 = 100;

fn next(state: &mut u64) -> u64 {
    *state = state
        .wrapping_mul(6_364_136_223_846_793_005)
        .wrapping_add(1_442_695_040_888_963_407);
    *state
}

fn input_frame() -> PolarsResult<DataFrame> {
    let mut state = 0_u64;
    let mut id1 = Vec::with_capacity(ROWS);
    let mut id2 = Vec::with_capacity(ROWS);
    let mut id3 = Vec::with_capacity(ROWS);
    let mut id4 = Vec::with_capacity(ROWS);
    let mut id5 = Vec::with_capacity(ROWS);
    let mut id6 = Vec::with_capacity(ROWS);
    let mut v1 = Vec::with_capacity(ROWS);
    let mut v2 = Vec::with_capacity(ROWS);
    let mut v3 = Vec::with_capacity(ROWS);
    for _ in 0..ROWS {
        id1.push(format!("id{:03}", next(&mut state) % GROUPS + 1));
        id2.push(format!("id{:03}", next(&mut state) % GROUPS + 1));
        id3.push(format!("id{:010}", next(&mut state) % 10_000 + 1));
        id4.push((next(&mut state) % GROUPS + 1) as i32);
        id5.push((next(&mut state) % GROUPS + 1) as i32);
        id6.push((next(&mut state) % 10_000 + 1) as i32);
        v1.push((next(&mut state) % 5 + 1) as i32);
        v2.push((next(&mut state) % 15 + 1) as i32);
        v3.push((next(&mut state) % 100_000_000) as f64 / 1_000_000.0);
    }
    DataFrame::new(ROWS, vec![
        Column::new("id1".into(), id1),
        Column::new("id2".into(), id2),
        Column::new("id3".into(), id3),
        Column::new("id4".into(), id4),
        Column::new("id5".into(), id5),
        Column::new("id6".into(), id6),
        Column::new("v1".into(), v1),
        Column::new("v2".into(), v2),
        Column::new("v3".into(), v3),
    ])
}

fn fingerprint(frame: &DataFrame) -> u64 {
    let mut hasher = DefaultHasher::new();
    frame.height().hash(&mut hasher);
    frame.width().hash(&mut hasher);
    frame.estimated_size().hash(&mut hasher);
    for column in frame.columns() {
        column.name().as_str().hash(&mut hasher);
        format!("{:?}", column.dtype()).hash(&mut hasher);
    }
    hasher.finish()
}

fn run(harness: &str, csv_path: Option<&str>) -> PolarsResult<DataFrame> {
    if harness == "csv_scan" {
        let path = csv_path.expect("csv_scan requires an input path");
        return LazyCsvReader::new(PlRefPath::new(path))
            .finish()?
            .filter(col("v2").lt(lit(5)))
            .collect();
    }
    let frame = input_frame()?;
    match harness {
        "groupby_low_cardinality" => frame
            .lazy()
            .group_by([col("id1")])
            .agg([col("v1").sum().alias("v1_sum")])
            .collect(),
        "groupby_high_cardinality" => frame
            .lazy()
            .group_by([col("id3")])
            .agg([
                col("v1").sum().alias("v1_sum"),
                col("v3").mean().alias("v3_mean"),
            ])
            .collect(),
        "groupby_multikey" => frame
            .lazy()
            .group_by([
                col("id1"), col("id2"), col("id3"),
                col("id4"), col("id5"), col("id6"),
            ])
            .agg([
                col("v3").sum().alias("v3_sum"),
                col("v1").count().alias("v1_count"),
            ])
            .collect(),
        "filter_retain" => frame
            .lazy()
            .filter(col("id1").neq_missing(lit("id046")))
            .select([
                col("id6").cast(DataType::Int64).sum(),
                col("v3").sum(),
            ])
            .collect(),
        _ => panic!("unknown harness: {harness}"),
    }
}

fn main() -> PolarsResult<()> {
    let mut args = std::env::args().skip(1);
    let harness = args.next().expect("missing harness id");
    let csv_path = args.next();
    let started = Instant::now();
    let output = run(&harness, csv_path.as_deref())?;
    let seconds = started.elapsed().as_secs_f64();
    println!(
        "{{\"harness_id\":\"{}\",\"operation_seconds\":{:.9},\"rows\":{},\"columns\":{},\"estimated_size\":{},\"fingerprint\":\"{:016x}\"}}",
        harness,
        seconds,
        output.height(),
        output.width(),
        output.estimated_size(),
        fingerprint(&output),
    );
    Ok(())
}
"""


def parse_cpu_list(raw: str) -> set[int]:
    cpus: set[int] = set()
    for part in raw.split(","):
        token = part.strip()
        if not token:
            continue
        if "-" in token:
            start_text, end_text = token.split("-", 1)
            start = int(start_text)
            end = int(end_text)
            if end < start:
                raise ValueError(f"invalid CPU range: {token}")
            cpus.update(range(start, end + 1))
        else:
            cpus.add(int(token))
    if not cpus or min(cpus) < 0:
        raise ValueError(f"invalid CPU list: {raw}")
    return cpus


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--targets",
        default=",".join(TARGET_SPECS),
        help="comma-separated target ids",
    )
    parser.add_argument("--raw-dir", type=pathlib.Path, default=DEFAULT_RAW_DIR)
    parser.add_argument(
        "--checkout-root", type=pathlib.Path, default=DEFAULT_CHECKOUT_ROOT
    )
    parser.add_argument("--jobs", type=int, default=min(32, os.cpu_count() or 1))
    parser.add_argument("--warmups", type=int, default=1)
    parser.add_argument(
        "--rounds",
        type=int,
        default=PRIMARY_ROUNDS,
        help="one warmup, exactly three paired rounds, and median point estimates",
    )
    parser.add_argument("--build-timeout", type=int, default=3600)
    parser.add_argument("--run-timeout", type=int, default=600)
    implementation = parser.add_mutually_exclusive_group()
    implementation.add_argument(
        "--allocator-revision",
        help="exact Git commit archived as the frozen allocator/pass snapshot",
    )
    implementation.add_argument(
        "--current-working-tree",
        action="store_true",
        help=(
            "freeze the current allocator/pass working tree for a diagnostic-only "
            "campaign that cannot enter primary-suite results"
        ),
    )
    parser.add_argument("--quiescence-timeout", type=int, default=3600)
    parser.add_argument("--quiescence-seconds", type=int, default=15)
    parser.add_argument(
        "--cpu-list",
        help="diagnostic current-working-tree CPU set; primary remains fixed at 32-63",
    )
    parser.add_argument(
        "--numa-node",
        type=int,
        help="diagnostic current-working-tree NUMA node; primary remains fixed at 1",
    )
    parser.add_argument("--keep-target", action="store_true")
    parser.add_argument("--reuse-builds", action="store_true")
    parser.add_argument(
        "--phase",
        choices=("build", "warmup", "full"),
        default="full",
        help="build only, stop after locked warmups, or run the complete campaign",
    )
    args = parser.parse_args(argv)
    targets = tuple(part.strip() for part in args.targets.split(",") if part.strip())
    unknown = sorted(set(targets) - set(TARGET_SPECS))
    if not targets or unknown:
        parser.error("unknown or empty targets: " + ",".join(unknown))
    if (
        args.jobs < 1
        or args.jobs > 32
        or args.warmups != 1
        or args.quiescence_timeout < 1
        or args.quiescence_seconds < 1
    ):
        parser.error("the campaign requires one warmup and positive resource limits")
    if args.rounds != PRIMARY_ROUNDS:
        parser.error("the campaign requires exactly three measured rounds")
    if args.allocator_revision is None and not args.current_working_tree:
        args.allocator_revision = SUITE_IMPLEMENTATION_REVISION
    if not args.current_working_tree and (
        args.cpu_list is not None or args.numa_node is not None
    ):
        parser.error("--cpu-list/--numa-node require --current-working-tree")
    args.cpu_list = args.cpu_list or MEASUREMENT_CPU_LIST
    args.numa_node = (
        str(args.numa_node) if args.numa_node is not None else MEASUREMENT_NUMA_NODE
    )
    try:
        parse_cpu_list(args.cpu_list)
        if int(args.numa_node) < 0:
            raise ValueError("NUMA node must be non-negative")
    except ValueError as error:
        parser.error(str(error))
    args.targets = targets
    return args


def sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def sha256_file(path: pathlib.Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def tree_digest(
    paths: Iterable[pathlib.Path], base: pathlib.Path, *, trailing_nul: bool = False
) -> str:
    digest = hashlib.sha256()
    for path in sorted(set(paths)):
        digest.update(path.relative_to(base).as_posix().encode())
        digest.update(b"\0")
        digest.update(path.read_bytes())
        if trailing_nul:
            digest.update(b"\0")
    return digest.hexdigest()


def implementation_files(snapshot: pathlib.Path) -> list[pathlib.Path]:
    files = [
        snapshot
        / "tools"
        / "unialloc-rustc-pass"
        / "unialloc-rustc-mir-rewrite-dry-run.rs"
    ]
    for source_root in (snapshot / "unialloc", snapshot / "alloc_macros"):
        files.extend(
            path
            for path in source_root.rglob("*")
            if path.is_file()
            and "target" not in path.parts
            and (path.suffix in {".rs", ".toml"} or path.name == "build.rs")
        )
    return sorted(set(files))


def working_tree_context_files(repository: pathlib.Path) -> list[pathlib.Path]:
    files = [
        repository / name for name in ("Cargo.toml", "Cargo.lock", "rust-toolchain")
    ]
    files.append(
        repository
        / "tools"
        / "unialloc-rustc-pass"
        / "unialloc-rustc-mir-rewrite-dry-run.rs"
    )
    for source_root in (repository / "unialloc", repository / "alloc_macros"):
        files.extend(
            path
            for path in source_root.rglob("*")
            if path.is_file()
            and "target" not in path.relative_to(repository).parts
            and ".git" not in path.relative_to(repository).parts
        )
    missing = [path for path in files if not path.is_file()]
    if missing:
        raise CampaignError(
            "working-tree snapshot is missing required files: "
            + ",".join(str(path) for path in missing)
        )
    return sorted(set(files))


def working_tree_identity(repository: pathlib.Path) -> dict[str, Any]:
    canonical_files = implementation_files(repository)
    context_files = working_tree_context_files(repository)
    status = git_output(repository, "status", "--short")
    return {
        "repository_head": git_output(repository, "rev-parse", "HEAD"),
        "repository_status": status,
        "repository_status_sha256": sha256_bytes(status.encode()),
        "unialloc_implementation_sha256": tree_digest(
            canonical_files, repository, trailing_nul=True
        ),
        "campaign_snapshot_sha256": tree_digest(context_files, repository),
    }


def write_json(path: pathlib.Path, value: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    temporary.replace(path)


def write_bytes(path: pathlib.Path, content: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_bytes(content)
    temporary.replace(path)


def archive_build_complete_result(
    output: pathlib.Path, target_id: str, raw_dir: pathlib.Path
) -> dict[str, Any] | None:
    """Retain the preregistered build-only result before full publication."""
    if not output.is_file():
        return None
    content = output.read_bytes()
    try:
        previous = json.loads(content)
    except json.JSONDecodeError as error:
        raise CampaignError(
            f"existing target result is invalid JSON: {output}"
        ) from error
    if previous.get("status") != "build_complete":
        return None
    previous_hash = previous.get("suite_manifest_sha256")
    if previous_hash != PREREGISTRATION_SUITE_MANIFEST_SHA256:
        raise CampaignError(
            f"existing build-complete result has unexpected suite hash: {previous_hash}"
        )
    archive_dir = raw_dir / "results" / "build-complete"
    archive = archive_dir / (
        f"{target_id}-{PREREGISTRATION_SUITE_MANIFEST_SHA256[:12]}.json"
    )
    if archive.is_file() and archive.read_bytes() != content:
        raise CampaignError(f"build-complete archive collision: {archive}")
    write_bytes(archive, content)
    digest = hashlib.sha256(content).hexdigest()
    index_path = archive_dir / "index.json"
    index: dict[str, Any]
    if index_path.is_file():
        index = json.loads(index_path.read_text(encoding="utf-8"))
    else:
        index = {
            "schema_version": 1,
            "preregistration_suite_manifest_sha256": (
                PREREGISTRATION_SUITE_MANIFEST_SHA256
            ),
            "records": {},
        }
    records = index.setdefault("records", {})
    records[target_id] = {
        "archive_path": str(archive.resolve()),
        "bytes": len(content),
        "sha256": digest,
        "source_path": str(output.resolve()),
        "status": "build_complete",
    }
    write_json(index_path, index)
    return records[target_id]


def ensure_force_load_wrapper(path: pathlib.Path) -> pathlib.Path:
    """Install the campaign wrapper with global metadata dependency lookup."""
    path.parent.mkdir(parents=True, exist_ok=True)
    if (
        not path.exists()
        or path.read_text(encoding="utf-8") != FORCE_LOAD_WRAPPER_SOURCE
    ):
        path.write_text(FORCE_LOAD_WRAPPER_SOURCE, encoding="utf-8")
    path.chmod(0o755)
    return path.resolve()


def ensure_direct_load_wrapper(path: pathlib.Path) -> pathlib.Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    if (
        not path.exists()
        or path.read_text(encoding="utf-8") != DIRECT_LOAD_WRAPPER_SOURCE
    ):
        path.write_text(DIRECT_LOAD_WRAPPER_SOURCE, encoding="utf-8")
    path.chmod(0o755)
    return path.resolve()


def execute(
    argv: Sequence[str | os.PathLike[str]],
    *,
    cwd: pathlib.Path,
    env: Mapping[str, str],
    timeout: int,
) -> dict[str, Any]:
    return matrix.execute(argv, cwd=cwd, env=dict(env), timeout=timeout)


def suite_targets() -> dict[str, dict[str, Any]]:
    suite = json.loads(SUITE_PATH.read_text(encoding="utf-8"))
    return {str(target["id"]): target for target in suite["targets"]}


def validate_specs_against_suite() -> None:
    targets = suite_targets()
    for target_id, spec in TARGET_SPECS.items():
        target = targets[target_id]
        if target["source"]["commit"] != spec.commit:
            raise CampaignError(f"{target_id} source commit drifted from suite")
        expected = [row["id"] for row in target["harnesses"]]
        if expected != list(spec.harness_filters):
            raise CampaignError(f"{target_id} harness order drifted from suite")


def git_output(checkout: pathlib.Path, *args: str) -> str:
    result = execute(["git", *args], cwd=checkout, env=os.environ.copy(), timeout=120)
    if result["exit_code"] != 0:
        raise CampaignError(result["stderr"].decode(errors="replace"))
    return result["stdout"].decode(errors="replace").strip()


def verify_checkout(checkout: pathlib.Path, spec: TargetSpec) -> dict[str, Any]:
    if not (checkout / ".git").exists():
        raise CampaignError(f"missing checkout: {checkout}")
    head = git_output(checkout, "rev-parse", "HEAD")
    status = git_output(checkout, "status", "--short")
    if head != spec.commit:
        raise CampaignError(
            f"{spec.target_id} checkout is {head}; expected {spec.commit}"
        )
    if status:
        raise CampaignError(f"{spec.target_id} checkout is dirty:\n{status}")
    return {
        "checkout": str(checkout.resolve()),
        "head": head,
        "source_ref": spec.source_ref,
        "upstream_toolchain": spec.upstream_toolchain,
        "campaign_toolchain": TOOLCHAIN,
        "cargo_lock_sha256": sha256_file(checkout / "Cargo.lock"),
    }


def allocator_revision_archive_bytes(revision: str) -> tuple[str, bytes]:
    resolved = git_output(ROOT, "rev-parse", f"{revision}^{{commit}}")
    result = execute(
        [
            "git",
            "archive",
            "--format=tar",
            resolved,
            "Cargo.toml",
            "Cargo.lock",
            "rust-toolchain",
            "unialloc",
            "alloc_macros",
            "tools/unialloc-rustc-pass/unialloc-rustc-mir-rewrite-dry-run.rs",
        ],
        cwd=ROOT,
        env=os.environ.copy(),
        timeout=300,
    )
    if result["exit_code"] != 0 or result["timed_out"]:
        raise CampaignError(
            "allocator revision archive failed:\n"
            + result["stderr"].decode(errors="replace")[-8000:]
        )
    return resolved, result["stdout"]


def archive_allocator_revision(snapshot: pathlib.Path, revision: str) -> str:
    resolved, archive_bytes = allocator_revision_archive_bytes(revision)
    snapshot.mkdir(parents=True)
    with tarfile.open(fileobj=io.BytesIO(archive_bytes), mode="r:") as archive:
        archive.extractall(snapshot, filter="data")
    return resolved


def git_archive_context_identity(revision: str) -> tuple[str, int]:
    _, archive_bytes = allocator_revision_archive_bytes(revision)
    files: dict[str, bytes] = {}
    with tarfile.open(fileobj=io.BytesIO(archive_bytes), mode="r:") as archive:
        for member in archive.getmembers():
            if not member.isfile():
                continue
            source = archive.extractfile(member)
            if source is None:
                raise CampaignError(f"cannot read archived file: {member.name}")
            files[member.name] = source.read()
    digest = hashlib.sha256()
    for relative_path, content in sorted(files.items()):
        digest.update(relative_path.encode())
        digest.update(b"\0")
        digest.update(content)
    return digest.hexdigest(), len(files)


def snapshot_allocator(
    raw_dir: pathlib.Path,
    revision: str | None = None,
    *,
    current_working_tree: bool = False,
) -> dict[str, Any]:
    if current_working_tree and revision is not None:
        raise CampaignError(
            "current-working-tree snapshots cannot also select an allocator revision"
        )
    if not current_working_tree and revision is None:
        raise CampaignError("an exact allocator revision is required by default")
    snapshot = raw_dir / "frozen-unialloc"
    record_path = raw_dir / "frozen-unialloc.json"
    requested_head = (
        git_output(ROOT, "rev-parse", f"{revision}^{{commit}}") if revision else None
    )
    live_identity = working_tree_identity(ROOT) if current_working_tree else None
    existing: dict[str, Any] = {}
    if snapshot.is_dir() and record_path.is_file():
        existing = json.loads(record_path.read_text(encoding="utf-8"))
        expected_mode = {
            "source_kind": "working_tree" if current_working_tree else "git_revision",
            "snapshot_source": (
                "working_tree_copy" if current_working_tree else "git_archive"
            ),
        }
        observed_mode = {field: existing.get(field) for field in expected_mode}
        if observed_mode != expected_mode:
            raise CampaignError(
                "existing snapshot source mode does not match this campaign; "
                "use a fresh --raw-dir"
            )
        if current_working_tree:
            assert live_identity is not None
            expected = {
                "source_kind": "working_tree",
                **live_identity,
            }
            observed = {field: existing.get(field) for field in expected}
            if observed != expected:
                raise CampaignError(
                    "existing working-tree snapshot does not match the current tree; "
                    "use a fresh --raw-dir"
                )
        else:
            existing_head = existing.get("allocator_revision") or existing.get(
                "source_head"
            )
            if requested_head is not None and existing_head != requested_head:
                raise CampaignError(
                    f"frozen snapshot is {existing_head}; requested {requested_head}"
                )
    else:
        if snapshot.exists():
            shutil.rmtree(snapshot)
        if revision:
            requested_head = archive_allocator_revision(snapshot, revision)
        else:
            assert live_identity is not None
            (snapshot / "tools" / "unialloc-rustc-pass").mkdir(parents=True)
            for name in ("Cargo.toml", "Cargo.lock", "rust-toolchain"):
                shutil.copy2(ROOT / name, snapshot / name)
            shutil.copytree(
                ROOT / "unialloc",
                snapshot / "unialloc",
                ignore=shutil.ignore_patterns("target", ".git"),
            )
            shutil.copytree(
                ROOT / "alloc_macros",
                snapshot / "alloc_macros",
                ignore=shutil.ignore_patterns("target", ".git"),
            )
            pass_source = (
                ROOT
                / "tools"
                / "unialloc-rustc-pass"
                / "unialloc-rustc-mir-rewrite-dry-run.rs"
            )
            snapshot_pass = (
                snapshot
                / "tools"
                / "unialloc-rustc-pass"
                / "unialloc-rustc-mir-rewrite-dry-run.rs"
            )
            shutil.copy2(pass_source, snapshot_pass)
            observed_live_identity = working_tree_identity(ROOT)
            if observed_live_identity != live_identity:
                shutil.rmtree(snapshot, ignore_errors=True)
                raise CampaignError("working tree changed while it was being frozen")

    snapshot_pass = (
        snapshot
        / "tools"
        / "unialloc-rustc-pass"
        / "unialloc-rustc-mir-rewrite-dry-run.rs"
    )
    canonical_files = implementation_files(snapshot)
    context_files = working_tree_context_files(snapshot)
    canonical_digest = tree_digest(canonical_files, snapshot, trailing_nul=True)
    context_digest = tree_digest(context_files, snapshot)
    existing_digest = existing.get("unialloc_implementation_sha256")
    if existing_digest not in {None, canonical_digest}:
        raise CampaignError("frozen allocator snapshot digest changed")
    existing_context_digest = existing.get("campaign_snapshot_sha256")
    if existing and existing_context_digest != context_digest:
        raise CampaignError("frozen allocator snapshot context digest changed")
    if requested_head is not None:
        archive_context_digest, archive_context_count = git_archive_context_identity(
            requested_head
        )
        if (
            context_digest != archive_context_digest
            or len(context_files) != archive_context_count
        ):
            raise CampaignError(
                "frozen allocator snapshot differs from its Git archive"
            )
    record = {
        **existing,
        "path": str(snapshot.resolve()),
        "unialloc_implementation_sha256": canonical_digest,
        "campaign_snapshot_sha256": context_digest,
        "file_count": len(canonical_files),
        "implementation_file_count": len(canonical_files),
        "implementation_total_bytes": sum(
            path.stat().st_size for path in canonical_files
        ),
        "campaign_snapshot_file_count": len(context_files),
        "implementation_files": [
            path.relative_to(snapshot).as_posix() for path in canonical_files
        ],
        "campaign_snapshot_files": [
            path.relative_to(snapshot).as_posix() for path in context_files
        ],
        "implementation_digest_algorithm": (
            "sha256(sorted(relative_path + NUL + contents + NUL)); "
            "roots=unialloc,alloc_macros,tools/unialloc-rustc-pass source"
        ),
        "campaign_snapshot_digest_algorithm": (
            "sha256(sorted(relative_path + NUL + contents)); includes root "
            "Cargo.toml,Cargo.lock,rust-toolchain and every archived file under "
            "unialloc and alloc_macros"
        ),
        "pass_source": str(snapshot_pass.resolve()),
        "pass_source_sha256": sha256_file(snapshot_pass),
        "source_kind": "working_tree" if current_working_tree else "git_revision",
        "allocator_revision": requested_head
        or existing.get("allocator_revision")
        or existing.get("source_head")
        or git_output(ROOT, "rev-parse", "HEAD"),
        "snapshot_source": "git_archive" if requested_head else "working_tree_copy",
        "source_head": existing.get("source_head")
        or requested_head
        or git_output(ROOT, "rev-parse", "HEAD"),
        "source_status_sha256": existing.get("source_status_sha256")
        or sha256_bytes(git_output(ROOT, "status", "--short").encode()),
    }
    if current_working_tree:
        assert live_identity is not None
        if (
            canonical_digest != live_identity["unialloc_implementation_sha256"]
            or context_digest != live_identity["campaign_snapshot_sha256"]
        ):
            raise CampaignError("frozen working-tree snapshot digest mismatch")
        record.update(
            {
                "source_kind": "working_tree",
                "repository_head": live_identity["repository_head"],
                "repository_status": live_identity["repository_status"],
                "repository_status_sha256": live_identity["repository_status_sha256"],
                "allocator_revision": live_identity["repository_head"],
                "source_head": live_identity["repository_head"],
                "source_status_sha256": live_identity["repository_status_sha256"],
            }
        )
    write_json(record_path, record)
    return record


def build_mir_driver(
    raw_dir: pathlib.Path, snapshot: Mapping[str, Any], timeout: int
) -> pathlib.Path:
    output = raw_dir / "tools" / "unialloc-rustc-wrapper"
    output.parent.mkdir(parents=True, exist_ok=True)
    source = pathlib.Path(str(snapshot["pass_source"]))
    record_path = output.with_suffix(".build.json")
    if output.is_file() and record_path.is_file():
        cached = json.loads(record_path.read_text(encoding="utf-8"))
        if (
            cached.get("success") is True
            and cached.get("toolchain") == TOOLCHAIN
            and cached.get("source_sha256") == sha256_file(source)
            and cached.get("binary_sha256") == sha256_file(output)
        ):
            return output.resolve()
    env = os.environ.copy()
    env["RUSTC_BOOTSTRAP"] = "1"
    result = execute(
        [
            "rustc",
            f"+{TOOLCHAIN}",
            "--cfg",
            "unialloc_rustc_current",
            source,
            "-O",
            "-o",
            output,
        ],
        cwd=ROOT,
        env=env,
        timeout=timeout,
    )
    record = {
        "success": result["exit_code"] == 0 and output.is_file(),
        "toolchain": TOOLCHAIN,
        "source": str(source),
        "source_sha256": sha256_file(source),
        "command": result["command"],
        "exit_code": result["exit_code"],
        "stderr_sha256": sha256_bytes(result["stderr"]),
    }
    if output.is_file():
        record["binary_sha256"] = sha256_file(output)
    write_json(record_path, record)
    if not record["success"]:
        raise CampaignError(
            "MIR driver build failed:\n"
            + result["stderr"].decode(errors="replace")[-8000:]
        )
    return output.resolve()


def _build_unialloc_rlib_unlocked(
    raw_dir: pathlib.Path,
    snapshot: Mapping[str, Any],
    jobs: int,
    timeout: int,
    *,
    directory: str,
    features: tuple[str, ...],
) -> dict[str, Any]:
    snapshot_root = pathlib.Path(str(snapshot["path"]))
    target_dir = raw_dir / directory / "target"
    build_dir = raw_dir / directory
    record_path = build_dir / "build.json"
    if record_path.is_file():
        cached = json.loads(record_path.read_text(encoding="utf-8"))
        rlib = pathlib.Path(str(cached.get("rlib", "")))
        accepted_digests = {
            snapshot["unialloc_implementation_sha256"],
            snapshot.get("campaign_snapshot_sha256"),
        }
        if (
            cached.get("success") is True
            and cached.get("unialloc_implementation_sha256") in accepted_digests
            and cached.get("features") == list(features)
            and rlib.is_file()
            and cached.get("rlib_sha256") == sha256_file(rlib)
        ):
            if (
                cached.get("unialloc_implementation_sha256")
                != snapshot["unialloc_implementation_sha256"]
            ):
                cached["legacy_campaign_digest_alias"] = cached[
                    "unialloc_implementation_sha256"
                ]
                cached["unialloc_implementation_sha256"] = snapshot[
                    "unialloc_implementation_sha256"
                ]
                cached["campaign_snapshot_sha256"] = snapshot.get(
                    "campaign_snapshot_sha256"
                )
                write_json(record_path, cached)
            return cached
    if target_dir.exists():
        shutil.rmtree(target_dir)
    env = os.environ.copy()
    env.update(
        {
            "CARGO_INCREMENTAL": "0",
            "CARGO_PROFILE_RELEASE_PANIC": "unwind",
            "TMPDIR": str((raw_dir / "tmp").resolve()),
        }
    )
    pathlib.Path(env["TMPDIR"]).mkdir(parents=True, exist_ok=True)
    command: list[str | os.PathLike[str]] = [
        "cargo",
        f"+{TOOLCHAIN}",
        "build",
        "--release",
        "--locked",
        "--manifest-path",
        snapshot_root / "unialloc" / "Cargo.toml",
        "--lib",
    ]
    if features:
        command.extend(["--features", ",".join(features)])
    command.extend(["--target-dir", target_dir, "--jobs", str(jobs)])
    result = execute(
        command,
        cwd=snapshot_root,
        env=env,
        timeout=timeout,
    )
    (build_dir / "build.stdout").write_bytes(result["stdout"])
    (build_dir / "build.stderr").write_bytes(result["stderr"])
    candidates = sorted((target_dir / "release" / "deps").glob("libunialloc-*.rlib"))
    success = result["exit_code"] == 0 and len(candidates) == 1
    record: dict[str, Any] = {
        "success": success,
        "toolchain": TOOLCHAIN,
        "features": list(features),
        "default_features_enabled": True,
        "stats_enabled": False,
        "unialloc_implementation_sha256": snapshot["unialloc_implementation_sha256"],
        "campaign_snapshot_sha256": snapshot.get("campaign_snapshot_sha256"),
        "allocator_revision": snapshot.get("allocator_revision"),
        "command": result["command"],
        "exit_code": result["exit_code"],
        "candidate_count": len(candidates),
    }
    if len(candidates) == 1:
        rlib = candidates[0].resolve()
        record.update(
            {
                "rlib": str(rlib),
                "rlib_sha256": sha256_file(rlib),
                "dependency_dir": str(rlib.parent),
            }
        )
    write_json(record_path, record)
    if not success:
        raise CampaignError(
            f"{directory} rlib build failed:\n"
            + result["stderr"].decode(errors="replace")[-8000:]
        )
    return record


def build_force_load_rlib(
    raw_dir: pathlib.Path,
    snapshot: Mapping[str, Any],
    jobs: int,
    timeout: int,
) -> dict[str, Any]:
    lock_path = raw_dir / "force-load" / "build.lock"
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open("a+", encoding="utf-8") as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        return _build_unialloc_rlib_unlocked(
            raw_dir,
            snapshot,
            jobs,
            timeout,
            directory="force-load",
            features=("type_isolation",),
        )


def build_direct_load_rlib(
    raw_dir: pathlib.Path,
    snapshot: Mapping[str, Any],
    jobs: int,
    timeout: int,
) -> dict[str, Any]:
    lock_path = raw_dir / "direct-load" / "build.lock"
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open("a+", encoding="utf-8") as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        return _build_unialloc_rlib_unlocked(
            raw_dir,
            snapshot,
            jobs,
            timeout,
            directory="direct-load",
            features=(),
        )


def copy_checkout(source: pathlib.Path, destination: pathlib.Path) -> None:
    if destination.exists():
        shutil.rmtree(destination)
    shutil.copytree(
        source,
        destination,
        ignore=shutil.ignore_patterns(".git", "target", "evaluation", "__pycache__"),
    )


def allocator_source() -> str:
    return f"""\n{ALLOCATOR_MARKER}
#[global_allocator]
static UNIALLOC_PRIMARY_ALLOCATOR: unialloc::UniAlloc = unialloc::UniAlloc;
"""


def unified_patch(path: str, before: str, after: str) -> str:
    return "".join(
        difflib.unified_diff(
            before.splitlines(keepends=True),
            after.splitlines(keepends=True),
            fromfile=f"a/{path}",
            tofile=f"b/{path}",
        )
    )


def prepare_worktree(
    spec: TargetSpec,
    checkout: pathlib.Path,
    worktree: pathlib.Path,
    raw_dir: pathlib.Path,
) -> dict[str, Any]:
    copy_checkout(checkout, worktree)
    patch_parts: list[str] = []
    if spec.mode == "polars-driver":
        package = worktree / "crates" / "polars-unialloc-primary"
        (package / "src").mkdir(parents=True)
        (package / "Cargo.toml").write_text(POLARS_MANIFEST, encoding="utf-8")
        source = allocator_source().lstrip() + "\n" + POLARS_SOURCE
        (package / "src" / "main.rs").write_text(source, encoding="utf-8")
        patch_parts.append(
            unified_patch(
                "crates/polars-unialloc-primary/Cargo.toml", "", POLARS_MANIFEST
            )
        )
        patch_parts.append(
            unified_patch("crates/polars-unialloc-primary/src/main.rs", "", source)
        )
        manifest = package / "Cargo.toml"
    else:
        assert spec.allocator_source is not None
        source_path = worktree / spec.allocator_source
        before = source_path.read_text(encoding="utf-8")
        if ALLOCATOR_MARKER in before:
            raise CampaignError(f"allocator marker already exists in {source_path}")
        if spec.target_id == "swc":
            if "extern crate swc_malloc;" not in before:
                raise CampaignError("SWC benchmark allocator import is absent")
            body = before.replace("extern crate swc_malloc;", "", 1).lstrip()
        else:
            body = before
        after = allocator_source().lstrip() + "\n" + body
        source_path.write_text(after, encoding="utf-8")
        patch_parts.append(unified_patch(spec.allocator_source, before, after))
        manifest = (
            worktree / "crates" / "swc" / "Cargo.toml"
            if spec.target_id == "swc"
            else worktree / "Cargo.toml"
        )
    patch_path = raw_dir / "patches" / f"{spec.target_id}-allocator.patch"
    patch_path.parent.mkdir(parents=True, exist_ok=True)
    patch_path.write_text("".join(patch_parts), encoding="utf-8")
    return {
        "manifest": manifest,
        "patch": patch_path,
        "patch_sha256": sha256_file(patch_path),
        "worktree": worktree,
        "allocator_source": (
            worktree / "crates" / "polars-unialloc-primary" / "src" / "main.rs"
            if spec.mode == "polars-driver"
            else worktree / str(spec.allocator_source)
        ),
    }


def build_command(spec: TargetSpec, *, locked: bool) -> list[str]:
    if spec.mode == "polars-driver":
        command = [
            "cargo",
            f"+{TOOLCHAIN}",
            "build",
            "--release",
            "--package",
            spec.package,
            "--message-format=json-render-diagnostics",
        ]
    else:
        command = [
            "cargo",
            f"+{TOOLCHAIN}",
            "bench",
            "--package",
            spec.package,
            "--bench",
            str(spec.bench_name),
            "--no-run",
            "--message-format=json-render-diagnostics",
        ]
    if locked:
        command.insert(3, "--locked")
    return command


def cargo_executable(stdout: bytes, spec: TargetSpec) -> pathlib.Path:
    candidates: list[pathlib.Path] = []
    for raw in stdout.decode(errors="replace").splitlines():
        try:
            row = json.loads(raw)
        except json.JSONDecodeError:
            continue
        executable = row.get("executable") if isinstance(row, dict) else None
        target = row.get("target", {}) if isinstance(row, dict) else {}
        if not executable or not isinstance(target, dict):
            continue
        name = str(target.get("name", ""))
        wanted = spec.package if spec.mode == "polars-driver" else spec.bench_name
        if name == wanted:
            candidates.append(pathlib.Path(str(executable)))
    existing = [path for path in candidates if path.is_file()]
    if len(existing) != 1:
        raise CampaignError(
            f"{spec.target_id} build exposed {len(existing)} executables"
        )
    return existing[0]


def activation_proof(binary: pathlib.Path) -> dict[str, Any]:
    result = execute(
        ["nm", "-C", binary], cwd=binary.parent, env=os.environ.copy(), timeout=300
    )
    text = result["stdout"].decode(errors="replace")
    matches = [line for line in text.splitlines() if "unialloc::" in line]
    return {
        "success": result["exit_code"] == 0 and bool(matches),
        "command": result["command"],
        "exit_code": result["exit_code"],
        "unialloc_symbol_count": len(matches),
        "nm_stdout_sha256": sha256_bytes(result["stdout"]),
    }


def typeiso_environment(
    base: Mapping[str, str],
    *,
    wrapper: pathlib.Path,
    force_wrapper: pathlib.Path,
    force_load: Mapping[str, Any],
    audit_dir: pathlib.Path,
    pass_log_dir: pathlib.Path,
    spec: TargetSpec,
    policy_flags: int,
) -> dict[str, str]:
    sysroot = matrix.rustc_sysroot(TOOLCHAIN)
    env = matrix.typeiso_environment(
        dict(base),
        wrapper=force_wrapper,
        audit_dir=audit_dir,
        pass_log_dir=pass_log_dir,
        sysroot=sysroot,
        target_crates=spec.target_crates,
        policy_flags=policy_flags,
    )
    return matrix.force_load_environment(
        env, driver_wrapper=wrapper, force_load=dict(force_load)
    )


def build_variants(
    spec: TargetSpec,
    prepared: Mapping[str, Any],
    raw_dir: pathlib.Path,
    snapshot: Mapping[str, Any],
    wrapper: pathlib.Path,
    force_wrapper: pathlib.Path,
    force_load: Mapping[str, Any],
    direct_wrapper: pathlib.Path,
    direct_load: Mapping[str, Any],
    *,
    jobs: int,
    timeout: int,
    reuse: bool,
) -> dict[str, dict[str, Any]]:
    worktree = pathlib.Path(str(prepared["worktree"]))
    binaries: dict[str, dict[str, Any]] = {}
    allocator_digest = str(snapshot["unialloc_implementation_sha256"])
    campaign_snapshot_digest = str(snapshot.get("campaign_snapshot_sha256", ""))
    force_wrapper_digest = sha256_file(force_wrapper)
    direct_wrapper_digest = sha256_file(direct_wrapper)
    for variant in BUILD_ORDER:
        target_dir = raw_dir / "cargo-targets" / spec.target_id / variant
        build_wrapper = direct_wrapper if variant == "unialloc" else force_wrapper
        build_wrapper_digest = (
            direct_wrapper_digest if variant == "unialloc" else force_wrapper_digest
        )
        record_path = raw_dir / "builds" / spec.target_id / variant / "build.json"
        if reuse and record_path.is_file():
            cached = json.loads(record_path.read_text(encoding="utf-8"))
            binary = pathlib.Path(str(cached.get("binary", "")))
            accepted_digests = {allocator_digest, campaign_snapshot_digest}
            if (
                cached.get("success") is True
                and cached.get("unialloc_implementation_sha256") in accepted_digests
                and (
                    cached.get("build_wrapper_sha256")
                    or cached.get("force_load_wrapper_sha256")
                )
                == build_wrapper_digest
                and binary.is_file()
                and cached.get("binary_sha256") == sha256_file(binary)
            ):
                if cached.get("unialloc_implementation_sha256") != allocator_digest:
                    cached["legacy_campaign_digest_alias"] = cached[
                        "unialloc_implementation_sha256"
                    ]
                    cached["unialloc_implementation_sha256"] = allocator_digest
                    cached["campaign_snapshot_sha256"] = campaign_snapshot_digest
                cached["build_wrapper"] = str(build_wrapper.resolve())
                cached["build_wrapper_sha256"] = build_wrapper_digest
                write_json(record_path, cached)
                binaries[variant] = cached
                continue
        audit_dir = raw_dir / "audits" / spec.target_id / variant
        pass_log_dir = raw_dir / "pass-logs" / spec.target_id / variant
        shutil.rmtree(audit_dir, ignore_errors=True)
        shutil.rmtree(pass_log_dir, ignore_errors=True)
        audit_dir.mkdir(parents=True)
        pass_log_dir.mkdir(parents=True)
        env = os.environ.copy()
        env.update(
            {
                "CARGO_TARGET_DIR": str(target_dir.resolve()),
                "CARGO_INCREMENTAL": "0",
                "TMPDIR": str((raw_dir / "tmp").resolve()),
                "RUSTFLAGS": f"--cfg unialloc_primary_variant_{variant}",
                "POLARS_MAX_THREADS": "1",
                "RAYON_NUM_THREADS": "1",
                "TOKIO_WORKER_THREADS": "1",
                "PYTHON_SYS_EXECUTABLE": shutil.which("python3") or "python3",
            }
        )
        if spec.target_id == "swc":
            env["RUSTFLAGS"] += (
                " -Zshare-generics=y -C target-feature=+sse2 -C link-args=-Wl,-z,nodelete"
            )
        if variant in {"typed_plain", "typeiso_perf"}:
            env = typeiso_environment(
                env,
                wrapper=wrapper,
                force_wrapper=force_wrapper,
                force_load=force_load,
                audit_dir=audit_dir,
                pass_log_dir=pass_log_dir,
                spec=spec,
                policy_flags=0 if variant == "typed_plain" else 1,
            )
            # Adding the temporary Polars workspace package changes the
            # workspace lock graph even though every upstream dependency stays
            # source-pinned.  The before/after lock hashes are retained.
            locked = spec.target_id != "polars"
        else:
            direct_target = (
                spec.package.replace("-", "_")
                if spec.mode == "polars-driver"
                else str(spec.bench_name).replace("-", "_")
            )
            env.update(
                {
                    "RUSTC_WORKSPACE_WRAPPER": str(direct_wrapper.resolve()),
                    "UNIALLOC_DIRECT_LOAD_RLIB": str(direct_load["rlib"]),
                    "UNIALLOC_DIRECT_LOAD_DEPENDENCY_DIR": str(
                        direct_load["dependency_dir"]
                    ),
                    "UNIALLOC_DIRECT_LOAD_TARGET_CRATE": direct_target,
                }
            )
            locked = spec.target_id != "polars"
        command = build_command(spec, locked=locked)
        command.extend(["--jobs", str(jobs)])
        started = time.time()
        result = execute(command, cwd=worktree, env=env, timeout=timeout)
        build_dir = record_path.parent
        build_dir.mkdir(parents=True, exist_ok=True)
        (build_dir / "build.stdout").write_bytes(result["stdout"])
        (build_dir / "build.stderr").write_bytes(result["stderr"])
        executable: pathlib.Path | None = None
        error: str | None = (
            "build timed out"
            if result["timed_out"]
            else (
                f"build exited with status {result['exit_code']}"
                if result["exit_code"] != 0
                else None
            )
        )
        if result["exit_code"] == 0 and not result["timed_out"]:
            try:
                executable = cargo_executable(result["stdout"], spec)
            except CampaignError as caught:
                error = str(caught)
        output_binary = (
            raw_dir
            / "binaries"
            / spec.target_id
            / variant
            / (
                "polars-primary"
                if spec.mode == "polars-driver"
                else str(spec.bench_name)
            )
        )
        if executable is not None:
            output_binary.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(executable, output_binary)
        audit = None
        if (
            result["exit_code"] == 0
            and not result["timed_out"]
            and variant in {"typed_plain", "typeiso_perf"}
        ):
            audit = matrix.summarize_audits(audit_dir)
        actual_mir = False
        if audit is not None:
            try:
                matrix.validate_typeiso_audits(audit, spec.target_crates)
                actual_mir = True
            except matrix.MatrixError as caught:
                error = str(caught)
        activation = (
            activation_proof(output_binary)
            if output_binary.is_file()
            else {"success": False}
        )
        success = (
            result["exit_code"] == 0
            and not result["timed_out"]
            and output_binary.is_file()
            and bool(activation.get("success"))
            and (variant == "unialloc" or actual_mir)
        )
        record: dict[str, Any] = {
            "success": success,
            "target_id": spec.target_id,
            "variant": variant,
            "source_commit": spec.commit,
            "source_ref": spec.source_ref,
            "toolchain": TOOLCHAIN,
            "upstream_toolchain": spec.upstream_toolchain,
            "unialloc_implementation_sha256": allocator_digest,
            "implementation_sha256": allocator_digest,
            "implementation_revision": snapshot.get("allocator_revision"),
            "campaign_snapshot_sha256": campaign_snapshot_digest,
            "allocator_revision": snapshot.get("allocator_revision"),
            "build_wrapper": str(build_wrapper.resolve()),
            "build_wrapper_sha256": build_wrapper_digest,
            "typeiso_force_load_wrapper": (
                str(force_wrapper.resolve())
                if variant in {"typed_plain", "typeiso_perf"}
                else None
            ),
            "direct_load_rlib": (
                str(direct_load["rlib"]) if variant == "unialloc" else None
            ),
            "direct_load_rlib_sha256": (
                str(direct_load["rlib_sha256"]) if variant == "unialloc" else None
            ),
            "stats_enabled": False,
            "policy_flags": (
                0
                if variant == "typed_plain"
                else (1 if variant == "typeiso_perf" else None)
            ),
            "command": result["command"],
            "build_started_unix": started,
            "build_wall_seconds": result["wall_seconds"],
            "exit_code": result["exit_code"],
            "timed_out": result["timed_out"],
            "binary": str(output_binary.resolve()) if output_binary.is_file() else None,
            "binary_sha256": sha256_file(output_binary)
            if output_binary.is_file()
            else None,
            "activation": activation,
            "actual_mir_provenance": actual_mir,
            "audit": audit,
            "audit_dir": str(audit_dir.resolve()),
            "pass_log_dir": str(pass_log_dir.resolve()),
            "allocator_patch": str(pathlib.Path(str(prepared["patch"])).resolve()),
            "allocator_patch_sha256": prepared["patch_sha256"],
            "cargo_lock_sha256": sha256_file(worktree / "Cargo.lock"),
            "error": error,
        }
        write_json(record_path, record)
        if not success:
            raise CampaignError(
                f"{spec.target_id}/{variant} build failed: {error or result['stderr'].decode(errors='replace')[-8000:]}"
            )
        shutil.rmtree(target_dir, ignore_errors=True)
        record["disposable_cargo_target_removed"] = True
        write_json(record_path, record)
        binaries[variant] = record
    digests = {
        str(record["unialloc_implementation_sha256"]) for record in binaries.values()
    }
    if digests != {allocator_digest}:
        raise CampaignError(
            f"{spec.target_id} variants do not share one allocator implementation"
        )
    return binaries


def generate_polars_csv(path: pathlib.Path) -> dict[str, Any]:
    if path.is_file():
        return {
            "path": str(path.resolve()),
            "rows": 1_000_000,
            "sha256": sha256_file(path),
            "reused": True,
        }
    path.parent.mkdir(parents=True, exist_ok=True)
    state = 0

    def nxt() -> int:
        nonlocal state
        state = (state * 6_364_136_223_846_793_005 + 1_442_695_040_888_963_407) & (
            (1 << 64) - 1
        )
        return state

    with path.open("w", encoding="utf-8") as handle:
        handle.write("id1,id2,id3,id4,id5,id6,v1,v2,v3\n")
        for _ in range(1_000_000):
            values = (
                f"id{nxt() % 100 + 1:03d}",
                f"id{nxt() % 100 + 1:03d}",
                f"id{nxt() % 10_000 + 1:010d}",
                str(nxt() % 100 + 1),
                str(nxt() % 100 + 1),
                str(nxt() % 10_000 + 1),
                str(nxt() % 5 + 1),
                str(nxt() % 15 + 1),
                f"{(nxt() % 100_000_000) / 1_000_000.0:.6f}",
            )
            handle.write(",".join(values) + "\n")
    return {
        "path": str(path.resolve()),
        "rows": 1_000_000,
        "sha256": sha256_file(path),
        "reused": False,
    }


def parse_gnu_time(path: pathlib.Path) -> dict[str, Any]:
    rows = [
        line
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.startswith("UNIALLOC_PRIMARY_TIME\t")
    ]
    if len(rows) != 1:
        raise CampaignError(f"GNU time emitted {len(rows)} records: {path}")
    fields = rows[0].split("\t")
    return {
        "user_seconds": float(fields[1]),
        "system_seconds": float(fields[2]),
        "cpu_percent": float(fields[3].removesuffix("%")),
        "peak_rss_kib": int(fields[4]),
        "major_faults": int(fields[5]),
        "minor_faults": int(fields[6]),
        "involuntary_switches": int(fields[7]),
        "voluntary_switches": int(fields[8]),
        "time_exit_code": int(fields[9]),
    }


ANSI_RE = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")
CRITERION_TIME_RE = re.compile(
    r"time:\s*\[\s*([0-9.]+)\s*(ps|ns|us|µs|ms|s)\s+"
    r"([0-9.]+)\s*(ps|ns|us|µs|ms|s)\s+"
    r"([0-9.]+)\s*(ps|ns|us|µs|ms|s)\s*\]"
)


def unit_seconds(value: float, unit: str) -> float:
    scales = {"ps": 1e-12, "ns": 1e-9, "us": 1e-6, "µs": 1e-6, "ms": 1e-3, "s": 1.0}
    return value * scales[unit]


def criterion_seconds(stdout: bytes) -> float:
    text = ANSI_RE.sub("", stdout.decode(errors="replace").replace("\r", "\n"))
    matches = list(CRITERION_TIME_RE.finditer(text))
    if len(matches) != 1:
        raise CampaignError(f"expected one Criterion estimate, found {len(matches)}")
    match = matches[0]
    value = unit_seconds(float(match.group(3)), match.group(4))
    if not math.isfinite(value) or value <= 0:
        raise CampaignError("Criterion median estimate is not finite and positive")
    return value


def polars_result(stdout: bytes, harness_id: str) -> tuple[float, str]:
    rows: list[dict[str, Any]] = []
    for line in stdout.decode(errors="replace").splitlines():
        try:
            value = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(value, dict) and value.get("harness_id") == harness_id:
            rows.append(value)
    if len(rows) != 1:
        raise CampaignError(f"Polars emitted {len(rows)} result records")
    seconds = float(rows[0].get("operation_seconds", 0))
    fingerprint = str(rows[0].get("fingerprint", ""))
    if not math.isfinite(seconds) or seconds <= 0 or not fingerprint:
        raise CampaignError("Polars result record is incomplete")
    return seconds, fingerprint


def command_for(
    spec: TargetSpec,
    binary: pathlib.Path,
    harness_id: str,
    polars_csv: pathlib.Path | None,
) -> list[str]:
    selector = spec.harness_filters[harness_id]
    if spec.mode == "polars-driver":
        command = [str(binary), selector]
        if harness_id == "csv_scan":
            if polars_csv is None:
                raise CampaignError("Polars CSV input is missing")
            command.append(str(polars_csv.resolve()))
        return command
    return [
        str(binary),
        "--bench",
        selector,
        "--warm-up-time",
        "0.05",
        "--measurement-time",
        "0.10",
        "--sample-size",
        "10",
        "--noplot",
    ]


def measurement_prefix(
    cpu_list: str = MEASUREMENT_CPU_LIST,
    numa_node: str = MEASUREMENT_NUMA_NODE,
) -> list[str]:
    return [
        "numactl",
        f"--physcpubind={cpu_list}",
        f"--membind={numa_node}",
        "taskset",
        "-c",
        cpu_list,
    ]


def rustpython_runtime_contract() -> dict[str, Any]:
    python_executable = pathlib.Path(sys.executable).resolve()
    library_directory = python_executable.parent.parent / "lib"
    soname = f"libpython{sys.version_info.major}.{sys.version_info.minor}.so.1.0"
    library = library_directory / soname
    if not library.is_file():
        raise CampaignError(f"RustPython runtime library is missing: {library}")
    return {
        "python_executable": str(python_executable),
        "library_directory": str(library_directory.resolve()),
        "library_path": str(library.resolve()),
        "library_soname": soname,
        "library_bytes": library.stat().st_size,
        "library_sha256": sha256_file(library),
    }


def apply_runtime_environment(
    spec: TargetSpec,
    environment: Mapping[str, str],
    runtime_dependency: Mapping[str, Any] | None,
) -> dict[str, str]:
    result = dict(environment)
    if spec.target_id != "rustpython":
        return result
    if runtime_dependency is None:
        raise CampaignError("RustPython runtime dependency contract is missing")
    library_directory = str(runtime_dependency["library_directory"])
    current = result.get("LD_LIBRARY_PATH", "")
    entries = [library_directory]
    entries.extend(
        entry for entry in current.split(":") if entry and entry != library_directory
    )
    result["LD_LIBRARY_PATH"] = ":".join(entries)
    return result


def prove_rustpython_runtime(
    binaries: Mapping[str, Mapping[str, Any]],
    raw_dir: pathlib.Path,
    runtime_dependency: Mapping[str, Any],
) -> dict[str, Any]:
    proof_dir = raw_dir / "dynamic-dependencies" / "rustpython"
    proof_dir.mkdir(parents=True, exist_ok=True)
    environment = apply_runtime_environment(
        TARGET_SPECS["rustpython"], os.environ.copy(), runtime_dependency
    )
    variants: dict[str, Any] = {}
    library_path = str(runtime_dependency["library_path"])
    library_soname = str(runtime_dependency["library_soname"])
    expected_digest = str(runtime_dependency["library_sha256"])
    for variant in VARIANTS:
        binary = pathlib.Path(str(binaries[variant]["binary"]))
        result = execute(["ldd", binary], cwd=ROOT, env=environment, timeout=60)
        output = proof_dir / f"{variant}.ldd"
        output.write_bytes(result["stdout"])
        resolved = result["stdout"].decode(errors="replace")
        match = re.search(
            rf"^\s*{re.escape(library_soname)}\s*=>\s*(\S+)",
            resolved,
            re.MULTILINE,
        )
        resolved_library = (
            pathlib.Path(match.group(1)).resolve() if match is not None else None
        )
        resolved_digest = (
            sha256_file(resolved_library)
            if resolved_library is not None and resolved_library.is_file()
            else None
        )
        success = (
            result["exit_code"] == 0
            and not result["timed_out"]
            and resolved_library is not None
            and str(resolved_library) == library_path
            and resolved_digest == expected_digest
        )
        variants[variant] = {
            "success": success,
            "binary_sha256": sha256_file(binary),
            "resolved_library_soname": library_soname,
            "resolved_library_sha256": resolved_digest,
            "ldd_record": str(output.resolve()),
            "ldd_record_sha256": sha256_file(output),
        }
        if not success:
            raise CampaignError(f"RustPython {variant} did not resolve {library_path}")
    resolved_sonames = {
        record["resolved_library_soname"] for record in variants.values()
    }
    resolved_digests = {
        record["resolved_library_sha256"] for record in variants.values()
    }
    if resolved_sonames != {library_soname} or resolved_digests != {expected_digest}:
        raise CampaignError(
            "RustPython variants did not resolve one identical libpython"
        )
    return {**dict(runtime_dependency), "variants": variants}


BUILD_PROCESS_NAMES = {
    "cargo",
    "rustc",
    "cc",
    "cc1",
    "cc1plus",
    "clang",
    "clang++",
    "gcc",
    "g++",
    "ld",
    "ld.lld",
    "lld",
    "make",
    "ninja",
}


def is_build_process(comm: str, command: str) -> bool:
    if comm in BUILD_PROCESS_NAMES:
        return True
    return "--crate-name" in command and any(
        marker in command
        for marker in (
            "/rustc",
            "unialloc-rustc-wrapper",
            "unialloc-force-load-wrapper",
        )
    )


def active_build_processes(
    cpu_list: str = MEASUREMENT_CPU_LIST,
) -> list[dict[str, Any]]:
    measured_cpus = parse_cpu_list(cpu_list)
    rows: list[dict[str, Any]] = []
    for process_dir in pathlib.Path("/proc").iterdir():
        if not process_dir.name.isdigit():
            continue
        pid = int(process_dir.name)
        try:
            comm = (process_dir / "comm").read_text(encoding="utf-8").strip()
            command = (
                (process_dir / "cmdline")
                .read_bytes()
                .replace(b"\0", b" ")
                .decode(errors="replace")
                .strip()
            )
            overlap = sorted(os.sched_getaffinity(pid) & measured_cpus)
        except (FileNotFoundError, PermissionError, ProcessLookupError):
            continue
        if overlap and is_build_process(comm, command):
            rows.append(
                {
                    "pid": pid,
                    "comm": comm,
                    "command": command[:1024],
                    "measurement_cpu_overlap": overlap,
                }
            )
    return sorted(rows, key=lambda row: int(row["pid"]))


def wait_for_build_quiescence(
    raw_dir: pathlib.Path,
    target_id: str,
    *,
    timeout: int,
    quiet_seconds: int,
    cpu_list: str = MEASUREMENT_CPU_LIST,
    numa_node: str = MEASUREMENT_NUMA_NODE,
) -> dict[str, Any]:
    started = time.time()
    quiet_started: float | None = None
    observations = 0
    last_active: list[dict[str, Any]] = []
    while True:
        observations += 1
        active = active_build_processes(cpu_list)
        now = time.time()
        if active:
            last_active = active
            quiet_started = None
        elif quiet_started is None:
            quiet_started = now
        elif now - quiet_started >= quiet_seconds:
            record = {
                "success": True,
                "target_id": target_id,
                "cpu_list": cpu_list,
                "numa_node": int(numa_node),
                "started_unix": started,
                "verified_unix": now,
                "wait_seconds": now - started,
                "required_quiet_seconds": quiet_seconds,
                "observations": observations,
                "active_build_processes_at_verification": [],
                "last_active_build_processes": last_active,
                "load_average_at_verification": list(os.getloadavg()),
            }
            write_json(
                raw_dir / "quiescence" / f"{target_id}-before-measured.json",
                record,
            )
            return record
        if now - started >= timeout:
            record = {
                "success": False,
                "target_id": target_id,
                "cpu_list": cpu_list,
                "numa_node": int(numa_node),
                "started_unix": started,
                "failed_unix": now,
                "timeout_seconds": timeout,
                "required_quiet_seconds": quiet_seconds,
                "observations": observations,
                "active_build_processes": active,
                "last_active_build_processes": last_active,
            }
            evidence = raw_dir / "quiescence" / f"{target_id}-before-measured.json"
            write_json(evidence, record)
            raise CampaignError(
                f"build quiescence timed out; evidence retained at {evidence}"
            )
        time.sleep(1.0)


@contextlib.contextmanager
def primary_measurement_lock(target_id: str, phase: str) -> Iterable[dict[str, Any]]:
    MEASUREMENT_LOCK.parent.mkdir(parents=True, exist_ok=True)
    with MEASUREMENT_LOCK.open("a+", encoding="utf-8") as handle:
        waited_at = time.time()
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        acquired_at = time.time()
        metadata = {
            "lock_path": str(MEASUREMENT_LOCK.resolve()),
            "target_id": target_id,
            "phase": phase,
            "pid": os.getpid(),
            "wait_seconds": acquired_at - waited_at,
            "acquired_unix": acquired_at,
        }
        handle.seek(0)
        handle.truncate()
        handle.write(json.dumps(metadata, sort_keys=True) + "\n")
        handle.flush()
        try:
            yield metadata
        finally:
            metadata["released_unix"] = time.time()
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def run_one(
    spec: TargetSpec,
    variant: str,
    harness_id: str,
    binary: pathlib.Path,
    worktree: pathlib.Path,
    raw_dir: pathlib.Path,
    *,
    phase: str,
    index: int,
    timeout: int,
    polars_csv: pathlib.Path | None,
    runtime_dependency: Mapping[str, Any] | None,
    cpu_list: str,
    numa_node: str,
) -> dict[str, Any]:
    run_dir = raw_dir / "runs" / spec.target_id / harness_id / variant
    run_dir.mkdir(parents=True, exist_ok=True)
    stem = f"{phase}-{index:02d}"
    time_path = run_dir / f"{stem}.time"
    stdout_path = run_dir / f"{stem}.stdout"
    stderr_path = run_dir / f"{stem}.stderr"
    command = command_for(spec, binary, harness_id, polars_csv)
    pinned_command = [*measurement_prefix(cpu_list, numa_node), *command]
    env = os.environ.copy()
    env.update(
        {
            "POLARS_MAX_THREADS": "1",
            "RAYON_NUM_THREADS": "1",
            "TOKIO_WORKER_THREADS": "1",
            "PYTHON_SYS_EXECUTABLE": shutil.which("python3") or "python3",
            "TMPDIR": str((raw_dir / "tmp").resolve()),
        }
    )
    env = apply_runtime_environment(spec, env, runtime_dependency)
    started = time.perf_counter_ns()
    load_before = os.getloadavg()
    result = execute(
        [
            "/usr/bin/time",
            "-f",
            GNU_TIME_FORMAT,
            "-o",
            time_path,
            "--",
            *pinned_command,
        ],
        cwd=worktree,
        env=env,
        timeout=timeout,
    )
    wall_seconds = (time.perf_counter_ns() - started) / 1_000_000_000
    load_after = os.getloadavg()
    stdout_path.write_bytes(result["stdout"])
    stderr_path.write_bytes(result["stderr"])
    metrics = parse_gnu_time(time_path)
    fingerprint: str | None = None
    if spec.mode == "polars-driver":
        performance, fingerprint = polars_result(result["stdout"], harness_id)
        performance_source = "benchmark-owned-operation-seconds"
    else:
        performance = criterion_seconds(result["stdout"])
        performance_source = "criterion-median-seconds-per-iteration"
    success = (
        result["exit_code"] == 0
        and not result["timed_out"]
        and metrics["time_exit_code"] == 0
        and performance > 0
        and metrics["peak_rss_kib"] > 0
    )
    record = {
        "success": success,
        "target_id": spec.target_id,
        "harness_id": harness_id,
        "variant": variant,
        "phase": phase,
        "index": index,
        "round": 0 if phase == "warmup" else index,
        "command": command,
        "measured_command": pinned_command,
        "cpu_affinity": cpu_list,
        "numa_memory_node": int(numa_node),
        "load_average_before": list(load_before),
        "load_average_after": list(load_after),
        "performance": performance,
        "performance_source": performance_source,
        "peak_rss_mib": metrics["peak_rss_kib"] / 1024.0,
        "process_wall_seconds": wall_seconds,
        "result_fingerprint": fingerprint,
        "stdout": str(stdout_path.resolve()),
        "stderr": str(stderr_path.resolve()),
        "time_record": str(time_path.resolve()),
        "stdout_sha256": sha256_file(stdout_path),
        "stderr_sha256": sha256_file(stderr_path),
        **metrics,
    }
    if runtime_dependency is not None:
        record["dynamic_runtime_dependency"] = dict(runtime_dependency)
    record_path = run_dir / f"{stem}.json"
    record["raw_record"] = str(record_path.resolve())
    write_json(record_path, record)
    if not success:
        raise CampaignError(f"failed run: {spec.target_id}/{harness_id}/{variant}")
    return record


def rotated(values: Sequence[str], offset: int) -> tuple[str, ...]:
    pivot = offset % len(values)
    return tuple(values[pivot:]) + tuple(values[:pivot])


def route_equivalent(measurements: Sequence[Mapping[str, Any]]) -> tuple[bool, float]:
    by_key = {
        (int(row["round"]), str(row["variant"])): float(row["performance"])
        for row in measurements
    }
    rounds = sorted({key[0] for key in by_key})
    ratios = [
        by_key[(round_number, "typed_plain")] / by_key[(round_number, "unialloc")]
        for round_number in rounds
    ]
    median = statistics.median(ratios)
    return ROUTE_RATIO_MIN <= median <= ROUTE_RATIO_MAX, median


def warmup_evidence(
    rows: Sequence[Mapping[str, Any]],
) -> dict[str, list[dict[str, str]]]:
    evidence: dict[str, list[dict[str, str]]] = {variant: [] for variant in VARIANTS}
    for row in rows:
        variant = str(row["variant"])
        record_path = pathlib.Path(str(row["raw_record"]))
        evidence[variant].append(
            {
                "record_path": str(record_path.resolve()),
                "sha256": sha256_file(record_path),
            }
        )
    return evidence


def harness_gates(
    binaries: Mapping[str, Mapping[str, Any]],
    *,
    correctness: bool,
    compiler_route_equivalent: bool,
) -> dict[str, bool]:
    build_values = list(binaries.values())
    return {
        "correctness": correctness,
        "build_success": all(record.get("success") is True for record in build_values),
        "allocator_activation": all(
            bool(record.get("activation", {}).get("success")) for record in build_values
        ),
        "actual_mir_provenance": all(
            bool(binaries[variant].get("actual_mir_provenance"))
            for variant in ("typed_plain", "typeiso_perf")
        ),
        "stats_disabled": all(
            record.get("stats_enabled") is False for record in build_values
        ),
        "compiler_route_equivalent": compiler_route_equivalent,
        "source_audit_retained": all(
            pathlib.Path(str(record["audit_dir"])).is_dir()
            and pathlib.Path(str(record["allocator_patch"])).is_file()
            for record in build_values
        ),
    }


def performance_contract(spec: TargetSpec) -> dict[str, str]:
    return {
        "performance_unit": "seconds",
        "performance_source": (
            "benchmark-owned-operation-seconds"
            if spec.mode == "polars-driver"
            else "criterion-median-seconds-per-iteration"
        ),
    }


def measure_target(
    spec: TargetSpec,
    binaries: Mapping[str, Mapping[str, Any]],
    worktree: pathlib.Path,
    raw_dir: pathlib.Path,
    *,
    warmups: int,
    rounds: int,
    timeout: int,
    include_rounds: bool,
    quiescence_timeout: int,
    quiescence_seconds: int,
    cpu_list: str,
    numa_node: str,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    polars_input: dict[str, Any] | None = None
    polars_csv: pathlib.Path | None = None
    runtime_dependency: dict[str, Any] | None = None
    runtime_proof: dict[str, Any] | None = None
    if spec.target_id == "polars":
        polars_csv = raw_dir / "inputs" / "polars-groupby-1m.csv"
        polars_input = generate_polars_csv(polars_csv)
        write_json(raw_dir / "inputs" / "polars-groupby-1m.json", polars_input)
    elif spec.target_id == "rustpython":
        runtime_dependency = rustpython_runtime_contract()
        runtime_proof = prove_rustpython_runtime(binaries, raw_dir, runtime_dependency)
    harness_results: list[dict[str, Any]] = []
    preflights: dict[str, list[dict[str, Any]]] = {
        harness_id: [] for harness_id in spec.harness_filters
    }
    lock_records: list[dict[str, Any]] = []
    with primary_measurement_lock(spec.target_id, "warmup") as lock_record:
        for harness_index, harness_id in enumerate(spec.harness_filters):
            for warmup in range(1, warmups + 1):
                for variant in rotated(VARIANTS, harness_index + warmup - 1):
                    record = run_one(
                        spec,
                        variant,
                        harness_id,
                        pathlib.Path(str(binaries[variant]["binary"])),
                        worktree,
                        raw_dir,
                        phase="warmup",
                        index=warmup,
                        timeout=timeout,
                        polars_csv=polars_csv,
                        runtime_dependency=runtime_dependency,
                        cpu_list=cpu_list,
                        numa_node=numa_node,
                    )
                    preflights[harness_id].append(record)
    lock_records.append(dict(lock_record))
    if not include_rounds:
        harness_results = []
        for harness_id in spec.harness_filters:
            fingerprints = {
                str(row["result_fingerprint"])
                for row in preflights[harness_id]
                if row.get("result_fingerprint") is not None
            }
            correctness = all(row["success"] for row in preflights[harness_id]) and (
                spec.mode == "criterion" or len(fingerprints) == 1
            )
            harness_results.append(
                {
                    "id": harness_id,
                    "metric_direction": "lower_is_better",
                    **performance_contract(spec),
                    "gates": harness_gates(
                        binaries,
                        correctness=correctness,
                        compiler_route_equivalent=False,
                    ),
                    "measurements": [],
                    "warmup_evidence": warmup_evidence(preflights[harness_id]),
                    "measurement_state": "ready_for_locked_rounds",
                }
            )
        return harness_results, {
            "polars_input": polars_input,
            "rustpython_dynamic_runtime": runtime_proof,
            "measurement_locks": lock_records,
            "measurement_affinity": {
                "cpu_list": cpu_list,
                "numa_node": int(numa_node),
            },
        }
    measured_rows: dict[str, list[dict[str, Any]]] = {
        harness_id: [] for harness_id in spec.harness_filters
    }
    quiescence: dict[str, Any] | None = None
    with primary_measurement_lock(spec.target_id, "measured") as lock_record:
        quiescence = wait_for_build_quiescence(
            raw_dir,
            spec.target_id,
            timeout=quiescence_timeout,
            quiet_seconds=quiescence_seconds,
            cpu_list=cpu_list,
            numa_node=numa_node,
        )
        for harness_index, harness_id in enumerate(spec.harness_filters):
            rows = measured_rows[harness_id]
            for round_number in range(1, rounds + 1):
                for variant in rotated(VARIANTS, harness_index + round_number - 1):
                    record = run_one(
                        spec,
                        variant,
                        harness_id,
                        pathlib.Path(str(binaries[variant]["binary"])),
                        worktree,
                        raw_dir,
                        phase="round",
                        index=round_number,
                        timeout=timeout,
                        polars_csv=polars_csv,
                        runtime_dependency=runtime_dependency,
                        cpu_list=cpu_list,
                        numa_node=numa_node,
                    )
                    rows.append(
                        {
                            "round": round_number,
                            "variant": variant,
                            "performance": record["performance"],
                            "peak_rss_mib": record["peak_rss_mib"],
                            "raw_record": str(
                                (
                                    raw_dir
                                    / "runs"
                                    / spec.target_id
                                    / harness_id
                                    / variant
                                    / f"round-{round_number:02d}.json"
                                ).resolve()
                            ),
                        }
                    )
    lock_records.append(dict(lock_record))
    for harness_id in spec.harness_filters:
        rows = measured_rows[harness_id]
        fingerprints = {
            str(row["result_fingerprint"])
            for row in preflights[harness_id]
            if row.get("result_fingerprint") is not None
        }
        correctness = all(row["success"] for row in preflights[harness_id]) and (
            spec.mode == "criterion" or len(fingerprints) == 1
        )
        route_gate, route_median = route_equivalent(rows)
        gates = harness_gates(
            binaries,
            correctness=correctness,
            compiler_route_equivalent=route_gate,
        )
        harness_results.append(
            {
                "id": harness_id,
                "metric_direction": "lower_is_better",
                **performance_contract(spec),
                "gates": gates,
                "compiler_route_median_typed_plain_over_unialloc": route_median,
                "compiler_route_accepted_interval": [
                    ROUTE_RATIO_MIN,
                    ROUTE_RATIO_MAX,
                ],
                "measurements": rows,
                "warmup_evidence": warmup_evidence(preflights[harness_id]),
            }
        )
    return harness_results, {
        "polars_input": polars_input,
        "rustpython_dynamic_runtime": runtime_proof,
        "measurement_locks": lock_records,
        "build_quiescence": quiescence,
        "measurement_affinity": {
            "cpu_list": cpu_list,
            "numa_node": int(numa_node),
        },
    }


def failed_target(
    spec: TargetSpec, error: str, raw_dir: pathlib.Path
) -> dict[str, Any]:
    return {
        "schema_version": 1,
        "target_id": spec.target_id,
        "source_commit": spec.commit,
        "status": "failed_closed",
        "suite_manifest_sha256": ANALYSIS_SUITE_MANIFEST_SHA256,
        "preregistration_suite_manifest_sha256": (
            PREREGISTRATION_SUITE_MANIFEST_SHA256
        ),
        "analysis_suite_manifest_sha256": ANALYSIS_SUITE_MANIFEST_SHA256,
        "blockers": [error],
        "harnesses": [
            {
                "id": harness_id,
                "metric_direction": "lower_is_better",
                "gates": {gate: False for gate in REQUIRED_GATES},
                "measurements": [],
            }
            for harness_id in spec.harness_filters
        ],
        "raw_path": str(raw_dir.resolve()),
    }


def run_target(
    spec: TargetSpec,
    args: argparse.Namespace,
    snapshot: Mapping[str, Any],
    wrapper: pathlib.Path,
    force_wrapper: pathlib.Path,
    force_load: Mapping[str, Any],
    direct_wrapper: pathlib.Path,
    direct_load: Mapping[str, Any],
) -> dict[str, Any]:
    checkout = args.checkout_root.resolve() / spec.checkout
    source = verify_checkout(checkout, spec)
    worktree = args.raw_dir.resolve() / "build-work" / spec.target_id
    prepared = prepare_worktree(spec, checkout, worktree, args.raw_dir.resolve())
    binaries = build_variants(
        spec,
        prepared,
        args.raw_dir.resolve(),
        snapshot,
        wrapper,
        force_wrapper,
        force_load,
        direct_wrapper,
        direct_load,
        jobs=args.jobs,
        timeout=args.build_timeout,
        reuse=args.reuse_builds,
    )
    if args.phase == "build":
        return {
            "schema_version": 1,
            "target_id": spec.target_id,
            "source_commit": spec.commit,
            "source_ref": spec.source_ref,
            "status": "build_complete",
            "unialloc_implementation_sha256": snapshot[
                "unialloc_implementation_sha256"
            ],
            "implementation_sha256": snapshot["unialloc_implementation_sha256"],
            "implementation_revision": snapshot.get("allocator_revision"),
            "campaign_snapshot_sha256": snapshot.get("campaign_snapshot_sha256"),
            "allocator_revision": snapshot.get("allocator_revision"),
            "suite_manifest_sha256": ANALYSIS_SUITE_MANIFEST_SHA256,
            "preregistration_suite_manifest_sha256": (
                PREREGISTRATION_SUITE_MANIFEST_SHA256
            ),
            "analysis_suite_manifest_sha256": ANALYSIS_SUITE_MANIFEST_SHA256,
            "harnesses": [
                {
                    "id": harness_id,
                    "metric_direction": "lower_is_better",
                    "gates": {gate: False for gate in REQUIRED_GATES},
                    "measurements": [],
                }
                for harness_id in spec.harness_filters
            ],
            "source": source,
            "build_records": {
                variant: str(
                    (
                        args.raw_dir.resolve()
                        / "builds"
                        / spec.target_id
                        / variant
                        / "build.json"
                    )
                )
                for variant in VARIANTS
            },
            "raw_path": str(args.raw_dir.resolve()),
        }
    harnesses, inputs = measure_target(
        spec,
        binaries,
        worktree,
        args.raw_dir.resolve(),
        warmups=args.warmups,
        rounds=args.rounds,
        timeout=args.run_timeout,
        include_rounds=args.phase == "full",
        quiescence_timeout=args.quiescence_timeout,
        quiescence_seconds=args.quiescence_seconds,
        cpu_list=args.cpu_list,
        numa_node=args.numa_node,
    )
    if args.phase == "warmup":
        status = "measurement_ready"
    else:
        data_eligible = all(
            all(row["gates"].get(gate) is True for gate in DATA_ELIGIBILITY_GATES)
            for row in harnesses
        )
        attribution_complete = all(
            row["gates"].get("compiler_route_equivalent") is True for row in harnesses
        )
        if not data_eligible:
            status = "complete_with_failed_gates"
        elif attribution_complete:
            status = "complete"
        else:
            status = "complete_with_attribution_limits"
    record = {
        "schema_version": 1,
        "target_id": spec.target_id,
        "source_commit": spec.commit,
        "source_ref": spec.source_ref,
        "status": status,
        "unialloc_implementation_sha256": snapshot["unialloc_implementation_sha256"],
        "implementation_sha256": snapshot["unialloc_implementation_sha256"],
        "implementation_revision": snapshot.get("allocator_revision"),
        "campaign_snapshot_sha256": snapshot.get("campaign_snapshot_sha256"),
        "allocator_revision": snapshot.get("allocator_revision"),
        "frozen_snapshot_record": str(
            (args.raw_dir.resolve() / "frozen-unialloc.json").resolve()
        ),
        "suite_manifest_sha256": ANALYSIS_SUITE_MANIFEST_SHA256,
        "preregistration_suite_manifest_sha256": (
            PREREGISTRATION_SUITE_MANIFEST_SHA256
        ),
        "analysis_suite_manifest_sha256": ANALYSIS_SUITE_MANIFEST_SHA256,
        "harnesses": harnesses,
        "source": source,
        "build_records": {
            variant: str(
                (
                    args.raw_dir.resolve()
                    / "builds"
                    / spec.target_id
                    / variant
                    / "build.json"
                )
            )
            for variant in VARIANTS
        },
        "audit_paths": {
            variant: binaries[variant]["audit_dir"] for variant in VARIANTS
        },
        "raw_path": str(args.raw_dir.resolve()),
        "patch_path": str(pathlib.Path(str(prepared["patch"])).resolve()),
        "inputs": inputs,
    }
    if not args.keep_target and args.phase == "full":
        shutil.rmtree(args.raw_dir.resolve() / "cargo-targets" / spec.target_id)
        record["disposable_cargo_target_removed"] = True
    return record


def primary_publication_allowed(
    record: Mapping[str, Any], *, current_working_tree: bool
) -> bool:
    return (
        not current_working_tree
        and record.get("campaign_classification") != "diagnostic_current_worktree"
        and record.get("primary_eligible") is not False
        and record.get("measured_rounds") in PUBLISHABLE_ROUND_COUNTS
        and record.get("status") in {"complete", "complete_with_attribution_limits"}
    )


def apply_diagnostic_result_metadata(
    record: dict[str, Any],
    snapshot: Mapping[str, Any],
    *,
    cpu_list: str,
    numa_node: str,
    measured_rounds: int,
) -> None:
    if snapshot.get("source_kind") != "working_tree":
        raise CampaignError("diagnostic campaign did not freeze a working tree")
    if measured_rounds != PRIMARY_ROUNDS:
        raise CampaignError("working-tree diagnostics require three measured rounds")
    record.update(
        {
            "campaign_classification": "diagnostic_current_worktree",
            "primary_eligible": False,
            "core_eligible": False,
            "primary_ineligibility_reason": "working_tree_implementation",
            "implementation_source_kind": snapshot["source_kind"],
            "repository_head": snapshot["repository_head"],
            "repository_status": snapshot["repository_status"],
            "repository_status_sha256": snapshot["repository_status_sha256"],
            "implementation_sha256": snapshot["unialloc_implementation_sha256"],
            "implementation_revision": snapshot["repository_head"],
            "campaign_snapshot_sha256": snapshot["campaign_snapshot_sha256"],
            "campaign_snapshot_file_count": snapshot["campaign_snapshot_file_count"],
            "measurement_cpu_list": cpu_list,
            "measurement_numa_node": int(numa_node),
            "measured_rounds": measured_rounds,
        }
    )


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    validate_specs_against_suite()
    args.raw_dir = args.raw_dir.resolve()
    args.checkout_root = args.checkout_root.resolve()
    args.raw_dir.mkdir(parents=True, exist_ok=True)
    (args.raw_dir / "tmp").mkdir(parents=True, exist_ok=True)
    snapshot = snapshot_allocator(
        args.raw_dir,
        args.allocator_revision,
        current_working_tree=args.current_working_tree,
    )
    if args.current_working_tree:
        if snapshot.get("source_kind") != "working_tree":
            raise CampaignError("diagnostic campaign did not freeze a working tree")
    else:
        if (
            snapshot.get("source_kind") != "git_revision"
            or snapshot.get("snapshot_source") != "git_archive"
        ):
            raise CampaignError(
                "exact campaign requires a Git-revision archive snapshot"
            )
        if snapshot.get("allocator_revision") != SUITE_IMPLEMENTATION_REVISION:
            raise CampaignError(
                "allocator revision differs from the primary suite implementation"
            )
        if (
            snapshot.get("unialloc_implementation_sha256")
            != SUITE_IMPLEMENTATION_SHA256
        ):
            raise CampaignError(
                "allocator implementation digest differs from the primary suite"
            )
    wrapper = build_mir_driver(args.raw_dir, snapshot, args.build_timeout)
    force_wrapper = ensure_force_load_wrapper(
        args.raw_dir / "tools" / "unialloc-force-load-wrapper"
    )
    force_load = build_force_load_rlib(
        args.raw_dir, snapshot, args.jobs, args.build_timeout
    )
    direct_wrapper = ensure_direct_load_wrapper(
        args.raw_dir / "tools" / "unialloc-direct-load-wrapper"
    )
    direct_load = build_direct_load_rlib(
        args.raw_dir, snapshot, args.jobs, args.build_timeout
    )
    exit_code = 0
    for target_id in args.targets:
        spec = TARGET_SPECS[target_id]
        try:
            record = run_target(
                spec,
                args,
                snapshot,
                wrapper,
                force_wrapper,
                force_load,
                direct_wrapper,
                direct_load,
            )
        except (CampaignError, matrix.MatrixError, OSError, ValueError) as error:
            record = failed_target(spec, str(error), args.raw_dir)
            exit_code = 2
        record["measured_rounds"] = args.rounds
        if args.current_working_tree:
            apply_diagnostic_result_metadata(
                record,
                snapshot,
                cpu_list=args.cpu_list,
                numa_node=args.numa_node,
                measured_rounds=args.rounds,
            )
        output = args.raw_dir / "results" / f"{target_id}.json"
        if args.phase != "build":
            archived = archive_build_complete_result(
                output, target_id, args.raw_dir.resolve()
            )
            if archived is not None:
                record["archived_build_complete_result"] = archived
        write_json(output, record)
        if primary_publication_allowed(
            record, current_working_tree=args.current_working_tree
        ):
            write_json(ASSEMBLER_TARGET_DIR / f"{target_id}.json", record)
        print(
            json.dumps(
                {"target": target_id, "status": record["status"], "result": str(output)}
            )
        )
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
