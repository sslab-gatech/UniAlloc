#!/usr/bin/env python3
"""Measure bounded exact Type Isolation cache retention under burst pressure.

The probe deliberately uses UniAlloc's public ``SemanticAlloc`` API.  It builds
one small Rust binary against the current checkout, then runs every matrix cell
in a fresh process so each measurement starts with empty thread-local semantic
cache state.  A cell allocates the full burst before freeing it; this models a
wave of simultaneously-live objects becoming cold without same-wave cache hits.

This is a cache-mechanics experiment.  It does not measure compiler coverage or
claim that the synthetic burst distribution represents an application trace.
"""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import os
from pathlib import Path
import platform
import shlex
import shutil
import subprocess
import sys
from typing import Any, Iterable


REPO_ROOT = Path(__file__).resolve().parents[2]
UNIALLOC_DIR = REPO_ROOT / "unialloc"
DEFAULT_SIZES = (8, 24, 64, 256, 1024, 4096, 16384, 65536, 65544)
DEFAULT_BURSTS = (1, 8, 32, 64, 128, 256)
TYPE_ISOLATION_SOURCE = UNIALLOC_DIR / "src" / "alloc_api" / "type_isolation.rs"


RUST_PROBE = r'''use std::alloc::Layout;
use std::env;
use unialloc::{
    semantic_auto_metadata_disable, semantic_stats_reset, semantic_stats_snapshot,
    type_cache_admission_stats_snapshot, type_isolation_side_cache_snapshot,
    AllocationMetadata, SemanticAlloc, UniAlloc, FLAG_TYPE_ISOLATED,
};
use unialloc::alloc_api::type_isolation::{
    MAX_PLAIN_TYPE_CACHE_RETAINED_BYTES, MAX_PLAIN_TYPE_CACHE_RETAINED_ENTRIES,
    MAX_TYPE_CACHE_OBJECT_SIZE, MAX_TYPE_CACHE_SLOT_BYTES, MIN_TYPE_CACHE_OBJECT_SIZE,
};

const MODULE_ID: u64 = 0x5052_4553_5355_5245; // "PRESSURE"

fn usage() -> ! {
    eprintln!("usage: cache-pressure-probe <same|distinct> <size> <burst> <seed>");
    std::process::exit(2);
}

fn main() {
    let args: Vec<String> = env::args().collect();
    if args.len() != 5 {
        usage();
    }
    let mode = args[1].as_str();
    if mode != "same" && mode != "distinct" {
        usage();
    }
    let size: usize = args[2].parse().unwrap_or_else(|_| usage());
    let burst: usize = args[3].parse().unwrap_or_else(|_| usage());
    let seed: u64 = args[4].parse().unwrap_or_else(|_| usage());
    if size == 0 || burst == 0 {
        usage();
    }

    let align = std::mem::align_of::<usize>();
    let layout = Layout::from_size_align(size, align).expect("valid probe layout");
    let allocator = UniAlloc::new();
    let mut pointers: Vec<*mut u8> = Vec::with_capacity(burst);

    semantic_auto_metadata_disable();
    semantic_stats_reset();
    let initial_cache = type_isolation_side_cache_snapshot();
    let initial_admission = type_cache_admission_stats_snapshot();

    for index in 0..burst {
        let type_id = if mode == "same" {
            seed
        } else {
            seed.wrapping_add(index as u64)
        };
        let metadata = AllocationMetadata::for_type(type_id)
            .with_module(MODULE_ID)
            .with_flags(FLAG_TYPE_ISOLATED);
        let pointer = unsafe { allocator.alloc_with_metadata(layout, metadata) };
        assert!(!pointer.is_null(), "probe allocation failed at index {index}");
        unsafe {
            pointer.write((index & 0xff) as u8);
            pointer.add(size - 1).write(((index >> 8) & 0xff) as u8);
        }
        pointers.push(pointer);
    }

    let before_free_stats = semantic_stats_snapshot();
    let mut previous_inserts = before_free_stats.typed_cache_inserts;
    let mut first_non_retained_free: Option<usize> = None;
    for (index, pointer) in pointers.into_iter().enumerate() {
        let type_id = if mode == "same" {
            seed
        } else {
            seed.wrapping_add(index as u64)
        };
        let metadata = AllocationMetadata::for_type(type_id)
            .with_module(MODULE_ID)
            .with_flags(FLAG_TYPE_ISOLATED);
        unsafe { allocator.dealloc_with_metadata(pointer, layout, metadata) };
        let current = semantic_stats_snapshot();
        if current.typed_cache_inserts == previous_inserts
            && first_non_retained_free.is_none()
        {
            first_non_retained_free = Some(index + 1);
        }
        previous_inserts = current.typed_cache_inserts;
    }

    let stats = semantic_stats_snapshot();
    let cache = type_isolation_side_cache_snapshot();
    let admission = type_cache_admission_stats_snapshot();
    let insert_events = stats
        .typed_cache_inserts
        .saturating_sub(before_free_stats.typed_cache_inserts);
    let non_retained_frees = burst.saturating_sub(insert_events);
    let l1_retained_entries = cache.occupied_entries;
    let l1_retained_bytes = cache.retained_bytes;
    let depot_retained_entries = admission
        .depot_current_entries
        .saturating_sub(initial_admission.depot_current_entries);
    let depot_retained_bytes = admission
        .depot_current_rounded_bytes
        .saturating_sub(initial_admission.depot_current_rounded_bytes);
    let retained_entries = l1_retained_entries.saturating_add(depot_retained_entries);
    let retained_bytes = l1_retained_bytes.saturating_add(depot_retained_bytes);
    let admitted_events = admission
        .admitted
        .events
        .saturating_sub(initial_admission.admitted.events);
    let first_non_retained_json = first_non_retained_free
        .map(|value| value.to_string())
        .unwrap_or_else(|| "null".to_string());

    println!(
        concat!(
            "{{",
            "\"mode\":\"{}\",",
            "\"requested_size\":{},\"alignment\":{},\"burst\":{},\"seed\":{},",
            "\"allocations\":{},\"frees\":{},",
            "\"cache_insert_events\":{},\"non_retained_frees\":{},",
            "\"first_non_retained_free\":{},",
            "\"retained_entries\":{},\"retained_bytes\":{},",
            "\"l1_retained_entries\":{},\"l1_retained_bytes\":{},",
            "\"depot_retained_entries\":{},\"depot_retained_bytes\":{},",
            "\"occupied_slots\":{},\"inline_occupied\":{},\"corrupt_slots\":{},",
            "\"typed_cache_hits\":{},\"typed_cache_bypasses_conflated\":{},",
            "\"typed_cache_wrong_identity_denials\":{},",
            "\"initial_retained_entries\":{},\"initial_retained_bytes\":{},",
            "\"admitted_events\":{},\"terminal_events\":{},",
            "\"rejected_too_small_events\":{},\"rejected_too_large_events\":{},",
            "\"rejected_probe_window_full_events\":{},",
            "\"rejected_slot_depth_events\":{},\"rejected_slot_byte_budget_events\":{},",
            "\"rejected_aggregate_byte_budget_events\":{},",
            "\"rejected_aggregate_entry_budget_events\":{},",
            "\"rejected_registry_pressure_events\":{},",
            "\"depot_attempts_events\":{},\"depot_inserts_events\":{},",
            "\"depot_rejected_capacity_events\":{},",
            "\"depot_rejected_registry_headroom_events\":{},",
            "\"min_cache_object_size\":{},\"max_cache_object_size\":{},",
            "\"per_slot_cold_byte_budget\":{},\"per_thread_plain_byte_budget\":{},",
            "\"per_thread_plain_entry_budget\":{}",
            "}}"
        ),
        mode,
        size,
        align,
        burst,
        seed,
        stats.typed_allocations,
        stats.typed_deallocations,
        insert_events,
        non_retained_frees,
        first_non_retained_json,
        retained_entries,
        retained_bytes,
        l1_retained_entries,
        l1_retained_bytes,
        depot_retained_entries,
        depot_retained_bytes,
        cache.occupied_slots,
        cache.inline_occupied,
        cache.corrupt_slots,
        stats.typed_cache_hits,
        stats.typed_cache_bypasses,
        stats.typed_cache_wrong_identity_denials,
        initial_cache
            .occupied_entries
            .saturating_add(initial_admission.depot_current_entries),
        initial_cache
            .retained_bytes
            .saturating_add(initial_admission.depot_current_rounded_bytes),
        admitted_events,
        admission.terminal_events(),
        admission.rejected_too_small.events,
        admission.rejected_too_large.events,
        admission.rejected_probe_window_full.events,
        admission.rejected_slot_depth.events,
        admission.rejected_slot_byte_budget.events,
        admission.rejected_aggregate_byte_budget.events,
        admission.rejected_aggregate_entry_budget.events,
        admission.rejected_registry_pressure.events,
        admission.depot_attempts.events,
        admission.depot_inserts.events,
        admission.depot_rejected_capacity.events,
        admission.depot_rejected_registry_headroom.events,
        MIN_TYPE_CACHE_OBJECT_SIZE,
        MAX_TYPE_CACHE_OBJECT_SIZE,
        MAX_TYPE_CACHE_SLOT_BYTES,
        MAX_PLAIN_TYPE_CACHE_RETAINED_BYTES,
        MAX_PLAIN_TYPE_CACHE_RETAINED_ENTRIES,
    );
}
'''


def parse_positive_csv(value: str, label: str) -> tuple[int, ...]:
    try:
        parsed = tuple(int(item.strip()) for item in value.split(",") if item.strip())
    except ValueError as error:
        raise argparse.ArgumentTypeError(f"{label} must be comma-separated integers") from error
    if not parsed or any(item <= 0 for item in parsed):
        raise argparse.ArgumentTypeError(f"{label} values must be positive")
    return parsed


def command_version(command: str) -> str:
    completed = subprocess.run(
        [command, "--version"],
        cwd=REPO_ROOT,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        check=True,
    )
    return completed.stdout.strip()


def git_value(*args: str) -> str:
    completed = subprocess.run(
        ["git", *args],
        cwd=REPO_ROOT,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=True,
    )
    return completed.stdout.strip()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def write_probe_crate(
    crate_dir: Path,
    *,
    unialloc_dir: Path = UNIALLOC_DIR,
    lockfile: Path = REPO_ROOT / "Cargo.lock",
) -> Path:
    src_dir = crate_dir / "src"
    src_dir.mkdir(parents=True, exist_ok=True)
    manifest = (
        "[package]\n"
        'name = "unialloc-cache-pressure-probe"\n'
        'version = "0.0.0"\n'
        'edition = "2021"\n\n'
        "[workspace]\n\n"
        "[dependencies]\n"
        f"unialloc = {{ path = {json.dumps(str(unialloc_dir.resolve()))}, "
        'features = ["stats", "type_isolation"] }\n'
    )
    (crate_dir / "Cargo.toml").write_text(manifest, encoding="utf-8")
    (src_dir / "main.rs").write_text(RUST_PROBE, encoding="utf-8")
    # Preserve the repository's pinned/yanked-compatible dependency resolution.
    # Resolving this old prototype from a fresh crates.io graph rejects spin
    # 0.9.0 because that release was yanked after the repository lock was made.
    shutil.copy2(lockfile, crate_dir / "Cargo.lock")
    return crate_dir / "Cargo.toml"


def build_probe(
    cargo: str,
    manifest: Path,
    output_dir: Path,
    jobs: int,
    timeout: int,
) -> tuple[Path, dict[str, Any]]:
    target_dir = output_dir / "target"
    command = [
        cargo,
        "build",
        "--release",
        "--manifest-path",
        str(manifest),
        "--target-dir",
        str(target_dir),
        "--jobs",
        str(jobs),
    ]
    completed = subprocess.run(
        command,
        cwd=REPO_ROOT,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=timeout,
        check=False,
    )
    (output_dir / "build.stdout.log").write_text(completed.stdout, encoding="utf-8")
    (output_dir / "build.stderr.log").write_text(completed.stderr, encoding="utf-8")
    record = {
        "command": command,
        "exit_code": completed.returncode,
        "stdout_log": "build.stdout.log",
        "stderr_log": "build.stderr.log",
    }
    if completed.returncode != 0:
        raise RuntimeError(
            f"probe build failed with exit code {completed.returncode}; "
            f"see {output_dir / 'build.stderr.log'}"
        )
    binary = target_dir / "release" / "unialloc-cache-pressure-probe"
    if not binary.is_file():
        raise RuntimeError(f"probe build succeeded without binary {binary}")
    record.update(
        {
            "manifest_sha256": sha256_file(manifest),
            "probe_source_sha256": sha256_file(manifest.parent / "src" / "main.rs"),
            "cargo_lock_sha256": sha256_file(manifest.parent / "Cargo.lock"),
            "binary_sha256": sha256_file(binary),
        }
    )
    return binary, record


def run_cell(
    binary: Path,
    mode: str,
    size: int,
    burst: int,
    seed: int,
    timeout: int,
) -> dict[str, Any]:
    command = [str(binary), mode, str(size), str(burst), str(seed)]
    completed = subprocess.run(
        command,
        cwd=REPO_ROOT,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=timeout,
        check=False,
    )
    if completed.returncode != 0:
        raise RuntimeError(
            f"probe cell failed ({shlex.join(command)}): "
            f"exit={completed.returncode} stderr={completed.stderr.strip()}"
        )
    try:
        row = json.loads(completed.stdout.strip())
    except json.JSONDecodeError as error:
        raise RuntimeError(
            f"probe emitted invalid JSON ({shlex.join(command)}): {completed.stdout!r}"
        ) from error
    if row["initial_retained_entries"] != 0 or row["initial_retained_bytes"] != 0:
        raise RuntimeError(f"fresh process started with nonempty semantic cache: {row}")
    if row["allocations"] != burst or row["frees"] != burst:
        raise RuntimeError(f"incomplete probe accounting: {row}")
    if row["cache_insert_events"] + row["non_retained_frees"] != burst:
        raise RuntimeError(f"free retention accounting does not close: {row}")
    if row["retained_entries"] != row["cache_insert_events"]:
        raise RuntimeError(f"plain-cache retained entries differ from insert events: {row}")
    row["retained_free_fraction"] = row["retained_entries"] / burst
    row["non_retained_free_fraction"] = row["non_retained_frees"] / burst
    row["repeat_seed"] = seed
    return row


def summarize(rows: Iterable[dict[str, Any]], maximum_burst: int) -> list[dict[str, Any]]:
    grouped: dict[tuple[int, str], list[dict[str, Any]]] = {}
    for row in rows:
        grouped.setdefault((row["requested_size"], row["mode"]), []).append(row)

    summaries: list[dict[str, Any]] = []
    for (size, mode), group in sorted(grouped.items()):
        maximum_rows = [row for row in group if row["burst"] == maximum_burst]
        first_rejections = [
            row["first_non_retained_free"]
            for row in group
            if row["first_non_retained_free"] is not None
        ]
        summaries.append(
            {
                "requested_size": size,
                "mode": mode,
                "max_observed_retained_entries": max(
                    row["retained_entries"] for row in group
                ),
                "max_observed_retained_bytes": max(row["retained_bytes"] for row in group),
                "max_observed_l1_retained_entries": max(
                    row.get("l1_retained_entries", row["retained_entries"])
                    for row in group
                ),
                "max_observed_l1_retained_bytes": max(
                    row.get("l1_retained_bytes", row["retained_bytes"])
                    for row in group
                ),
                "max_observed_depot_retained_entries": max(
                    row.get("depot_retained_entries", 0) for row in group
                ),
                "max_observed_depot_retained_bytes": max(
                    row.get("depot_retained_bytes", 0) for row in group
                ),
                "first_non_retained_free_range": (
                    [min(first_rejections), max(first_rejections)] if first_rejections else None
                ),
                "at_maximum_burst": {
                    "burst": maximum_burst,
                    "retained_entries_range": [
                        min(row["retained_entries"] for row in maximum_rows),
                        max(row["retained_entries"] for row in maximum_rows),
                    ],
                    "retained_bytes_range": [
                        min(row["retained_bytes"] for row in maximum_rows),
                        max(row["retained_bytes"] for row in maximum_rows),
                    ],
                    "non_retained_fraction_range": [
                        min(row["non_retained_free_fraction"] for row in maximum_rows),
                        max(row["non_retained_free_fraction"] for row in maximum_rows),
                    ],
                },
            }
        )
    return summaries


def default_output_dir() -> Path:
    stamp = dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    return REPO_ROOT / "evaluation" / "raw" / f"typeiso-cache-pressure-{stamp}-{os.getpid()}"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--sizes",
        default=",".join(map(str, DEFAULT_SIZES)),
        help="comma-separated requested object sizes",
    )
    parser.add_argument(
        "--bursts",
        default=",".join(map(str, DEFAULT_BURSTS)),
        help="comma-separated allocate-all-then-free burst sizes",
    )
    parser.add_argument(
        "--distinct-repeats",
        type=int,
        default=3,
        help="identity-seed repetitions for distinct-identity cells",
    )
    parser.add_argument("--output-dir", type=Path, default=None)
    parser.add_argument("--cargo", default="cargo")
    parser.add_argument("--jobs", type=int, default=max(1, min(8, os.cpu_count() or 1)))
    parser.add_argument("--build-timeout", type=int, default=900)
    parser.add_argument("--cell-timeout", type=int, default=30)
    parser.add_argument(
        "--unialloc-dir",
        type=Path,
        default=UNIALLOC_DIR,
        help="UniAlloc crate to evaluate; may point at a frozen implementation snapshot",
    )
    args = parser.parse_args(argv)

    sizes = parse_positive_csv(args.sizes, "sizes")
    bursts = parse_positive_csv(args.bursts, "bursts")
    if args.distinct_repeats <= 0:
        parser.error("--distinct-repeats must be positive")
    output_dir = (args.output_dir or default_output_dir()).resolve()
    output_dir.mkdir(parents=True, exist_ok=False)
    unialloc_dir = args.unialloc_dir.resolve()
    lockfile = unialloc_dir.parent / "Cargo.lock"
    type_isolation_source = unialloc_dir / "src" / "alloc_api" / "type_isolation.rs"
    if not type_isolation_source.is_file():
        parser.error(f"missing Type Isolation source: {type_isolation_source}")
    if not lockfile.is_file():
        parser.error(f"missing workspace lockfile beside UniAlloc crate: {lockfile}")

    manifest = write_probe_crate(
        output_dir / "probe-crate",
        unialloc_dir=unialloc_dir,
        lockfile=lockfile,
    )
    binary, build = build_probe(
        args.cargo, manifest, output_dir, args.jobs, args.build_timeout
    )

    rows: list[dict[str, Any]] = []
    base_seed = 0xCACE_0000_0000_0001
    for size in sizes:
        for burst in bursts:
            rows.append(
                run_cell(binary, "same", size, burst, base_seed, args.cell_timeout)
            )
            for repeat in range(args.distinct_repeats):
                seed = base_seed + (repeat + 1) * 0x0010_0000_0000
                rows.append(
                    run_cell(binary, "distinct", size, burst, seed, args.cell_timeout)
                )

    first = rows[0]
    report = {
        "schema_version": 2,
        "experiment": "type-isolation-exact-cache-controlled-pressure-sweep",
        "generated_at_utc": dt.datetime.now(dt.timezone.utc).isoformat(),
        "repository": {
            "root": str(REPO_ROOT),
            "head": git_value("rev-parse", "HEAD"),
            "dirty": bool(git_value("status", "--porcelain")),
            "type_isolation_source": {
                "path": str(type_isolation_source),
                "sha256": sha256_file(type_isolation_source),
            },
            "sweep_script": {
                "path": str(Path(__file__).resolve().relative_to(REPO_ROOT)),
                "sha256": sha256_file(Path(__file__).resolve()),
            },
        },
        "host": {
            "platform": platform.platform(),
            "machine": platform.machine(),
            "python": sys.version.split()[0],
            "cargo": command_version(args.cargo),
            "rustc": command_version("rustc"),
        },
        "method": {
            "traffic": "manual SemanticAlloc API; allocate full burst then free full burst",
            "isolation": "one fresh process per matrix cell",
            "modes": {
                "same": "all objects use one exact semantic identity",
                "distinct": "each object uses a distinct exact semantic type_id",
            },
            "sizes": list(sizes),
            "bursts": list(bursts),
            "distinct_repeats": args.distinct_repeats,
            "retention_accounting": (
                "cache_insert_events is the free-admission numerator; in this no-pop burst, "
                "retained_entries is the sum of thread-local L1 and process-wide depot owners, "
                "and remaining frees were synchronously handed to the raw allocator"
            ),
        },
        "policy_constants_reported_by_probe": {
            "min_cache_object_size": first["min_cache_object_size"],
            "max_cache_object_size": first["max_cache_object_size"],
            "per_slot_cold_byte_budget": first["per_slot_cold_byte_budget"],
            "per_thread_plain_byte_budget": first["per_thread_plain_byte_budget"],
            "per_thread_plain_entry_budget": first[
                "per_thread_plain_entry_budget"
            ],
        },
        "telemetry_boundary": {
            "typed_cache_bypasses_conflated": (
                "existing runtime counter combines allocation lookup misses and rejected "
                "free insertions; conclusions use deallocation count minus insert events"
            ),
            "excludes": [
                "compiler attribution coverage",
                "application lifetime distributions",
                "segregated cache policy",
                "eviction and thread-exit release reason counters",
            ],
        },
        "build": build,
        "summary": summarize(rows, max(bursts)),
        "rows": rows,
    }
    report_path = output_dir / "results.json"
    report_path.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(report_path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
