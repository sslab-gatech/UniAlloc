#!/usr/bin/env python3
"""Tests for Type Isolation cache-retention event-flow analysis."""

from __future__ import annotations

import importlib.util
import json
import pathlib
import tempfile
import unittest


ROOT = pathlib.Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "evaluation" / "scripts" / "analyze_typeiso_cache_retention.py"
spec = importlib.util.spec_from_file_location("typeiso_cache_retention", SCRIPT)
assert spec is not None and spec.loader is not None
analyzer = importlib.util.module_from_spec(spec)
spec.loader.exec_module(analyzer)


def coverage_measurement(
    *,
    app: str = "ripgrep",
    typed_allocations: int = 2266,
    typed_deallocations: int = 2266,
    cache_inserts: int = 342,
    cache_hits: int = 34,
    cache_bypasses: int = 4156,
    side_cache_entries: int = 308,
    metadata_segregation_side_cache_entries: int | None = None,
    admission_stats: dict[str, int] | None = None,
    type_stats: list[dict[str, int]] | None = None,
    warmup: bool = False,
    measurement_index: int | None = 0,
) -> dict[str, object]:
    stats = {
        "typed_allocations": typed_allocations,
        "typed_deallocations": typed_deallocations,
        "cache_inserts": cache_inserts,
        "cache_hits": cache_hits,
        "cache_bypasses": cache_bypasses,
        "side_cache_entries": side_cache_entries,
        "side_cache_corrupt_slots": 0,
    }
    if metadata_segregation_side_cache_entries is not None:
        stats.update(
            {
                "metadata_segregation_side_cache_entries": (
                    metadata_segregation_side_cache_entries
                ),
                "metadata_segregation_side_cache_corrupt_buckets": 0,
            }
        )
    if admission_stats is not None:
        stats.update(admission_stats)
    measurement: dict[str, object] = {
        "app": app,
        "variant": "typeiso_coverage",
        "warmup": warmup,
        "measurement_index": measurement_index,
        "round": 0,
        "stats": stats,
    }
    if type_stats is not None:
        measurement["type_stats"] = type_stats
    return measurement


def complete_admission_stats(
    *,
    admitted_events: int = 342,
    rejected_too_small_events: int = 1924,
    terminal_events: int = 2266,
) -> dict[str, int]:
    outcomes = (
        "admitted",
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
        "ownership_registry_full_failstop",
        "tiny_side_inserts",
        "tiny_side_hits",
    )
    result: dict[str, int] = {}
    for outcome in outcomes:
        events = 0
        if outcome == "admitted":
            events = admitted_events
        elif outcome == "rejected_too_small":
            events = rejected_too_small_events
        result[f"cache_admission_{outcome}_events"] = events
        result[f"cache_admission_{outcome}_rounded_bytes"] = events * 24
    result["cache_admission_terminal_events"] = terminal_events
    return result


class TypeIsoCacheRetentionTests(unittest.TestCase):
    def test_entry_budget_telemetry_is_part_of_the_complete_schema(self) -> None:
        self.assertIn(
            "rejected_aggregate_entry_budget",
            analyzer.ADMISSION_REJECTION_OUTCOMES,
        )
        self.assertIn(
            "rejected_registry_pressure",
            analyzer.ADMISSION_REJECTION_OUTCOMES,
        )
        for metric in ("events", "rounded_bytes"):
            self.assertIn(
                f"cache_admission_depot_from_aggregate_entry_budget_{metric}",
                analyzer.OPTIONAL_COUNTER_DEFAULTS,
            )

    def test_ripgrep_like_event_flow(self) -> None:
        result = analyzer.analyze_measurement(coverage_measurement())

        self.assertEqual(result["counts"]["allocation_misses"], 2232)
        self.assertEqual(result["counts"]["immediate_non_admissions"], 1924)
        self.assertEqual(
            result["counts"]["inferred_post_admission_terminal_exits"], 0
        )
        self.assertEqual(result["counts"]["cache_nonretained_at_report_events"], 1924)
        self.assertEqual(result["counts"]["bypass_residual"], 0)
        self.assertAlmostEqual(
            result["rates"]["allocation_miss_rate_of_typed_allocations"],
            2232 / 2266,
        )
        self.assertAlmostEqual(
            result["rates"]["immediate_non_admission_rate_of_typed_deallocations"],
            1924 / 2266,
        )
        self.assertAlmostEqual(
            result["rates"]["cache_nonretained_at_report_rate_of_typed_deallocations"],
            1924 / 2266,
        )

    def test_retention_snapshot_sums_plain_and_metadata_segregated_caches(self) -> None:
        result = analyzer.analyze_measurement(
            coverage_measurement(
                side_cache_entries=300,
                metadata_segregation_side_cache_entries=8,
            )
        )

        self.assertEqual(result["counts"]["side_cache_entries"], 300)
        self.assertEqual(
            result["counts"]["metadata_segregation_side_cache_entries"], 8
        )
        self.assertEqual(result["counts"]["retained_side_cache_entries"], 308)
        self.assertEqual(result["counts"]["cache_nonretained_at_report_events"], 1924)
        self.assertAlmostEqual(
            result["rates"]["retained_at_report_rate_of_typed_deallocations"],
            308 / 2266,
        )

    def test_historical_plain_only_snapshot_remains_supported(self) -> None:
        measurement = coverage_measurement()
        stats = measurement["stats"]
        assert isinstance(stats, dict)
        self.assertNotIn("metadata_segregation_side_cache_entries", stats)

        result = analyzer.analyze_measurement(measurement)

        self.assertEqual(
            result["counts"]["metadata_segregation_side_cache_entries"], 0
        )
        self.assertEqual(result["counts"]["retained_side_cache_entries"], 308)
        self.assertFalse(result["admission_reason_conservation"]["available"])

    def test_process_wide_depot_participates_in_retention_conservation(self) -> None:
        measurement = coverage_measurement(
            typed_allocations=100,
            typed_deallocations=100,
            cache_inserts=80,
            cache_hits=60,
            cache_bypasses=85,
            side_cache_entries=5,
            metadata_segregation_side_cache_entries=3,
        )
        stats = measurement["stats"]
        assert isinstance(stats, dict)
        stats.update(
            {
                "cache_admission_depot_attempts_events": 20,
                "cache_admission_depot_inserts_events": 18,
                "cache_admission_depot_rescued_overflow_events": 15,
                "cache_admission_depot_hits_events": 10,
                "cache_admission_depot_hits_after_recorded_l1_bypass_events": 10,
                "cache_admission_depot_evictions_events": 2,
                "cache_admission_depot_rejected_capacity_events": 2,
                "cache_admission_depot_from_local_eviction_events": 3,
                "cache_admission_depot_current_entries": 10,
                "cache_admission_depot_peak_entries": 18,
                "cache_admission_depot_current_rounded_bytes": 480,
                "cache_admission_depot_peak_rounded_bytes": 864,
            }
        )

        result = analyzer.analyze_measurement(measurement)

        self.assertEqual(result["counts"]["retained_side_cache_entries"], 8)
        self.assertEqual(result["counts"]["retained_cache_entries"], 18)
        self.assertEqual(result["counts"]["l1_overflows_rescued_by_depot"], 15)
        self.assertEqual(
            result["counts"]["inferred_post_admission_terminal_exits"], 2
        )
        self.assertEqual(result["counts"]["cache_nonretained_at_report_events"], 22)
        self.assertEqual(result["counts"]["bypass_residual"], 0)
        self.assertEqual(result["depot"]["rejected_offers"], 2)
        self.assertEqual(result["depot"]["rejected_pending_l1_overflows"], 2)
        self.assertEqual(result["depot"]["known_post_admission_exits"], 2)
        self.assertAlmostEqual(result["depot"]["hit_rate_per_insert"], 10 / 18)
        self.assertAlmostEqual(
            result["depot"]["rescue_rate_per_pending_l1_overflow_attempt"],
            15 / 17,
        )

    def test_complete_admission_reasons_conserve_typed_deallocations(self) -> None:
        result = analyzer.analyze_measurement(
            coverage_measurement(admission_stats=complete_admission_stats())
        )

        conservation = result["admission_reason_conservation"]
        self.assertTrue(conservation["available"])
        self.assertEqual(conservation["terminal_events"], 2266)
        self.assertEqual(conservation["rejected_events"], 1924)
        self.assertEqual(conservation["expected_rejected_events"], 1924)
        self.assertEqual(
            conservation["outcomes"]["rejected_too_small"],
            {"events": 1924, "rounded_bytes": 1924 * 24},
        )
        self.assertEqual(conservation["tiny_side_inserts"]["events"], 0)

    def test_semantic_type_rows_rank_pressure_without_claiming_exact_layout(self) -> None:
        fields = {
            "module_id": 2,
            "allocations": 10,
            "deallocations": 10,
            "cache_hits": 4,
            "cache_inserts": 5,
            "cache_bypasses": 11,
            "observed_alloc_size": 48,
            "observed_alloc_align": 8,
            "observed_dealloc_size": 48,
            "observed_dealloc_align": 8,
            "policy_flags_seen": 1,
        }
        result = analyzer.analyze_measurement(
            coverage_measurement(
                type_stats=[
                    {"type_id": 11, "callsite": 101, **fields},
                    {
                        "type_id": 22,
                        "callsite": 202,
                        **{
                            **fields,
                            "allocations": 2,
                            "deallocations": 2,
                            "cache_hits": 2,
                            "cache_inserts": 2,
                            "cache_bypasses": 0,
                        },
                    },
                ]
            )
        )

        rows = result["semantic_type_rows"]
        self.assertTrue(rows["available"])
        self.assertEqual(rows["semantic_row_count"], 2)
        self.assertEqual(rows["distinct_type_ids"], 2)
        self.assertEqual(rows["counter_sums"]["immediate_non_admissions"], 5)
        self.assertEqual(rows["top_pressure_rows"][0]["type_id"], 11)
        self.assertEqual(
            rows["top_pressure_rows"][0]["immediate_non_admissions"], 5
        )
        self.assertIn("last non-zero layout", rows["layout_scope"])
        self.assertIn("no event order", rows["inference_boundary"])

    def test_rejects_incomplete_or_nonconserving_admission_reasons(self) -> None:
        incomplete = complete_admission_stats()
        incomplete.pop("cache_admission_rejected_too_large_events")
        cases = {
            "incomplete": coverage_measurement(admission_stats=incomplete),
            "terminal mismatch": coverage_measurement(
                admission_stats=complete_admission_stats(terminal_events=2265)
            ),
            "rejection mismatch": coverage_measurement(
                admission_stats=complete_admission_stats(
                    rejected_too_small_events=1923
                )
            ),
        }
        for label, measurement in cases.items():
            with self.subTest(label=label):
                with self.assertRaises(analyzer.RetentionAnalysisError):
                    analyzer.analyze_measurement(measurement)

    def test_medians_are_grouped_by_app_and_warmups_are_excluded(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = pathlib.Path(temporary) / "results.json"
            path.write_text(
                json.dumps(
                    {
                        "measurements": [
                            coverage_measurement(warmup=True, measurement_index=None),
                            coverage_measurement(measurement_index=0),
                            coverage_measurement(
                                typed_allocations=10,
                                typed_deallocations=10,
                                cache_inserts=10,
                                cache_hits=5,
                                cache_bypasses=5,
                                side_cache_entries=5,
                                measurement_index=1,
                            ),
                            {"app": "ripgrep", "variant": "native", "stats": None},
                        ]
                    }
                ),
                encoding="utf-8",
            )

            result = analyzer.analyze_result_paths([path])

        self.assertEqual(result["measurement_count"], 2)
        self.assertEqual(result["schema_version"], 3)
        self.assertTrue(result["evidence_scope"]["event_only"])
        self.assertEqual(
            result["evidence_scope"]["side_cache_entries_scope"],
            "current_reporting_thread_plain_and_metadata_segregated_tls_caches",
        )
        self.assertIn(
            "reason-specific",
            result["evidence_scope"]["immediate_non_admission_status"],
        )
        self.assertIn(
            "plain compiler/type-isolation",
            result["evidence_scope"]["admission_conservation_scope"],
        )
        self.assertIn(
            "direct security-escape measurement",
            result["evidence_scope"]["cache_nonretention_status"],
        )
        self.assertEqual(
            result["evidence_scope"]["initial_cache_state"],
            "assumed_empty_at_counter_interval_start",
        )
        aggregate = result["applications"][0]
        self.assertEqual(aggregate["app"], "ripgrep")
        self.assertEqual(aggregate["measurement_count"], 2)
        self.assertEqual(
            aggregate["fieldwise_median_counts"]["cache_nonretained_at_report_events"],
            (1924 + 0) / 2,
        )
        self.assertEqual(
            aggregate["count_ranges"]["cache_nonretained_at_report_events"],
            [0, 1924],
        )
        self.assertIn("different measurements", aggregate["aggregation_boundary"])

    def test_rejects_invalid_conservation_cases(self) -> None:
        cases = {
            "more hits than allocations": coverage_measurement(cache_hits=2267),
            "more inserts than deallocations": coverage_measurement(cache_inserts=2267),
            "end entries exceed admitted flow": coverage_measurement(
                cache_inserts=342, cache_hits=34, side_cache_entries=309
            ),
            "combined end entries exceed admitted flow": coverage_measurement(
                cache_inserts=342,
                cache_hits=34,
                side_cache_entries=308,
                metadata_segregation_side_cache_entries=1,
            ),
            "bypass counter cannot cover both legs": coverage_measurement(
                cache_bypasses=4155
            ),
        }
        for label, measurement in cases.items():
            with self.subTest(label=label):
                with self.assertRaises(analyzer.RetentionAnalysisError):
                    analyzer.analyze_measurement(measurement)

    def test_zero_event_rates_remain_undefined(self) -> None:
        result = analyzer.analyze_measurement(
            coverage_measurement(
                typed_allocations=0,
                typed_deallocations=0,
                cache_inserts=0,
                cache_hits=0,
                cache_bypasses=0,
                side_cache_entries=0,
            )
        )
        self.assertIsNone(
            result["rates"]["cache_nonretained_at_report_rate_of_typed_deallocations"]
        )
        self.assertIsNone(
            result["rates"]["bypass_residual_rate_of_cache_bypasses"]
        )

    def test_rejects_corrupt_metadata_segregated_snapshot(self) -> None:
        measurement = coverage_measurement(
            side_cache_entries=307,
            metadata_segregation_side_cache_entries=1,
        )
        stats = measurement["stats"]
        assert isinstance(stats, dict)
        stats["metadata_segregation_side_cache_corrupt_buckets"] = 1

        with self.assertRaisesRegex(
            analyzer.RetentionAnalysisError,
            "metadata_segregation_side_cache_corrupt_buckets",
        ):
            analyzer.analyze_measurement(measurement)


if __name__ == "__main__":
    unittest.main()
