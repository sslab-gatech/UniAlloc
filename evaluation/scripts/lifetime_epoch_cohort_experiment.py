#!/usr/bin/env python3
"""Paired evaluation for rolling lifetime-epoch HugeTLB cohorts."""

from __future__ import annotations

import argparse
import json
import math
import random
import shutil
import statistics
import subprocess
import time
from pathlib import Path
from typing import Any


POLICIES = ("long-huge", "epoch-cohort")
BASELINE_POLICY = "long-huge"
TARGET_POLICY = "epoch-cohort"
DEFAULT_MIN_TARGET_REDUCTION = 0.40
DEFAULT_MIN_ALL_BOUNDARY_REDUCTION = 0.25
PAIR_PARAMETER_FIELDS = (
    "warmup_cycles",
    "measured_cycles",
    "seed",
    "slot_bytes",
    "slots_per_region",
    "regions_per_extent",
    "large_objects",
    "large_regions",
    "bridge_objects",
    "bridge_regions",
    "total_allocations",
    "measured_allocation_objects",
)
AUC_PREFIXES = ("overlap", "target", "reset", "all_boundary", "full_cycle")
AUC_KINDS = ("retained", "hugetlb", "pinned", "live")
TIMING_FIELDS = (
    "measured_lifecycle_ns_per_object",
    "measured_allocation_ns",
    "measured_release_ns",
    "measured_epoch_advance_ns",
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--run-id", default=time.strftime("lifetime-epoch-cohort-%Y%m%d-%H%M%S")
    )
    parser.add_argument("--warmup-cycles", type=int, default=2)
    parser.add_argument("--cycles", type=int, default=8)
    parser.add_argument("--repeats", type=int, default=15)
    parser.add_argument("--numa-node", type=int, default=0)
    parser.add_argument("--cpu", type=int, default=0)
    parser.add_argument("--seed", type=int, default=20260714)
    parser.add_argument("--timeout", type=int, default=120)
    parser.add_argument("--bootstrap-samples", type=int, default=10_000)
    parser.add_argument(
        "--min-target-reduction",
        type=float,
        default=DEFAULT_MIN_TARGET_REDUCTION,
    )
    parser.add_argument(
        "--min-all-boundary-reduction",
        type=float,
        default=DEFAULT_MIN_ALL_BOUNDARY_REDUCTION,
    )
    parser.add_argument("--skip-build", action="store_true")
    args = parser.parse_args()
    if (
        args.warmup_cycles < 0
        or args.cycles <= 0
        or args.repeats <= 0
        or args.bootstrap_samples <= 0
        or args.timeout <= 0
        or not 0.0 <= args.min_target_reduction <= 1.0
        or not 0.0 <= args.min_all_boundary_reduction <= 1.0
    ):
        parser.error("invalid cycles, repeats, timeout, bootstrap, or reduction gate")
    return args


def repo_root() -> Path:
    return Path(__file__).resolve().parents[2]


def pinned_command(command: list[str], numa_node: int, cpu: int) -> list[str]:
    if not shutil.which("numactl"):
        raise RuntimeError("numactl is required for reproducible CPU and NUMA placement")
    return [
        "numactl",
        f"--physcpubind={cpu}",
        f"--membind={numa_node}",
        *command,
    ]


def probe_command(
    binary: Path,
    policy: str,
    args: argparse.Namespace,
    repeat_seed: int,
) -> list[str]:
    if policy not in POLICIES:
        raise ValueError(f"unknown policy {policy}")
    command = [
        str(binary),
        "--policy",
        policy,
        "--warmup-cycles",
        str(args.warmup_cycles),
        "--measured-cycles",
        str(args.cycles),
        "--seed",
        str(repeat_seed),
        "--require-hugetlb",
    ]
    return pinned_command(command, args.numa_node, args.cpu)


def parse_probe(stdout: str) -> dict[str, Any]:
    rows = [line for line in stdout.splitlines() if line.strip().startswith("{")]
    if len(rows) != 1:
        raise RuntimeError(f"expected one probe JSON row, got {len(rows)}")
    row = json.loads(rows[0])
    if row.get("source") != "lifetime_epoch_cohort_probe":
        raise RuntimeError(f"unexpected probe source: {row.get('source')}")
    return row


def hugepages_free() -> int | None:
    try:
        for line in Path("/proc/meminfo").read_text(encoding="utf-8").splitlines():
            if line.startswith("HugePages_Free:"):
                return int(line.split()[1])
    except (OSError, ValueError, IndexError):
        return None
    return None


def run_probe(
    command: list[str], *, cwd: Path, timeout: int
) -> tuple[dict[str, Any], str]:
    before = hugepages_free()
    completed = subprocess.run(
        command,
        cwd=cwd,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=timeout,
        check=False,
    )
    after = hugepages_free()
    row = parse_probe(completed.stdout)
    row["process_returncode"] = completed.returncode
    row["hugepages_free_before"] = before
    row["hugepages_free_after"] = after
    row["pool_restored"] = before is None or after is None or after >= before
    return row, completed.stderr


def group_rows(rows: list[dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    grouped = {policy: [] for policy in POLICIES}
    for row in rows:
        policy = str(row.get("policy"))
        if policy not in grouped:
            raise RuntimeError(f"unexpected policy row {policy}")
        grouped[policy].append(row)
    return grouped


def row_gate_failures(row: dict[str, Any]) -> list[str]:
    failures: list[str] = []

    def require_bool(field: str) -> None:
        if not bool(row.get(field)):
            failures.append(field)

    for field in (
        "passed",
        "geometry_gate_passed",
        "accounting_consistent",
        "release_gate_passed",
        "runtime_validation_gate_passed",
        "hugetlb_gate_passed",
        "own_mappings_released",
        "pool_restored",
    ):
        require_bool(field)
    for field in (
        "hugetlb_fallback_extent_mappings",
        "ordinary_extent_mappings",
        "mapping_failures",
        "allocation_fallbacks",
        "extent_unmap_failures",
        "unsupported_layout_bypasses",
        "unknown_bypasses",
        "nohugepage_advice_failures",
        "runtime_validation_excluded_objects",
        "runtime_validation_excluded_bytes",
        "predictor_tn_objects",
        "predictor_fp_objects",
        "predictor_fn_objects",
        "placement_tn_objects",
        "placement_fp_objects",
        "placement_fn_objects",
        "final_current_extents",
        "final_live_objects",
    ):
        if int(row.get(field, -1)) != 0:
            failures.append(field)
    if int(row.get("process_returncode", -1)) != 0:
        failures.append("process_returncode")
    geometry = {
        "slot_bytes": 4096,
        "slots_per_region": 16,
        "regions_per_extent": 32,
        "large_objects": 496,
        "large_regions": 31,
        "bridge_objects": 256,
        "bridge_regions": 16,
    }
    for field, expected in geometry.items():
        if int(row.get(field, -1)) != expected:
            failures.append(field)
    total = int(row.get("total_allocations", -1))
    measured_objects = int(row.get("measured_allocation_objects", -1))
    slot_bytes = int(row.get("slot_bytes", 0))
    for field in (
        "routed_allocations",
        "routed_deallocations",
        "runtime_validated_objects",
        "predictor_tp_objects",
        "placement_tp_objects",
    ):
        if int(row.get(field, -2)) != total:
            failures.append(field)
    if int(row.get("slot_bump_allocations", -1)) + int(
        row.get("slot_reuse_hits", -1)
    ) != total:
        failures.append("slot_allocation_accounting")
    for field in ("runtime_validated_bytes", "predictor_tp_bytes", "placement_tp_bytes"):
        if int(row.get(field, -1)) != total * slot_bytes:
            failures.append(field)
    if int(row.get("sentinel_touches", -1)) != total * 4:
        failures.append("sentinel_touches")
    if int(row.get("phase_advances", -1)) != int(
        row.get("expected_phase_advances", -2)
    ):
        failures.append("phase_advances")
    expected_measured = int(row.get("measured_cycles", -1)) * (496 + 256)
    if measured_objects != expected_measured:
        failures.append("measured_allocation_objects")
    if row.get("all_boundary_definition") != "target_plus_reset":
        failures.append("all_boundary_definition")
    if row.get("full_cycle_definition") != "overlap_plus_target_plus_reset":
        failures.append("full_cycle_definition")
    for prefix in AUC_PREFIXES:
        for kind in AUC_KINDS:
            field = f"{prefix}_{kind}_byte_epochs"
            if int(row.get(field, -1)) < 0:
                failures.append(field)
    for field in TIMING_FIELDS:
        if float(row.get(field, 0.0)) <= 0.0:
            failures.append(field)
    return sorted(set(failures))


def paired_evidence(rows_by_policy: dict[str, list[dict[str, Any]]]) -> dict[str, Any]:
    by_repeat: dict[str, dict[int, dict[str, Any]]] = {}
    for policy, rows in rows_by_policy.items():
        by_repeat[policy] = {}
        for row in rows:
            repeat = int(row["repeat"])
            if repeat in by_repeat[policy]:
                raise RuntimeError(f"duplicate {policy} repeat {repeat}")
            by_repeat[policy][repeat] = row
    repeats = sorted(
        set(by_repeat[BASELINE_POLICY]) | set(by_repeat[TARGET_POLICY])
    )
    mismatches: list[dict[str, Any]] = []
    for repeat in repeats:
        baseline = by_repeat[BASELINE_POLICY].get(repeat)
        target = by_repeat[TARGET_POLICY].get(repeat)
        if baseline is None or target is None:
            mismatches.append({"repeat": repeat, "reason": "missing_pair"})
            continue
        differing = [
            field
            for field in PAIR_PARAMETER_FIELDS
            if baseline.get(field) != target.get(field)
        ]
        if baseline.get("trace_digest") != target.get("trace_digest"):
            differing.append("trace_digest")
        if differing:
            mismatches.append(
                {
                    "repeat": repeat,
                    "reason": "parameter_or_trace_mismatch",
                    "fields": sorted(set(differing)),
                    "baseline_digest": baseline.get("trace_digest"),
                    "target_digest": target.get("trace_digest"),
                }
            )
    expected = len(rows_by_policy[BASELINE_POLICY])
    complete = (
        len(rows_by_policy[TARGET_POLICY]) == expected
        and len(repeats) == expected
        and not mismatches
    )
    return {
        "passed": complete,
        "paired_repeats": len(repeats) - len(mismatches),
        "mismatches": mismatches,
    }


def paired_ratio(
    rows_by_policy: dict[str, list[dict[str, Any]]], field: str
) -> dict[str, Any]:
    baseline = {
        int(row["repeat"]): float(row[field])
        for row in rows_by_policy[BASELINE_POLICY]
    }
    target = {
        int(row["repeat"]): float(row[field])
        for row in rows_by_policy[TARGET_POLICY]
    }
    repeats = sorted(set(baseline) & set(target))
    if not repeats or any(baseline[repeat] <= 0.0 for repeat in repeats):
        return {"available": False, "field": field}
    ratios = [target[repeat] / baseline[repeat] for repeat in repeats]
    ratio_of_sums = sum(target[repeat] for repeat in repeats) / sum(
        baseline[repeat] for repeat in repeats
    )
    return {
        "available": True,
        "field": field,
        "ratio_of_sums": ratio_of_sums,
        "reduction": 1.0 - ratio_of_sums,
        "paired_ratios": ratios,
        "min_reduction": 1.0 - max(ratios),
        "max_reduction": 1.0 - min(ratios),
    }


def paired_bootstrap_cost(
    rows_by_policy: dict[str, list[dict[str, Any]]],
    field: str,
    *,
    seed: int,
    bootstrap_samples: int,
) -> dict[str, Any]:
    baseline = {
        int(row["repeat"]): float(row[field])
        for row in rows_by_policy[BASELINE_POLICY]
    }
    target = {
        int(row["repeat"]): float(row[field])
        for row in rows_by_policy[TARGET_POLICY]
    }
    repeats = sorted(set(baseline) & set(target))
    if not repeats or any(
        baseline[repeat] <= 0.0
        or target[repeat] <= 0.0
        or not math.isfinite(baseline[repeat])
        or not math.isfinite(target[repeat])
        for repeat in repeats
    ):
        return {"available": False, "field": field}
    ratios = [target[repeat] / baseline[repeat] for repeat in repeats]
    if any(ratio <= 0.0 or not math.isfinite(ratio) for ratio in ratios):
        return {"available": False, "field": field}

    def geomean(values: list[float]) -> float:
        return math.exp(sum(math.log(value) for value in values) / len(values))

    geomean_ratio = geomean(ratios)
    rng = random.Random(seed ^ sum(field.encode("utf-8")))
    bootstrapped = []
    for _ in range(bootstrap_samples):
        sample = [ratios[rng.randrange(len(ratios))] for _ in ratios]
        bootstrapped.append(geomean(sample) - 1.0)
    bootstrapped.sort()
    lower = bootstrapped[int(0.025 * (bootstrap_samples - 1))]
    upper = bootstrapped[int(0.975 * (bootstrap_samples - 1))]
    return {
        "available": True,
        "field": field,
        "geomean_ratio": geomean_ratio,
        "overhead": geomean_ratio - 1.0,
        "bootstrap_95pct_overhead": [lower, upper],
        "paired_ratios": ratios,
    }


def policy_summary(rows: list[dict[str, Any]]) -> dict[str, Any]:
    fields = [
        *(f"{prefix}_{kind}_byte_epochs" for prefix in AUC_PREFIXES for kind in AUC_KINDS),
        *TIMING_FIELDS,
        "measured_hugetlb_extent_mappings",
        "measured_epoch_cohort_extent_mappings",
        "measured_extent_unmaps",
        "measured_identity_region_assignments",
        "measured_identity_region_releases",
        "hugetlb_extent_mappings",
        "extent_unmaps",
        "runtime_validated_objects",
        "runtime_validation_excluded_objects",
        "predictor_tp_objects",
    ]
    result: dict[str, Any] = {"samples": len(rows)}
    for field in fields:
        result[f"median_{field}"] = statistics.median(
            float(row[field]) for row in rows
        )
    failures = [
        {"repeat": int(row["repeat"]), "failures": row_gate_failures(row)}
        for row in rows
        if row_gate_failures(row)
    ]
    result["hard_gates_passed"] = not failures
    result["hard_gate_failures"] = failures
    return result


def summarize(
    rows: list[dict[str, Any]],
    *,
    seed: int = 20260714,
    bootstrap_samples: int = 10_000,
    min_target_reduction: float = DEFAULT_MIN_TARGET_REDUCTION,
    min_all_boundary_reduction: float = DEFAULT_MIN_ALL_BOUNDARY_REDUCTION,
) -> dict[str, Any]:
    rows_by_policy = group_rows(rows)
    summaries = {
        policy: policy_summary(policy_rows)
        for policy, policy_rows in rows_by_policy.items()
    }
    pairs = paired_evidence(rows_by_policy)
    target_reduction = paired_ratio(
        rows_by_policy, "target_retained_byte_epochs"
    )
    all_boundary_reduction = paired_ratio(
        rows_by_policy, "all_boundary_retained_byte_epochs"
    )
    full_cycle_reduction = paired_ratio(
        rows_by_policy, "full_cycle_retained_byte_epochs"
    )
    auc_comparisons = {
        f"{prefix}_{kind}": paired_ratio(
            rows_by_policy, f"{prefix}_{kind}_byte_epochs"
        )
        for prefix in AUC_PREFIXES
        for kind in AUC_KINDS
    }
    lifecycle = {
        field: paired_bootstrap_cost(
            rows_by_policy,
            field,
            seed=seed,
            bootstrap_samples=bootstrap_samples,
        )
        for field in TIMING_FIELDS
    }
    structural_go = pairs["passed"] and all(
        summary["hard_gates_passed"] for summary in summaries.values()
    )
    target_effect_go = bool(
        target_reduction.get("available")
        and target_reduction["reduction"] >= min_target_reduction
    )
    all_boundary_effect_go = bool(
        all_boundary_reduction.get("available")
        and all_boundary_reduction["reduction"] >= min_all_boundary_reduction
    )
    go = structural_go and target_effect_go and all_boundary_effect_go
    return {
        "source": "lifetime_epoch_cohort_experiment",
        "recommendation": "FRAGMENTATION-GO" if go else "FRAGMENTATION-NO-GO",
        "go": go,
        "fragmentation_mechanism_go": go,
        "go_scope": "fragmentation_viability_under_runtime_and_hugetlb_hard_gates",
        "lifecycle_cost_role": "reported_tradeoff_not_part_of_fragmentation_go_gate",
        "performance_evaluated": False,
        "performance_recommendation": "INCONCLUSIVE",
        "claim_boundary": {
            "slot_bytes": 4096,
            "identity": "one_exact_type_module_callsite_identity",
            "lifetime_hint": "perfect_long_lived",
            "workload": "synthetic_rolling_overlap_large31_bridge16_regions",
            "supported_claim": "epoch_cohort_isolation_reduces_hugetlb_retention_during_the_bridge_only_target_window",
            "open_claim": "generalization_to_real_rust_workloads_and_profile_derived_lifetime_hints",
        },
        "structural_go": structural_go,
        "effect_go": target_effect_go and all_boundary_effect_go,
        "hard_gates": {
            "paired_parameters_and_trace": pairs["passed"],
            "runtime_and_hugetlb": all(
                summary["hard_gates_passed"] for summary in summaries.values()
            ),
            "target_reduction_at_least": min_target_reduction,
            "all_boundary_reduction_at_least": min_all_boundary_reduction,
            "target_reduction_passed": target_effect_go,
            "all_boundary_reduction_passed": all_boundary_effect_go,
        },
        "paired_evidence": pairs,
        "target_auc_reduction": target_reduction,
        "all_boundary_auc_reduction": all_boundary_reduction,
        "all_boundary_definition": "target_plus_reset",
        "full_cycle_auc_reduction": full_cycle_reduction,
        "full_cycle_definition": "overlap_plus_target_plus_reset",
        "auc_comparisons": auc_comparisons,
        "lifecycle_cost": lifecycle,
        "policy_summaries": summaries,
    }


def main() -> int:
    args = parse_args()
    root = repo_root()
    binary = root / "target" / "release" / "examples" / "lifetime_epoch_cohort_probe"
    if not args.skip_build:
        subprocess.run(
            [
                "cargo",
                "build",
                "-p",
                "unialloc",
                "--release",
                "--example",
                "lifetime_epoch_cohort_probe",
                "--features",
                "lifetime_hugepage",
            ],
            cwd=root,
            check=True,
            timeout=args.timeout,
        )
    if not binary.exists():
        raise RuntimeError(f"missing probe binary {binary}")

    result_dir = root / "evaluation" / "results" / args.run_id
    result_dir.mkdir(parents=True, exist_ok=False)
    raw_path = result_dir / "raw.jsonl"
    stderr_path = result_dir / "stderr.log"
    rows: list[dict[str, Any]] = []
    scheduler = random.Random(args.seed)
    with raw_path.open("w", encoding="utf-8") as raw, stderr_path.open(
        "w", encoding="utf-8"
    ) as errors:
        for repeat in range(args.repeats):
            repeat_seed = args.seed + repeat * 1_000_003
            order = list(POLICIES)
            scheduler.shuffle(order)
            for order_index, policy in enumerate(order):
                command = probe_command(binary, policy, args, repeat_seed)
                row, stderr = run_probe(command, cwd=root, timeout=args.timeout)
                row["repeat"] = repeat
                row["order_index"] = order_index
                row["command"] = command
                rows.append(row)
                raw.write(json.dumps(row, sort_keys=True) + "\n")
                raw.flush()
                if stderr:
                    errors.write(f"repeat={repeat} policy={policy}\n{stderr}\n")
                    errors.flush()

    summary = summarize(
        rows,
        seed=args.seed,
        bootstrap_samples=args.bootstrap_samples,
        min_target_reduction=args.min_target_reduction,
        min_all_boundary_reduction=args.min_all_boundary_reduction,
    )
    summary["configuration"] = {
        "warmup_cycles": args.warmup_cycles,
        "measured_cycles": args.cycles,
        "repeats": args.repeats,
        "numa_node": args.numa_node,
        "cpu": args.cpu,
        "seed": args.seed,
        "bootstrap_samples": args.bootstrap_samples,
        "hugetlb_required": True,
        "fresh_process_per_sample": True,
        "randomized_pair_order": True,
    }
    summary_path = result_dir / "summary.json"
    summary_path.write_text(
        json.dumps(summary, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    print(json.dumps(summary, indent=2, sort_keys=True))
    print(f"raw={raw_path}")
    print(f"summary={summary_path}")
    return 0 if summary["go"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
