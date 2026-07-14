#!/usr/bin/env python3
"""Compare lifetime-directed THP against ordinary memory and explicit HugeTLB.

The probe owns the allocator-level accounting.  This driver adds a strict
backing-evidence contract: MADV_HUGEPAGE/MADV_COLLAPSE counters describe intent,
while a positive /proc/self/smaps_rollup AnonHugePages delta proves that the
process actually held anonymous THPs.  /proc/vmstat deltas are system-wide
corroboration and are never treated as process attribution on their own.
"""

from __future__ import annotations

import argparse
import json
import random
import statistics
import time
from pathlib import Path
from typing import Any, NamedTuple

import lifetime_hugepage_allocator_experiment as hugepage


THP_VMSTAT_FIELDS = (
    "thp_fault_alloc",
    "thp_fault_fallback",
    "thp_fault_fallback_charge",
    "thp_collapse_alloc",
    "thp_collapse_alloc_failed",
    "thp_split_page",
    "thp_split_page_failed",
    "thp_split_pmd",
)
DEFAULT_MIN_STEADY_THP_COVERAGE = 0.95


class BackingCase(NamedTuple):
    name: str
    policy: str
    backing: str
    confidence_threshold: int


def cases(confidence_threshold: int) -> tuple[BackingCase, ...]:
    """Return paired arms; prediction/error geometry is supplied by the CLI."""

    return (
        BackingCase("system-default", "raw-default", "system-default", 0),
        BackingCase(
            "ordinary-no-thp", "ordinary-segregated", "ordinary-no-thp", 0
        ),
        BackingCase("all-thp", "all-thp-segregated", "thp", 0),
        BackingCase("static-long-thp", "long-thp", "thp", 0),
        BackingCase(
            "confidence-thp", "long-thp", "thp", confidence_threshold
        ),
        BackingCase("static-epoch-thp", "epoch-cohort-thp", "thp", 0),
        BackingCase(
            "confidence-epoch-thp",
            "epoch-cohort-thp",
            "thp",
            confidence_threshold,
        ),
        BackingCase("explicit-hugetlb", "long-huge", "hugetlb", 0),
    )


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--run-id", default=time.strftime("lifetime-thp-allocator-%Y%m%d-%H%M%S")
    )
    parser.add_argument("--objects", type=int, default=262_144)
    parser.add_argument("--slot-bytes", type=int, default=4096)
    parser.add_argument("--types-per-truth", type=int, default=1)
    parser.add_argument("--long-fraction", type=float, default=0.5)
    parser.add_argument("--false-long-rate", type=float, default=0.05)
    parser.add_argument("--false-short-rate", type=float, default=0.05)
    parser.add_argument(
        "--confidence-threshold",
        type=int,
        default=hugepage.DEFAULT_CONFIDENCE_THRESHOLD,
    )
    parser.add_argument(
        "--correct-confidence",
        type=int,
        default=hugepage.DEFAULT_CORRECT_CONFIDENCE,
    )
    parser.add_argument(
        "--error-confidence",
        type=int,
        default=hugepage.DEFAULT_ERROR_CONFIDENCE,
    )
    parser.add_argument(
        "--confidence-overlap-rate",
        type=float,
        default=hugepage.DEFAULT_CONFIDENCE_OVERLAP_RATE,
    )
    parser.add_argument("--unknown-rate", type=float, default=0.0)
    parser.add_argument("--ephemeral-waves", type=int, default=2)
    parser.add_argument("--warmup-passes", type=int, default=2)
    parser.add_argument("--passes", type=int, default=16)
    parser.add_argument("--repeats", type=int, default=5)
    parser.add_argument("--numa-node", type=int, default=0)
    parser.add_argument("--cpu", type=int, default=0)
    parser.add_argument("--seed", type=int, default=20260714)
    parser.add_argument("--timeout", type=int, default=900)
    parser.add_argument("--skip-build", action="store_true")
    parser.add_argument("--allow-hugetlb-fallback", action="store_true")
    parser.add_argument(
        "--min-steady-thp-coverage",
        type=float,
        default=DEFAULT_MIN_STEADY_THP_COVERAGE,
    )
    parser.add_argument(
        "--allow-zero-thp-coverage",
        "--allow-thp-fallback",
        dest="allow_zero_thp_coverage",
        action="store_true",
        help=(
            "diagnostic mode: permit THP-requested arms to finish with zero "
            "observed AnonHugePages coverage"
        ),
    )
    args = parser.parse_args()
    if (
        args.objects < 2
        or args.slot_bytes < 8
        or args.types_per_truth <= 0
        or not 0.0 < args.long_fraction < 1.0
        or not 0.0 <= args.false_long_rate <= 0.5
        or not 0.0 <= args.false_short_rate <= 0.5
        or not 0 <= args.confidence_threshold <= 100
        or not 1 <= args.correct_confidence <= 100
        or not 1 <= args.error_confidence <= 100
        or not 0.0 <= args.confidence_overlap_rate <= 1.0
        or not 0.0 <= args.unknown_rate <= 1.0
        or args.ephemeral_waves <= 0
        or args.passes <= 0
        or args.repeats <= 0
        or not 0.0 <= args.min_steady_thp_coverage <= 1.0
    ):
        parser.error("invalid workload or predictor configuration")
    return args


def probe_command(
    binary: Path,
    case: BackingCase,
    args: argparse.Namespace,
    repeat_seed: int,
) -> list[str]:
    command = [
        str(binary),
        "--policy",
        case.policy,
        "--identity-mode",
        "exact",
        "--types-per-truth",
        str(args.types_per_truth),
        "--objects",
        str(args.objects),
        "--slot-bytes",
        str(args.slot_bytes),
        "--long-fraction",
        str(args.long_fraction),
        "--false-long-rate",
        str(args.false_long_rate),
        "--false-short-rate",
        str(args.false_short_rate),
        "--confidence-threshold",
        str(case.confidence_threshold),
        "--correct-confidence",
        str(args.correct_confidence),
        "--error-confidence",
        str(args.error_confidence),
        "--confidence-overlap-rate",
        str(args.confidence_overlap_rate),
        "--unknown-rate",
        str(args.unknown_rate),
        "--ephemeral-waves",
        str(args.ephemeral_waves),
        "--warmup-passes",
        str(args.warmup_passes),
        "--passes",
        str(args.passes),
        "--seed",
        str(repeat_seed),
    ]
    allow_zero_thp_coverage = getattr(
        args,
        "allow_zero_thp_coverage",
        getattr(args, "allow_thp_fallback", False),
    )
    if case.backing == "thp" and not allow_zero_thp_coverage:
        command.append("--require-thp")
    elif case.backing == "ordinary-no-thp":
        command.append("--require-no-thp")
    elif case.backing == "hugetlb" and not args.allow_hugetlb_fallback:
        command.append("--require-hugetlb")
    return hugepage.pinned_command(command, args.numa_node, args.cpu)


def anon_huge_delta_kib(row: dict[str, Any]) -> int:
    baseline = int(row["baseline_anon_hugepages_kib"])
    return max(
        int(row[field]) - baseline
        for field in (
            "peak_anon_hugepages_kib",
            "post_epoch_anon_hugepages_kib",
            "steady_anon_hugepages_kib",
            "wave_max_anon_hugepages_kib",
        )
    )


def thp_vmstat_alloc_delta(row: dict[str, Any]) -> int:
    return int(row["vmstat_thp_fault_alloc_delta"]) + int(
        row["vmstat_thp_collapse_alloc_delta"]
    )


def allocator_thp_coverage(row: dict[str, Any]) -> float:
    requested_kib = int(row["lifetime_peak_thp_extents"]) * 2048
    if requested_kib == 0:
        return 0.0
    return min(1.0, anon_huge_delta_kib(row) / requested_kib)


def steady_thp_backend_vma_coverage(row: dict[str, Any]) -> float:
    requested_kib = int(row["steady_thp_extents"]) * 2048
    if requested_kib == 0:
        return 0.0
    observed_kib = max(
        0,
        int(row["steady_anon_hugepages_kib"])
        - int(row["baseline_anon_hugepages_kib"]),
    )
    return min(1.0, observed_kib / requested_kib)


def steady_allocator_thp_coverage(row: dict[str, Any]) -> float:
    """Coverage denominator follows what the allocator can confirm.

    Eager advice uses every steady THP-backend VMA. Epoch-delayed collapse uses
    only extents synchronously confirmed by MADV_COLLAPSE; candidate VMA
    coverage remains separately reported by steady_thp_backend_vma_coverage.
    """

    if str(row.get("policy", "")) == "epoch-cohort-thp":
        requested_kib = int(row["steady_thp_collapse_confirmed_extents"]) * 2048
        if requested_kib == 0:
            return 0.0
        observed_kib = max(
            0,
            int(row["steady_anon_hugepages_kib"])
            - int(row["baseline_anon_hugepages_kib"]),
        )
        return min(1.0, observed_kib / requested_kib)
    return steady_thp_backend_vma_coverage(row)


def collapse_success_rate(row: dict[str, Any]) -> float | None:
    attempts = int(row["thp_collapse_attempts"])
    if attempts == 0:
        return None
    return int(row["thp_collapse_successes"]) / attempts


def add_thp_derived_metrics(row: dict[str, Any]) -> None:
    total = int(row.get("total_allocations", 0))
    epoch_advance_ns = int(row.get("epoch_advance_ns", 0))
    first_epoch_advance_ns = int(row.get("first_epoch_advance_ns", 0))
    if total > 0:
        row["epoch_advance_ns_per_allocation"] = epoch_advance_ns / total
        row["first_epoch_advance_ns_per_allocation"] = (
            first_epoch_advance_ns / total
        )
        lifecycle_ns = sum(
            int(row.get(field, 0))
            for field in (
                "allocation_ns",
                "ephemeral_release_ns",
                "wave_ns",
                "teardown_ns",
                "first_epoch_advance_ns",
            )
        )
        row["lifecycle_including_epoch_ns_per_allocation"] = lifecycle_ns / total
        row["allocator_lifecycle_ns_per_allocation"] = lifecycle_ns / total
        row["allocator_lifecycle_excluding_first_epoch_ns_per_allocation"] = float(
            row.get("observed_lifecycle_ns_per_allocation", 0.0)
        )
    else:
        row["epoch_advance_ns_per_allocation"] = 0.0
        row["first_epoch_advance_ns_per_allocation"] = 0.0
        row["lifecycle_including_epoch_ns_per_allocation"] = 0.0
        row["allocator_lifecycle_ns_per_allocation"] = 0.0
        row["allocator_lifecycle_excluding_first_epoch_ns_per_allocation"] = 0.0
    row["allocator_thp_coverage"] = allocator_thp_coverage(row)
    row["steady_allocator_thp_coverage"] = steady_allocator_thp_coverage(row)
    row["steady_thp_backend_vma_coverage"] = steady_thp_backend_vma_coverage(row)
    row["allocator_thp_collapse_success_rate"] = collapse_success_rate(row)


def validate_backing_evidence(
    row: dict[str, Any],
    expected_backing: str,
    *,
    min_steady_thp_coverage: float | None = None,
) -> list[str]:
    """Return strict evidence failures without trusting allocator advice."""

    required = {
        "baseline_anon_hugepages_kib",
        "peak_anon_hugepages_kib",
        "post_epoch_anon_hugepages_kib",
        "steady_anon_hugepages_kib",
        "wave_peak_anon_hugepages_kib",
        "wave_max_anon_hugepages_kib",
        "thp_actual_backing_observed",
        "thp_backing_gate_passed",
        "thp_enabled_mode",
        "smaps_rollup_available",
        "smaps_rollup_parse_success",
        "post_epoch_rss_kib",
        "post_epoch_anon_kib",
        "post_epoch_hugetlb_kib",
        "wave_peak_observed",
        "wave_peak_rss_kib",
        "wave_peak_anon_kib",
        "wave_peak_hugetlb_kib",
        "wave_peak_effective_resident_kib",
        "peak_effective_resident_kib",
        "post_epoch_effective_resident_kib",
        "steady_effective_resident_kib",
        "max_effective_resident_kib",
        "overall_max_effective_resident_kib",
        "vmstat_scope",
        "vmstat_thp_fault_alloc_delta",
        "vmstat_thp_collapse_alloc_delta",
        "thp_extent_mappings",
        "thp_candidate_extent_mappings",
        "thp_advice_attempts",
        "thp_advice_successes",
        "thp_advice_failures",
        "thp_collapse_attempts",
        "thp_collapse_eligible_extents",
        "thp_collapse_successes",
        "thp_collapse_failures",
        "thp_collapse_last_error_code",
        "lifetime_peak_thp_extents",
        "steady_thp_extents",
        "steady_thp_collapse_confirmed_extents",
        "retained_byte_epochs",
        "hugetlb_byte_epochs",
        "ordinary_byte_epochs",
        "thp_backend_vma_byte_epochs",
        "byte_epoch_accounting_consistent",
        "first_epoch_advance_ns",
        "wave_evidence_sampling_ns",
        "placement_metric_semantics",
        "policy_intent_placement_success_rate",
        "policy_intent_placement_failure_rate",
        "policy_intent_placement_precision",
        "policy_intent_placement_recall",
        "runtime_confirmed_placement_available",
        "runtime_confirmed_placement_success_rate",
        "runtime_confirmed_placement_failure_rate",
        "runtime_confirmed_placement_precision",
        "runtime_confirmed_placement_recall",
    }
    missing = sorted(required.difference(row))
    if missing:
        return [f"missing:{field}" for field in missing]

    failures: list[str] = []
    if min_steady_thp_coverage is None:
        min_steady_thp_coverage = float(
            row.get(
                "required_min_steady_thp_coverage",
                DEFAULT_MIN_STEADY_THP_COVERAGE,
            )
        )
    delta = anon_huge_delta_kib(row)
    observed = bool(row["thp_actual_backing_observed"])
    if observed != (delta > 0):
        failures.append("thp_observed_mismatch")
    if row["vmstat_scope"] != "system-wide-delta":
        failures.append("vmstat_scope")
    peak_effective = int(row.get("peak_rss_kib", 0)) + int(
        row.get("peak_hugetlb_kib", 0)
    )
    post_epoch_effective = int(row["post_epoch_rss_kib"]) + int(
        row["post_epoch_hugetlb_kib"]
    )
    steady_effective = int(row.get("steady_rss_kib", 0)) + int(
        row.get("steady_hugetlb_kib", 0)
    )
    wave_effective = int(row["wave_peak_rss_kib"]) + int(
        row["wave_peak_hugetlb_kib"]
    )
    if (
        int(row["peak_effective_resident_kib"]) != peak_effective
        or int(row["post_epoch_effective_resident_kib"])
        != post_epoch_effective
        or int(row["steady_effective_resident_kib"]) != steady_effective
        or int(row["wave_peak_effective_resident_kib"]) != wave_effective
        or int(row["max_effective_resident_kib"])
        != max(peak_effective, post_epoch_effective, steady_effective, wave_effective)
        or int(row["overall_max_effective_resident_kib"])
        != max(peak_effective, post_epoch_effective, steady_effective, wave_effective)
    ):
        failures.append("effective_resident_accounting")
    byte_epoch_total = sum(
        int(row[field])
        for field in (
            "hugetlb_byte_epochs",
            "ordinary_byte_epochs",
            "thp_backend_vma_byte_epochs",
        )
    )
    if (
        byte_epoch_total != int(row["retained_byte_epochs"])
        or not bool(row["byte_epoch_accounting_consistent"])
    ):
        failures.append("byte_epoch_accounting")

    if row["placement_metric_semantics"] != "policy-intent":
        failures.append("policy_intent_placement_semantics")
    for legacy, explicit in (
        ("placement_success_rate", "policy_intent_placement_success_rate"),
        ("placement_failure_rate", "policy_intent_placement_failure_rate"),
        ("placement_precision", "policy_intent_placement_precision"),
        ("placement_recall", "policy_intent_placement_recall"),
    ):
        if legacy in row and float(row[legacy]) != float(row[explicit]):
            failures.append(f"policy_intent_alias:{legacy}")

    expected_runtime_confirmed = str(row.get("policy", "")) in {
        "all-huge-segregated",
        "long-huge",
        "epoch-cohort",
        "epoch-cohort-thp",
    }
    if bool(row["runtime_confirmed_placement_available"]) != expected_runtime_confirmed:
        failures.append("runtime_confirmed_placement_availability")

    collapse_eligible = int(row["thp_collapse_eligible_extents"])
    collapse_attempts = int(row["thp_collapse_attempts"])
    collapse_successes = int(row["thp_collapse_successes"])
    collapse_failures = int(row["thp_collapse_failures"])
    advice_attempts = int(row["thp_advice_attempts"])
    advice_successes = int(row["thp_advice_successes"])
    advice_failures = int(row["thp_advice_failures"])
    if advice_attempts != advice_successes + advice_failures:
        failures.append("thp_advice_result_accounting")
    if collapse_eligible != collapse_attempts + advice_failures:
        failures.append("thp_collapse_eligible_attempt_accounting")
    if collapse_attempts != collapse_successes + collapse_failures:
        failures.append("thp_collapse_result_accounting")

    formal_thp_evidence = expected_backing in {
        "thp",
        "thp-eligible",
        "ordinary-no-thp",
    }
    if formal_thp_evidence:
        if row["thp_enabled_mode"] != "madvise":
            failures.append("thp_enabled_mode")
        if not bool(row["smaps_rollup_available"]):
            failures.append("smaps_rollup_unavailable")
        if not bool(row["smaps_rollup_parse_success"]):
            failures.append("smaps_rollup_parse")

    if expected_backing == "thp":
        if delta <= 0:
            failures.append("anon_hugepages")
        if thp_vmstat_alloc_delta(row) <= 0:
            failures.append("vmstat_thp_allocation")
        if not bool(row["thp_backing_gate_passed"]):
            failures.append("probe_thp_gate")
        if int(row["thp_extent_mappings"]) <= 0:
            failures.append("thp_extent_mappings")
        if steady_allocator_thp_coverage(row) < min_steady_thp_coverage:
            failures.append("steady_thp_coverage")
        if str(row.get("policy", "")) == "epoch-cohort-thp":
            if int(row["steady_thp_collapse_confirmed_extents"]) <= 0:
                failures.append("steady_thp_collapse_confirmation")
    elif expected_backing == "thp-eligible":
        if int(row["thp_extent_mappings"]) <= 0:
            failures.append("thp_extent_mappings")
    elif expected_backing == "ordinary-no-thp":
        if delta > 0:
            failures.append("unexpected_anon_hugepages")
        if not bool(row["thp_backing_gate_passed"]):
            failures.append("probe_no_thp_gate")
    elif expected_backing == "hugetlb":
        if int(row.get("peak_hugetlb_kib", 0)) <= 0:
            failures.append("hugetlb_backing")
    return failures


def parse_probe(
    stdout: str,
    expected_backing: str,
    *,
    min_steady_thp_coverage: float = DEFAULT_MIN_STEADY_THP_COVERAGE,
) -> dict[str, Any]:
    json_rows = [line for line in stdout.splitlines() if line.strip().startswith("{")]
    if len(json_rows) != 1:
        raise RuntimeError(f"expected one probe JSON row, got {len(json_rows)}")

    def unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in pairs:
            if key in result:
                raise RuntimeError(f"duplicate probe JSON key: {key}")
            result[key] = value
        return result

    # json.loads normally accepts duplicate keys with last-write-wins, which
    # can silently replace the allocator's lifetime peak with one snapshot.
    json.loads(json_rows[0], object_pairs_hook=unique_object)
    row = hugepage.parse_probe(stdout)
    row["required_min_steady_thp_coverage"] = min_steady_thp_coverage
    failures = validate_backing_evidence(
        row,
        expected_backing,
        min_steady_thp_coverage=min_steady_thp_coverage,
    )
    if failures:
        raise RuntimeError(f"backing evidence failed ({','.join(failures)}): {row}")
    row["observed_anon_huge_delta_kib"] = anon_huge_delta_kib(row)
    row["observed_vmstat_thp_alloc_delta"] = thp_vmstat_alloc_delta(row)
    add_thp_derived_metrics(row)
    return row


def median_or_zero(rows: list[dict[str, Any]], field: str) -> float:
    return statistics.median(float(row.get(field, 0)) for row in rows)


def median_optional(rows: list[dict[str, Any]], field: str) -> float | None:
    values = [float(row[field]) for row in rows if row.get(field) is not None]
    return statistics.median(values) if values else None


def optional_delta(target: float | None, baseline: float | None) -> float | None:
    if target is None or baseline is None:
        return None
    return target - baseline


def summarize(rows_by_case: dict[str, list[dict[str, Any]]]) -> dict[str, Any]:
    case_summaries: dict[str, Any] = {}
    invariants = True
    for name, rows in rows_by_case.items():
        for row in rows:
            add_thp_derived_metrics(row)
        backing = str(rows[0]["expected_backing"])
        evidence_failures = sorted(
            {
                failure
                for row in rows
                for failure in validate_backing_evidence(row, backing)
            }
        )
        coverages = [allocator_thp_coverage(row) for row in rows]
        steady_coverages = [steady_allocator_thp_coverage(row) for row in rows]
        steady_vma_coverages = [
            steady_thp_backend_vma_coverage(row) for row in rows
        ]
        collapse_rates = [
            rate for row in rows if (rate := collapse_success_rate(row)) is not None
        ]
        case_summaries[name] = {
            "expected_backing": backing,
            "samples": len(rows),
            "median_ns_per_touch": median_or_zero(rows, "ns_per_touch"),
            "median_allocator_lifecycle_ns_per_allocation": median_or_zero(
                rows, "allocator_lifecycle_ns_per_allocation"
            ),
            "median_allocator_lifecycle_excluding_first_epoch_ns_per_allocation": median_or_zero(
                rows, "allocator_lifecycle_excluding_first_epoch_ns_per_allocation"
            ),
            "median_observed_lifecycle_ns_per_allocation": median_or_zero(
                rows, "allocator_lifecycle_excluding_first_epoch_ns_per_allocation"
            ),
            "observed_lifecycle_metric_semantics": "excluding-first-epoch-advance",
            "median_epoch_advance_ns": median_or_zero(rows, "epoch_advance_ns"),
            "median_epoch_advance_ns_per_allocation": median_or_zero(
                rows, "epoch_advance_ns_per_allocation"
            ),
            "median_first_epoch_advance_ns": median_or_zero(
                rows, "first_epoch_advance_ns"
            ),
            "median_first_epoch_advance_ns_per_allocation": median_or_zero(
                rows, "first_epoch_advance_ns_per_allocation"
            ),
            "median_wave_evidence_sampling_ns": median_or_zero(
                rows, "wave_evidence_sampling_ns"
            ),
            "median_lifecycle_including_epoch_ns_per_allocation": median_or_zero(
                rows, "lifecycle_including_epoch_ns_per_allocation"
            ),
            "median_peak_rss_kib": median_or_zero(rows, "peak_rss_kib"),
            "median_peak_hugetlb_kib": median_or_zero(
                rows, "peak_hugetlb_kib"
            ),
            "median_max_effective_resident_kib": median_or_zero(
                rows, "max_effective_resident_kib"
            ),
            "median_steady_effective_resident_kib": median_or_zero(
                rows, "steady_effective_resident_kib"
            ),
            "median_anon_huge_delta_kib": statistics.median(
                anon_huge_delta_kib(row) for row in rows
            ),
            "median_allocator_thp_coverage": statistics.median(coverages),
            "min_allocator_thp_coverage": min(coverages),
            "median_steady_allocator_thp_coverage": statistics.median(
                steady_coverages
            ),
            "min_steady_allocator_thp_coverage": min(steady_coverages),
            "median_steady_thp_backend_vma_coverage": statistics.median(
                steady_vma_coverages
            ),
            "min_steady_thp_backend_vma_coverage": min(steady_vma_coverages),
            "median_process_thp_delta_coverage": median_or_zero(
                rows, "peak_process_thp_delta_coverage"
            ),
            "median_vmstat_thp_alloc_delta": statistics.median(
                thp_vmstat_alloc_delta(row) for row in rows
            ),
            "median_vmstat_thp_fault_fallback_delta": median_or_zero(
                rows, "vmstat_thp_fault_fallback_delta"
            ),
            "median_vmstat_thp_collapse_failed_delta": median_or_zero(
                rows, "vmstat_thp_collapse_alloc_failed_delta"
            ),
            "median_thp_advice_attempts": median_or_zero(
                rows, "thp_advice_attempts"
            ),
            "median_thp_advice_failures": median_or_zero(
                rows, "thp_advice_failures"
            ),
            "median_thp_collapse_attempts": median_or_zero(
                rows, "thp_collapse_attempts"
            ),
            "median_thp_collapse_successes": median_or_zero(
                rows, "thp_collapse_successes"
            ),
            "median_thp_collapse_failures": median_or_zero(
                rows, "thp_collapse_failures"
            ),
            "thp_collapse_last_error_codes": sorted(
                {
                    int(row["thp_collapse_last_error_code"])
                    for row in rows
                    if int(row["thp_collapse_last_error_code"]) != 0
                }
            ),
            "median_thp_collapse_success_rate": (
                statistics.median(collapse_rates) if collapse_rates else None
            ),
            "min_thp_collapse_success_rate": (
                min(collapse_rates) if collapse_rates else None
            ),
            "median_thp_backend_vma_byte_epochs": median_or_zero(
                rows, "thp_backend_vma_byte_epochs"
            ),
            "median_classification_coverage": median_or_zero(
                rows, "classification_coverage"
            ),
            "median_classification_failure_rate": median_or_zero(
                rows, "classification_failure_rate"
            ),
            "placement_metric_semantics": "policy-intent",
            "median_policy_intent_placement_failure_rate": median_or_zero(
                rows, "policy_intent_placement_failure_rate"
            ),
            # Backward-compatible alias, explicitly labeled above.
            "median_placement_failure_rate": median_or_zero(
                rows, "policy_intent_placement_failure_rate"
            ),
            "runtime_confirmed_placement_available": all(
                bool(row["runtime_confirmed_placement_available"]) for row in rows
            ),
            "median_runtime_confirmed_placement_success_rate": median_optional(
                rows, "runtime_confirmed_placement_success_rate"
            ),
            "median_runtime_confirmed_placement_failure_rate": median_optional(
                rows, "runtime_confirmed_placement_failure_rate"
            ),
            "median_runtime_confirmed_placement_precision": median_optional(
                rows, "runtime_confirmed_placement_precision"
            ),
            "median_runtime_confirmed_placement_recall": median_optional(
                rows, "runtime_confirmed_placement_recall"
            ),
            "thp_enabled_modes": sorted({str(row["thp_enabled_mode"]) for row in rows}),
            "all_smaps_rollup_available": all(
                bool(row["smaps_rollup_available"]) for row in rows
            ),
            "all_smaps_rollup_parse_success": all(
                bool(row["smaps_rollup_parse_success"]) for row in rows
            ),
            "median_steady_retained_bytes": median_or_zero(
                rows, "steady_retained_bytes"
            ),
            "median_steady_retained_slack_bytes": median_or_zero(
                rows, "steady_retained_slack_bytes"
            ),
            "evidence_failures": evidence_failures,
        }
        invariants = invariants and not evidence_failures

    trace_names = [
        name
        for name in (
            "static-long-thp",
            "confidence-thp",
            "static-epoch-thp",
            "confidence-epoch-thp",
            "explicit-hugetlb",
        )
        if name in rows_by_case
        and all("prediction_trace_digest" in row for row in rows_by_case[name])
    ]
    trace_evidence: list[dict[str, Any]] = []
    if trace_names:
        for repeat in sorted(
            {int(row["repeat"]) for name in trace_names for row in rows_by_case[name]}
        ):
            digests = {
                name: next(
                    str(row["prediction_trace_digest"])
                    for row in rows_by_case[name]
                    if int(row["repeat"]) == repeat
                )
                for name in trace_names
            }
            matched = len(set(digests.values())) == 1
            invariants = invariants and matched
            trace_evidence.append(
                {"repeat": repeat, "matched": matched, "digests": digests}
            )

    comparisons: dict[str, Any] = {}
    comparison_index = 0
    for baseline in ("system-default", "ordinary-no-thp", "explicit-hugetlb"):
        if baseline not in rows_by_case:
            continue
        for target in (
            "all-thp",
            "static-long-thp",
            "confidence-thp",
            "static-epoch-thp",
            "confidence-epoch-thp",
        ):
            if target not in rows_by_case:
                continue
            comparison_index += 1
            comparisons[f"{target}_vs_{baseline}"] = {
                "touch": hugepage.paired_geomean_effect(
                    rows_by_case,
                    baseline,
                    target,
                    "ns_per_touch",
                    seed=20260714 + comparison_index * 3,
                ),
                "allocator_lifecycle": hugepage.paired_geomean_effect(
                    rows_by_case,
                    baseline,
                    target,
                    "allocator_lifecycle_ns_per_allocation",
                    seed=20260715 + comparison_index * 3,
                ),
                "allocator_lifecycle_excluding_first_epoch": hugepage.paired_geomean_effect(
                    rows_by_case,
                    baseline,
                    target,
                    "allocator_lifecycle_excluding_first_epoch_ns_per_allocation",
                    seed=20260715 + comparison_index * 3 + 1,
                ),
                "allocator_lifecycle_including_epoch": hugepage.paired_geomean_effect(
                    rows_by_case,
                    baseline,
                    target,
                    "allocator_lifecycle_ns_per_allocation",
                    seed=20260715 + comparison_index * 3 + 2,
                ),
                "max_effective_resident": hugepage.paired_geomean_effect(
                    rows_by_case,
                    baseline,
                    target,
                    "max_effective_resident_kib",
                    seed=20260716 + comparison_index * 3,
                ),
                "steady_effective_resident": hugepage.paired_geomean_effect(
                    rows_by_case,
                    baseline,
                    target,
                    "steady_effective_resident_kib",
                    seed=20260716 + comparison_index * 3 + 1,
                ),
                "classification_failure_rate_delta": (
                    case_summaries[target]["median_classification_failure_rate"]
                    - case_summaries[baseline]["median_classification_failure_rate"]
                ),
                "placement_metric_semantics": "policy-intent",
                "policy_intent_placement_failure_rate_delta": (
                    case_summaries[target][
                        "median_policy_intent_placement_failure_rate"
                    ]
                    - case_summaries[baseline][
                        "median_policy_intent_placement_failure_rate"
                    ]
                ),
                "runtime_confirmed_placement_failure_rate_delta": optional_delta(
                    case_summaries[target][
                        "median_runtime_confirmed_placement_failure_rate"
                    ],
                    case_summaries[baseline][
                        "median_runtime_confirmed_placement_failure_rate"
                    ],
                ),
                "anon_huge_delta_kib": case_summaries[target][
                    "median_anon_huge_delta_kib"
                ],
            }

    return {
        "schema_version": 2,
        "evidence_contract": {
            "actual_thp_backing": "/proc/self/smaps_rollup AnonHugePages delta",
            "corroboration": "/proc/vmstat THP counter delta (system-wide)",
            "advice_is_backing_proof": False,
            "formal_host_thp_mode": "madvise",
            "default_min_steady_thp_coverage": DEFAULT_MIN_STEADY_THP_COVERAGE,
            "eager_thp_coverage_denominator": "steady THP-backend extents",
            "epoch_thp_coverage_denominator": "steady MADV_COLLAPSE-confirmed extents",
            "candidate_vma_coverage": "reported separately for every THP-backend VMA",
            "resident_metric": "max(VmRSS + HugetlbPages) across initial/post-epoch/steady/wave peaks",
            "placement_rate": "policy-intent unless runtime_confirmed_placement_available",
        },
        "case_summaries": case_summaries,
        "comparisons": comparisons,
        "paired_prediction_traces": trace_evidence,
        "invariants_passed": invariants,
        "claim_grade": False,
        "claim_boundary": (
            "synthetic paired trace; vmstat deltas are system-wide and only "
            "smaps_rollup AnonHugePages establishes process THP backing; "
            "the epoch-cohort arms combine static classification with delayed "
            "runtime collapse, and a canonical-bucket epoch-only control remains future work"
        ),
    }


def main() -> int:
    args = parse_args()
    root = hugepage.repo_root()
    binary = root / "target/release/examples/lifetime_hugepage_allocator_probe"
    raw_dir = root / "evaluation/raw" / args.run_id
    result_dir = root / "evaluation/results" / args.run_id
    raw_dir.mkdir(parents=True, exist_ok=False)
    result_dir.mkdir(parents=True, exist_ok=False)

    if not args.skip_build:
        hugepage.run_checked(
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

    matrix = list(cases(args.confidence_threshold))
    rows_by_case: dict[str, list[dict[str, Any]]] = {
        case.name: [] for case in matrix
    }
    randomizer = random.Random(args.seed)
    samples_path = raw_dir / "samples.jsonl"
    with samples_path.open("w", encoding="utf-8") as samples:
        for repeat in range(args.repeats):
            order = list(matrix)
            randomizer.shuffle(order)
            repeat_seed = args.seed + repeat * 1_000_003
            for order_index, case in enumerate(order):
                completed = hugepage.run_checked(
                    probe_command(binary, case, args, repeat_seed),
                    cwd=root,
                    timeout=args.timeout,
                )
                expected_backing = (
                    "thp-eligible"
                    if case.backing == "thp" and args.allow_zero_thp_coverage
                    else case.backing
                )
                row = parse_probe(
                    completed.stdout,
                    expected_backing,
                    min_steady_thp_coverage=args.min_steady_thp_coverage,
                )
                row.update(
                    {
                        "case": case.name,
                        "expected_backing": expected_backing,
                        "repeat": repeat,
                        "order_index": order_index,
                        "repeat_seed": repeat_seed,
                    }
                )
                rows_by_case[case.name].append(row)
                samples.write(json.dumps(row, sort_keys=True) + "\n")
                samples.flush()
                print(
                    f"{case.name} repeat={repeat} "
                    f"anon_huge_delta_kib={row['observed_anon_huge_delta_kib']} "
                    f"ns/touch={row['ns_per_touch']:.3f}"
                )

    summary = summarize(rows_by_case)
    summary.update(
        {
            "run_id": args.run_id,
            "objects": args.objects,
            "slot_bytes": args.slot_bytes,
            "long_fraction": args.long_fraction,
            "false_long_rate": args.false_long_rate,
            "false_short_rate": args.false_short_rate,
            "confidence_threshold": args.confidence_threshold,
            "correct_confidence": args.correct_confidence,
            "error_confidence": args.error_confidence,
            "confidence_overlap_rate": args.confidence_overlap_rate,
            "unknown_rate": args.unknown_rate,
            "ephemeral_waves": args.ephemeral_waves,
            "repeats": args.repeats,
            "min_steady_thp_coverage": args.min_steady_thp_coverage,
            "allow_zero_thp_coverage": args.allow_zero_thp_coverage,
        }
    )
    summary_path = result_dir / "summary.json"
    summary_path.write_text(
        json.dumps(summary, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    manifest = {
        "run_id": args.run_id,
        "raw_samples": str(samples_path.relative_to(root)),
        "summary": str(summary_path.relative_to(root)),
        "commands_are_numa_pinned": True,
        "thp_vmstat_fields": list(THP_VMSTAT_FIELDS),
    }
    (result_dir / "manifest.json").write_text(
        json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    print(f"wrote {summary_path}")
    return 0 if summary["invariants_passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
