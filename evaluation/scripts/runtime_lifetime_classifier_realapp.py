#!/usr/bin/env python3
"""Run the existing real-app matrix with marker-free lifetime learning enabled.

This wrapper deliberately leaves ``realworld_type_isolation_matrix.py`` intact.
It changes only the UniAlloc feature set and injected initializer for the two
existing Type Isolation variants, then attaches the allocator's adaptive
classifier telemetry to the source matrix result.
"""

from __future__ import annotations

import hashlib
import json
import pathlib
import sys
from typing import Any

import realworld_type_isolation_matrix as matrix


RUNTIME_STATS_PREFIX = "UNIALLOC_RUNTIME_LIFETIME_STATS="
RUNTIME_SITE_PREFIX = "UNIALLOC_RUNTIME_LIFETIME_SITE_SNAPSHOT="
RUNTIME_SITE_SOURCE = "unialloc-lifetime-adaptive-site-snapshot-v2"
RUNTIME_SITE_ABI_VERSION = 2
FORCE_TRACK_ALL_MAXIMUM_ALLOCATIONS = 1_000_000
FORCE_TRACK_ALL_MAXIMUM_REQUESTED_BYTES = 4 * 1024 * 1024 * 1024
LIVE_SURVIVAL_MINIMUM_AGE_BYTES = 8 * 1024 * 1024
RUNTIME_SITE_KEY_FIELDS = (
    "callsite",
    "type_id",
    "module_id",
    "requested_size",
    "align",
)
RUNTIME_SITE_REQUIRED_FIELDS = frozenset(
    (
        *RUNTIME_SITE_KEY_FIELDS,
        "minimum_payload_capacity",
        "maximum_payload_capacity",
        "allocation_count",
        "allocation_requested_bytes",
        "allocation_payload_bytes",
        "tracked_allocations",
        "bypassed_allocations",
        "completed_outcomes",
        "short_outcomes",
        "long_outcomes",
        "censored_outcomes",
        "short_requested_bytes",
        "long_requested_bytes",
        "censored_requested_bytes",
        "total_completed_age_bytes",
        "maximum_completed_age_bytes",
        "tracked_inflight",
        "inflight_age_lower_bound_bytes",
        "live_survival_observations",
        "live_survival_requested_bytes",
        "live_survival_payload_bytes",
        "minimum_live_survival_age_bytes",
        "maximum_live_survival_age_bytes",
        "current_live_survival_inflight",
        "current_live_survival_inflight_requested_bytes",
        "current_live_survival_inflight_payload_bytes",
        "first_allocation_pressure",
        "last_allocation_pressure",
        "predictor_key_ambiguous",
        "predictor_flags_seen",
        "predictor_placement_hint_bits_union",
        "latest_prediction",
        "latest_static_prior",
    )
)
ADAPTIVE_VARIANTS = frozenset(("typeiso_perf", "typeiso_coverage"))
_ORIGINAL_DEPENDENCY_FOR_VARIANT = matrix.dependency_for_variant
_ORIGINAL_ALLOCATOR_SOURCE = matrix.allocator_source
_ORIGINAL_IMPLEMENTATION_DIGEST = matrix.implementation_digest
_RUNTIME_MODE = "adaptive"

RUNTIME_REQUIRED_FIELDS = frozenset(
    (
        "policy",
        "backend",
        "phase_advances",
        "adaptive_pressure_bytes",
        "adaptive_pressure_epoch",
        "adaptive_epoch_advances",
        "adaptive_site_count",
        "adaptive_cold_sites",
        "adaptive_short_sites",
        "adaptive_long_sites",
        "adaptive_eligible_allocations",
        "adaptive_training_allocations",
        "adaptive_cold_bypassed_allocations",
        "adaptive_short_routed_allocations",
        "adaptive_short_bypassed_allocations",
        "adaptive_long_routed_allocations",
        "adaptive_missing_identity_bypasses",
        "adaptive_site_table_bypasses",
        "adaptive_prior_conflicts",
        "adaptive_observation_recording",
        "adaptive_observation_site_count",
        "adaptive_observation_table_bypasses",
        "adaptive_force_track_all",
        "adaptive_force_track_all_maximum_allocations",
        "adaptive_force_track_all_maximum_requested_bytes",
        "adaptive_force_track_all_admitted_allocations",
        "adaptive_force_track_all_admitted_requested_bytes",
        "adaptive_force_track_all_guard_bypasses",
        "adaptive_force_track_all_pressure_allocations",
        "adaptive_force_track_all_pressure_requested_bytes",
        "adaptive_force_track_all_raw_pressure_allocations",
        "adaptive_force_track_all_raw_pressure_requested_bytes",
        "adaptive_force_track_all_raw_reallocation_pressure_allocations",
        "adaptive_force_track_all_raw_reallocation_pressure_requested_bytes",
        "adaptive_live_trailers",
        "adaptive_trailer_corruptions",
        "adaptive_live_survival_registrations",
        "adaptive_live_survival_registration_bypasses",
        "adaptive_live_survival_scans",
        "adaptive_live_survival_slots_examined",
        "adaptive_live_survival_observations",
        "adaptive_live_survival_promotions",
        "adaptive_live_survival_stale_abstentions",
        "adaptive_short_observations",
        "adaptive_long_observations",
        "adaptive_censored_observations",
        "adaptive_decisive_observation_bytes",
        "adaptive_promotions",
        "adaptive_demotions",
        "adaptive_prior_corrections",
        "predictor_tp",
        "predictor_tp_bytes",
        "predictor_tn",
        "predictor_tn_bytes",
        "predictor_fp",
        "predictor_fp_bytes",
        "predictor_fn",
        "predictor_fn_bytes",
        "static_hint_tp",
        "static_hint_tp_bytes",
        "static_hint_tn",
        "static_hint_tn_bytes",
        "static_hint_fp",
        "static_hint_fp_bytes",
        "static_hint_fn",
        "static_hint_fn_bytes",
        "static_hint_abstained",
        "static_hint_abstained_bytes",
        "ordinary_extent_mappings",
        "thp_extent_mappings",
        "thp_advice_attempts",
        "thp_advice_successes",
        "thp_advice_errors",
        "nohugepage_advice_failures",
        "mapping_failures",
        "unsupported_layout_bypasses",
        "live_objects",
        "all_mappings_released",
        "routed_allocations",
        "routed_deallocations",
    )
)


RUNTIME_SITE_BUFFER_RUST = '''
    // Fixed storage keeps observation capture allocation-free at process exit.
    static RUNTIME_LIFETIME_SITE_ROWS: std::sync::Mutex<
        [unialloc::LifetimeAdaptiveSiteSnapshot;
            unialloc::LIFETIME_ADAPTIVE_SITE_SNAPSHOT_CAPACITY]
    > = std::sync::Mutex::new(
        [unialloc::LifetimeAdaptiveSiteSnapshot::empty();
            unialloc::LIFETIME_ADAPTIVE_SITE_SNAPSHOT_CAPACITY]
    );
'''


RUNTIME_CAPTURE_RUST = '''
        let mut runtime_lifetime_site_rows = RUNTIME_LIFETIME_SITE_ROWS
            .lock()
            .unwrap_or_else(|poisoned| poisoned.into_inner());
        let runtime_lifetime_site_count =
            unialloc::lifetime_hugepage_adaptive_site_snapshot(
                &mut runtime_lifetime_site_rows[..],
            );
        assert!(runtime_lifetime_site_count <= runtime_lifetime_site_rows.len());
        let lifetime = unialloc::lifetime_hugepage_stats_snapshot();
'''


RUNTIME_REPORT_RUST = f'''
        eprintln!(
            r#"{RUNTIME_STATS_PREFIX}{{{{\"policy\":{{}},\"backend\":{{}},\"phase_advances\":{{}},\"adaptive_pressure_bytes\":{{}},\"adaptive_pressure_epoch\":{{}},\"adaptive_epoch_advances\":{{}},\"adaptive_site_count\":{{}},\"adaptive_cold_sites\":{{}},\"adaptive_short_sites\":{{}},\"adaptive_long_sites\":{{}},\"adaptive_eligible_allocations\":{{}},\"adaptive_training_allocations\":{{}},\"adaptive_cold_bypassed_allocations\":{{}},\"adaptive_short_routed_allocations\":{{}},\"adaptive_short_bypassed_allocations\":{{}},\"adaptive_long_routed_allocations\":{{}},\"adaptive_missing_identity_bypasses\":{{}},\"adaptive_site_table_bypasses\":{{}},\"adaptive_prior_conflicts\":{{}},\"adaptive_live_trailers\":{{}},\"adaptive_trailer_corruptions\":{{}},\"adaptive_short_observations\":{{}},\"adaptive_long_observations\":{{}},\"adaptive_censored_observations\":{{}},\"adaptive_decisive_observation_bytes\":{{}},\"adaptive_promotions\":{{}},\"adaptive_demotions\":{{}},\"adaptive_prior_corrections\":{{}},\"predictor_tp\":{{}},\"predictor_tp_bytes\":{{}},\"predictor_tn\":{{}},\"predictor_tn_bytes\":{{}},\"predictor_fp\":{{}},\"predictor_fp_bytes\":{{}},\"predictor_fn\":{{}},\"predictor_fn_bytes\":{{}},\"static_hint_tp\":{{}},\"static_hint_tp_bytes\":{{}},\"static_hint_tn\":{{}},\"static_hint_tn_bytes\":{{}},\"static_hint_fp\":{{}},\"static_hint_fp_bytes\":{{}},\"static_hint_fn\":{{}},\"static_hint_fn_bytes\":{{}},\"static_hint_abstained\":{{}},\"static_hint_abstained_bytes\":{{}},\"ordinary_extent_mappings\":{{}},\"thp_extent_mappings\":{{}},\"thp_advice_attempts\":{{}},\"thp_advice_successes\":{{}},\"thp_advice_errors\":{{}},\"nohugepage_advice_failures\":{{}},\"mapping_failures\":{{}},\"unsupported_layout_bypasses\":{{}},\"live_objects\":{{}},\"all_mappings_released\":{{}},\"routed_allocations\":{{}},\"routed_deallocations\":{{}},\"adaptive_observation_recording\":{{}},\"adaptive_observation_site_count\":{{}},\"adaptive_observation_table_bypasses\":{{}},\"adaptive_force_track_all\":{{}},\"adaptive_force_track_all_maximum_allocations\":{{}},\"adaptive_force_track_all_maximum_requested_bytes\":{{}},\"adaptive_force_track_all_admitted_allocations\":{{}},\"adaptive_force_track_all_admitted_requested_bytes\":{{}},\"adaptive_force_track_all_guard_bypasses\":{{}},\"adaptive_force_track_all_pressure_allocations\":{{}},\"adaptive_force_track_all_pressure_requested_bytes\":{{}},\"adaptive_force_track_all_raw_pressure_allocations\":{{}},\"adaptive_force_track_all_raw_pressure_requested_bytes\":{{}},\"adaptive_force_track_all_raw_reallocation_pressure_allocations\":{{}},\"adaptive_force_track_all_raw_reallocation_pressure_requested_bytes\":{{}},\"adaptive_live_survival_registrations\":{{}},\"adaptive_live_survival_registration_bypasses\":{{}},\"adaptive_live_survival_scans\":{{}},\"adaptive_live_survival_slots_examined\":{{}},\"adaptive_live_survival_observations\":{{}},\"adaptive_live_survival_promotions\":{{}},\"adaptive_live_survival_stale_abstentions\":{{}}}}}}"#,
            lifetime.policy as u8,
            lifetime.backend as u8,
            lifetime.phase_advances,
            lifetime.adaptive_pressure_bytes,
            lifetime.adaptive_pressure_epoch,
            lifetime.adaptive_epoch_advances,
            lifetime.adaptive_site_count,
            lifetime.adaptive_cold_sites,
            lifetime.adaptive_short_sites,
            lifetime.adaptive_long_sites,
            lifetime.adaptive_eligible_allocations,
            lifetime.adaptive_training_allocations,
            lifetime.adaptive_cold_bypassed_allocations,
            lifetime.adaptive_short_routed_allocations,
            lifetime.adaptive_short_bypassed_allocations,
            lifetime.adaptive_long_routed_allocations,
            lifetime.adaptive_missing_identity_bypasses,
            lifetime.adaptive_site_table_bypasses,
            lifetime.adaptive_prior_conflicts,
            lifetime.adaptive_live_trailers,
            lifetime.adaptive_trailer_corruptions,
            lifetime.adaptive_short_observations,
            lifetime.adaptive_long_observations,
            lifetime.adaptive_censored_observations,
            lifetime.adaptive_decisive_observation_bytes,
            lifetime.adaptive_promotions,
            lifetime.adaptive_demotions,
            lifetime.adaptive_prior_corrections,
            lifetime.adaptive_predictor_true_positive_objects,
            lifetime.adaptive_predictor_true_positive_bytes,
            lifetime.adaptive_predictor_true_negative_objects,
            lifetime.adaptive_predictor_true_negative_bytes,
            lifetime.adaptive_predictor_false_positive_objects,
            lifetime.adaptive_predictor_false_positive_bytes,
            lifetime.adaptive_predictor_false_negative_objects,
            lifetime.adaptive_predictor_false_negative_bytes,
            lifetime.adaptive_static_hint_true_positive_objects,
            lifetime.adaptive_static_hint_true_positive_bytes,
            lifetime.adaptive_static_hint_true_negative_objects,
            lifetime.adaptive_static_hint_true_negative_bytes,
            lifetime.adaptive_static_hint_false_positive_objects,
            lifetime.adaptive_static_hint_false_positive_bytes,
            lifetime.adaptive_static_hint_false_negative_objects,
            lifetime.adaptive_static_hint_false_negative_bytes,
            lifetime.adaptive_static_hint_abstained_objects,
            lifetime.adaptive_static_hint_abstained_bytes,
            lifetime.ordinary_extent_mappings,
            lifetime.thp_extent_mappings,
            lifetime.thp_advice_attempts,
            lifetime.thp_advice_successes,
            lifetime.thp_advice_errors,
            lifetime.nohugepage_advice_failures,
            lifetime.mapping_failures,
            lifetime.unsupported_layout_bypasses,
            lifetime.live_objects,
            lifetime.all_mappings_released,
            lifetime.routed_allocations,
            lifetime.routed_deallocations,
            lifetime.adaptive_observation_recording,
            lifetime.adaptive_observation_site_count,
            lifetime.adaptive_observation_table_bypasses,
            lifetime.adaptive_force_track_all,
            lifetime.adaptive_force_track_all_maximum_allocations,
            lifetime.adaptive_force_track_all_maximum_requested_bytes,
            lifetime.adaptive_force_track_all_admitted_allocations,
            lifetime.adaptive_force_track_all_admitted_requested_bytes,
            lifetime.adaptive_force_track_all_guard_bypasses,
            lifetime.adaptive_force_track_all_pressure_allocations,
            lifetime.adaptive_force_track_all_pressure_requested_bytes,
            lifetime.adaptive_force_track_all_raw_pressure_allocations,
            lifetime.adaptive_force_track_all_raw_pressure_requested_bytes,
            lifetime.adaptive_force_track_all_raw_reallocation_pressure_allocations,
            lifetime.adaptive_force_track_all_raw_reallocation_pressure_requested_bytes,
            lifetime.adaptive_live_survival_registrations,
            lifetime.adaptive_live_survival_registration_bypasses,
            lifetime.adaptive_live_survival_scans,
            lifetime.adaptive_live_survival_slots_examined,
            lifetime.adaptive_live_survival_observations,
            lifetime.adaptive_live_survival_promotions,
            lifetime.adaptive_live_survival_stale_abstentions,
        );
        {{
            use std::io::Write as _;
            let runtime_lifetime_stderr = std::io::stderr();
            let mut runtime_lifetime_stderr = runtime_lifetime_stderr.lock();
            write!(
                &mut runtime_lifetime_stderr,
                r#"{RUNTIME_SITE_PREFIX}{{{{\"source\":\"{RUNTIME_SITE_SOURCE}\",\"abi_version\":{{}},\"required_rows\":{{}},\"captured_rows\":{{}},\"truncated\":false,\"rows\":["#,
                unialloc::LIFETIME_ADAPTIVE_SITE_SNAPSHOT_ABI_VERSION,
                runtime_lifetime_site_count,
                runtime_lifetime_site_count,
            ).expect("write runtime lifetime site envelope");
            for (index, row) in runtime_lifetime_site_rows
                .iter()
                .take(runtime_lifetime_site_count)
                .enumerate()
            {{
                if index != 0 {{
                    write!(&mut runtime_lifetime_stderr, ",")
                        .expect("write runtime lifetime site separator");
                }}
                write!(
                    &mut runtime_lifetime_stderr,
                    r#"{{{{\"callsite\":{{}},\"type_id\":{{}},\"module_id\":{{}},\"requested_size\":{{}},\"align\":{{}},\"minimum_payload_capacity\":{{}},\"maximum_payload_capacity\":{{}},\"allocation_count\":{{}},\"allocation_requested_bytes\":{{}},\"allocation_payload_bytes\":{{}},\"tracked_allocations\":{{}},\"bypassed_allocations\":{{}},\"completed_outcomes\":{{}},\"short_outcomes\":{{}},\"long_outcomes\":{{}},\"censored_outcomes\":{{}},\"short_requested_bytes\":{{}},\"long_requested_bytes\":{{}},\"censored_requested_bytes\":{{}},\"total_completed_age_bytes\":{{}},\"maximum_completed_age_bytes\":{{}},\"tracked_inflight\":{{}},\"inflight_age_lower_bound_bytes\":{{}},\"live_survival_observations\":{{}},\"live_survival_requested_bytes\":{{}},\"live_survival_payload_bytes\":{{}},\"minimum_live_survival_age_bytes\":{{}},\"maximum_live_survival_age_bytes\":{{}},\"current_live_survival_inflight\":{{}},\"current_live_survival_inflight_requested_bytes\":{{}},\"current_live_survival_inflight_payload_bytes\":{{}},\"first_allocation_pressure\":{{}},\"last_allocation_pressure\":{{}},\"predictor_key_ambiguous\":{{}},\"predictor_flags_seen\":{{}},\"predictor_placement_hint_bits_union\":{{}},\"latest_prediction\":{{}},\"latest_static_prior\":{{}}}}}}"#,
                row.callsite,
                row.type_id,
                row.module_id,
                row.requested_size,
                row.align,
                row.minimum_payload_capacity,
                row.maximum_payload_capacity,
                row.allocation_count,
                row.allocation_requested_bytes,
                row.allocation_payload_bytes,
                row.tracked_allocations,
                row.bypassed_allocations,
                row.completed_outcomes,
                row.short_outcomes,
                row.long_outcomes,
                row.censored_outcomes,
                row.short_requested_bytes,
                row.long_requested_bytes,
                row.censored_requested_bytes,
                row.total_completed_age_bytes,
                row.maximum_completed_age_bytes,
                row.tracked_inflight,
                row.inflight_age_lower_bound_bytes,
                row.live_survival_observations,
                row.live_survival_requested_bytes,
                row.live_survival_payload_bytes,
                row.minimum_live_survival_age_bytes,
                row.maximum_live_survival_age_bytes,
                row.current_live_survival_inflight,
                row.current_live_survival_inflight_requested_bytes,
                row.current_live_survival_inflight_payload_bytes,
                row.first_allocation_pressure,
                row.last_allocation_pressure,
                row.predictor_key_ambiguous,
                row.predictor_flags_seen,
                row.predictor_placement_hint_bits_union,
                row.latest_prediction,
                row.latest_static_prior,
                ).expect("write runtime lifetime site row");
            }}
            writeln!(&mut runtime_lifetime_stderr, "]}}}}")
                .expect("finish runtime lifetime site envelope");
        }}
'''


RUNTIME_INITIALIZE_ADAPTIVE_RUST = '''
        assert!(unialloc::lifetime_hugepage_stats_reset());
        assert!(unialloc::lifetime_hugepage_configure_with_backend(
            unialloc::LifetimeHugepagePolicy::AdaptiveRuntimeHugepage,
            unialloc::LifetimePageBackend::TransparentHugepage,
        ));
'''


RUNTIME_INITIALIZE_ADAPTIVE_OBSERVATIONS_RUST = f'''
        assert!(unialloc::lifetime_hugepage_stats_reset());
        assert!(unialloc::lifetime_hugepage_adaptive_site_recording_enable());
        assert!(unialloc::lifetime_hugepage_adaptive_site_force_track_all_enable(
            {FORCE_TRACK_ALL_MAXIMUM_ALLOCATIONS},
            {FORCE_TRACK_ALL_MAXIMUM_REQUESTED_BYTES},
        ));
        assert!(unialloc::lifetime_hugepage_configure_with_backend(
            unialloc::LifetimeHugepagePolicy::AdaptiveRuntimeHugepage,
            unialloc::LifetimePageBackend::TransparentHugepage,
        ));
'''


RUNTIME_INITIALIZE_OFF_RUST = '''
        assert!(unialloc::lifetime_hugepage_stats_reset());
        assert!(unialloc::lifetime_hugepage_configure(
            unialloc::LifetimeHugepagePolicy::Disabled,
        ));
'''


def runtime_initializer_rust(*, record_sites: bool = False) -> str:
    if _RUNTIME_MODE == "adaptive":
        if record_sites:
            return RUNTIME_INITIALIZE_ADAPTIVE_OBSERVATIONS_RUST
        return RUNTIME_INITIALIZE_ADAPTIVE_RUST
    return RUNTIME_INITIALIZE_OFF_RUST


def adaptive_dependency_for_variant(variant: str) -> tuple[str, tuple[str, ...]]:
    if variant not in ADAPTIVE_VARIANTS:
        return _ORIGINAL_DEPENDENCY_FOR_VARIANT(variant)
    original_dependency, original_features = _ORIGINAL_DEPENDENCY_FOR_VARIANT(variant)
    del original_dependency
    features = tuple(dict.fromkeys((*original_features, "lifetime_hugepage")))
    return matrix.cargo_path_dependency(matrix.ROOT / "unialloc", features), features


def adaptive_allocator_source(variant: str) -> str:
    source = _ORIGINAL_ALLOCATOR_SOURCE(variant)
    if variant not in ADAPTIVE_VARIANTS:
        return source
    if variant == "typeiso_coverage":
        allocator_marker = (
            "    static ALLOCATOR: unialloc::UniAlloc = unialloc::UniAlloc;\n"
        )
        if allocator_marker not in source:
            raise matrix.MatrixError("coverage allocator injection marker changed")
        source = source.replace(
            allocator_marker,
            allocator_marker + RUNTIME_SITE_BUFFER_RUST,
            1,
        )
        capture_marker = (
            '    extern "C" fn report() {\n'
            "        let stats = unialloc::semantic_stats_snapshot();"
        )
        if capture_marker not in source:
            raise matrix.MatrixError("coverage capture injection marker changed")
        source = source.replace(
            capture_marker,
            '    extern "C" fn report() {\n'
            f"{RUNTIME_CAPTURE_RUST}"
            "        let stats = unialloc::semantic_stats_snapshot();",
            1,
        )
        # Anchor on the report/initializer boundary rather than the final field
        # of the report.  The coverage report grows as allocator counters are
        # added, while this function boundary remains the insertion contract.
        report_marker = '\n    }\n\n    extern "C" fn initialize() {'
        if report_marker not in source:
            raise matrix.MatrixError("coverage report injection marker changed")
        source = source.replace(
            report_marker,
            f"{RUNTIME_REPORT_RUST}"
            "    }\n\n"
            '    extern "C" fn initialize() {',
            1,
        )
        initialize_marker = (
            '    extern "C" fn initialize() {\n'
            "        unialloc::semantic_stats_reset();"
        )
        if initialize_marker not in source:
            raise matrix.MatrixError("coverage initializer injection marker changed")
        return source.replace(
            initialize_marker,
            '    extern "C" fn initialize() {\n'
            f"{runtime_initializer_rust(record_sites=True)}"
            "        unialloc::semantic_stats_reset();",
            1,
        )

    close = source.rfind("\n}")
    if close < 0:
        raise matrix.MatrixError("performance allocator module has no closing brace")
    initializer = f'''

    extern "C" fn initialize() {{{runtime_initializer_rust()}    }}

    #[used]
    #[cfg_attr(target_family = "unix", unsafe(link_section = ".init_array"))]
    static INITIALIZER: extern "C" fn() = initialize;
'''
    return source[:close] + initializer + source[close:]


def parse_runtime_stats(stderr: str) -> dict[str, Any] | None:
    parsed: dict[str, Any] | None = None
    for line in stderr.splitlines():
        if not line.startswith(RUNTIME_STATS_PREFIX):
            continue
        try:
            candidate = json.loads(line[len(RUNTIME_STATS_PREFIX) :])
        except json.JSONDecodeError:
            continue
        if isinstance(candidate, dict) and RUNTIME_REQUIRED_FIELDS.issubset(candidate):
            parsed = candidate
    return parsed


def parse_runtime_site_rows(stderr: str) -> list[dict[str, Any]]:
    """Parse exact-layout ABI v2 rows without relabeling right-censored objects."""
    envelopes: list[dict[str, Any]] = []
    for line in stderr.splitlines():
        if not line.startswith(RUNTIME_SITE_PREFIX):
            continue
        try:
            candidate = json.loads(line[len(RUNTIME_SITE_PREFIX) :])
        except json.JSONDecodeError as error:
            raise matrix.MatrixError("malformed runtime lifetime site JSON") from error
        if not isinstance(candidate, dict):
            raise matrix.MatrixError("runtime lifetime site snapshot is not an object")
        envelopes.append(candidate)
    if not envelopes:
        return []
    if len(envelopes) != 1:
        raise matrix.MatrixError("multiple runtime lifetime site snapshots were emitted")
    envelope = envelopes[0]
    expected_envelope_fields = {
        "source",
        "abi_version",
        "required_rows",
        "captured_rows",
        "truncated",
        "rows",
    }
    if set(envelope) != expected_envelope_fields:
        raise matrix.MatrixError("runtime lifetime site snapshot envelope is invalid")
    if envelope["source"] != RUNTIME_SITE_SOURCE:
        raise matrix.MatrixError("runtime lifetime site snapshot source is invalid")
    if envelope["abi_version"] != RUNTIME_SITE_ABI_VERSION:
        raise matrix.MatrixError("runtime lifetime site ABI version mismatch")
    for field in ("required_rows", "captured_rows"):
        value = envelope[field]
        if not isinstance(value, int) or isinstance(value, bool) or value < 0:
            raise matrix.MatrixError(f"runtime lifetime site snapshot {field} is invalid")
    if not isinstance(envelope["truncated"], bool):
        raise matrix.MatrixError("runtime lifetime site snapshot truncated flag is invalid")
    rows = envelope["rows"]
    if not isinstance(rows, list) or not all(isinstance(row, dict) for row in rows):
        raise matrix.MatrixError("runtime lifetime site snapshot rows are invalid")
    if (
        envelope["truncated"]
        or envelope["required_rows"] != envelope["captured_rows"]
        or envelope["captured_rows"] != len(rows)
    ):
        raise matrix.MatrixError("runtime lifetime site snapshot is incomplete")
    validate_runtime_site_rows(rows)
    return rows


def validate_runtime_site_rows(
    rows: list[dict[str, Any]], stats: dict[str, Any] | None = None
) -> None:
    bool_fields = {"predictor_key_ambiguous"}
    integer_fields = RUNTIME_SITE_REQUIRED_FIELDS - bool_fields
    identities: set[tuple[int, int, int, int, int]] = set()
    for row in rows:
        if set(row) != RUNTIME_SITE_REQUIRED_FIELDS:
            raise matrix.MatrixError("runtime lifetime site record does not match ABI v2")
        for field in integer_fields:
            value = row[field]
            if not isinstance(value, int) or isinstance(value, bool) or value < 0:
                raise matrix.MatrixError(
                    f"runtime lifetime site field {field} is not a nonnegative integer"
                )
        if not isinstance(row["predictor_key_ambiguous"], bool):
            raise matrix.MatrixError(
                "runtime lifetime site field predictor_key_ambiguous is not boolean"
            )
        identity = tuple(int(row[field]) for field in RUNTIME_SITE_KEY_FIELDS)
        if identity in identities:
            raise matrix.MatrixError("duplicate exact runtime lifetime site identity")
        identities.add(identity)
        if any(row[field] == 0 for field in ("callsite", "type_id")):
            raise matrix.MatrixError("runtime lifetime site identity contains zero")
        if row["requested_size"] == 0:
            raise matrix.MatrixError("runtime lifetime site requested size is zero")
        align = row["align"]
        if align == 0 or align & (align - 1):
            raise matrix.MatrixError("runtime lifetime site alignment is not a power of two")
        if row["latest_prediction"] not in {0, 1, 2}:
            raise matrix.MatrixError("runtime lifetime site prediction is invalid")
        if row["latest_static_prior"] not in {0, 1, 2}:
            raise matrix.MatrixError("runtime lifetime site static prior is invalid")
        minimum_payload = row["minimum_payload_capacity"]
        maximum_payload = row["maximum_payload_capacity"]
        if minimum_payload == 0 or minimum_payload > maximum_payload:
            raise matrix.MatrixError("runtime lifetime site payload bounds are invalid")
        if row["allocation_count"] != (
            row["tracked_allocations"] + row["bypassed_allocations"]
        ):
            raise matrix.MatrixError("runtime lifetime site allocation accounting does not close")
        if row["completed_outcomes"] != (
            row["short_outcomes"]
            + row["long_outcomes"]
            + row["censored_outcomes"]
        ):
            raise matrix.MatrixError("runtime lifetime site outcome accounting does not close")
        if row["tracked_allocations"] != (
            row["completed_outcomes"] + row["tracked_inflight"]
        ):
            raise matrix.MatrixError("runtime lifetime site tracked-flow accounting does not close")
        requested_size = row["requested_size"]
        if row["allocation_requested_bytes"] != row["allocation_count"] * requested_size:
            raise matrix.MatrixError("runtime lifetime site requested-byte accounting does not close")
        for outcome in ("short", "long", "censored"):
            if row[f"{outcome}_requested_bytes"] != row[f"{outcome}_outcomes"] * requested_size:
                raise matrix.MatrixError(
                    f"runtime lifetime site {outcome}-byte accounting does not close"
                )
        survival_count = row["live_survival_observations"]
        survival_current = row["current_live_survival_inflight"]
        if survival_count > row["tracked_allocations"] or survival_current > survival_count:
            raise matrix.MatrixError("runtime lifetime live-survival object accounting is invalid")
        if survival_current > row["tracked_inflight"]:
            raise matrix.MatrixError("runtime lifetime live-survival inflight exceeds live trailers")
        if row["live_survival_requested_bytes"] != survival_count * requested_size:
            raise matrix.MatrixError("runtime lifetime live-survival requested bytes do not close")
        if (
            row["current_live_survival_inflight_requested_bytes"]
            != survival_current * requested_size
        ):
            raise matrix.MatrixError(
                "runtime lifetime current live-survival requested bytes do not close"
            )
        survival_payload = row["live_survival_payload_bytes"]
        if not (
            survival_count * minimum_payload
            <= survival_payload
            <= survival_count * maximum_payload
        ):
            raise matrix.MatrixError("runtime lifetime live-survival payload bytes are invalid")
        current_survival_payload = row["current_live_survival_inflight_payload_bytes"]
        if not (
            survival_current * minimum_payload
            <= current_survival_payload
            <= survival_current * maximum_payload
        ):
            raise matrix.MatrixError(
                "runtime lifetime current live-survival payload bytes are invalid"
            )
        if current_survival_payload > survival_payload:
            raise matrix.MatrixError(
                "runtime lifetime current live-survival payload exceeds cumulative provenance"
            )
        minimum_survival_age = row["minimum_live_survival_age_bytes"]
        maximum_survival_age = row["maximum_live_survival_age_bytes"]
        if survival_count == 0:
            if minimum_survival_age != 0 or maximum_survival_age != 0:
                raise matrix.MatrixError("runtime lifetime empty live-survival age is invalid")
        elif not (
            LIVE_SURVIVAL_MINIMUM_AGE_BYTES
            <= minimum_survival_age
            <= maximum_survival_age
        ):
            raise matrix.MatrixError("runtime lifetime live-survival age is below the contract")
        if (
            row["inflight_age_lower_bound_bytes"]
            < survival_current * LIVE_SURVIVAL_MINIMUM_AGE_BYTES
        ):
            raise matrix.MatrixError(
                "runtime lifetime inflight age omits a confirmed live survivor"
            )
        payload_bytes = row["allocation_payload_bytes"]
        if not (
            row["allocation_count"] * minimum_payload
            <= payload_bytes
            <= row["allocation_count"] * maximum_payload
        ):
            raise matrix.MatrixError("runtime lifetime site payload-byte accounting is invalid")
        if row["maximum_completed_age_bytes"] > row["total_completed_age_bytes"]:
            raise matrix.MatrixError("runtime lifetime site completed-age accounting is invalid")
        if row["tracked_inflight"] == 0 and row["inflight_age_lower_bound_bytes"] != 0:
            raise matrix.MatrixError("runtime lifetime live-age accounting is invalid")
        if row["first_allocation_pressure"] > row["last_allocation_pressure"]:
            raise matrix.MatrixError("runtime lifetime site pressure range is invalid")

    if stats is None:
        return
    if stats["adaptive_observation_table_bypasses"] != 0:
        raise matrix.MatrixError("runtime lifetime observation table lost exact-site evidence")
    if len(rows) != stats["adaptive_observation_site_count"]:
        raise matrix.MatrixError("runtime lifetime observation row count does not close")
    expected_totals = {
        "allocation_count": stats["adaptive_eligible_allocations"],
        "tracked_allocations": (
            stats["adaptive_training_allocations"]
            + stats["adaptive_short_routed_allocations"]
            + stats["adaptive_long_routed_allocations"]
        ),
        "bypassed_allocations": (
            stats["adaptive_cold_bypassed_allocations"]
            + stats["adaptive_short_bypassed_allocations"]
        ),
        "short_outcomes": stats["adaptive_short_observations"],
        "censored_outcomes": stats["adaptive_censored_observations"],
        "tracked_inflight": stats["adaptive_live_trailers"],
    }
    for field, expected in expected_totals.items():
        actual = sum(int(row[field]) for row in rows)
        if actual != expected:
            raise matrix.MatrixError(
                f"runtime lifetime observation {field} total does not close"
            )
    unique_long_observations = sum(
        int(row["long_outcomes"]) + int(row["current_live_survival_inflight"])
        for row in rows
    )
    if unique_long_observations != stats["adaptive_long_observations"]:
        raise matrix.MatrixError(
            "runtime lifetime unique Long observation total does not close"
        )
    if sum(int(row["live_survival_observations"]) for row in rows) != stats[
        "adaptive_live_survival_observations"
    ]:
        raise matrix.MatrixError(
            "runtime lifetime live-survival provenance total does not close"
        )
    if stats["adaptive_force_track_all"]:
        tracked_requested_bytes = sum(
            int(row["allocation_requested_bytes"])
            - int(row["bypassed_allocations"]) * int(row["requested_size"])
            for row in rows
        )
        if (
            stats["adaptive_force_track_all_admitted_requested_bytes"]
            != tracked_requested_bytes
        ):
            raise matrix.MatrixError(
                "runtime force-track requested-byte accounting does not close"
            )


def validate_runtime_stats(stats: dict[str, Any], mode: str) -> None:
    bool_fields = {
        "all_mappings_released",
        "adaptive_observation_recording",
        "adaptive_force_track_all",
    }
    for field in RUNTIME_REQUIRED_FIELDS - bool_fields:
        value = stats[field]
        if not isinstance(value, int) or isinstance(value, bool) or value < 0:
            raise matrix.MatrixError(f"runtime lifetime field {field} is not a nonnegative integer")
    for field in bool_fields:
        if not isinstance(stats[field], bool):
            raise matrix.MatrixError(f"runtime lifetime field {field} is not boolean")
    if stats["adaptive_site_count"] != (
        stats["adaptive_cold_sites"]
        + stats["adaptive_short_sites"]
        + stats["adaptive_long_sites"]
    ):
        raise matrix.MatrixError("runtime lifetime site accounting does not close")
    if stats["adaptive_eligible_allocations"] != (
        stats["adaptive_training_allocations"]
        + stats["adaptive_cold_bypassed_allocations"]
        + stats["adaptive_short_routed_allocations"]
        + stats["adaptive_short_bypassed_allocations"]
        + stats["adaptive_long_routed_allocations"]
        + stats["adaptive_force_track_all_guard_bypasses"]
    ):
        raise matrix.MatrixError("runtime lifetime allocation accounting does not close")
    if stats["adaptive_trailer_corruptions"] != 0:
        raise matrix.MatrixError("runtime lifetime trailer corruption was observed")
    if stats["adaptive_live_survival_stale_abstentions"] != 0:
        raise matrix.MatrixError("runtime lifetime live-survivor sampling lost provenance")
    if not (
        stats["adaptive_live_survival_promotions"]
        <= stats["adaptive_live_survival_observations"]
        <= stats["adaptive_live_survival_registrations"]
    ):
        raise matrix.MatrixError("runtime lifetime live-survivor accounting is invalid")
    if (
        stats["adaptive_live_survival_observations"] > 0
        and stats["adaptive_live_survival_scans"] == 0
    ):
        raise matrix.MatrixError("runtime lifetime live-survivor observations lack a scan")
    if stats["adaptive_live_survival_slots_examined"] < stats[
        "adaptive_live_survival_observations"
    ]:
        raise matrix.MatrixError("runtime lifetime live-survivor scan accounting is invalid")
    if stats["mapping_failures"] != 0:
        raise matrix.MatrixError("runtime lifetime arena mapping failure was observed")
    tracked_allocations = (
        stats["adaptive_training_allocations"]
        + stats["adaptive_short_routed_allocations"]
        + stats["adaptive_long_routed_allocations"]
    )
    if stats["adaptive_observation_recording"]:
        # A survivor is counted as a Long observation while its trailer remains
        # live, so stats alone cannot distinguish it from completed Long truth.
        # The exact-site ABI carries the non-overlapping current-survivor count
        # and closes this identity in validate_runtime_site_rows().
        if (
            stats["adaptive_short_observations"]
            + stats["adaptive_censored_observations"]
            > tracked_allocations
            or stats["adaptive_long_observations"] > tracked_allocations
        ):
            raise matrix.MatrixError("runtime lifetime outcome totals exceed tracked flow")
    else:
        tracked_outcomes = (
            stats["adaptive_short_observations"]
            + stats["adaptive_long_observations"]
            + stats["adaptive_censored_observations"]
            + stats["adaptive_trailer_corruptions"]
            + stats["adaptive_live_trailers"]
        )
        if tracked_allocations != tracked_outcomes:
            raise matrix.MatrixError("runtime lifetime tracked-flow accounting does not close")
    decisive_objects = (
        stats["adaptive_short_observations"] + stats["adaptive_long_observations"]
    )
    static_classified_objects = (
        stats["static_hint_tp"]
        + stats["static_hint_tn"]
        + stats["static_hint_fp"]
        + stats["static_hint_fn"]
    )
    if decisive_objects != static_classified_objects + stats["static_hint_abstained"]:
        raise matrix.MatrixError("runtime static-hint object accounting does not close")
    static_classified_bytes = (
        stats["static_hint_tp_bytes"]
        + stats["static_hint_tn_bytes"]
        + stats["static_hint_fp_bytes"]
        + stats["static_hint_fn_bytes"]
    )
    if stats["adaptive_decisive_observation_bytes"] != (
        static_classified_bytes + stats["static_hint_abstained_bytes"]
    ):
        raise matrix.MatrixError("runtime static-hint byte accounting does not close")
    expected_policy = 5 if mode == "adaptive" else 0
    if stats["policy"] != expected_policy:
        raise matrix.MatrixError(
            f"runtime lifetime policy mismatch: expected {expected_policy}, got {stats['policy']}"
        )
    expected_observation_recording = mode == "adaptive"
    if stats["adaptive_observation_recording"] != expected_observation_recording:
        raise matrix.MatrixError(
            "runtime lifetime exact-site observation recording state mismatch"
        )
    expected_force_track_all = mode == "adaptive"
    if stats["adaptive_force_track_all"] != expected_force_track_all:
        raise matrix.MatrixError("runtime lifetime force-track-all state mismatch")
    if expected_force_track_all:
        if (
            stats["adaptive_force_track_all_maximum_allocations"]
            != FORCE_TRACK_ALL_MAXIMUM_ALLOCATIONS
            or stats["adaptive_force_track_all_maximum_requested_bytes"]
            != FORCE_TRACK_ALL_MAXIMUM_REQUESTED_BYTES
        ):
            raise matrix.MatrixError("runtime lifetime force-track-all guards changed")
        tracked_allocations = (
            stats["adaptive_training_allocations"]
            + stats["adaptive_short_routed_allocations"]
            + stats["adaptive_long_routed_allocations"]
        )
        if stats["adaptive_force_track_all_admitted_allocations"] != tracked_allocations:
            raise matrix.MatrixError(
                "runtime lifetime force-track admitted allocation accounting does not close"
            )
        if (
            stats["adaptive_pressure_bytes"]
            != stats["adaptive_force_track_all_pressure_requested_bytes"]
        ):
            raise matrix.MatrixError(
                "runtime lifetime global requested-byte pressure clock does not close"
            )
        if (
            stats["adaptive_force_track_all_pressure_allocations"]
            < stats["adaptive_force_track_all_admitted_allocations"]
            or stats["adaptive_force_track_all_pressure_requested_bytes"]
            < stats["adaptive_force_track_all_admitted_requested_bytes"]
        ):
            raise matrix.MatrixError(
                "runtime lifetime global pressure omits admitted exact-site allocations"
            )
        external_pressure_allocations = (
            stats["adaptive_force_track_all_raw_pressure_allocations"]
            + stats[
                "adaptive_force_track_all_raw_reallocation_pressure_allocations"
            ]
        )
        external_pressure_bytes = (
            stats["adaptive_force_track_all_raw_pressure_requested_bytes"]
            + stats[
                "adaptive_force_track_all_raw_reallocation_pressure_requested_bytes"
            ]
        )
        if (
            external_pressure_allocations
            > stats["adaptive_force_track_all_pressure_allocations"]
            or external_pressure_bytes
            > stats["adaptive_force_track_all_pressure_requested_bytes"]
        ):
            raise matrix.MatrixError(
                "runtime lifetime raw pressure subsets exceed the global clock"
            )
        fail_closed_fields = (
            "adaptive_force_track_all_guard_bypasses",
            "adaptive_cold_bypassed_allocations",
            "adaptive_short_bypassed_allocations",
            "adaptive_site_table_bypasses",
            "adaptive_observation_table_bypasses",
            "thp_extent_mappings",
            "thp_advice_attempts",
            "thp_advice_successes",
            "thp_advice_errors",
            "nohugepage_advice_failures",
        )
        failed = [field for field in fail_closed_fields if stats[field] != 0]
        if failed:
            raise matrix.MatrixError(
                "runtime lifetime force-track evidence is incomplete: "
                + ", ".join(failed)
            )
    elif any(
        stats[field] != 0
        for field in (
            "adaptive_force_track_all_maximum_allocations",
            "adaptive_force_track_all_maximum_requested_bytes",
            "adaptive_force_track_all_admitted_allocations",
            "adaptive_force_track_all_admitted_requested_bytes",
            "adaptive_force_track_all_guard_bypasses",
            "adaptive_force_track_all_pressure_allocations",
            "adaptive_force_track_all_pressure_requested_bytes",
            "adaptive_force_track_all_raw_pressure_allocations",
            "adaptive_force_track_all_raw_pressure_requested_bytes",
            "adaptive_force_track_all_raw_reallocation_pressure_allocations",
            "adaptive_force_track_all_raw_reallocation_pressure_requested_bytes",
        )
    ):
        raise matrix.MatrixError("force-track counters are nonzero while disabled")


def runtime_implementation_digest() -> str:
    digest = hashlib.sha256()
    digest.update(_ORIGINAL_IMPLEMENTATION_DIGEST().encode("ascii"))
    digest.update(b"\0")
    digest.update(pathlib.Path(__file__).resolve().read_bytes())
    digest.update(b"\0")
    digest.update(_RUNTIME_MODE.encode("ascii"))
    return digest.hexdigest()


def extract_runtime_mode(argv: list[str]) -> tuple[str, list[str]]:
    mode = "adaptive"
    cleaned: list[str] = []
    index = 0
    while index < len(argv):
        value = argv[index]
        if value == "--runtime-lifetime-mode":
            if index + 1 >= len(argv):
                raise matrix.MatrixError("--runtime-lifetime-mode requires off or adaptive")
            mode = argv[index + 1]
            index += 2
            continue
        if value.startswith("--runtime-lifetime-mode="):
            mode = value.split("=", 1)[1]
            index += 1
            continue
        cleaned.append(value)
        index += 1
    if mode not in {"off", "adaptive"}:
        raise matrix.MatrixError("--runtime-lifetime-mode must be off or adaptive")
    return mode, cleaned


def aggregate_runtime_stats(rows: list[dict[str, Any]]) -> dict[str, Any] | None:
    """Retain per-process evidence and sum only event counters across measured runs."""
    samples = [
        row["runtime_lifetime_stats"]
        for row in rows
        if not row.get("warmup") and isinstance(row.get("runtime_lifetime_stats"), dict)
    ]
    if not samples:
        return None
    event_fields = (
        "adaptive_pressure_bytes",
        "adaptive_epoch_advances",
        "adaptive_eligible_allocations",
        "adaptive_training_allocations",
        "adaptive_cold_bypassed_allocations",
        "adaptive_short_routed_allocations",
        "adaptive_short_bypassed_allocations",
        "adaptive_long_routed_allocations",
        "adaptive_missing_identity_bypasses",
        "adaptive_site_table_bypasses",
        "adaptive_observation_table_bypasses",
        "adaptive_force_track_all_admitted_allocations",
        "adaptive_force_track_all_admitted_requested_bytes",
        "adaptive_force_track_all_guard_bypasses",
        "adaptive_force_track_all_pressure_allocations",
        "adaptive_force_track_all_pressure_requested_bytes",
        "adaptive_force_track_all_raw_pressure_allocations",
        "adaptive_force_track_all_raw_pressure_requested_bytes",
        "adaptive_force_track_all_raw_reallocation_pressure_allocations",
        "adaptive_force_track_all_raw_reallocation_pressure_requested_bytes",
        "adaptive_prior_conflicts",
        "adaptive_trailer_corruptions",
        "adaptive_short_observations",
        "adaptive_long_observations",
        "adaptive_censored_observations",
        "adaptive_decisive_observation_bytes",
        "adaptive_promotions",
        "adaptive_demotions",
        "adaptive_prior_corrections",
        "predictor_tp",
        "predictor_tp_bytes",
        "predictor_tn",
        "predictor_tn_bytes",
        "predictor_fp",
        "predictor_fp_bytes",
        "predictor_fn",
        "predictor_fn_bytes",
        "static_hint_tp",
        "static_hint_tp_bytes",
        "static_hint_tn",
        "static_hint_tn_bytes",
        "static_hint_fp",
        "static_hint_fp_bytes",
        "static_hint_fn",
        "static_hint_fn_bytes",
        "static_hint_abstained",
        "static_hint_abstained_bytes",
        "ordinary_extent_mappings",
        "thp_extent_mappings",
        "thp_advice_attempts",
        "thp_advice_successes",
        "thp_advice_errors",
        "nohugepage_advice_failures",
        "mapping_failures",
        "unsupported_layout_bypasses",
        "routed_allocations",
        "routed_deallocations",
    )
    totals = {
        field: sum(int(sample.get(field, 0)) for sample in samples)
        for field in event_fields
    }
    return {
        "measured_runs": len(samples),
        "totals": totals,
        "per_run": samples,
    }


def raw_dir_from_argv(argv: list[str]) -> pathlib.Path:
    for index, value in enumerate(argv):
        if value == "--raw-dir" and index + 1 < len(argv):
            return pathlib.Path(argv[index + 1]).resolve()
        if value.startswith("--raw-dir="):
            return pathlib.Path(value.split("=", 1)[1]).resolve()
    raise matrix.MatrixError("runtime wrapper requires an explicit --raw-dir")


def attach_runtime_stats(raw_dir: pathlib.Path, mode: str = "adaptive") -> pathlib.Path:
    result_path = raw_dir / "results.json"
    result = json.loads(result_path.read_text(encoding="utf-8"))
    by_app_variant: dict[tuple[str, str], list[dict[str, Any]]] = {}
    for row in result.get("measurements", []):
        app = str(row["app"])
        variant = str(row["variant"])
        round_index = int(row["round"])
        stderr_path = raw_dir / "runs" / app / f"round-{round_index:02d}" / variant / "stderr.bin"
        if not stderr_path.is_file():
            raise matrix.MatrixError(f"missing retained stderr for runtime stats: {stderr_path}")
        stderr = stderr_path.read_text(encoding="utf-8", errors="replace")
        stats = parse_runtime_stats(stderr)
        site_rows = parse_runtime_site_rows(stderr)
        if variant == "typeiso_coverage" and stats is None:
            raise matrix.MatrixError(
                f"typeiso_coverage emitted no runtime lifetime stats: {app}/round-{round_index:02d}"
            )
        if variant == "typeiso_coverage" and RUNTIME_SITE_PREFIX not in stderr:
            raise matrix.MatrixError(
                f"typeiso_coverage emitted no runtime lifetime sites: {app}/round-{round_index:02d}"
            )
        if stats is not None:
            validate_runtime_stats(stats, mode)
            validate_runtime_site_rows(site_rows, stats)
        elif site_rows:
            raise matrix.MatrixError(
                f"runtime lifetime sites emitted without stats: {app}/round-{round_index:02d}"
            )
        row["runtime_lifetime_stats"] = stats
        row["runtime_lifetime_sites"] = site_rows if stats is not None else None
        by_app_variant.setdefault((app, variant), []).append(row)
    for summary in result.get("summaries", []):
        summary["runtime_lifetime_stats"] = aggregate_runtime_stats(
            by_app_variant.get((str(summary["app"]), str(summary["variant"])), [])
        )
    selected_variants = set(map(str, result.get("variants", ())))
    result["runtime_lifetime_classifier"] = {
        "mode": mode,
        "enabled": mode == "adaptive" and bool(selected_variants & ADAPTIVE_VARIANTS),
        "instrumented_variants": sorted(selected_variants & ADAPTIVE_VARIANTS),
        "epoch_bytes": 2 * 1024 * 1024,
        "short_age_bytes_exclusive": 2 * 1024 * 1024,
        "long_age_bytes_inclusive": 8 * 1024 * 1024,
        "semantic_epoch_markers_required": False,
        "site_snapshot_abi_version": RUNTIME_SITE_ABI_VERSION,
        "site_join_key": list(RUNTIME_SITE_KEY_FIELDS),
        "force_track_all_coverage_only": True,
        "force_track_all_maximum_allocations": FORCE_TRACK_ALL_MAXIMUM_ALLOCATIONS,
        "force_track_all_maximum_requested_bytes": (
            FORCE_TRACK_ALL_MAXIMUM_REQUESTED_BYTES
        ),
        "adaptive_sampled_rows_prevalence_claim_eligible": False,
        "live_observations_are_right_censored": True,
        "pressure_clock": (
            "process-wide requested bytes across semantic, raw, guard-page, "
            "unsupported-layout, missing-identity, and address-moving reallocation "
            "allocation generations"
        ),
        "wrapper": str(pathlib.Path(__file__).resolve()),
        "wrapper_sha256": matrix.sha256_file(pathlib.Path(__file__).resolve()),
    }
    matrix.persist_result(result_path, result)
    return result_path


def main() -> int:
    global _RUNTIME_MODE

    mode, matrix_argv = extract_runtime_mode(sys.argv[1:])
    _RUNTIME_MODE = mode
    matrix.dependency_for_variant = adaptive_dependency_for_variant
    matrix.allocator_source = adaptive_allocator_source
    matrix.implementation_digest = runtime_implementation_digest
    sys.argv[1:] = matrix_argv
    if "--help" in matrix_argv or "-h" in matrix_argv:
        return matrix.main()
    if "--discard-run-output" in matrix_argv:
        raise matrix.MatrixError(
            "runtime lifetime evaluation requires retained stderr; remove --discard-run-output"
        )
    raw_dir = raw_dir_from_argv(matrix_argv)
    code = matrix.main()
    if code != 0:
        return code
    result_path = attach_runtime_stats(raw_dir, mode)
    print(json.dumps({"runtime_lifetime_results": str(result_path), "success": True}, sort_keys=True))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except matrix.MatrixError as error:
        print(f"error: {error}", file=sys.stderr)
        raise SystemExit(1)
