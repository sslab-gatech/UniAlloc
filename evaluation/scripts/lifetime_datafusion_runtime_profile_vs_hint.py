#!/usr/bin/env python3
"""Same-binary DataFusion runtime-profile versus compiler-hint screen.

The runtime-profile arm uses policy 11, which preserves exact site identity
while masking every static lifetime prior to Unknown.  Each arm starts in a
fresh process with zero benchmark warm-up iterations, so online learning,
trailers, allocator locking, and THP promotion all occur inside measured work.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
from pathlib import Path
import statistics
import sys
from typing import Any, Mapping, Sequence


ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str((ROOT / "evaluation/scripts").resolve()))

import lifetime_prior_six_program_campaign as campaign  # noqa: E402
import lifetime_resident_datafusion_compiler_directed as datafusion  # noqa: E402


RUNTIME_ARM = "adaptive-runtime-profile-thp"
HINT_ARM = "compiler-directed-hugepage-compiler-prior"
# The reused fixed-work binary is the validated g1024 variant.  The shared
# generator currently defaults to g128, so bind its pure digest validator to
# the immutable binary geometry before parsing either arm.
datafusion.GROUP_COUNT = 1024
FIXED_WORK_FIELDS = datafusion.FIXED_WORK_INVARIANT_FIELDS


class ContractError(RuntimeError):
    """The fixed-work, runtime-mechanism, or physical-backing gate failed."""


def write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def fixed_work_projection(row: Mapping[str, Any]) -> dict[str, Any]:
    return {field: row[field] for field in FIXED_WORK_FIELDS}


def validate_runtime_profile(
    stats: Mapping[str, Any], sites: Sequence[Mapping[str, Any]], mechanism: Mapping[str, Any]
) -> dict[str, Any]:
    if stats.get("policy") != 11 or stats.get("backend") != 1:
        raise ContractError("runtime-profile arm did not select policy 11 with THP backend")
    if stats.get("adaptive_observation_recording") is not True:
        raise ContractError("runtime-profile exact-site observations were not enabled")
    if stats.get("adaptive_force_track_all") is not False:
        raise ContractError("runtime-profile arm unexpectedly force-tracked every allocation")
    if int(stats.get("adaptive_site_count", 0)) <= 0 or not sites:
        raise ContractError("runtime-profile arm learned no exact allocation sites")
    if int(stats.get("adaptive_training_allocations", 0)) <= 0:
        raise ContractError("runtime-profile arm paid no cold-training allocations")
    if int(stats.get("routed_allocations", 0)) <= 0:
        raise ContractError("runtime-profile arm routed no allocations")
    if int(stats.get("adaptive_trailer_corruptions", -1)) != 0:
        raise ContractError("runtime-profile trailer corruption was observed")
    if int(stats.get("adaptive_live_trailers", -1)) != int(stats.get("live_objects", -2)):
        raise ContractError("runtime-profile live trailer accounting does not match live objects")
    classified = sum(
        int(stats.get(field, 0))
        for field in ("static_hint_tp", "static_hint_tn", "static_hint_fp", "static_hint_fn")
    )
    if classified != 0 or int(stats.get("static_hint_abstained", 0)) <= 0:
        raise ContractError("policy 11 did not mask static lifetime priors to Unknown")
    if any(int(row.get("latest_static_prior", -1)) != 0 for row in sites):
        raise ContractError("runtime-profile site export retained a static lifetime prior")
    if int(mechanism.get("thp_collapse_attempts", 0)) <= 0:
        raise ContractError("runtime-profile arm never attempted THP collapse")
    return {
        "policy": 11,
        "static_prior_masked": True,
        "site_count": int(stats["adaptive_site_count"]),
        "training_allocations": int(stats["adaptive_training_allocations"]),
        "cold_bypassed_allocations": int(stats["adaptive_cold_bypassed_allocations"]),
        "short_routed_allocations": int(stats["adaptive_short_routed_allocations"]),
        "short_bypassed_allocations": int(stats["adaptive_short_bypassed_allocations"]),
        "long_routed_allocations": int(stats["adaptive_long_routed_allocations"]),
        "promotions": int(stats["adaptive_promotions"]),
        "demotions": int(stats["adaptive_demotions"]),
        "pressure_bytes": int(stats["adaptive_pressure_bytes"]),
        "pressure_epochs": int(stats["adaptive_pressure_epoch"]),
        "routed_allocations": int(stats["routed_allocations"]),
        "routed_deallocations": int(stats["routed_deallocations"]),
        "trailer_corruptions": int(stats["adaptive_trailer_corruptions"]),
        "live_trailers_at_report": int(stats["adaptive_live_trailers"]),
        "live_objects_at_report": int(stats["live_objects"]),
        "learning_window": {
            "short_if_released_before_later_pressure_bytes": 2 * 1024 * 1024,
            "long_if_survives_later_pressure_bytes": 8 * 1024 * 1024,
            "between_thresholds": "censored",
            "minimum_decisive_samples_without_prior": 8,
        },
        "arena_lock_acquisitions_lower_bound": int(stats["adaptive_eligible_allocations"]),
        "arena_lock_counter_boundary": (
            "policy11 has no dedicated lock counter; every adaptive eligible allocation "
            "is classified after acquiring the single ARENA lock, and unsupported or "
            "missing-identity attempts may add more"
        ),
        "thp_collapse_attempts": int(mechanism["thp_collapse_attempts"]),
        "thp_collapse_successes": int(mechanism["thp_collapse_successes"]),
        "thp_collapse_errors": int(mechanism["thp_collapse_errors"]),
        "retained_empty_extent_reuse_hits": int(
            mechanism["retained_empty_extent_reuse_hits"]
        ),
    }


def validate_hint(
    stats: Mapping[str, Any], sites: Sequence[Mapping[str, Any]], mechanism: Mapping[str, Any]
) -> dict[str, Any]:
    if stats.get("policy") != 9 or stats.get("backend") != 1:
        raise ContractError("compiler-hint arm did not select policy 9 with THP backend")
    if sites or stats.get("adaptive_observation_recording") is not False:
        raise ContractError("compiler-hint arm entered runtime site observation")
    adaptive_work = sum(
        int(stats.get(field, 0))
        for field in (
            "adaptive_training_allocations",
            "adaptive_cold_bypassed_allocations",
            "adaptive_short_routed_allocations",
            "adaptive_short_bypassed_allocations",
            "adaptive_long_routed_allocations",
            "adaptive_live_trailers",
            "runtime_validated_objects",
        )
    )
    if adaptive_work != 0:
        raise ContractError("compiler-hint arm paid adaptive runtime-learning work")
    if int(stats.get("routed_allocations", 0)) <= 0:
        raise ContractError("compiler-hint arm routed no allocations")
    if int(mechanism.get("thp_collapse_attempts", 0)) <= 0:
        raise ContractError("compiler-hint arm never attempted THP collapse")
    return {
        "policy": 9,
        "runtime_learning": False,
        "routed_allocations": int(stats["routed_allocations"]),
        "routed_deallocations": int(stats["routed_deallocations"]),
        "adaptive_training_allocations": int(stats["adaptive_training_allocations"]),
        "adaptive_live_trailers": int(stats["adaptive_live_trailers"]),
        "runtime_validated_objects": int(stats["runtime_validated_objects"]),
        "thp_collapse_attempts": int(mechanism["thp_collapse_attempts"]),
        "thp_collapse_successes": int(mechanism["thp_collapse_successes"]),
        "thp_collapse_errors": int(mechanism["thp_collapse_errors"]),
        "retained_empty_extent_reuse_hits": int(
            mechanism["retained_empty_extent_reuse_hits"]
        ),
    }


def run_one(
    *, binary: Path, raw_dir: Path, label: str, arm: str, args: argparse.Namespace
) -> dict[str, Any]:
    artifact_dir = raw_dir / label
    env = os.environ.copy()
    env.update(
        {
            "UNIALLOC_LIFETIME_EXPERIMENT_ARM": arm,
            "RAYON_NUM_THREADS": str(args.target_partitions),
            "TOKIO_WORKER_THREADS": "2",
        }
    )
    command = [
        "taskset",
        "-c",
        args.cpus,
        str(binary),
        str(args.batches_per_table),
        str(args.query_iterations),
        str(args.target_partitions),
    ]
    process = campaign.execute_monitored_process(
        command,
        cwd=ROOT,
        env=env,
        artifact_dir=artifact_dir,
        timeout=args.timeout,
        sample_interval=args.sample_interval,
    )
    stdout = Path(process["stdout_path"]).read_text(encoding="utf-8", errors="replace")
    stderr = Path(process["stderr_path"]).read_text(encoding="utf-8", errors="replace")
    if process["timed_out"] or process["exit_code"] != 0:
        raise ContractError(f"{label} failed:\n{stderr[-8000:]}")
    if process["single_process_guard"].get("passed") is not True:
        raise ContractError(f"{label} spawned a descendant process")
    fixed_work = datafusion.parse_result_record(
        stdout,
        batches_per_table=args.batches_per_table,
        query_iterations=args.query_iterations,
        target_partitions=args.target_partitions,
    )
    stats = campaign.runtime_lifetime.parse_runtime_stats(stderr)
    if stats is None:
        raise ContractError(f"{label} emitted no runtime stats")
    sites = campaign.runtime_lifetime.parse_runtime_site_rows(stderr)
    mechanism = campaign.parse_mechanism(stderr)
    fragmentation = campaign.parse_fragmentation(stderr)
    query_samples, bounds = datafusion.conservative_query_window_samples(
        process["smaps_samples"],
        fixed_work=fixed_work,
        process_wall_seconds=float(process["wall_seconds"]),
    )
    procfs = campaign.summarize_proc_samples(query_samples)
    if not query_samples or int(procfs.get("peak_rss_kib", 0)) <= 0:
        raise ContractError(f"{label} has no query-window procfs evidence")
    physical = [int(row["anon_hugepages_kib"]) for row in query_samples]
    if not physical:
        raise ContractError(f"{label} has no physical THP samples")
    # Persist the expensive process evidence before applying mechanism gates so
    # a failed gate remains diagnosable without repeating the macro workload.
    write_json(artifact_dir / "runtime-stats.json", stats)
    write_json(artifact_dir / "runtime-sites.json", sites)
    write_json(artifact_dir / "query-window-smaps-samples.json", query_samples)
    write_json(artifact_dir / "mechanism.json", mechanism)
    proof = (
        validate_runtime_profile(stats, sites, mechanism)
        if arm == RUNTIME_ARM
        else validate_hint(stats, sites, mechanism)
    )
    record = {
        **{key: value for key, value in process.items() if key != "smaps_samples"},
        "success": True,
        "label": label,
        "runtime_arm": arm,
        "runtime_policy": int(stats["policy"]),
        "fresh_process": True,
        "warmup_iterations": 0,
        "warmup_semantics": (
            "no unmeasured iteration and no persisted profile; policy11 starts Cold and "
            "learns during resident construction plus the fixed query workload"
        ),
        "fixed_work": fixed_work,
        "fixed_work_projection": fixed_work_projection(fixed_work),
        "operation_seconds_including_resident_build_and_learning": float(
            fixed_work["total_seconds"]
        ),
        "runtime_stats": stats,
        "runtime_sites": sites,
        "mechanism": mechanism,
        "fragmentation": fragmentation,
        "procfs": procfs,
        "query_window_clock_bounds": bounds,
        "query_window_sample_count": len(query_samples),
        "physical_anon_hugepages_kib": {
            "minimum": min(physical),
            "median": statistics.median(physical),
            "maximum": max(physical),
            "positive_samples": sum(value > 0 for value in physical),
            "sample_count": len(physical),
        },
        "mechanism_proof": proof,
    }
    write_json(artifact_dir / "runtime-stats.json", stats)
    write_json(artifact_dir / "runtime-sites.json", sites)
    write_json(artifact_dir / "query-window-smaps-samples.json", query_samples)
    write_json(artifact_dir / "mechanism.json", mechanism)
    write_json(artifact_dir / "run.json", record)
    return record


def percent_faster(baseline_seconds: float, candidate_seconds: float) -> float:
    return (baseline_seconds - candidate_seconds) * 100.0 / baseline_seconds


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--binary", type=Path, required=True)
    parser.add_argument("--raw-dir", type=Path, required=True)
    parser.add_argument("--order", choices=("runtime-first", "hint-first"), default="runtime-first")
    parser.add_argument("--batches-per-table", type=int, default=256)
    parser.add_argument("--query-iterations", type=int, default=896)
    parser.add_argument("--target-partitions", type=int, default=1)
    parser.add_argument("--cpus", default="120-123")
    parser.add_argument("--sample-interval", type=float, default=0.1)
    parser.add_argument("--timeout", type=int, default=300)
    args = parser.parse_args()
    binary = args.binary.resolve()
    raw_dir = args.raw_dir.resolve()
    if not binary.is_file():
        raise ContractError(f"missing binary: {binary}")
    raw_dir.mkdir(parents=True, exist_ok=True)
    arms = (
        (("runtime-profile-first", RUNTIME_ARM), ("compiler-hint-second", HINT_ARM))
        if args.order == "runtime-first"
        else (("compiler-hint-first", HINT_ARM), ("runtime-profile-second", RUNTIME_ARM))
    )
    runs = [
        run_one(binary=binary, raw_dir=raw_dir, label=label, arm=arm, args=args)
        for label, arm in arms
    ]
    by_arm = {row["runtime_arm"]: row for row in runs}
    runtime = by_arm[RUNTIME_ARM]
    hint = by_arm[HINT_ARM]
    if runtime["fixed_work_projection"] != hint["fixed_work_projection"]:
        raise ContractError("fixed DataFusion work or correctness digest diverged")
    query_speedup = percent_faster(
        float(runtime["fixed_work"]["query_seconds"]),
        float(hint["fixed_work"]["query_seconds"]),
    )
    operation_speedup = percent_faster(
        float(runtime["fixed_work"]["total_seconds"]),
        float(hint["fixed_work"]["total_seconds"]),
    )
    rss_change = (
        int(hint["procfs"]["peak_rss_kib"])
        - int(runtime["procfs"]["peak_rss_kib"])
    ) * 100.0 / int(runtime["procfs"]["peak_rss_kib"])
    result = {
        "schema_version": 1,
        "classification": "same-binary-policy11-runtime-profile-vs-policy9-compiler-hint",
        "binary": str(binary),
        "binary_sha256": sha256_file(binary),
        "order": args.order,
        "cpus": args.cpus,
        "warmup_iterations": 0,
        "fixed_work_match": True,
        "fixed_work": runtime["fixed_work_projection"],
        "runs": runs,
        "comparison": {
            "compiler_hint_query_speedup_percent": query_speedup,
            "compiler_hint_operation_speedup_percent_including_learning": operation_speedup,
            "compiler_hint_peak_rss_change_percent": rss_change,
            "compiler_hint_peak_anonymous_change_percent": (
                int(hint["procfs"]["peak_anonymous_kib"])
                - int(runtime["procfs"]["peak_anonymous_kib"])
            )
            * 100.0
            / int(runtime["procfs"]["peak_anonymous_kib"]),
            "compiler_hint_peak_pss_change_percent": (
                int(hint["procfs"]["peak_pss_kib"])
                - int(runtime["procfs"]["peak_pss_kib"])
            )
            * 100.0
            / int(runtime["procfs"]["peak_pss_kib"]),
        },
        "performance_claim_eligible": False,
        "claim_boundary": (
            "one fresh process per arm is a directional screen; the same binary and fixed "
            "digests isolate online runtime profiling from direct compiler-hint placement, "
            "while confidence intervals require repeated AB/BA pairs"
        ),
    }
    for value in result["comparison"].values():
        if not math.isfinite(float(value)):
            raise ContractError("comparison produced a non-finite value")
    write_json(raw_dir / "result.json", result)
    print(json.dumps(result["comparison"], indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
