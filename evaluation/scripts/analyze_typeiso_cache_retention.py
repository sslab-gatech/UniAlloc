#!/usr/bin/env python3
"""Infer Type Isolation cache-retention flow from real-world counters.

This analyzer consumes ``results.json`` artifacts produced by
``realworld_type_isolation_matrix.py`` and selects measured
``typeiso_coverage`` records.  The result is deliberately event based: the
reporter exposes current-thread L1 snapshots and a process-wide L2 depot gauge,
without a per-object lifetime trace.

For each measurement the event-flow model uses these counter names::

    A = typed_allocations
    D = typed_deallocations
    I = cache_inserts
    H = cache_hits
    S_plain = side_cache_entries
    S_metadata = metadata_segregation_side_cache_entries
    S_depot = cache_admission_depot_current_entries
    S = S_plain + S_metadata + S_depot
    B = cache_bypasses
    R = cache_admission_depot_rescued_overflow_events
    P = cache_admission_depot_hits_after_recorded_l1_bypass_events

and derives::

    allocation_misses                     = A - H
    immediate_non_admissions              = D - I
    inferred_post_admission_terminal_exits = I - H - S
    cache_nonretained_at_report_events      = D - H - S
    bypass_residual                        = B - (A - H) - (D - I) - R - P

``side_cache_entries`` and ``metadata_segregation_side_cache_entries`` sample
the reporting thread's current plain and out-of-line metadata-segregated TLS
caches.  Historical artifacts lack the second field and are interpreted as
having zero reported metadata-segregated entries.  Consequently,
post-admission exits are an inference for workloads whose worker caches have
drained before the atexit reporter runs.  ``D - H - S`` is an end-snapshot
non-retention residual: it includes objects freed after their last allocation
demand. Security-escape measurement requires direct per-object lifetime
tracing.

Reason-total conservation is specific to the plain compiler/type-isolation
policy used by this runner. Delayed-free, guard-page, lifetime-hugepage, and
other policy combinations need lifecycle-specific admission accounting.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import pathlib
import statistics
import sys
from collections import defaultdict
from typing import Any, Iterable, Sequence


TYPEISO_COVERAGE_VARIANT = "typeiso_coverage"
REQUIRED_COUNTERS = (
    "typed_allocations",
    "typed_deallocations",
    "cache_inserts",
    "cache_hits",
    "cache_bypasses",
    "side_cache_entries",
)

OPTIONAL_COUNTER_DEFAULTS = {
    "metadata_segregation_side_cache_entries": 0,
    "cache_admission_depot_current_entries": 0,
    "cache_admission_depot_peak_entries": 0,
    "cache_admission_depot_current_rounded_bytes": 0,
    "cache_admission_depot_peak_rounded_bytes": 0,
    "cache_admission_depot_attempts_events": 0,
    "cache_admission_depot_attempts_rounded_bytes": 0,
    "cache_admission_depot_inserts_events": 0,
    "cache_admission_depot_inserts_rounded_bytes": 0,
    "cache_admission_depot_rescued_overflow_events": 0,
    "cache_admission_depot_rescued_overflow_rounded_bytes": 0,
    "cache_admission_depot_hits_events": 0,
    "cache_admission_depot_hits_rounded_bytes": 0,
    "cache_admission_depot_hits_after_recorded_l1_bypass_events": 0,
    "cache_admission_depot_hits_after_recorded_l1_bypass_rounded_bytes": 0,
    "cache_admission_depot_evictions_events": 0,
    "cache_admission_depot_evictions_rounded_bytes": 0,
    "cache_admission_depot_rejected_capacity_events": 0,
    "cache_admission_depot_rejected_capacity_rounded_bytes": 0,
    "cache_admission_depot_rejected_policy_events": 0,
    "cache_admission_depot_rejected_policy_rounded_bytes": 0,
    "cache_admission_depot_rejected_registry_headroom_events": 0,
    "cache_admission_depot_rejected_registry_headroom_rounded_bytes": 0,
    "cache_admission_depot_from_probe_window_full_events": 0,
    "cache_admission_depot_from_probe_window_full_rounded_bytes": 0,
    "cache_admission_depot_from_slot_depth_events": 0,
    "cache_admission_depot_from_slot_depth_rounded_bytes": 0,
    "cache_admission_depot_from_slot_byte_budget_events": 0,
    "cache_admission_depot_from_slot_byte_budget_rounded_bytes": 0,
    "cache_admission_depot_from_aggregate_byte_budget_events": 0,
    "cache_admission_depot_from_aggregate_byte_budget_rounded_bytes": 0,
    "cache_admission_depot_from_aggregate_entry_budget_events": 0,
    "cache_admission_depot_from_aggregate_entry_budget_rounded_bytes": 0,
    "cache_admission_depot_from_segregated_capacity_events": 0,
    "cache_admission_depot_from_segregated_capacity_rounded_bytes": 0,
    "cache_admission_depot_from_local_eviction_events": 0,
    "cache_admission_depot_from_local_eviction_rounded_bytes": 0,
}

ADMISSION_REJECTION_OUTCOMES = (
    "rejected_too_small",
    "rejected_too_large",
    "rejected_metadata_ineligible",
    "rejected_probe_window_full",
    "rejected_slot_depth",
    "rejected_slot_byte_budget",
    "rejected_aggregate_byte_budget",
    "rejected_aggregate_entry_budget",
    "rejected_structural_or_alignment",
    "rejected_segregated_capacity",
    "rejected_registry_pressure",
)
ADMISSION_TERMINAL_OUTCOMES = ("admitted", *ADMISSION_REJECTION_OUTCOMES)
ADMISSION_AUXILIARY_OUTCOMES = (
    "ownership_registry_full_failstop",
    "tiny_side_inserts",
    "tiny_side_hits",
)
ADMISSION_OUTCOMES = (*ADMISSION_TERMINAL_OUTCOMES, *ADMISSION_AUXILIARY_OUTCOMES)
ADMISSION_METRICS = ("events", "rounded_bytes")
ADMISSION_TERMINAL_FIELD = "cache_admission_terminal_events"

DERIVED_COUNT_FIELDS = (
    "retained_side_cache_entries",
    "retained_cache_entries",
    "allocation_misses",
    "immediate_non_admissions",
    "l1_overflows_rescued_by_depot",
    "inferred_post_admission_terminal_exits",
    "cache_nonretained_at_report_events",
    "bypass_residual",
)

DERIVED_RATE_FIELDS = (
    "allocation_miss_rate_of_typed_allocations",
    "immediate_non_admission_rate_of_typed_deallocations",
    "post_admission_terminal_exit_rate_of_typed_deallocations",
    "post_admission_terminal_exit_rate_of_cache_inserts",
    "cache_nonretained_at_report_rate_of_typed_deallocations",
    "retained_at_report_rate_of_typed_deallocations",
    "bypass_residual_rate_of_cache_bypasses",
)


class RetentionAnalysisError(RuntimeError):
    """Raised when an artifact cannot support the event-flow inference."""


def non_negative_integer(value: object, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise RetentionAnalysisError(f"{label} must be a non-negative integer")
    return value


def event_rate(numerator: int, denominator: int) -> float | None:
    """Return an event rate, preserving an undefined zero-event denominator."""

    if denominator == 0:
        return None
    return numerator / denominator


def analyze_admission_reason_conservation(
    stats: dict[str, Any],
    *,
    app: str,
    typed_deallocations: int,
    cache_inserts: int,
) -> dict[str, Any]:
    """Validate an all-or-none reason-specific cache-admission snapshot."""

    outcome_fields = {
        f"cache_admission_{outcome}_{metric}"
        for outcome in ADMISSION_OUTCOMES
        for metric in ADMISSION_METRICS
    }
    required_fields = outcome_fields | {ADMISSION_TERMINAL_FIELD}
    present_fields = required_fields.intersection(stats)
    if not present_fields:
        return {
            "available": False,
            "reason": "reason-specific admission fields are absent from this artifact",
        }
    missing_fields = sorted(required_fields - present_fields)
    if missing_fields:
        raise RetentionAnalysisError(
            f"{app}: incomplete cache-admission telemetry; missing "
            + ", ".join(missing_fields)
        )

    outcomes: dict[str, dict[str, int]] = {}
    for outcome in ADMISSION_OUTCOMES:
        outcomes[outcome] = {
            metric: non_negative_integer(
                stats[f"cache_admission_{outcome}_{metric}"],
                f"{app}.stats.cache_admission_{outcome}_{metric}",
            )
            for metric in ADMISSION_METRICS
        }
    terminal_events = non_negative_integer(
        stats[ADMISSION_TERMINAL_FIELD],
        f"{app}.stats.{ADMISSION_TERMINAL_FIELD}",
    )
    rejected_events = sum(
        outcomes[outcome]["events"] for outcome in ADMISSION_REJECTION_OUTCOMES
    )
    expected_rejected_events = typed_deallocations - cache_inserts
    calculated_terminal_events = sum(
        outcomes[outcome]["events"] for outcome in ADMISSION_TERMINAL_OUTCOMES
    )

    if terminal_events != typed_deallocations:
        raise RetentionAnalysisError(
            f"{app}: cache_admission_terminal_events ({terminal_events}) differs from "
            f"typed_deallocations ({typed_deallocations})"
        )
    if rejected_events != expected_rejected_events:
        raise RetentionAnalysisError(
            f"{app}: cache-admission rejected reason sum ({rejected_events}) differs "
            f"from typed_deallocations - cache_inserts ({expected_rejected_events})"
        )
    if outcomes["admitted"]["events"] != cache_inserts:
        raise RetentionAnalysisError(
            f"{app}: admitted reason events ({outcomes['admitted']['events']}) differs "
            f"from cache_inserts ({cache_inserts})"
        )
    if calculated_terminal_events != terminal_events:
        raise RetentionAnalysisError(
            f"{app}: calculated terminal reason sum ({calculated_terminal_events}) "
            f"differs from cache_admission_terminal_events ({terminal_events})"
        )

    rejected_rounded_bytes = sum(
        outcomes[outcome]["rounded_bytes"]
        for outcome in ADMISSION_REJECTION_OUTCOMES
    )
    return {
        "available": True,
        "terminal_events": terminal_events,
        "calculated_terminal_events": calculated_terminal_events,
        "rejected_events": rejected_events,
        "expected_rejected_events": expected_rejected_events,
        "rejected_rounded_bytes": rejected_rounded_bytes,
        "outcomes": {
            outcome: outcomes[outcome] for outcome in ADMISSION_TERMINAL_OUTCOMES
        },
        "ownership_registry_full_failstop": outcomes[
            "ownership_registry_full_failstop"
        ],
        "tiny_side_inserts": outcomes["tiny_side_inserts"],
        "tiny_side_hits": outcomes["tiny_side_hits"],
    }


def analyze_semantic_type_rows(
    measurement: dict[str, Any], *, app: str
) -> dict[str, Any]:
    """Summarize optional per-semantic-row pressure without claiming event order."""

    raw_rows = measurement.get("type_stats")
    if raw_rows is None:
        return {
            "available": False,
            "reason": "per-semantic-row records are absent from this artifact",
        }
    if not isinstance(raw_rows, list):
        raise RetentionAnalysisError(f"{app}.type_stats must be a list")

    required = (
        "type_id",
        "module_id",
        "callsite",
        "allocations",
        "deallocations",
        "cache_hits",
        "cache_inserts",
        "cache_bypasses",
        "observed_alloc_size",
        "observed_alloc_align",
        "observed_dealloc_size",
        "observed_dealloc_align",
        "policy_flags_seen",
    )
    analyzed_rows: list[dict[str, Any]] = []
    for position, raw_row in enumerate(raw_rows):
        if not isinstance(raw_row, dict):
            raise RetentionAnalysisError(
                f"{app}.type_stats[{position}] must be a JSON object"
            )
        row = {
            field: non_negative_integer(
                raw_row.get(field), f"{app}.type_stats[{position}].{field}"
            )
            for field in required
        }
        allocation_misses = row["allocations"] - row["cache_hits"]
        immediate_non_admissions = row["deallocations"] - row["cache_inserts"]
        if allocation_misses < 0:
            raise RetentionAnalysisError(
                f"{app}.type_stats[{position}]: cache_hits exceeds allocations"
            )
        if immediate_non_admissions < 0:
            raise RetentionAnalysisError(
                f"{app}.type_stats[{position}]: cache_inserts exceeds deallocations"
            )
        l1_pressure_before_depot_correction = (
            row["cache_bypasses"]
            - allocation_misses
            - immediate_non_admissions
        )
        if l1_pressure_before_depot_correction < 0:
            raise RetentionAnalysisError(
                f"{app}.type_stats[{position}]: cache_bypasses is smaller than "
                "allocation misses plus immediate non-admissions"
            )
        analyzed_rows.append(
            {
                **row,
                "allocation_misses": allocation_misses,
                "immediate_non_admissions": immediate_non_admissions,
                "l1_pressure_before_depot_correction": (
                    l1_pressure_before_depot_correction
                ),
                "cache_hit_rate_of_allocations": event_rate(
                    row["cache_hits"], row["allocations"]
                ),
                "immediate_non_admission_rate_of_deallocations": event_rate(
                    immediate_non_admissions, row["deallocations"]
                ),
            }
        )

    top_pressure_rows = sorted(
        analyzed_rows,
        key=lambda row: (
            row["immediate_non_admissions"],
            row["cache_bypasses"],
            row["deallocations"],
        ),
        reverse=True,
    )[:10]
    return {
        "available": True,
        "semantic_row_count": len(analyzed_rows),
        "distinct_type_ids": len({row["type_id"] for row in analyzed_rows}),
        "counter_sums": {
            field: sum(row[field] for row in analyzed_rows)
            for field in (
                "allocations",
                "deallocations",
                "cache_hits",
                "cache_inserts",
                "cache_bypasses",
                "allocation_misses",
                "immediate_non_admissions",
            )
        },
        "top_pressure_rows": top_pressure_rows,
        "identity_scope": "type_id + module_id + callsite",
        "layout_scope": (
            "observed layout fields contain the last non-zero layout seen for the "
            "semantic row; one row may aggregate multiple runtime layouts"
        ),
        "inference_boundary": (
            "rows have no event order or thread identity; allocations and rejections "
            "in one row do not prove that a later exact-layout allocation could reuse "
            "a rejected object. Depot rescue and plain-L1-miss/depot-hit counters are "
            "process-wide only, so l1_pressure_before_depot_correction includes those "
            "known L2 flows and is a pressure-ranking field rather than an unexplained "
            "bypass residual"
        ),
    }


def analyze_measurement(
    measurement: dict[str, Any],
    *,
    source_artifact: str = "<memory>",
    source_measurement_position: int | None = None,
) -> dict[str, Any]:
    """Validate and derive one ``typeiso_coverage`` event-flow record."""

    if not isinstance(measurement, dict):
        raise RetentionAnalysisError("measurement must be a JSON object")
    app = measurement.get("app")
    if not isinstance(app, str) or not app:
        raise RetentionAnalysisError("measurement app must be a non-empty string")
    variant = measurement.get("variant")
    if variant != TYPEISO_COVERAGE_VARIANT:
        raise RetentionAnalysisError(
            f"{app}: expected variant {TYPEISO_COVERAGE_VARIANT!r}, got {variant!r}"
        )
    stats = measurement.get("stats")
    if not isinstance(stats, dict):
        raise RetentionAnalysisError(f"{app}: typeiso_coverage measurement has no stats object")

    counters = {
        name: non_negative_integer(stats.get(name), f"{app}.stats.{name}")
        for name in REQUIRED_COUNTERS
    }
    optional_counters = {
        name: non_negative_integer(stats.get(name, default), f"{app}.stats.{name}")
        for name, default in OPTIONAL_COUNTER_DEFAULTS.items()
    }
    allocations = counters["typed_allocations"]
    deallocations = counters["typed_deallocations"]
    inserts = counters["cache_inserts"]
    hits = counters["cache_hits"]
    bypasses = counters["cache_bypasses"]
    plain_side_cache_entries = counters["side_cache_entries"]
    metadata_side_cache_entries = optional_counters[
        "metadata_segregation_side_cache_entries"
    ]
    retained_side_cache_entries = (
        plain_side_cache_entries + metadata_side_cache_entries
    )
    depot_current_entries = optional_counters[
        "cache_admission_depot_current_entries"
    ]
    retained_cache_entries = retained_side_cache_entries + depot_current_entries
    depot_rescued_overflow = optional_counters[
        "cache_admission_depot_rescued_overflow_events"
    ]
    depot_hits_after_recorded_l1_bypass = optional_counters[
        "cache_admission_depot_hits_after_recorded_l1_bypass_events"
    ]
    depot_attempts = optional_counters["cache_admission_depot_attempts_events"]
    depot_inserts = optional_counters["cache_admission_depot_inserts_events"]
    depot_hits = optional_counters["cache_admission_depot_hits_events"]
    depot_evictions = optional_counters["cache_admission_depot_evictions_events"]
    depot_local_eviction_attempts = optional_counters[
        "cache_admission_depot_from_local_eviction_events"
    ]
    depot_rejected_offers = (
        optional_counters["cache_admission_depot_rejected_capacity_events"]
        + optional_counters["cache_admission_depot_rejected_policy_events"]
        + optional_counters[
            "cache_admission_depot_rejected_registry_headroom_events"
        ]
    )
    depot_stats_available = "cache_admission_depot_attempts_events" in stats
    admission_reason_conservation = analyze_admission_reason_conservation(
        stats,
        app=app,
        typed_deallocations=deallocations,
        cache_inserts=inserts,
    )
    semantic_type_rows = analyze_semantic_type_rows(measurement, app=app)

    if hits > allocations:
        raise RetentionAnalysisError(
            f"{app}: cache_hits ({hits}) exceeds typed_allocations ({allocations})"
        )
    if inserts > deallocations:
        raise RetentionAnalysisError(
            f"{app}: cache_inserts ({inserts}) exceeds typed_deallocations ({deallocations})"
        )
    if hits + retained_cache_entries > inserts:
        raise RetentionAnalysisError(
            f"{app}: cache conservation fails: cache_hits + retained cache entries "
            f"({hits} + {retained_cache_entries}) exceeds cache_inserts ({inserts})"
        )

    allocation_misses = allocations - hits
    immediate_non_admissions = deallocations - inserts
    post_admission_terminal_exits = inserts - hits - retained_cache_entries
    cache_nonretained_at_report_events = deallocations - hits - retained_cache_entries
    decomposed_cache_nonretention = immediate_non_admissions + post_admission_terminal_exits
    if cache_nonretained_at_report_events != decomposed_cache_nonretention:
        raise RetentionAnalysisError(
            f"{app}: cache-nonretention conservation fails: {cache_nonretained_at_report_events} != "
            f"{immediate_non_admissions} + {post_admission_terminal_exits}"
        )

    # An L1 overflow records its local bypass before the process-wide exact-type
    # depot can rescue it.  Account for that diagnostic leg explicitly so the
    # residual continues to identify unexplained bypass events.
    expected_bypasses = (
        allocation_misses
        + immediate_non_admissions
        + depot_rescued_overflow
        + depot_hits_after_recorded_l1_bypass
    )
    bypass_residual = bypasses - expected_bypasses
    if bypass_residual < 0:
        raise RetentionAnalysisError(
            f"{app}: cache_bypasses ({bypasses}) is smaller than allocation misses plus "
            f"immediate non-admissions ({expected_bypasses})"
        )

    depot_pending_attempts = depot_attempts - depot_local_eviction_attempts
    depot_accepted_local_transfers = depot_inserts - depot_rescued_overflow
    if depot_pending_attempts < 0 or depot_accepted_local_transfers < 0:
        raise RetentionAnalysisError(
            f"{app}: depot source counters exceed their parent attempt/insert totals"
        )
    depot_rejected_pending = depot_pending_attempts - depot_rescued_overflow
    depot_rejected_local_transfers = (
        depot_local_eviction_attempts - depot_accepted_local_transfers
    )
    if depot_rejected_pending < 0 or depot_rejected_local_transfers < 0:
        raise RetentionAnalysisError(
            f"{app}: depot accepted counters exceed their pending/local attempt totals"
        )
    depot_known_post_admission_exits = (
        depot_evictions + depot_rejected_local_transfers
    )
    if depot_stats_available:
        if depot_rejected_offers != depot_attempts - depot_inserts:
            raise RetentionAnalysisError(
                f"{app}: depot rejection conservation fails: rejected offers "
                f"({depot_rejected_offers}) != attempts - inserts "
                f"({depot_attempts} - {depot_inserts})"
            )
        if depot_known_post_admission_exits > post_admission_terminal_exits:
            raise RetentionAnalysisError(
                f"{app}: known depot post-admission exits "
                f"({depot_known_post_admission_exits}) exceed inferred total "
                f"({post_admission_terminal_exits})"
            )

    corrupt_slots = stats.get("side_cache_corrupt_slots")
    if corrupt_slots is not None:
        corrupt_slots = non_negative_integer(
            corrupt_slots, f"{app}.stats.side_cache_corrupt_slots"
        )
        if corrupt_slots:
            raise RetentionAnalysisError(
                f"{app}: side_cache_corrupt_slots is {corrupt_slots}; retention inference "
                "requires an uncorrupted end snapshot"
            )

    corrupt_buckets = stats.get("metadata_segregation_side_cache_corrupt_buckets")
    if corrupt_buckets is not None:
        corrupt_buckets = non_negative_integer(
            corrupt_buckets,
            f"{app}.stats.metadata_segregation_side_cache_corrupt_buckets",
        )
        if corrupt_buckets:
            raise RetentionAnalysisError(
                f"{app}: metadata_segregation_side_cache_corrupt_buckets is "
                f"{corrupt_buckets}; retention inference requires an uncorrupted "
                "end snapshot"
            )

    counts = {
        **counters,
        **optional_counters,
        "retained_side_cache_entries": retained_side_cache_entries,
        "retained_cache_entries": retained_cache_entries,
        "allocation_misses": allocation_misses,
        "immediate_non_admissions": immediate_non_admissions,
        "l1_overflows_rescued_by_depot": depot_rescued_overflow,
        "inferred_post_admission_terminal_exits": post_admission_terminal_exits,
        "cache_nonretained_at_report_events": cache_nonretained_at_report_events,
        "bypass_residual": bypass_residual,
    }
    rates = {
        "allocation_miss_rate_of_typed_allocations": event_rate(
            allocation_misses, allocations
        ),
        "immediate_non_admission_rate_of_typed_deallocations": event_rate(
            immediate_non_admissions, deallocations
        ),
        "post_admission_terminal_exit_rate_of_typed_deallocations": event_rate(
            post_admission_terminal_exits, deallocations
        ),
        "post_admission_terminal_exit_rate_of_cache_inserts": event_rate(
            post_admission_terminal_exits, inserts
        ),
        "cache_nonretained_at_report_rate_of_typed_deallocations": event_rate(
            cache_nonretained_at_report_events, deallocations
        ),
        "retained_at_report_rate_of_typed_deallocations": event_rate(
            retained_cache_entries, deallocations
        ),
        "bypass_residual_rate_of_cache_bypasses": event_rate(
            bypass_residual, bypasses
        ),
    }
    return {
        "app": app,
        "variant": variant,
        "measurement_index": measurement.get("measurement_index"),
        "round": measurement.get("round"),
        "warmup": bool(measurement.get("warmup", False)),
        "source_artifact": source_artifact,
        "source_measurement_position": source_measurement_position,
        "counts": counts,
        "rates": rates,
        "admission_reason_conservation": admission_reason_conservation,
        "depot": {
            "available": depot_stats_available,
            "attempts": depot_attempts,
            "pending_l1_overflow_attempts": depot_pending_attempts,
            "local_eviction_transfer_attempts": depot_local_eviction_attempts,
            "inserts": depot_inserts,
            "rescued_l1_overflows": depot_rescued_overflow,
            "hits": depot_hits,
            "hits_after_recorded_l1_bypass": (
                depot_hits_after_recorded_l1_bypass
            ),
            "evictions": depot_evictions,
            "rejected_offers": depot_rejected_offers,
            "rejected_pending_l1_overflows": depot_rejected_pending,
            "accepted_local_eviction_transfers": depot_accepted_local_transfers,
            "rejected_local_eviction_transfers": depot_rejected_local_transfers,
            "known_post_admission_exits": depot_known_post_admission_exits,
            "unclassified_post_admission_exits": (
                post_admission_terminal_exits - depot_known_post_admission_exits
            ),
            "current_entries": depot_current_entries,
            "peak_entries": optional_counters[
                "cache_admission_depot_peak_entries"
            ],
            "current_rounded_bytes": optional_counters[
                "cache_admission_depot_current_rounded_bytes"
            ],
            "peak_rounded_bytes": optional_counters[
                "cache_admission_depot_peak_rounded_bytes"
            ],
            "hit_rate_per_insert": event_rate(
                depot_hits,
                depot_inserts,
            ),
            "rescue_rate_per_pending_l1_overflow_attempt": event_rate(
                depot_rescued_overflow,
                depot_pending_attempts,
            ),
        },
        "semantic_type_rows": semantic_type_rows,
    }


def load_results(path: pathlib.Path) -> tuple[dict[str, Any], dict[str, Any]]:
    try:
        payload = path.read_bytes()
        document = json.loads(payload)
    except (OSError, json.JSONDecodeError) as error:
        raise RetentionAnalysisError(f"cannot read results artifact {path}: {error}") from error
    if not isinstance(document, dict):
        raise RetentionAnalysisError(f"results artifact must contain a JSON object: {path}")
    measurements = document.get("measurements")
    if not isinstance(measurements, list):
        raise RetentionAnalysisError(f"results artifact has no measurements list: {path}")
    return document, {
        "path": str(path),
        "bytes": len(payload),
        "sha256": hashlib.sha256(payload).hexdigest(),
    }


def median_or_none(values: Iterable[int | float | None]) -> int | float | None:
    present = [value for value in values if value is not None]
    if not present:
        return None
    return statistics.median(present)


def range_or_none(values: Iterable[int | float | None]) -> list[int | float] | None:
    present = [value for value in values if value is not None]
    if not present:
        return None
    return [min(present), max(present)]


def aggregate_by_app(measurements: Sequence[dict[str, Any]]) -> list[dict[str, Any]]:
    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for measurement in measurements:
        grouped[measurement["app"]].append(measurement)

    applications = []
    for app in sorted(grouped):
        rows = grouped[app]
        fieldwise_median_counts = {
            field: median_or_none(row["counts"][field] for row in rows)
            for field in (
                *REQUIRED_COUNTERS,
                *OPTIONAL_COUNTER_DEFAULTS,
                *DERIVED_COUNT_FIELDS,
            )
        }
        count_ranges = {
            field: range_or_none(row["counts"][field] for row in rows)
            for field in (
                *REQUIRED_COUNTERS,
                *OPTIONAL_COUNTER_DEFAULTS,
                *DERIVED_COUNT_FIELDS,
            )
        }
        median_rates = {
            field: median_or_none(row["rates"][field] for row in rows)
            for field in DERIVED_RATE_FIELDS
        }
        rate_ranges = {
            field: range_or_none(row["rates"][field] for row in rows)
            for field in DERIVED_RATE_FIELDS
        }
        applications.append(
            {
                "app": app,
                "measurement_count": len(rows),
                "fieldwise_median_counts": fieldwise_median_counts,
                "count_ranges": count_ranges,
                "median_rates": median_rates,
                "rate_ranges": rate_ranges,
                "aggregation_boundary": (
                    "count medians are computed independently per field and may come from "
                    "different measurements; use one measurement row for conservation "
                    "equations and use median_rates plus rate_ranges for cross-run reporting"
                ),
            }
        )
    return applications


def analyze_result_paths(
    paths: Sequence[pathlib.Path], *, include_warmups: bool = False
) -> dict[str, Any]:
    if not paths:
        raise RetentionAnalysisError("at least one results artifact is required")

    inputs: list[dict[str, Any]] = []
    analyzed: list[dict[str, Any]] = []
    for path in paths:
        resolved = path.expanduser().resolve()
        document, artifact = load_results(resolved)
        inputs.append(artifact)
        for position, measurement in enumerate(document["measurements"]):
            if not isinstance(measurement, dict):
                raise RetentionAnalysisError(
                    f"measurement {position} in {resolved} must be a JSON object"
                )
            if measurement.get("variant") != TYPEISO_COVERAGE_VARIANT:
                continue
            if measurement.get("warmup", False) and not include_warmups:
                continue
            analyzed.append(
                analyze_measurement(
                    measurement,
                    source_artifact=str(resolved),
                    source_measurement_position=position,
                )
            )

    if not analyzed:
        raise RetentionAnalysisError(
            "no measured typeiso_coverage records were found in the input artifacts"
        )

    return {
        "schema_version": 3,
        "source": "unialloc-typeiso-cache-retention-event-flow-analysis",
        "variant": TYPEISO_COVERAGE_VARIANT,
        "evidence_scope": {
            "unit": "events",
            "event_only": True,
            "byte_weighted": False,
            "direct_per_object_lifetime_tracking": False,
            "side_cache_entries_scope": (
                "current_reporting_thread_plain_and_metadata_segregated_tls_caches"
            ),
            "side_cache_entries_limitation": (
                "plain and metadata-segregated side-cache entries are sampled by the "
                "atexit reporting thread and are not a process-wide retained-entry gauge; "
                "live caches owned by other threads are not included"
            ),
            "depot_entries_scope": (
                "process-wide exact-type depot occupancy summed shard-by-shard; the atexit "
                "reporter is quiescent, while a concurrent caller would not receive a "
                "single-instant cross-shard snapshot"
            ),
            "post_admission_exit_status": (
                "inferred from insert-hit-end-snapshot conservation; it includes terminal "
                "release causes collectively and does not attribute eviction, thread drain, "
                "or other release reasons"
            ),
            "cache_nonretention_status": (
                "D - H - S is an end-snapshot residual; it counts frees after the "
                "workload's last allocation demand and entries drained with worker TLS "
                "before the reporting-thread snapshot; direct security-escape measurement "
                "requires per-object lifetime tracing"
            ),
            "immediate_non_admission_status": (
                "derived from deallocations minus inserts; new artifacts validate this total "
                "against reason-specific event and allocator-rounded-byte counters"
            ),
            "admission_conservation_scope": (
                "strict terminal-events equality applies to the plain compiler/type-isolation "
                "policy in this runner; delayed-free, guard-page, lifetime-hugepage, and "
                "other policy combinations require lifecycle-specific accounting"
            ),
            "initial_cache_state": "assumed_empty_at_counter_interval_start",
            "initial_cache_limitation": (
                "the runner resets counters in a fresh process but does not emit a side-cache "
                "snapshot at reset time; the inference relies on fresh-process cache state"
            ),
            "claim_boundary": (
                "rates describe counter events in these executions; retained bytes, unique "
                "objects, time-at-risk, and adversarial pressure thresholds require separate "
                "measurement"
            ),
        },
        "formulas": {
            "symbols": {
                "A": "typed_allocations",
                "D": "typed_deallocations",
                "I": "cache_inserts",
                "H": "cache_hits",
                "S_plain": "side_cache_entries at report",
                "S_metadata": (
                    "metadata_segregation_side_cache_entries at report"
                ),
                "S_depot": "cache_admission_depot_current_entries at report",
                "S": "S_plain + S_metadata + S_depot",
                "B": "cache_bypasses",
                "R": "cache_admission_depot_rescued_overflow_events",
                "P": (
                    "cache_admission_depot_hits_after_recorded_l1_bypass_events"
                ),
            },
            "allocation_misses": "A - H",
            "immediate_non_admissions": "D - I",
            "inferred_post_admission_terminal_exits": "I - H - S",
            "cache_nonretained_at_report_events": "D - H - S",
            "bypass_residual": "B - (A - H) - (D - I) - R - P",
        },
        "filters": {
            "include_warmups": include_warmups,
            "variant": TYPEISO_COVERAGE_VARIANT,
        },
        "inputs": inputs,
        "measurement_count": len(analyzed),
        "measurements": analyzed,
        "applications": aggregate_by_app(analyzed),
    }


def argument_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "results",
        nargs="+",
        type=pathlib.Path,
        help="realworld_type_isolation_matrix results.json artifact(s)",
    )
    parser.add_argument(
        "--include-warmups",
        action="store_true",
        help="include typeiso_coverage warmup records in per-measurement and median output",
    )
    parser.add_argument(
        "--output",
        "-o",
        type=pathlib.Path,
        help="write JSON to this path instead of stdout",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = argument_parser().parse_args(argv)
    try:
        result = analyze_result_paths(
            args.results, include_warmups=args.include_warmups
        )
        rendered = json.dumps(result, indent=2, sort_keys=True) + "\n"
        if args.output is None:
            sys.stdout.write(rendered)
        else:
            args.output.parent.mkdir(parents=True, exist_ok=True)
            args.output.write_text(rendered, encoding="utf-8")
    except (OSError, RetentionAnalysisError) as error:
        print(f"typeiso cache retention analysis failed: {error}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
