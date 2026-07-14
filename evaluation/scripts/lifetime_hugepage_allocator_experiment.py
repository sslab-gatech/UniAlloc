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
from typing import Any, NamedTuple


DEFAULT_CONFIDENCE_THRESHOLD = 80
DEFAULT_CORRECT_CONFIDENCE = 90
DEFAULT_ERROR_CONFIDENCE = 60
DEFAULT_CONFIDENCE_OVERLAP_RATE = 0.10
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


class Case(NamedTuple):
    name: str
    policy: str
    false_long: float
    false_short: float
    identity_mode: str
    confidence_threshold: int
    correct_confidence: int
    error_confidence: int
    confidence_overlap_rate: float
    unknown_rate: float


# Keep the controls explicit even where two perfect-prediction rows are
# intentionally equivalent.  `long-huge-oracle` is the truth ceiling;
# `long-huge-binary` is the policy comparator for paired error arms.
BASE_CASES = (
    Case("raw-default", "raw-default", 0.0, 0.0, "exact", 0, 100, 100, 0.0, 0.0),
    Case("policy-off", "policy-off", 0.0, 0.0, "exact", 0, 100, 100, 0.0, 0.0),
    Case(
        "ordinary-segregated",
        "ordinary-segregated",
        0.0,
        0.0,
        "exact",
        0,
        100,
        100,
        0.0,
        0.0,
    ),
    Case(
        "all-huge-segregated",
        "all-huge-segregated",
        0.0,
        0.0,
        "exact",
        0,
        100,
        100,
        0.0,
        0.0,
    ),
    Case(
        "long-huge-binary", "long-huge", 0.0, 0.0, "exact", 0, 100, 100, 0.0, 0.0
    ),
    Case(
        "long-huge-oracle", "long-huge", 0.0, 0.0, "exact", 0, 100, 100, 0.0, 0.0
    ),
    Case(
        "confidence-only",
        "long-huge",
        0.0,
        0.0,
        "exact",
        DEFAULT_CONFIDENCE_THRESHOLD,
        DEFAULT_CORRECT_CONFIDENCE,
        DEFAULT_ERROR_CONFIDENCE,
        DEFAULT_CONFIDENCE_OVERLAP_RATE,
        0.0,
    ),
    Case(
        "confidence-epoch",
        "epoch-cohort",
        0.0,
        0.0,
        "exact",
        DEFAULT_CONFIDENCE_THRESHOLD,
        DEFAULT_CORRECT_CONFIDENCE,
        DEFAULT_ERROR_CONFIDENCE,
        DEFAULT_CONFIDENCE_OVERLAP_RATE,
        0.0,
    ),
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
    parser.add_argument(
        "--confidence-threshold", type=int, default=DEFAULT_CONFIDENCE_THRESHOLD
    )
    parser.add_argument(
        "--correct-confidence", type=int, default=DEFAULT_CORRECT_CONFIDENCE
    )
    parser.add_argument(
        "--error-confidence", type=int, default=DEFAULT_ERROR_CONFIDENCE
    )
    parser.add_argument(
        "--confidence-overlap-rate",
        type=float,
        default=DEFAULT_CONFIDENCE_OVERLAP_RATE,
    )
    parser.add_argument("--unknown-rate", type=float, default=0.0)
    parser.add_argument("--ephemeral-waves", type=int, default=1)
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
        or not 0 <= args.confidence_threshold <= 100
        or not 1 <= args.correct_confidence <= 100
        or not 1 <= args.error_confidence <= 100
        or not 0.0 <= args.confidence_overlap_rate <= 1.0
        or not 0.0 <= args.unknown_rate <= 1.0
    ):
        parser.error("invalid workload geometry, error rate, or confidence configuration")
    return args


def repo_root() -> Path:
    return Path(__file__).resolve().parents[2]


def cases(
    error_rates: tuple[float, ...],
    include_shuffled: bool,
    include_lifetime_only_baseline: bool,
    *,
    confidence_threshold: int = DEFAULT_CONFIDENCE_THRESHOLD,
    correct_confidence: int = DEFAULT_CORRECT_CONFIDENCE,
    error_confidence: int = DEFAULT_ERROR_CONFIDENCE,
    confidence_overlap_rate: float = DEFAULT_CONFIDENCE_OVERLAP_RATE,
    unknown_rate: float = 0.0,
) -> list[Case]:
    result = [
        case._replace(
            confidence_threshold=(
                confidence_threshold
                if case.name in {"confidence-only", "confidence-epoch"}
                else 0
            ),
            correct_confidence=(
                correct_confidence
                if case.name
                in {"long-huge-binary", "confidence-only", "confidence-epoch"}
                else 100
            ),
            error_confidence=(
                error_confidence
                if case.name
                in {"long-huge-binary", "confidence-only", "confidence-epoch"}
                else 100
            ),
            confidence_overlap_rate=(
                confidence_overlap_rate
                if case.name
                in {"long-huge-binary", "confidence-only", "confidence-epoch"}
                else 0.0
            ),
            unknown_rate=(
                unknown_rate
                if case.name
                in {"long-huge-binary", "confidence-only", "confidence-epoch"}
                else 0.0
            ),
        )
        for case in BASE_CASES
    ]

    def append_policy_triplet(suffix: str, false_long: float, false_short: float) -> None:
        result.extend(
            (
                Case(
                    f"long-huge-binary-{suffix}",
                    "long-huge",
                    false_long,
                    false_short,
                    "exact",
                    0,
                    correct_confidence,
                    error_confidence,
                    confidence_overlap_rate,
                    unknown_rate,
                ),
                Case(
                    f"confidence-only-{suffix}",
                    "long-huge",
                    false_long,
                    false_short,
                    "exact",
                    confidence_threshold,
                    correct_confidence,
                    error_confidence,
                    confidence_overlap_rate,
                    unknown_rate,
                ),
                Case(
                    f"confidence-epoch-{suffix}",
                    "epoch-cohort",
                    false_long,
                    false_short,
                    "exact",
                    confidence_threshold,
                    correct_confidence,
                    error_confidence,
                    confidence_overlap_rate,
                    unknown_rate,
                ),
            )
        )

    for rate in error_rates:
        label = str(rate).replace(".", "p")
        append_policy_triplet(f"error-{label}", rate, rate)
        append_policy_triplet(f"fp-heavy-{label}", rate, 0.0)
        if include_lifetime_only_baseline:
            result.append(
                Case(
                    f"long-huge-lifetime-only-error-{label}",
                    "long-huge",
                    rate,
                    rate,
                    "lifetime-only",
                    0,
                    correct_confidence,
                    error_confidence,
                    confidence_overlap_rate,
                    unknown_rate,
                )
            )
    if include_shuffled:
        append_policy_triplet("shuffled", 0.5, 0.5)
        if include_lifetime_only_baseline:
            result.append(
                Case(
                    "long-huge-lifetime-only-shuffled",
                    "long-huge",
                    0.5,
                    0.5,
                    "lifetime-only",
                    0,
                    correct_confidence,
                    error_confidence,
                    confidence_overlap_rate,
                    unknown_rate,
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
    case: Case | tuple[str, str, float, float, str],
    args: argparse.Namespace,
    repeat_seed: int,
) -> list[str]:
    if len(case) == 5:
        name, policy, false_long, false_short, identity_mode = case
        case = Case(
            name,
            policy,
            false_long,
            false_short,
            identity_mode,
            0,
            getattr(args, "correct_confidence", DEFAULT_CORRECT_CONFIDENCE),
            getattr(args, "error_confidence", DEFAULT_ERROR_CONFIDENCE),
            getattr(args, "confidence_overlap_rate", 0.0),
            getattr(args, "unknown_rate", 0.0),
        )
    command = [
        str(binary),
        "--policy",
        case.policy,
        "--identity-mode",
        case.identity_mode,
        "--types-per-truth",
        str(args.types_per_truth),
        "--objects",
        str(args.objects),
        "--slot-bytes",
        str(args.slot_bytes),
        "--long-fraction",
        str(args.long_fraction),
        "--false-long-rate",
        str(case.false_long),
        "--false-short-rate",
        str(case.false_short),
        "--confidence-threshold",
        str(case.confidence_threshold),
        "--correct-confidence",
        str(case.correct_confidence),
        "--error-confidence",
        str(case.error_confidence),
        "--confidence-overlap-rate",
        str(case.confidence_overlap_rate),
        "--unknown-rate",
        str(case.unknown_rate),
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
    total_allocations = int(row.get("total_allocations", 0))
    if total_allocations > 0:
        observed_lifecycle_ns = sum(
            int(row.get(field, 0))
            for field in (
                "allocation_ns",
                "ephemeral_release_ns",
                "wave_ns",
                "teardown_ns",
            )
        )
        row["observed_lifecycle_ns_per_allocation"] = (
            observed_lifecycle_ns / total_allocations
        )
    wave_allocations = int(row.get("wave_allocations", 0))
    if wave_allocations > 0:
        row["wave_ns_per_allocation"] = int(row.get("wave_ns", 0)) / wave_allocations
    return row


def hugepages_free() -> int | None:
    try:
        for line in Path("/proc/meminfo").read_text(encoding="utf-8").splitlines():
            if line.startswith("HugePages_Free:"):
                return int(line.split()[1])
    except (OSError, ValueError, IndexError):
        return None
    return None


CONFUSION_CELLS = ("tp", "tn", "fp", "fn")
CONFUSION_UNITS = ("objects", "bytes")


def safe_ratio(numerator: int, denominator: int) -> float | None:
    return None if denominator == 0 else numerator / denominator


def confusion_counts(
    rows: list[dict[str, Any]], prefix: str, unit: str
) -> dict[str, int]:
    return {
        cell: sum(int(row[f"{prefix}_{cell}_{unit}"]) for row in rows)
        for cell in CONFUSION_CELLS
    }


def confusion_metrics(
    counts: dict[str, int], *, coverage_total: int
) -> dict[str, Any]:
    decided = sum(counts.values())
    correct = counts["tp"] + counts["tn"]
    incorrect = counts["fp"] + counts["fn"]
    return {
        **counts,
        "decided": decided,
        "coverage": safe_ratio(decided, coverage_total),
        "success": safe_ratio(correct, decided),
        "failure": safe_ratio(incorrect, decided),
        "precision": safe_ratio(counts["tp"], counts["tp"] + counts["fp"]),
        "recall": safe_ratio(counts["tp"], counts["tp"] + counts["fn"]),
    }


def reported_classification_metric(row: dict[str, Any], name: str) -> float | None:
    for field in (f"classification_{name}", f"classification_{name}_rate"):
        if field in row:
            return float(row[field])
    return None


def close_metric(reported: float | None, derived: float | None) -> bool:
    if reported is None or derived is None:
        return True
    return math.isclose(reported, derived, rel_tol=1e-6, abs_tol=1e-6)


def row_confusion_closure(row: dict[str, Any]) -> tuple[bool, list[str]]:
    required = {
        "total_allocations",
        "slot_bytes",
        "static_classified",
        "static_unknown",
        "runtime_validated_objects",
        "runtime_validated_bytes",
        "runtime_validation_excluded_objects",
        "runtime_validation_excluded_bytes",
        "routed_allocations",
        "routed_deallocations",
        *(
            f"{prefix}_{cell}_{unit}"
            for prefix in ("predictor", "placement", "effective_placement")
            for cell in CONFUSION_CELLS
            for unit in CONFUSION_UNITS
        ),
    }
    missing = sorted(field for field in required if field not in row)
    if missing:
        return False, [f"missing:{field}" for field in missing]

    total = int(row["total_allocations"])
    classified = int(row["static_classified"])
    unknown = int(row["static_unknown"])
    validated_objects = int(row["runtime_validated_objects"])
    validated_bytes = int(row["runtime_validated_bytes"])
    excluded_objects = int(row["runtime_validation_excluded_objects"])
    failures: list[str] = []

    if classified + unknown != total:
        failures.append("static_objects")

    matrices: dict[tuple[str, str], dict[str, int]] = {}
    for prefix in ("predictor", "placement", "effective_placement"):
        for unit in CONFUSION_UNITS:
            counts = {
                cell: int(row[f"{prefix}_{cell}_{unit}"])
                for cell in CONFUSION_CELLS
            }
            matrices[(prefix, unit)] = counts
            if prefix == "effective_placement":
                expected = total if unit == "objects" else total * int(row["slot_bytes"])
            else:
                expected = validated_objects if unit == "objects" else validated_bytes
            if sum(counts.values()) != expected:
                failures.append(f"{prefix}_{unit}")

    routed_deallocations = int(row["routed_deallocations"])
    if validated_objects + excluded_objects != routed_deallocations:
        failures.append("runtime_objects")
    policy = str(row.get("policy") or "")
    if policy not in {"raw-default", "policy-off"}:
        if classified != int(row["routed_allocations"]):
            failures.append("classified_routed")
        if "unknown_bypasses" in row and unknown != int(row["unknown_bypasses"]):
            failures.append("unknown_bypasses")

    static_fields = [f"static_{cell}_objects" for cell in CONFUSION_CELLS]
    static_counts: dict[str, int] | None = None
    if all(field in row for field in static_fields):
        static_counts = {
            cell: int(row[f"static_{cell}_objects"])
            for cell in CONFUSION_CELLS
        }
        if sum(static_counts.values()) != classified:
            failures.append("static_confusion_objects")
    elif any(field in row for field in static_fields):
        failures.append("static_confusion_partial")

    # The probe's reported classification rates describe the static predictor.
    # Runtime predictor counters are equivalent for routed policies, while the
    # policy-off control intentionally has no runtime-routed observations.
    reported_counts = static_counts or matrices[("predictor", "objects")]
    predictor = confusion_metrics(reported_counts, coverage_total=total)
    derived = {
        "coverage": safe_ratio(classified, total),
        "success": predictor["success"],
        "failure": predictor["failure"],
        "precision": predictor["precision"],
        "recall": predictor["recall"],
    }
    for name, value in derived.items():
        if not close_metric(reported_classification_metric(row, name), value):
            failures.append(f"classification_{name}")
    effective = confusion_metrics(
        matrices[("effective_placement", "objects")], coverage_total=total
    )
    for name in ("success", "failure", "precision", "recall"):
        reported = row.get(f"placement_{name}")
        if reported is None:
            reported = row.get(f"placement_{name}_rate")
        if not close_metric(
            None if reported is None else float(reported), effective[name]
        ):
            failures.append(f"placement_{name}")
    return not failures, failures


def aggregate_classification(rows: list[dict[str, Any]]) -> dict[str, Any]:
    marker_fields = {
        "static_classified",
        "static_unknown",
        "runtime_validated_objects",
        "predictor_tp_objects",
        "placement_tp_objects",
        "effective_placement_tp_objects",
    }
    if not any(marker_fields.intersection(row) for row in rows):
        return {
            "available": False,
            "all_confusion_closed": True,
            "closure_failures": [],
        }

    closures = [row_confusion_closure(row) for row in rows]
    complete = all(ok for ok, _ in closures)
    failures = sorted({failure for _, row_failures in closures for failure in row_failures})
    required_counts_present = all(
        all(
            f"{prefix}_{cell}_{unit}" in row
            for prefix in ("predictor", "placement", "effective_placement")
            for cell in CONFUSION_CELLS
            for unit in CONFUSION_UNITS
        )
        for row in rows
    )
    if not required_counts_present:
        return {
            "available": True,
            "all_confusion_closed": False,
            "closure_failures": failures,
        }

    total_objects = sum(int(row.get("total_allocations", 0)) for row in rows)
    total_bytes = sum(
        int(row.get("total_allocations", 0)) * int(row.get("slot_bytes", 0))
        for row in rows
    )
    total_runtime_slot_bytes = sum(
        int(row.get("total_allocations", 0))
        * int(row.get("arena_slot_bytes", row.get("slot_bytes", 0)))
        for row in rows
    )
    static_classified = sum(int(row.get("static_classified", 0)) for row in rows)
    static_unknown = sum(int(row.get("static_unknown", 0)) for row in rows)
    validated_objects = sum(int(row.get("runtime_validated_objects", 0)) for row in rows)
    validated_bytes = sum(int(row.get("runtime_validated_bytes", 0)) for row in rows)
    excluded_objects = sum(
        int(row.get("runtime_validation_excluded_objects", 0)) for row in rows
    )
    excluded_bytes = sum(
        int(row.get("runtime_validation_excluded_bytes", 0)) for row in rows
    )
    return {
        "available": True,
        "all_confusion_closed": complete,
        "closure_failures": failures,
        "total_objects": total_objects,
        "total_bytes": total_bytes,
        "total_runtime_slot_bytes": total_runtime_slot_bytes,
        "static_classified_objects": static_classified,
        "static_unknown_objects": static_unknown,
        "static_coverage": safe_ratio(static_classified, static_classified + static_unknown),
        "runtime_validated_objects": validated_objects,
        "runtime_validated_bytes": validated_bytes,
        "runtime_validation_excluded_objects": excluded_objects,
        "runtime_validation_excluded_bytes": excluded_bytes,
        "runtime_byte_weighting": "rounded_arena_slot_bytes",
        "effective_placement_byte_weighting": "requested_payload_bytes",
        "runtime_validation_coverage_objects": safe_ratio(
            validated_objects, validated_objects + excluded_objects + static_unknown
        ),
        "predictor_objects": confusion_metrics(
            confusion_counts(rows, "predictor", "objects"),
            coverage_total=total_objects,
        ),
        "predictor_bytes": confusion_metrics(
            confusion_counts(rows, "predictor", "bytes"),
            coverage_total=total_runtime_slot_bytes,
        ),
        "runtime_placement_objects": confusion_metrics(
            confusion_counts(rows, "placement", "objects"),
            coverage_total=total_objects,
        ),
        "runtime_placement_bytes": confusion_metrics(
            confusion_counts(rows, "placement", "bytes"),
            coverage_total=total_runtime_slot_bytes,
        ),
        "effective_placement_objects": confusion_metrics(
            confusion_counts(rows, "effective_placement", "objects"),
            coverage_total=total_objects,
        ),
        "effective_placement_bytes": confusion_metrics(
            confusion_counts(rows, "effective_placement", "bytes"),
            coverage_total=total_bytes,
        ),
        # Primary end-to-end placement aliases include abstained/Unknown
        # predictions, which fall back to ordinary pages.
        "placement_objects": confusion_metrics(
            confusion_counts(rows, "effective_placement", "objects"),
            coverage_total=total_objects,
        ),
        "placement_bytes": confusion_metrics(
            confusion_counts(rows, "effective_placement", "bytes"),
            coverage_total=total_bytes,
        ),
        "median_current_epoch": statistics.median(
            int(row.get("current_epoch", 0)) for row in rows
        ),
        "median_phase_advances": statistics.median(
            int(row.get("phase_advances", 0)) for row in rows
        ),
        "median_epoch_cohort_extent_mappings": statistics.median(
            int(row.get("epoch_cohort_extent_mappings", 0)) for row in rows
        ),
    }


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
    for field in (
        "retained_byte_epochs",
        "hugetlb_byte_epochs",
        "ordinary_byte_epochs",
        "peak_cohort_pinned_unassigned_region_bytes",
        "steady_cohort_pinned_unassigned_region_bytes",
    ):
        result[f"median_{field}"] = statistics.median(
            float(row.get(field, 0)) for row in rows
        )
    result["median_observed_lifecycle_ns_per_allocation"] = statistics.median(
        float(row.get("observed_lifecycle_ns_per_allocation", row["allocation_ns_per_object"]))
        for row in rows
    )
    wave_costs = [float(row["wave_ns_per_allocation"]) for row in rows if "wave_ns_per_allocation" in row]
    result["median_wave_ns_per_allocation"] = (
        statistics.median(wave_costs) if wave_costs else None
    )
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
    classification = aggregate_classification(rows)
    result["classification"] = classification
    result["all_confusion_closed"] = classification["all_confusion_closed"]
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


def confidence_policy_triplets(
    rows_by_case: dict[str, list[dict[str, Any]]],
) -> list[tuple[str, str, str]]:
    triplets: list[tuple[str, str, str]] = []
    base = ("long-huge-binary", "confidence-only", "confidence-epoch")
    if all(name in rows_by_case for name in base):
        triplets.append(base)
    for epoch_name in sorted(rows_by_case):
        if not epoch_name.startswith("confidence-epoch-"):
            continue
        suffix = epoch_name.removeprefix("confidence-epoch-")
        binary_name = f"long-huge-binary-{suffix}"
        confidence_name = f"confidence-only-{suffix}"
        if binary_name in rows_by_case and confidence_name in rows_by_case:
            triplets.append((binary_name, confidence_name, epoch_name))
    return triplets


def confidence_binary_pairs(
    rows_by_case: dict[str, list[dict[str, Any]]],
) -> list[tuple[str, str]]:
    return [
        (binary_name, epoch_name)
        for binary_name, _confidence_name, epoch_name in confidence_policy_triplets(
            rows_by_case
        )
    ]


def paired_prediction_trace_evidence(
    rows_by_case: dict[str, list[dict[str, Any]]],
) -> dict[str, Any]:
    triplets = confidence_policy_triplets(rows_by_case)
    trace_available = any(
        "prediction_trace_digest" in row
        for rows in rows_by_case.values()
        for row in rows
    )
    if not trace_available:
        return {
            "available": False,
            "all_matched": True,
            "pairs": [],
            "mismatches": [],
        }

    triplet_evidence: list[dict[str, Any]] = []
    mismatches: list[dict[str, Any]] = []
    for binary_name, confidence_name, epoch_name in triplets:
        traces = {
            name: {
                int(row["repeat"]): row.get("prediction_trace_digest")
                for row in rows_by_case[name]
            }
            for name in (binary_name, confidence_name, epoch_name)
        }
        repeats = sorted(set().union(*(set(values) for values in traces.values())))
        matched = 0
        for repeat in repeats:
            digests = {name: values.get(repeat) for name, values in traces.items()}
            present = [digest for digest in digests.values() if digest is not None]
            if len(present) == 3 and len(set(present)) == 1:
                matched += 1
                continue
            mismatches.append(
                {
                    "binary": binary_name,
                    "confidence_only": confidence_name,
                    "confidence_epoch": epoch_name,
                    "repeat": repeat,
                    "digests": digests,
                }
            )
        triplet_evidence.append(
            {
                "binary": binary_name,
                "confidence_only": confidence_name,
                "confidence_epoch": epoch_name,
                "repeat_pairs": len(repeats),
                "matched_repeat_pairs": matched,
            }
        )
    return {
        "available": True,
        "all_matched": bool(triplets) and not mismatches,
        "pairs": triplet_evidence,
        "mismatches": mismatches,
    }


def relative_reduction(target: float | int | None, baseline: float | int | None) -> float | None:
    if target is None or baseline is None:
        return None
    return 0.0 if float(baseline) == 0.0 else 1.0 - float(target) / float(baseline)


def metric_delta(target: float | int | None, baseline: float | int | None) -> float | None:
    if target is None or baseline is None:
        return None
    return float(target) - float(baseline)


def policy_comparison(
    rows_by_case: dict[str, list[dict[str, Any]]],
    summaries: dict[str, dict[str, Any]],
    baseline_name: str,
    target_name: str,
    *,
    seed: int,
) -> dict[str, Any]:
    baseline = summaries[baseline_name]
    target = summaries[target_name]
    baseline_classification = baseline["classification"]
    target_classification = target["classification"]
    entry: dict[str, Any] = {
        "baseline": baseline_name,
        "target": target_name,
        "steady_retained_bytes_reduction": relative_reduction(
            target["median_steady_retained_bytes"],
            baseline["median_steady_retained_bytes"],
        ),
        "steady_retained_slack_bytes_reduction": relative_reduction(
            target["median_steady_retained_slack_bytes"],
            baseline["median_steady_retained_slack_bytes"],
        ),
        "peak_hugetlb_extents_reduction": relative_reduction(
            target["median_peak_hugetlb_extents"],
            baseline["median_peak_hugetlb_extents"],
        ),
        "retained_byte_epochs_reduction": relative_reduction(
            target["median_retained_byte_epochs"],
            baseline["median_retained_byte_epochs"],
        ),
        "hugetlb_byte_epochs_reduction": relative_reduction(
            target["median_hugetlb_byte_epochs"],
            baseline["median_hugetlb_byte_epochs"],
        ),
        "ordinary_byte_epochs_reduction": relative_reduction(
            target["median_ordinary_byte_epochs"],
            baseline["median_ordinary_byte_epochs"],
        ),
        "touch": paired_geomean_effect(
            rows_by_case,
            baseline_name,
            target_name,
            "ns_per_touch",
            seed=seed,
        ),
    }
    if baseline_classification["available"] and target_classification["available"]:
        baseline_predictor = baseline_classification["predictor_objects"]
        target_predictor = target_classification["predictor_objects"]
        baseline_placement = baseline_classification["effective_placement_objects"]
        target_placement = target_classification["effective_placement_objects"]
        entry["classification"] = {
            "baseline_static_coverage": baseline_classification["static_coverage"],
            "target_static_coverage": target_classification["static_coverage"],
            "static_coverage_delta": metric_delta(
                target_classification["static_coverage"],
                baseline_classification["static_coverage"],
            ),
            "predictor_precision_delta": metric_delta(
                target_predictor["precision"], baseline_predictor["precision"]
            ),
            "predictor_recall_delta": metric_delta(
                target_predictor["recall"], baseline_predictor["recall"]
            ),
            "predictor_false_positive_objects_reduction": relative_reduction(
                target_predictor["fp"], baseline_predictor["fp"]
            ),
            "predictor_false_negative_objects_reduction": relative_reduction(
                target_predictor["fn"], baseline_predictor["fn"]
            ),
            "effective_placement_precision_delta": metric_delta(
                target_placement["precision"], baseline_placement["precision"]
            ),
            "effective_placement_recall_delta": metric_delta(
                target_placement["recall"], baseline_placement["recall"]
            ),
            "effective_placement_false_positive_objects_reduction": relative_reduction(
                target_placement["fp"], baseline_placement["fp"]
            ),
            "effective_placement_false_positive_bytes_reduction": relative_reduction(
                target_classification["effective_placement_bytes"]["fp"],
                baseline_classification["effective_placement_bytes"]["fp"],
            ),
        }
    return entry


def confidence_epoch_comparisons(
    rows_by_case: dict[str, list[dict[str, Any]]],
    summaries: dict[str, dict[str, Any]],
    *,
    seed: int,
) -> dict[str, Any]:
    comparisons: dict[str, Any] = {}
    for index, (binary_name, confidence_name, epoch_name) in enumerate(
        confidence_policy_triplets(rows_by_case)
    ):
        comparisons[epoch_name] = {
            "binary": binary_name,
            "confidence_only": confidence_name,
            "confidence_epoch": epoch_name,
            "abstention_gain": policy_comparison(
                rows_by_case,
                summaries,
                binary_name,
                confidence_name,
                seed=seed + index * 3,
            ),
            "cohort_increment": policy_comparison(
                rows_by_case,
                summaries,
                confidence_name,
                epoch_name,
                seed=seed + index * 3 + 1,
            ),
            "total": policy_comparison(
                rows_by_case,
                summaries,
                binary_name,
                epoch_name,
                seed=seed + index * 3 + 2,
            ),
        }
    return comparisons


def summarize(
    rows_by_case: dict[str, list[dict[str, Any]]], *, seed: int = 20260714
) -> dict[str, Any]:
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
    resident_baseline_name = (
        "raw-default" if "raw-default" in summaries else "policy-off"
    )
    baseline_steady_resident = summaries[resident_baseline_name][
        "median_steady_effective_resident_kib"
    ]

    def steady_resident_reduction(target: str) -> float:
        if baseline_steady_resident == 0:
            return 0.0
        return 1.0 - (
            summaries[target]["median_steady_effective_resident_kib"]
            / baseline_steady_resident
        )

    ordinary_steady_resident_reduction = steady_resident_reduction(
        "ordinary-segregated"
    )
    long_huge_steady_resident_reduction = steady_resident_reduction(
        "long-huge-oracle"
    )
    policy_off_steady_resident = summaries["policy-off"][
        "median_steady_effective_resident_kib"
    ]

    def reduction_vs_policy_off(target: str) -> float:
        if policy_off_steady_resident == 0:
            return 0.0
        return 1.0 - (
            summaries[target]["median_steady_effective_resident_kib"]
            / policy_off_steady_resident
        )
    classification_invariants = all(
        summary["all_confusion_closed"] for summary in summaries.values()
    )
    trace_evidence = paired_prediction_trace_evidence(rows_by_case)
    allocator_invariants = all(
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
    invariants = (
        allocator_invariants
        and classification_invariants
        and trace_evidence["all_matched"]
    )
    binary_placement_go = (
        invariants
        and all_huge_peak > 0
        and long_huge_peak > 0
        and peak_hugetlb_reduction >= 0.4
        and summaries["long-huge-oracle"]["median_steady_ordinary_extents"] == 0
        and ordinary_steady_resident_reduction >= 0.4
        and long_huge_steady_resident_reduction >= 0.4
    )
    touch_ci = touch["bootstrap_95pct"]
    confidence_comparisons = confidence_epoch_comparisons(
        rows_by_case, summaries, seed=seed
    )
    confidence_error_rows = {
        name: comparison
        for name, comparison in confidence_comparisons.items()
        if "-error-" in name or "-fp-heavy-" in name or name.endswith("-shuffled")
    }
    confidence_abstention_evaluated = bool(confidence_error_rows)
    confidence_abstention_go = confidence_abstention_evaluated and any(
        (comparison["abstention_gain"].get("retained_byte_epochs_reduction") or 0.0)
        > 0.0
        and (
            comparison["abstention_gain"]
            .get("classification", {})
            .get("effective_placement_false_positive_objects_reduction")
            or 0.0
        )
        >= 0.5
        for comparison in confidence_error_rows.values()
    )
    # The persistent-long/ephemeral-wave matrix cannot identify incremental
    # epoch-cohort reclamation. A separate rolling-overlap evaluator owns that
    # claim and its lifecycle-cost gate.
    epoch_cohort_reclaim_evaluated = False
    epoch_cohort_reclaim_go = None
    if binary_placement_go and confidence_abstention_go:
        verdict = "go-binary-and-confidence-abstention-epoch-unresolved"
    elif binary_placement_go and touch_ci[0] > 0.0:
        verdict = "go-binary-lifetime-placement-and-tlb"
    elif binary_placement_go:
        verdict = "go-binary-lifetime-placement-performance-inconclusive"
    else:
        verdict = "no-go-binary-lifetime-placement"
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
        if name.startswith("long-huge-error-")
        or name.startswith("long-huge-binary-error-")
        or name.startswith("long-huge-binary-fp-heavy-")
        or name in {"long-huge-shuffled", "long-huge-binary-shuffled"}
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
        if exact_name.startswith("long-huge-binary-"):
            suffix = exact_name.removeprefix("long-huge-binary-")
        else:
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
        "schema_version": 2,
        "production_allocator_path": True,
        "case_summaries": summaries,
        "comparisons": {
            "long_huge_peak_hugetlb_reduction_vs_all_huge": peak_hugetlb_reduction,
            "ordinary_segregated_steady_resident_reduction_vs_policy_off": reduction_vs_policy_off(
                "ordinary-segregated"
            ),
            "long_huge_steady_resident_reduction_vs_policy_off": reduction_vs_policy_off(
                "long-huge-oracle"
            ),
            "resident_comparison_baseline": resident_baseline_name,
            "ordinary_segregated_steady_resident_reduction_vs_raw_default": (
                ordinary_steady_resident_reduction
            ),
            "long_huge_steady_resident_reduction_vs_raw_default": (
                long_huge_steady_resident_reduction
            ),
            "long_huge_touch_vs_ordinary_segregated": touch,
            "misprediction_sensitivity": sensitivity,
            "lifetime_only_misprediction_sensitivity": lifetime_only_sensitivity,
            "exact_identity_packing_vs_lifetime_only": identity_packing,
            "confidence_epoch_vs_binary": confidence_comparisons,
            "paired_prediction_trace": trace_evidence,
        },
        "allocator_invariants_passed": allocator_invariants,
        "classification_invariants_passed": classification_invariants,
        "paired_prediction_traces_passed": trace_evidence["all_matched"],
        "invariants_passed": invariants,
        "binary_placement_go": binary_placement_go,
        "confidence_abstention_evaluated": confidence_abstention_evaluated,
        "confidence_abstention_go": confidence_abstention_go,
        "epoch_cohort_reclaim_evaluated": epoch_cohort_reclaim_evaluated,
        "epoch_cohort_reclaim_go": epoch_cohort_reclaim_go,
        "structural_go": binary_placement_go,
        "verdict": verdict,
        "claim_grade": False,
        "claim_boundary": (
            "binary lifetime placement and confidence abstention use synthetic calibrated "
            "hints plus injected errors; this persistent-long matrix does not identify "
            "incremental epoch-cohort reclaim, and real-workload trained profiles remain open"
        ),
    }


def perf_cases(matrix: list[Case]) -> list[Case]:
    base_names = {
        "ordinary-segregated",
        "all-huge-segregated",
        "long-huge-binary",
        "confidence-only",
        "confidence-epoch",
    }
    return [
        case
        for case in matrix
        if case.name in base_names
        or case.name.startswith("long-huge-binary-error-")
        or case.name.startswith("long-huge-binary-fp-heavy-")
        or case.name.startswith("confidence-only-error-")
        or case.name.startswith("confidence-only-fp-heavy-")
        or case.name.startswith("confidence-epoch-error-")
        or case.name.startswith("confidence-epoch-fp-heavy-")
        or case.name
        in {
            "long-huge-binary-shuffled",
            "confidence-only-shuffled",
            "confidence-epoch-shuffled",
        }
    ]


def perf_command(
    binary: Path,
    case: Case,
    args: argparse.Namespace,
    repeat_seed: int,
) -> list[str]:
    return [
        "sudo",
        "-n",
        "perf",
        "stat",
        "-x",
        ";",
        "-e",
        ",".join(DEFAULT_PERF_EVENTS),
        "--",
        *probe_command(binary, case, args, repeat_seed),
    ]


def run_perf(
    binary: Path,
    raw_dir: Path,
    args: argparse.Namespace,
    matrix: list[Case],
) -> dict[str, Any]:
    if not shutil.which("perf") or not shutil.which("sudo"):
        return {"status": "skipped", "reason": "perf or sudo unavailable"}
    preflight = subprocess.run(
        ["sudo", "-n", "true"],
        cwd=repo_root(),
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=min(args.timeout, 10),
        check=False,
    )
    if preflight.returncode != 0:
        return {
            "status": "skipped",
            "reason": "sudo -n unavailable",
            "stderr": preflight.stderr.strip(),
        }

    records: list[dict[str, Any]] = []
    for case in perf_cases(matrix):
        command = perf_command(binary, case, args, args.seed)
        completed = subprocess.run(
            command,
            cwd=repo_root(),
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=args.timeout,
            check=False,
        )
        stdout_name = f"perf-{case.name}.stdout"
        stderr_name = f"perf-{case.name}.stderr"
        (raw_dir / stdout_name).write_text(completed.stdout, encoding="utf-8")
        (raw_dir / stderr_name).write_text(completed.stderr, encoding="utf-8")
        records.append(
            {
                "case": case.name,
                "returncode": completed.returncode,
                "stdout": stdout_name,
                "stderr": stderr_name,
                "command": command,
            }
        )

    result = {
        "status": (
            "collected"
            if records and all(record["returncode"] == 0 for record in records)
            else "partial"
        ),
        "events": list(DEFAULT_PERF_EVENTS),
        "records": records,
    }
    (raw_dir / "perf-manifest.json").write_text(
        json.dumps(result, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    return result


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
        confidence_threshold=args.confidence_threshold,
        correct_confidence=args.correct_confidence,
        error_confidence=args.error_confidence,
        confidence_overlap_rate=args.confidence_overlap_rate,
        unknown_rate=args.unknown_rate,
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
            "confidence_threshold": args.confidence_threshold,
            "correct_confidence": args.correct_confidence,
            "error_confidence": args.error_confidence,
            "confidence_overlap_rate": args.confidence_overlap_rate,
            "unknown_rate": args.unknown_rate,
            "ephemeral_waves": args.ephemeral_waves,
            "warmup_passes": args.warmup_passes,
            "passes": args.passes,
            "repeats": args.repeats,
            "numa_node": args.numa_node,
            "cpu": args.cpu,
            "seed": args.seed,
            "hugepages_free_before": pool_before,
            "hugepages_free_after": pool_after,
            "global_pool_restored_diagnostic": (
                pool_before is not None and pool_before == pool_after
            ),
            "perf": (
                run_perf(binary, raw_dir, args, matrix)
                if args.perf
                else {"status": "skipped"}
            ),
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
