#!/usr/bin/env python3
"""Run the oracle-hint lifetime/hugepage feasibility matrix.

The probe intentionally separates page-size and lifetime-placement effects:

* ordinary-mixed: ordinary-page control;
* ordinary-segregated: ordinary pages split by lifetime;
* huge-mixed: naive HugeTLB placement with lifetimes interleaved;
* huge-segregated: same HugeTLB peak, separate arenas by lifetime;
* tiered: long-lived on HugeTLB, ephemeral on ordinary pages.

Generated raw/results artifacts live in the repository's ignored evaluation
directories. Copy a selected report into docs before committing it.
"""

from __future__ import annotations

import argparse
import json
import random
import shutil
import statistics
import subprocess
import sys
import time
from pathlib import Path
from typing import Any


POLICIES = (
    "ordinary-mixed",
    "ordinary-segregated",
    "huge-mixed",
    "huge-segregated",
    "tiered",
)
DEFAULT_PERF_EVENTS = (
    "cycles",
    "instructions",
    "page-faults",
    "ls_l1_d_tlb_miss.all",
    "ls_l1_d_tlb_miss.all_l2_miss",
    "ls_l1_d_tlb_miss.tlb_reload_2m_l2_hit",
    "ls_l1_d_tlb_miss.tlb_reload_2m_l2_miss",
    "ls_l1_d_tlb_miss.tlb_reload_4k_l2_hit",
    "ls_l1_d_tlb_miss.tlb_reload_4k_l2_miss",
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--run-id", default=time.strftime("lifetime-hugepage-%Y%m%d-%H%M%S"))
    parser.add_argument("--extents", type=int, default=1024)
    parser.add_argument("--slot-bytes", type=int, default=4096)
    parser.add_argument("--warmup-passes", type=int, default=2)
    parser.add_argument("--passes", type=int, default=16)
    parser.add_argument("--repeats", type=int, default=5)
    parser.add_argument("--numa-node", type=int, default=0)
    parser.add_argument("--cpu", type=int, default=0)
    parser.add_argument("--seed", type=int, default=20260714)
    parser.add_argument("--timeout", type=int, default=900)
    parser.add_argument("--skip-build", action="store_true")
    parser.add_argument("--perf", action="store_true")
    parser.add_argument("--allow-hugetlb-fallback", action="store_true")
    args = parser.parse_args()
    if args.extents <= 0 or args.extents % 2:
        parser.error("--extents must be a positive even integer")
    if args.repeats <= 0 or args.passes <= 0:
        parser.error("--repeats and --passes must be positive")
    return args


def repo_root() -> Path:
    return Path(__file__).resolve().parents[2]


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
        raise RuntimeError("numactl is required for reproducible CPU and memory placement")
    return [
        "numactl",
        f"--physcpubind={cpu}",
        f"--membind={numa_node}",
        *command,
    ]


def probe_command(binary: Path, policy: str, args: argparse.Namespace) -> list[str]:
    command = [
        str(binary),
        "--policy",
        policy,
        "--extents",
        str(args.extents),
        "--slot-bytes",
        str(args.slot_bytes),
        "--warmup-passes",
        str(args.warmup_passes),
        "--passes",
        str(args.passes),
    ]
    if not args.allow_hugetlb_fallback:
        command.append("--require-hugetlb")
    return pinned_command(command, args.numa_node, args.cpu)


def parse_probe(stdout: str) -> dict[str, Any]:
    rows = [line for line in stdout.splitlines() if line.strip().startswith("{")]
    if len(rows) != 1:
        raise RuntimeError(f"expected one probe JSON row, got {len(rows)}")
    row = json.loads(rows[0])
    if row.get("source") != "lifetime_hugepage_probe" or not row.get("passed"):
        raise RuntimeError(f"probe reported failure: {row}")
    return row


def median_metrics(rows: list[dict[str, Any]]) -> dict[str, Any]:
    timed_fields = (
        "allocation_ns_per_object",
        "release_ns_per_object",
        "ns_per_touch",
        "touch_ns",
    )
    result: dict[str, Any] = {
        "samples": len(rows),
        "fallback_extents": max(row["fallback_extents"] for row in rows),
        "nohugepage_advice_failures": max(
            row["nohugepage_advice_failures"] for row in rows
        ),
        "hugetlb_pool_restored": all(row["hugetlb_pool_restored"] for row in rows),
        "own_mappings_released": all(row["own_mappings_released"] for row in rows),
    }
    for field in timed_fields:
        values = [float(row[field]) for row in rows]
        result[f"median_{field}"] = statistics.median(values)
        result[f"min_{field}"] = min(values)
        result[f"max_{field}"] = max(values)
    for field in (
        "peak_hugetlb_extents",
        "peak_ordinary_extents",
        "reclaimed_hugetlb_extents",
        "reclaimed_ordinary_extents",
        "teardown_hugetlb_extents",
        "teardown_ordinary_extents",
        "retained_hugetlb_extents",
        "retained_ordinary_extents",
        "stranded_huge_slots",
        "stranded_slots",
        "retained_bytes",
        "live_bytes",
        "stranded_bytes",
        "huge_fragmentation_ratio",
        "total_fragmentation_ratio",
    ):
        values = {row[field] for row in rows}
        if len(values) != 1:
            raise RuntimeError(f"structural metric {field} varied across samples: {values}")
        result[field] = values.pop()
    return result


def relative_reduction(new: float, baseline: float) -> float:
    return 0.0 if baseline == 0 else 1.0 - new / baseline


def summarize(rows_by_policy: dict[str, list[dict[str, Any]]]) -> dict[str, Any]:
    policies = {policy: median_metrics(rows) for policy, rows in rows_by_policy.items()}
    ordinary = policies["ordinary-mixed"]
    ordinary_segregated = policies["ordinary-segregated"]
    huge_mixed = policies["huge-mixed"]
    huge_segregated = policies["huge-segregated"]
    tiered = policies["tiered"]
    retained_reduction = relative_reduction(
        float(tiered["retained_hugetlb_extents"]),
        float(huge_mixed["retained_hugetlb_extents"]),
    )
    segregation_peak = float(huge_segregated["peak_hugetlb_extents"])
    segregation_reclaim_fraction = (
        0.0
        if segregation_peak == 0
        else float(huge_segregated["reclaimed_hugetlb_extents"]) / segregation_peak
    )
    scan_vs_ordinary_mixed = relative_reduction(
        float(tiered["median_ns_per_touch"]),
        float(ordinary["median_ns_per_touch"]),
    )
    scan_vs_ordinary_segregated = relative_reduction(
        float(tiered["median_ns_per_touch"]),
        float(ordinary_segregated["median_ns_per_touch"]),
    )
    scan_vs_huge_mixed = relative_reduction(
        float(tiered["median_ns_per_touch"]),
        float(huge_mixed["median_ns_per_touch"]),
    )
    structural_go = (
        retained_reduction >= 0.5
        and segregation_reclaim_fraction >= 0.5
        and float(huge_mixed["huge_fragmentation_ratio"]) >= 0.49
        and float(tiered["huge_fragmentation_ratio"]) <= 0.01
        and all(policy["fallback_extents"] == 0 for policy in policies.values())
        and all(policy["nohugepage_advice_failures"] == 0 for policy in policies.values())
        and all(policy["own_mappings_released"] for policy in policies.values())
    )
    if structural_go and scan_vs_ordinary_segregated >= 0.10:
        verdict = "go-fragmentation-and-tlb"
    elif structural_go:
        verdict = "go-fragmentation-performance-inconclusive"
    else:
        verdict = "no-go-current-prototype"
    return {
        "schema_version": 1,
        "oracle_hints": True,
        "policies": policies,
        "comparisons": {
            "tiered_retained_hugetlb_reduction_vs_huge_mixed": retained_reduction,
            "huge_segregated_peak_pages_reclaimed": segregation_reclaim_fraction,
            "tiered_scan_improvement_vs_ordinary_mixed": scan_vs_ordinary_mixed,
            "tiered_scan_improvement_vs_ordinary_segregated": scan_vs_ordinary_segregated,
            "tiered_scan_improvement_vs_huge_mixed": scan_vs_huge_mixed,
        },
        "structural_go": structural_go,
        "verdict": verdict,
        "claim_boundary": "manual-oracle lifetime hints; fixed-size research arena; not compiler-derived or production-integrated",
    }


def run_perf(
    binary: Path,
    raw_dir: Path,
    args: argparse.Namespace,
) -> dict[str, str]:
    if not shutil.which("perf") or not shutil.which("sudo"):
        return {"status": "skipped", "reason": "perf or sudo unavailable"}
    result: dict[str, str] = {"status": "collected"}
    for policy in POLICIES:
        command = [
            "sudo",
            "-n",
            "perf",
            "stat",
            "-x",
            ";",
            "-e",
            ",".join(DEFAULT_PERF_EVENTS),
            "--",
            *probe_command(binary, policy, args),
        ]
        completed = run_checked(command, cwd=repo_root(), timeout=args.timeout)
        (raw_dir / f"perf-{policy}.stderr").write_text(completed.stderr, encoding="utf-8")
        (raw_dir / f"perf-{policy}.stdout").write_text(completed.stdout, encoding="utf-8")
    return result


def main() -> int:
    args = parse_args()
    root = repo_root()
    binary = root / "target/release/examples/lifetime_hugepage_probe"
    raw_dir = root / "evaluation/raw" / args.run_id
    result_dir = root / "evaluation/results" / args.run_id
    raw_dir.mkdir(parents=True, exist_ok=False)
    result_dir.mkdir(parents=True, exist_ok=False)

    if not args.skip_build:
        run_checked(
            ["cargo", "build", "--release", "-p", "unialloc", "--example", "lifetime_hugepage_probe"],
            cwd=root,
            timeout=args.timeout,
        )
    if not binary.is_file():
        raise RuntimeError(f"probe binary is missing: {binary}")

    rows_by_policy: dict[str, list[dict[str, Any]]] = {policy: [] for policy in POLICIES}
    randomizer = random.Random(args.seed)
    raw_path = raw_dir / "samples.jsonl"
    with raw_path.open("w", encoding="utf-8") as raw_file:
        for repeat in range(args.repeats):
            policy_order = list(POLICIES)
            randomizer.shuffle(policy_order)
            for order_index, policy in enumerate(policy_order):
                completed = run_checked(
                    probe_command(binary, policy, args), cwd=root, timeout=args.timeout
                )
                row = parse_probe(completed.stdout)
                row["repeat"] = repeat
                row["order_index"] = order_index
                rows_by_policy[policy].append(row)
                raw_file.write(json.dumps(row, sort_keys=True) + "\n")
                raw_file.flush()
                print(
                    f"{policy} repeat={repeat} ns/touch={row['ns_per_touch']:.3f} "
                    f"retained_huge={row['retained_hugetlb_extents']}"
                )

    summary = summarize(rows_by_policy)
    summary.update(
        {
            "run_id": args.run_id,
            "extents": args.extents,
            "slot_bytes": args.slot_bytes,
            "warmup_passes": args.warmup_passes,
            "passes": args.passes,
            "repeats": args.repeats,
            "numa_node": args.numa_node,
            "cpu": args.cpu,
            "seed": args.seed,
            "perf": run_perf(binary, raw_dir, args) if args.perf else {"status": "skipped"},
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
        print(f"lifetime_hugepage_experiment: {error}", file=sys.stderr)
        raise SystemExit(2)
