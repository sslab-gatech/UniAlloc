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
        "adaptive_live_trailers",
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
        "mapping_failures",
        "live_objects",
        "all_mappings_released",
        "routed_allocations",
        "routed_deallocations",
    )
)


RUNTIME_REPORT_RUST = f'''
        let lifetime = unialloc::lifetime_hugepage_stats_snapshot();
        eprintln!(
            r#"{RUNTIME_STATS_PREFIX}{{{{\"policy\":{{}},\"backend\":{{}},\"phase_advances\":{{}},\"adaptive_pressure_bytes\":{{}},\"adaptive_pressure_epoch\":{{}},\"adaptive_epoch_advances\":{{}},\"adaptive_site_count\":{{}},\"adaptive_cold_sites\":{{}},\"adaptive_short_sites\":{{}},\"adaptive_long_sites\":{{}},\"adaptive_eligible_allocations\":{{}},\"adaptive_training_allocations\":{{}},\"adaptive_cold_bypassed_allocations\":{{}},\"adaptive_short_routed_allocations\":{{}},\"adaptive_short_bypassed_allocations\":{{}},\"adaptive_long_routed_allocations\":{{}},\"adaptive_missing_identity_bypasses\":{{}},\"adaptive_site_table_bypasses\":{{}},\"adaptive_prior_conflicts\":{{}},\"adaptive_live_trailers\":{{}},\"adaptive_trailer_corruptions\":{{}},\"adaptive_short_observations\":{{}},\"adaptive_long_observations\":{{}},\"adaptive_censored_observations\":{{}},\"adaptive_decisive_observation_bytes\":{{}},\"adaptive_promotions\":{{}},\"adaptive_demotions\":{{}},\"adaptive_prior_corrections\":{{}},\"predictor_tp\":{{}},\"predictor_tp_bytes\":{{}},\"predictor_tn\":{{}},\"predictor_tn_bytes\":{{}},\"predictor_fp\":{{}},\"predictor_fp_bytes\":{{}},\"predictor_fn\":{{}},\"predictor_fn_bytes\":{{}},\"static_hint_tp\":{{}},\"static_hint_tp_bytes\":{{}},\"static_hint_tn\":{{}},\"static_hint_tn_bytes\":{{}},\"static_hint_fp\":{{}},\"static_hint_fp_bytes\":{{}},\"static_hint_fn\":{{}},\"static_hint_fn_bytes\":{{}},\"static_hint_abstained\":{{}},\"static_hint_abstained_bytes\":{{}},\"ordinary_extent_mappings\":{{}},\"thp_extent_mappings\":{{}},\"thp_advice_attempts\":{{}},\"thp_advice_successes\":{{}},\"thp_advice_errors\":{{}},\"mapping_failures\":{{}},\"live_objects\":{{}},\"all_mappings_released\":{{}},\"routed_allocations\":{{}},\"routed_deallocations\":{{}}}}}}"#,
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
            lifetime.mapping_failures,
            lifetime.live_objects,
            lifetime.all_mappings_released,
            lifetime.routed_allocations,
            lifetime.routed_deallocations,
        );
'''


RUNTIME_INITIALIZE_ADAPTIVE_RUST = '''
        assert!(unialloc::lifetime_hugepage_stats_reset());
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


def runtime_initializer_rust() -> str:
    if _RUNTIME_MODE == "adaptive":
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
        report_marker = (
            "            side_cache.corrupt_slots,\n"
            "        );\n"
            "    }\n\n"
            '    extern "C" fn initialize() {'
        )
        if report_marker not in source:
            raise matrix.MatrixError("coverage report injection marker changed")
        source = source.replace(
            report_marker,
            "            side_cache.corrupt_slots,\n"
            "        );\n"
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
            f"{runtime_initializer_rust()}"
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


def validate_runtime_stats(stats: dict[str, Any], mode: str) -> None:
    for field in RUNTIME_REQUIRED_FIELDS - {"all_mappings_released"}:
        value = stats[field]
        if not isinstance(value, int) or isinstance(value, bool) or value < 0:
            raise matrix.MatrixError(f"runtime lifetime field {field} is not a nonnegative integer")
    if not isinstance(stats["all_mappings_released"], bool):
        raise matrix.MatrixError("runtime lifetime field all_mappings_released is not boolean")
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
    ):
        raise matrix.MatrixError("runtime lifetime allocation accounting does not close")
    if stats["adaptive_trailer_corruptions"] != 0:
        raise matrix.MatrixError("runtime lifetime trailer corruption was observed")
    if stats["mapping_failures"] != 0:
        raise matrix.MatrixError("runtime lifetime arena mapping failure was observed")
    tracked_allocations = (
        stats["adaptive_training_allocations"]
        + stats["adaptive_short_routed_allocations"]
        + stats["adaptive_long_routed_allocations"]
    )
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
        "mapping_failures",
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
        stats = parse_runtime_stats(stderr_path.read_text(encoding="utf-8", errors="replace"))
        if variant == "typeiso_coverage" and stats is None:
            raise matrix.MatrixError(
                f"typeiso_coverage emitted no runtime lifetime stats: {app}/round-{round_index:02d}"
            )
        if stats is not None:
            validate_runtime_stats(stats, mode)
        row["runtime_lifetime_stats"] = stats
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
