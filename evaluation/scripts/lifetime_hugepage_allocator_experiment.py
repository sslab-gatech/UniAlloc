#!/usr/bin/env python3
"""Evaluate the production UniAlloc lifetime/size-class payload arena."""

from __future__ import annotations

import argparse
import json
import math
import random
import shutil
import statistics
import subprocess
import sys
import time
from pathlib import Path
from typing import Any


BASE_CASES = (
    ("policy-off", "policy-off", 0.0, 0.0, "exact"),
    ("ordinary-segregated", "ordinary-segregated", 0.0, 0.0, "exact"),
    ("all-huge-segregated", "all-huge-segregated", 0.0, 0.0, "exact"),
    ("long-huge-oracle", "long-huge", 0.0, 0.0, "exact"),
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--run-id", default=time.strftime("lifetime-hugepage-allocator-%Y%m%d-%H%M%S")
    )
    parser.add_argument("--objects", type=int, default=262_144)
    parser.add_argument("--slot-bytes", type=int, default=4096)
    parser.add_argument("--types-per-truth", type=int, default=1)
    parser.add_argument("--long-fraction", type=float, default=0.5)
    parser.add_argument("--error-rates", default="0.001,0.01,0.05")
    parser.add_argument("--include-shuffled", action="store_true")
    parser.add_argument("--include-lifetime-only-baseline", action="store_true")
    parser.add_argument("--ephemeral-waves", type=int, default=1)
    parser.add_argument("--warmup-passes", type=int, default=2)
    parser.add_argument("--passes", type=int, default=16)
    parser.add_argument("--repeats", type=int, default=5)
    parser.add_argument("--numa-node", type=int, default=0)
    parser.add_argument("--cpu", type=int, default=0)
    parser.add_argument("--seed", type=int, default=20260714)
    parser.add_argument("--timeout", type=int, default=900)
    parser.add_argument("--skip-build", action="store_true")
    parser.add_argument("--allow-hugetlb-fallback", action="store_true")
    args = parser.parse_args()
    try:
        args.error_rates = tuple(
            float(item) for item in args.error_rates.split(",") if item.strip()
        )
    except ValueError as error:
        parser.error(f"invalid --error-rates: {error}")
    if (
        args.objects < 2
        or args.types_per_truth <= 0
        or args.slot_bytes < 8
        or args.repeats <= 0
        or args.passes <= 0
        or args.ephemeral_waves <= 0
        or not 0.0 < args.long_fraction < 1.0
        or any(rate <= 0.0 or rate > 0.5 for rate in args.error_rates)
    ):
        parser.error("invalid workload geometry or error rate")
    return args


def repo_root() -> Path:
    return Path(__file__).resolve().parents[2]


def cases(
    error_rates: tuple[float, ...],
    include_shuffled: bool,
    include_lifetime_only_baseline: bool,
) -> list[tuple[str, str, float, float, str]]:
    result = list(BASE_CASES)
    for rate in error_rates:
        label = str(rate).replace(".", "p")
        result.append((f"long-huge-error-{label}", "long-huge", rate, rate, "exact"))
        if include_lifetime_only_baseline:
            result.append(
                (
                    f"long-huge-lifetime-only-error-{label}",
                    "long-huge",
                    rate,
                    rate,
                    "lifetime-only",
                )
            )
    if include_shuffled:
        result.append(("long-huge-shuffled", "long-huge", 0.5, 0.5, "exact"))
        if include_lifetime_only_baseline:
            result.append(
                (
                    "long-huge-lifetime-only-shuffled",
                    "long-huge",
                    0.5,
                    0.5,
                    "lifetime-only",
                )
            )
    return result


def run_checked(command: list[str], *, cwd: Path, timeout: int) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        command,
        cwd=cwd,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=timeout,
        check=True,
    )


def pinned_command(command: list[str], numa_node: int, cpu: int) -> list[str]:
    if not shutil.which("numactl"):
        raise RuntimeError("numactl is required for reproducible placement")
    return [
        "numactl",
        f"--physcpubind={cpu}",
        f"--membind={numa_node}",
        *command,
    ]


def probe_command(
    binary: Path,
    case: tuple[str, str, float, float, str],
    args: argparse.Namespace,
    repeat_seed: int,
) -> list[str]:
    _, policy, false_long, false_short, identity_mode = case
    command = [
        str(binary),
        "--policy",
        policy,
        "--identity-mode",
        identity_mode,
        "--types-per-truth",
        str(args.types_per_truth),
        "--objects",
        str(args.objects),
        "--slot-bytes",
        str(args.slot_bytes),
        "--long-fraction",
        str(args.long_fraction),
        "--false-long-rate",
        str(false_long),
        "--false-short-rate",
        str(false_short),
        "--ephemeral-waves",
        str(args.ephemeral_waves),
        "--warmup-passes",
        str(args.warmup_passes),
        "--passes",
        str(args.passes),
        "--seed",
        str(repeat_seed),
    ]
    if not args.allow_hugetlb_fallback:
        command.append("--require-hugetlb")
    return pinned_command(command, args.numa_node, args.cpu)


def parse_probe(stdout: str) -> dict[str, Any]:
    rows = [line for line in stdout.splitlines() if line.strip().startswith("{")]
    if len(rows) != 1:
        raise RuntimeError(f"expected one probe JSON row, got {len(rows)}")
    row = json.loads(rows[0])
    if row.get("source") != "lifetime_hugepage_allocator_probe" or not row.get("passed"):
        raise RuntimeError(f"probe reported failure: {row}")
    return row


def hugepages_free() -> int | None:
    try:
        for line in Path("/proc/meminfo").read_text(encoding="utf-8").splitlines():
            if line.startswith("HugePages_Free:"):
                return int(line.split()[1])
    except (OSError, ValueError, IndexError):
        return None
    return None


def median_metrics(rows: list[dict[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {"samples": len(rows)}
    for field in (
        "allocation_ns_per_object",
        "ns_per_touch",
        "peak_rss_kib",
        "steady_rss_kib",
        "peak_hugetlb_kib",
        "steady_hugetlb_kib",
        "peak_hugetlb_extents",
        "peak_ordinary_extents",
        "peak_identity_regions",
        "peak_retained_bytes",
        "peak_reusable_unassigned_region_bytes",
        "peak_assigned_region_slack_bytes",
        "peak_retained_slack_bytes",
        "peak_stranded_bytes",
        "steady_hugetlb_extents",
        "steady_ordinary_extents",
        "steady_identity_regions",
        "steady_retained_bytes",
        "steady_reusable_unassigned_region_bytes",
        "steady_assigned_region_slack_bytes",
        "steady_retained_slack_bytes",
        "steady_stranded_bytes",
        "slot_reuse_hits",
        "identity_region_assignments",
        "identity_region_releases",
    ):
        result[f"median_{field}"] = statistics.median(float(row[field]) for row in rows)
    # Linux accounts explicit hugetlb mappings outside VmRSS.  Add the two
    # process-status fields before comparing resident working sets so the
    # ordinary and HugeTLB policies use the same physical-memory denominator.
    result["median_peak_effective_resident_kib"] = statistics.median(
        float(row["peak_rss_kib"]) + float(row["peak_hugetlb_kib"]) for row in rows
    )
    result["median_steady_effective_resident_kib"] = statistics.median(
        float(row["steady_rss_kib"]) + float(row["steady_hugetlb_kib"])
        for row in rows
    )
    for field in (
        "allocation_fallbacks",
        "hugetlb_fallback_extent_mappings",
        "mapping_failures",
        "nohugepage_advice_failures",
        "extent_unmap_failures",
        "unsupported_layout_bypasses",
    ):
        result[f"max_{field}"] = max(int(row[field]) for row in rows)
    result["all_passed"] = all(bool(row["passed"]) for row in rows)
    result["all_own_mappings_released"] = all(
        bool(row["own_mappings_released"]) for row in rows
    )
    result["all_accounting_consistent"] = all(
        bool(row["accounting_consistent"]) for row in rows
    )
    result["all_region_assignments_released"] = all(
        int(row["identity_region_assignments"])
        == int(row["identity_region_releases"])
        and int(row["final_identity_regions"]) == 0
        for row in rows
    )
    result["all_routed_allocations_released"] = all(
        int(row["routed_allocations"]) == int(row["routed_deallocations"])
        and int(row["routed_allocations"])
        == int(row["slot_bump_allocations"]) + int(row["slot_reuse_hits"])
        and int(row["final_live_objects"]) == 0
        and int(row["final_live_slot_bytes"]) == 0
        for row in rows
    )
    return result


def paired_geomean_effect(
    rows_by_case: dict[str, list[dict[str, Any]]],
    baseline: str,
    target: str,
    field: str,
    *,
    seed: int = 20260714,
    bootstrap_samples: int = 10_000,
) -> dict[str, Any]:
    baseline_by_repeat = {int(row["repeat"]): float(row[field]) for row in rows_by_case[baseline]}
    target_by_repeat = {int(row["repeat"]): float(row[field]) for row in rows_by_case[target]}
    repeats = sorted(set(baseline_by_repeat) & set(target_by_repeat))
    if len(repeats) != len(baseline_by_repeat) or len(repeats) != len(target_by_repeat):
        raise RuntimeError(f"unpaired rows for {baseline} -> {target}")
    ratios = [target_by_repeat[idx] / baseline_by_repeat[idx] for idx in repeats]
    if not ratios or any(ratio <= 0.0 or not math.isfinite(ratio) for ratio in ratios):
        raise RuntimeError(f"invalid paired ratios for {field}")

    def geomean(values: list[float]) -> float:
        return math.exp(sum(math.log(value) for value in values) / len(values))

    ratio = geomean(ratios)
    rng = random.Random(seed)
    bootstrap = []
    for _ in range(bootstrap_samples):
        sample = [ratios[rng.randrange(len(ratios))] for _ in ratios]
        bootstrap.append(1.0 - geomean(sample))
    bootstrap.sort()
    lower = bootstrap[int(0.025 * (bootstrap_samples - 1))]
    upper = bootstrap[int(0.975 * (bootstrap_samples - 1))]
    return {
        "baseline": baseline,
        "target": target,
        "field": field,
        "pairs": len(ratios),
        "geomean_ratio": ratio,
        "improvement": 1.0 - ratio,
        "bootstrap_95pct": [lower, upper],
    }


def summarize(rows_by_case: dict[str, list[dict[str, Any]]], *, seed: int = 20260714) -> dict[str, Any]:
    summaries = {name: median_metrics(rows) for name, rows in rows_by_case.items()}
    touch = paired_geomean_effect(
        rows_by_case,
        "ordinary-segregated",
        "long-huge-oracle",
        "ns_per_touch",
        seed=seed,
    )
    all_huge_peak = summaries["all-huge-segregated"]["median_peak_hugetlb_extents"]
    long_huge_peak = summaries["long-huge-oracle"]["median_peak_hugetlb_extents"]
    peak_hugetlb_reduction = (
        0.0 if all_huge_peak == 0 else 1.0 - long_huge_peak / all_huge_peak
    )
    policy_off_steady_resident = summaries["policy-off"][
        "median_steady_effective_resident_kib"
    ]

    def steady_resident_reduction(target: str) -> float:
        if policy_off_steady_resident == 0:
            return 0.0
        return 1.0 - (
            summaries[target]["median_steady_effective_resident_kib"]
            / policy_off_steady_resident
        )

    ordinary_steady_resident_reduction = steady_resident_reduction(
        "ordinary-segregated"
    )
    long_huge_steady_resident_reduction = steady_resident_reduction(
        "long-huge-oracle"
    )
    invariants = all(
        summary["all_passed"]
        and summary["all_own_mappings_released"]
        and summary["all_accounting_consistent"]
        and summary["all_region_assignments_released"]
        and summary["all_routed_allocations_released"]
        and summary["max_allocation_fallbacks"] == 0
        and summary["max_hugetlb_fallback_extent_mappings"] == 0
        and summary["max_mapping_failures"] == 0
        and summary["max_nohugepage_advice_failures"] == 0
        and summary["max_extent_unmap_failures"] == 0
        and summary["max_unsupported_layout_bypasses"] == 0
        for summary in summaries.values()
    )
    structural_go = (
        invariants
        and all_huge_peak > 0
        and long_huge_peak > 0
        and peak_hugetlb_reduction >= 0.4
        and summaries["long-huge-oracle"]["median_steady_ordinary_extents"] == 0
        and ordinary_steady_resident_reduction >= 0.4
        and long_huge_steady_resident_reduction >= 0.4
    )
    touch_ci = touch["bootstrap_95pct"]
    if structural_go and touch_ci[0] > 0.0:
        verdict = "go-integrated-placement-and-tlb"
    elif structural_go:
        verdict = "go-integrated-placement-performance-inconclusive"
    else:
        verdict = "no-go-integrated-arena"
    sensitivity = {
        name: {
            "median_steady_retained_bytes": summary["median_steady_retained_bytes"],
            "median_steady_retained_slack_bytes": summary[
                "median_steady_retained_slack_bytes"
            ],
            "median_steady_reusable_unassigned_region_bytes": summary[
                "median_steady_reusable_unassigned_region_bytes"
            ],
            "median_steady_assigned_region_slack_bytes": summary[
                "median_steady_assigned_region_slack_bytes"
            ],
            "median_peak_hugetlb_extents": summary["median_peak_hugetlb_extents"],
            "median_ns_per_touch": summary["median_ns_per_touch"],
        }
        for name, summary in summaries.items()
        if name.startswith("long-huge-error-") or name == "long-huge-shuffled"
    }
    lifetime_only_sensitivity = {
        name: {
            "median_steady_retained_bytes": summary["median_steady_retained_bytes"],
            "median_steady_retained_slack_bytes": summary[
                "median_steady_retained_slack_bytes"
            ],
            "median_steady_reusable_unassigned_region_bytes": summary[
                "median_steady_reusable_unassigned_region_bytes"
            ],
            "median_steady_assigned_region_slack_bytes": summary[
                "median_steady_assigned_region_slack_bytes"
            ],
            "median_peak_hugetlb_extents": summary["median_peak_hugetlb_extents"],
            "median_ns_per_touch": summary["median_ns_per_touch"],
        }
        for name, summary in summaries.items()
        if name.startswith("long-huge-lifetime-only-error-")
        or name == "long-huge-lifetime-only-shuffled"
    }
    identity_packing = {}
    for exact_name, exact_summary in sensitivity.items():
        suffix = exact_name.removeprefix("long-huge-")
        relaxed_name = f"long-huge-lifetime-only-{suffix}"
        relaxed_summary = lifetime_only_sensitivity.get(relaxed_name)
        if relaxed_summary is None:
            continue
        relaxed_retained = relaxed_summary["median_steady_retained_bytes"]
        relaxed_slack = relaxed_summary["median_steady_retained_slack_bytes"]
        identity_packing[exact_name] = {
            "exact_steady_retained_bytes": exact_summary[
                "median_steady_retained_bytes"
            ],
            "lifetime_only_steady_retained_bytes": relaxed_retained,
            "retained_bytes_reduction": (
                0.0
                if relaxed_retained == 0
                else 1.0
                - exact_summary["median_steady_retained_bytes"] / relaxed_retained
            ),
            "exact_steady_retained_slack_bytes": exact_summary[
                "median_steady_retained_slack_bytes"
            ],
            "lifetime_only_steady_retained_slack_bytes": relaxed_slack,
            "retained_slack_reduction": (
                0.0
                if relaxed_slack == 0
                else 1.0
                - exact_summary["median_steady_retained_slack_bytes"] / relaxed_slack
            ),
        }
    return {
        "schema_version": 1,
        "production_allocator_path": True,
        "case_summaries": summaries,
        "comparisons": {
            "long_huge_peak_hugetlb_reduction_vs_all_huge": peak_hugetlb_reduction,
            "ordinary_segregated_steady_resident_reduction_vs_policy_off": ordinary_steady_resident_reduction,
            "long_huge_steady_resident_reduction_vs_policy_off": long_huge_steady_resident_reduction,
            "long_huge_touch_vs_ordinary_segregated": touch,
            "misprediction_sensitivity": sensitivity,
            "lifetime_only_misprediction_sensitivity": lifetime_only_sensitivity,
            "exact_identity_packing_vs_lifetime_only": identity_packing,
        },
        "invariants_passed": invariants,
        "structural_go": structural_go,
        "verdict": verdict,
        "claim_grade": False,
        "claim_boundary": (
            "production allocator integration with manual-oracle and injected-error hints; "
            "compiler profile plumbing is implemented, while real-workload trained profiles remain open"
        ),
    }


def main() -> int:
    args = parse_args()
    root = repo_root()
    binary = root / "target/release/examples/lifetime_hugepage_allocator_probe"
    raw_dir = root / "evaluation/raw" / args.run_id
    result_dir = root / "evaluation/results" / args.run_id
    raw_dir.mkdir(parents=True, exist_ok=False)
    result_dir.mkdir(parents=True, exist_ok=False)

    if not args.skip_build:
        run_checked(
            [
                "cargo",
                "build",
                "--release",
                "-p",
                "unialloc",
                "--example",
                "lifetime_hugepage_allocator_probe",
                "--features",
                "lifetime_hugepage",
            ],
            cwd=root,
            timeout=args.timeout,
        )
    if not binary.is_file():
        raise RuntimeError(f"missing probe binary: {binary}")

    matrix = cases(
        args.error_rates,
        args.include_shuffled,
        args.include_lifetime_only_baseline,
    )
    rows_by_case: dict[str, list[dict[str, Any]]] = {name: [] for name, *_ in matrix}
    randomizer = random.Random(args.seed)
    pool_before = hugepages_free()
    raw_path = raw_dir / "samples.jsonl"
    with raw_path.open("w", encoding="utf-8") as raw_file:
        for repeat in range(args.repeats):
            order = list(matrix)
            randomizer.shuffle(order)
            repeat_seed = args.seed + repeat * 1_000_003
            for order_index, case in enumerate(order):
                name = case[0]
                completed = run_checked(
                    probe_command(binary, case, args, repeat_seed),
                    cwd=root,
                    timeout=args.timeout,
                )
                row = parse_probe(completed.stdout)
                row.update(
                    {
                        "case": name,
                        "repeat": repeat,
                        "order_index": order_index,
                        "repeat_seed": repeat_seed,
                    }
                )
                rows_by_case[name].append(row)
                raw_file.write(json.dumps(row, sort_keys=True) + "\n")
                raw_file.flush()
                print(
                    f"{name} repeat={repeat} ns/touch={row['ns_per_touch']:.3f} "
                    f"peak_huge={row['peak_hugetlb_extents']} "
                    f"steady_retained={row['steady_retained_bytes']} "
                    f"steady_slack={row['steady_retained_slack_bytes']}"
                )

    summary = summarize(rows_by_case, seed=args.seed)
    pool_after = hugepages_free()
    summary.update(
        {
            "run_id": args.run_id,
            "objects": args.objects,
            "slot_bytes": args.slot_bytes,
            "types_per_truth": args.types_per_truth,
            "long_fraction": args.long_fraction,
            "error_rates": args.error_rates,
            "include_lifetime_only_baseline": args.include_lifetime_only_baseline,
            "ephemeral_waves": args.ephemeral_waves,
            "warmup_passes": args.warmup_passes,
            "passes": args.passes,
            "repeats": args.repeats,
            "numa_node": args.numa_node,
            "cpu": args.cpu,
            "seed": args.seed,
            "hugepages_free_before": pool_before,
            "hugepages_free_after": pool_after,
            "global_pool_restored_diagnostic": pool_before is not None and pool_before == pool_after,
        }
    )
    summary_path = result_dir / "summary.json"
    summary_path.write_text(json.dumps(summary, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(json.dumps(summary, indent=2, sort_keys=True))
    print(f"raw={raw_path}")
    print(f"summary={summary_path}")
    return 0 if summary["structural_go"] else 1


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (RuntimeError, subprocess.CalledProcessError, subprocess.TimeoutExpired) as error:
        print(f"lifetime_hugepage_allocator_experiment: {error}", file=sys.stderr)
        raise SystemExit(2)
