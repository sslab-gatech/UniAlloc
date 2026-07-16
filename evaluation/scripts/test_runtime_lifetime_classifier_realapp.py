#!/usr/bin/env python3
"""Unit tests for the marker-free real-application evaluation wrapper."""

from __future__ import annotations

import importlib.util
import json
import pathlib
import subprocess
import sys
import tempfile
import unittest


ROOT = pathlib.Path(__file__).resolve().parents[2]
SCRIPTS = ROOT / "evaluation" / "scripts"
SCRIPT = SCRIPTS / "runtime_lifetime_classifier_realapp.py"

sys.path.insert(0, str(SCRIPTS))
spec = importlib.util.spec_from_file_location("runtime_lifetime_classifier_realapp", SCRIPT)
assert spec is not None and spec.loader is not None
wrapper = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = wrapper
spec.loader.exec_module(wrapper)


def valid_stats(**overrides: int | bool) -> dict[str, int | bool]:
    stats: dict[str, int | bool] = {field: 0 for field in wrapper.RUNTIME_REQUIRED_FIELDS}
    stats["all_mappings_released"] = True
    stats["adaptive_observation_recording"] = True
    stats["adaptive_force_track_all"] = True
    stats["adaptive_force_track_all_maximum_allocations"] = (
        wrapper.FORCE_TRACK_ALL_MAXIMUM_ALLOCATIONS
    )
    stats["adaptive_force_track_all_maximum_requested_bytes"] = (
        wrapper.FORCE_TRACK_ALL_MAXIMUM_REQUESTED_BYTES
    )
    stats.update(overrides)
    if "adaptive_force_track_all_pressure_allocations" not in overrides:
        stats["adaptive_force_track_all_pressure_allocations"] = stats[
            "adaptive_force_track_all_admitted_allocations"
        ]
    if "adaptive_force_track_all_pressure_requested_bytes" not in overrides:
        stats["adaptive_force_track_all_pressure_requested_bytes"] = stats[
            "adaptive_force_track_all_admitted_requested_bytes"
        ]
    if "adaptive_pressure_bytes" not in overrides:
        stats["adaptive_pressure_bytes"] = stats[
            "adaptive_force_track_all_pressure_requested_bytes"
        ]
    return stats


def valid_site(**overrides: int | bool) -> dict[str, int | bool]:
    row: dict[str, int | bool] = {
        "callsite": 11,
        "type_id": 22,
        "module_id": 33,
        "requested_size": 63,
        "align": 8,
        "minimum_payload_capacity": 64,
        "maximum_payload_capacity": 64,
        "allocation_count": 1,
        "allocation_requested_bytes": 63,
        "allocation_payload_bytes": 64,
        "tracked_allocations": 1,
        "bypassed_allocations": 0,
        "completed_outcomes": 1,
        "short_outcomes": 1,
        "long_outcomes": 0,
        "censored_outcomes": 0,
        "short_requested_bytes": 63,
        "long_requested_bytes": 0,
        "censored_requested_bytes": 0,
        "total_completed_age_bytes": 32,
        "maximum_completed_age_bytes": 32,
        "tracked_inflight": 0,
        "inflight_age_lower_bound_bytes": 0,
        "live_survival_observations": 0,
        "live_survival_requested_bytes": 0,
        "live_survival_payload_bytes": 0,
        "minimum_live_survival_age_bytes": 0,
        "maximum_live_survival_age_bytes": 0,
        "current_live_survival_inflight": 0,
        "current_live_survival_inflight_requested_bytes": 0,
        "current_live_survival_inflight_payload_bytes": 0,
        "first_allocation_pressure": 64,
        "last_allocation_pressure": 64,
        "predictor_key_ambiguous": False,
        "predictor_flags_seen": 1,
        "predictor_placement_hint_bits_union": 0,
        "latest_prediction": 0,
        "latest_static_prior": 0,
    }
    row.update(overrides)
    return row


def site_snapshot(rows: list[dict[str, int | bool]]) -> str:
    return wrapper.RUNTIME_SITE_PREFIX + json.dumps(
        {
            "source": wrapper.RUNTIME_SITE_SOURCE,
            "abi_version": wrapper.RUNTIME_SITE_ABI_VERSION,
            "required_rows": len(rows),
            "captured_rows": len(rows),
            "truncated": False,
            "rows": rows,
        }
    )


class RuntimeLifetimeClassifierRealAppTests(unittest.TestCase):
    def setUp(self) -> None:
        wrapper._RUNTIME_MODE = "adaptive"

    def test_adaptive_variants_add_only_lifetime_hugepage_feature(self) -> None:
        _dependency, performance = wrapper.adaptive_dependency_for_variant("typeiso_perf")
        _dependency, coverage = wrapper.adaptive_dependency_for_variant("typeiso_coverage")
        baseline_dependency, baseline_features = wrapper.adaptive_dependency_for_variant("unialloc")

        self.assertEqual(performance, ("type_isolation", "lifetime_hugepage"))
        self.assertEqual(coverage, ("stats", "type_isolation", "lifetime_hugepage"))
        self.assertEqual(
            (baseline_dependency, baseline_features),
            wrapper._ORIGINAL_DEPENDENCY_FOR_VARIANT("unialloc"),
        )

    def test_injected_sources_enable_adaptive_thp_before_workload(self) -> None:
        performance = wrapper.adaptive_allocator_source("typeiso_perf")
        coverage = wrapper.adaptive_allocator_source("typeiso_coverage")

        for source in (performance, coverage):
            self.assertEqual(source.count("AdaptiveRuntimeHugepage"), 1)
            self.assertEqual(source.count("TransparentHugepage"), 1)
            self.assertEqual(source.count(".init_array"), 1)
        self.assertNotIn(wrapper.RUNTIME_STATS_PREFIX, performance)
        self.assertNotIn("adaptive_site_recording_enable", performance)
        self.assertNotIn("adaptive_site_force_track_all_enable", performance)
        self.assertEqual(coverage.count(wrapper.RUNTIME_STATS_PREFIX), 1)
        self.assertEqual(coverage.count(wrapper.RUNTIME_SITE_PREFIX), 1)
        self.assertEqual(coverage.count("adaptive_site_recording_enable"), 1)
        self.assertEqual(coverage.count("adaptive_site_force_track_all_enable"), 1)
        self.assertIn(str(wrapper.FORCE_TRACK_ALL_MAXIMUM_ALLOCATIONS), coverage)
        self.assertIn(str(wrapper.FORCE_TRACK_ALL_MAXIMUM_REQUESTED_BYTES), coverage)
        self.assertEqual(coverage.count("RUNTIME_LIFETIME_SITE_ROWS"), 2)
        self.assertLess(
            coverage.index("static RUNTIME_LIFETIME_SITE_ROWS"),
            coverage.index("lifetime_hugepage_adaptive_site_recording_enable"),
        )
        self.assertLess(
            coverage.index("lifetime_hugepage_adaptive_site_snapshot"),
            coverage.index(wrapper.matrix.STATS_PREFIX),
        )
        self.assertLess(
            coverage.index("lifetime_hugepage_adaptive_site_recording_enable"),
            coverage.index("lifetime_hugepage_adaptive_site_force_track_all_enable"),
        )
        self.assertLess(
            coverage.index("lifetime_hugepage_adaptive_site_force_track_all_enable"),
            coverage.index("lifetime_hugepage_configure_with_backend"),
        )
        self.assertLess(
            coverage.index("lifetime_hugepage_configure_with_backend"),
            coverage.index("semantic_stats_recording_enable"),
        )

    def test_generated_coverage_source_is_valid_rust_syntax(self) -> None:
        source = wrapper.adaptive_allocator_source("typeiso_coverage")
        with tempfile.NamedTemporaryFile(suffix=".rs") as rust_source:
            rust_source.write(source.encode("utf-8"))
            rust_source.flush()
            result = subprocess.run(
                ["rustfmt", "--edition", "2024", "--emit", "stdout", rust_source.name],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE,
                check=False,
            )
        self.assertEqual(result.returncode, 0, result.stderr.decode(errors="replace"))

    def test_off_mode_preserves_features_and_explicitly_disables_policy(self) -> None:
        wrapper._RUNTIME_MODE = "off"
        _dependency, features = wrapper.adaptive_dependency_for_variant("typeiso_perf")
        source = wrapper.adaptive_allocator_source("typeiso_perf")

        self.assertEqual(features, ("type_isolation", "lifetime_hugepage"))
        self.assertIn("LifetimeHugepagePolicy::Disabled", source)
        self.assertNotIn("AdaptiveRuntimeHugepage", source)

    def test_runtime_parser_uses_last_valid_record(self) -> None:
        first = valid_stats(policy=5, adaptive_long_observations=1)
        second = valid_stats(policy=5, adaptive_long_observations=2)
        stderr = "\n".join(
            (
                wrapper.RUNTIME_STATS_PREFIX + json.dumps(first),
                f"{wrapper.RUNTIME_STATS_PREFIX}not-json",
                wrapper.RUNTIME_STATS_PREFIX + json.dumps(second),
            )
        )
        self.assertEqual(wrapper.parse_runtime_stats(stderr), second)
        self.assertIsNone(wrapper.parse_runtime_stats("ordinary stderr"))
        self.assertIsNone(wrapper.parse_runtime_stats(wrapper.RUNTIME_STATS_PREFIX + "{}"))

    def test_site_parser_preserves_right_censored_live_rows(self) -> None:
        live = valid_site(
            completed_outcomes=0,
            short_outcomes=0,
            short_requested_bytes=0,
            total_completed_age_bytes=0,
            maximum_completed_age_bytes=0,
            tracked_inflight=1,
            inflight_age_lower_bound_bytes=8 * 1024 * 1024,
        )
        parsed = wrapper.parse_runtime_site_rows(site_snapshot([live]))

        self.assertEqual(parsed, [live])
        self.assertEqual(parsed[0]["long_outcomes"], 0)
        self.assertEqual(parsed[0]["tracked_inflight"], 1)
        self.assertNotIn("inflight_outcome", parsed[0])

    def test_site_parser_rejects_duplicate_exact_five_key(self) -> None:
        row = valid_site()
        with self.assertRaises(wrapper.matrix.MatrixError):
            wrapper.parse_runtime_site_rows(site_snapshot([row, row]))

    def test_site_parser_rejects_truncated_snapshot(self) -> None:
        envelope = json.loads(site_snapshot([valid_site()]).split("=", 1)[1])
        envelope["required_rows"] = 2
        envelope["truncated"] = True
        with self.assertRaises(wrapper.matrix.MatrixError):
            wrapper.parse_runtime_site_rows(
                wrapper.RUNTIME_SITE_PREFIX + json.dumps(envelope)
            )

    def test_site_validator_rejects_broken_allocation_and_tracked_flow(self) -> None:
        with self.assertRaises(wrapper.matrix.MatrixError):
            wrapper.validate_runtime_site_rows([valid_site(allocation_count=2)])
        with self.assertRaises(wrapper.matrix.MatrixError):
            wrapper.validate_runtime_site_rows([valid_site(tracked_inflight=1)])

    def test_site_validator_counts_current_survivor_as_unique_long_once(self) -> None:
        survivor = valid_site(
            completed_outcomes=0,
            short_outcomes=0,
            short_requested_bytes=0,
            total_completed_age_bytes=0,
            maximum_completed_age_bytes=0,
            tracked_inflight=1,
            inflight_age_lower_bound_bytes=wrapper.LIVE_SURVIVAL_MINIMUM_AGE_BYTES,
            live_survival_observations=1,
            live_survival_requested_bytes=63,
            live_survival_payload_bytes=64,
            minimum_live_survival_age_bytes=wrapper.LIVE_SURVIVAL_MINIMUM_AGE_BYTES,
            maximum_live_survival_age_bytes=wrapper.LIVE_SURVIVAL_MINIMUM_AGE_BYTES,
            current_live_survival_inflight=1,
            current_live_survival_inflight_requested_bytes=63,
            current_live_survival_inflight_payload_bytes=64,
            latest_prediction=2,
        )
        stats = valid_stats(
            policy=5,
            adaptive_site_count=1,
            adaptive_long_sites=1,
            adaptive_eligible_allocations=1,
            adaptive_training_allocations=1,
            adaptive_force_track_all_admitted_allocations=1,
            adaptive_force_track_all_admitted_requested_bytes=63,
            adaptive_force_track_all_pressure_allocations=1,
            adaptive_force_track_all_pressure_requested_bytes=63,
            adaptive_pressure_bytes=63,
            adaptive_live_trailers=1,
            adaptive_live_survival_registrations=1,
            adaptive_live_survival_scans=1,
            adaptive_live_survival_slots_examined=1,
            adaptive_live_survival_observations=1,
            adaptive_live_survival_promotions=1,
            adaptive_long_observations=1,
            adaptive_decisive_observation_bytes=64,
            adaptive_promotions=1,
            static_hint_abstained=1,
            static_hint_abstained_bytes=64,
            adaptive_observation_site_count=1,
        )

        wrapper.validate_runtime_stats(stats, "adaptive")
        wrapper.validate_runtime_site_rows([survivor], stats)

        completed = dict(survivor)
        completed.update(
            {
                "completed_outcomes": 1,
                "long_outcomes": 1,
                "long_requested_bytes": 63,
                "total_completed_age_bytes": wrapper.LIVE_SURVIVAL_MINIMUM_AGE_BYTES,
                "maximum_completed_age_bytes": wrapper.LIVE_SURVIVAL_MINIMUM_AGE_BYTES,
                "tracked_inflight": 0,
                "inflight_age_lower_bound_bytes": 0,
                "current_live_survival_inflight": 0,
                "current_live_survival_inflight_requested_bytes": 0,
                "current_live_survival_inflight_payload_bytes": 0,
            }
        )
        completed_stats = dict(stats)
        completed_stats["adaptive_live_trailers"] = 0
        wrapper.validate_runtime_stats(completed_stats, "adaptive")
        wrapper.validate_runtime_site_rows([completed], completed_stats)

    def test_site_validator_rejects_double_counted_or_malformed_live_provenance(self) -> None:
        survivor = valid_site(
            completed_outcomes=0,
            short_outcomes=0,
            short_requested_bytes=0,
            total_completed_age_bytes=0,
            maximum_completed_age_bytes=0,
            tracked_inflight=1,
            inflight_age_lower_bound_bytes=wrapper.LIVE_SURVIVAL_MINIMUM_AGE_BYTES,
            live_survival_observations=1,
            live_survival_requested_bytes=63,
            live_survival_payload_bytes=64,
            minimum_live_survival_age_bytes=wrapper.LIVE_SURVIVAL_MINIMUM_AGE_BYTES,
            maximum_live_survival_age_bytes=wrapper.LIVE_SURVIVAL_MINIMUM_AGE_BYTES,
            current_live_survival_inflight=1,
            current_live_survival_inflight_requested_bytes=63,
            current_live_survival_inflight_payload_bytes=64,
        )
        for field, value in (
            ("live_survival_requested_bytes", 126),
            ("minimum_live_survival_age_bytes", 1),
            ("current_live_survival_inflight_payload_bytes", 0),
        ):
            with self.subTest(field=field):
                broken = dict(survivor)
                broken[field] = value
                with self.assertRaises(wrapper.matrix.MatrixError):
                    wrapper.validate_runtime_site_rows([broken])

    def test_aggregate_excludes_warmups_and_sums_event_counters(self) -> None:
        rows = [
            {
                "warmup": True,
                "runtime_lifetime_stats": {"adaptive_long_observations": 100},
            },
            {
                "warmup": False,
                "runtime_lifetime_stats": {
                    "adaptive_long_observations": 2,
                    "predictor_tp": 1,
                },
            },
            {
                "warmup": False,
                "runtime_lifetime_stats": {
                    "adaptive_long_observations": 3,
                    "predictor_tp": 2,
                },
            },
        ]
        aggregate = wrapper.aggregate_runtime_stats(rows)

        assert aggregate is not None
        self.assertEqual(aggregate["measured_runs"], 2)
        self.assertEqual(aggregate["totals"]["adaptive_long_observations"], 5)
        self.assertEqual(aggregate["totals"]["predictor_tp"], 3)
        self.assertEqual(
            aggregate["totals"]["adaptive_force_track_all_pressure_allocations"],
            0,
        )
        self.assertEqual(len(aggregate["per_run"]), 2)

    def test_attach_preserves_per_run_stats_and_adds_contract(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            raw_dir = pathlib.Path(temporary)
            run_dir = raw_dir / "runs" / "ripgrep" / "round-00" / "typeiso_coverage"
            run_dir.mkdir(parents=True)
            stats = valid_stats(
                policy=5,
                adaptive_site_count=1,
                adaptive_long_sites=1,
                adaptive_eligible_allocations=4,
                adaptive_long_routed_allocations=4,
                adaptive_force_track_all_admitted_allocations=4,
                adaptive_force_track_all_admitted_requested_bytes=256,
                adaptive_long_observations=4,
                adaptive_observation_site_count=1,
                adaptive_decisive_observation_bytes=256,
                predictor_tp=3,
                static_hint_abstained=4,
                static_hint_abstained_bytes=256,
                phase_advances=0,
            )
            (run_dir / "stderr.bin").write_text(
                "\n".join(
                    (
                        wrapper.RUNTIME_STATS_PREFIX + json.dumps(stats),
                        site_snapshot(
                            [
                                valid_site(
                                    requested_size=64,
                                    allocation_count=4,
                                    allocation_requested_bytes=256,
                                    allocation_payload_bytes=256,
                                    tracked_allocations=4,
                                    completed_outcomes=4,
                                    short_outcomes=0,
                                    long_outcomes=4,
                                    short_requested_bytes=0,
                                    long_requested_bytes=256,
                                    total_completed_age_bytes=32 * 1024 * 1024,
                                    maximum_completed_age_bytes=8 * 1024 * 1024,
                                    latest_prediction=2,
                                )
                            ]
                        ),
                    )
                )
                + "\n",
                encoding="utf-8",
            )
            result = {
                "measurements": [
                    {
                        "app": "ripgrep",
                        "variant": "typeiso_coverage",
                        "round": 0,
                        "warmup": False,
                    }
                ],
                "summaries": [
                    {"app": "ripgrep", "variant": "typeiso_coverage"}
                ],
                "variants": ["typeiso_coverage"],
            }
            (raw_dir / "results.json").write_text(json.dumps(result), encoding="utf-8")

            result_path = wrapper.attach_runtime_stats(raw_dir)
            attached = json.loads(result_path.read_text(encoding="utf-8"))

        self.assertEqual(attached["measurements"][0]["runtime_lifetime_stats"], stats)
        self.assertEqual(
            attached["measurements"][0]["runtime_lifetime_sites"][0]["long_outcomes"],
            4,
        )
        aggregate = attached["summaries"][0]["runtime_lifetime_stats"]
        self.assertEqual(aggregate["totals"]["adaptive_long_observations"], 4)
        self.assertFalse(
            attached["runtime_lifetime_classifier"]["semantic_epoch_markers_required"]
        )
        self.assertTrue(attached["runtime_lifetime_classifier"]["enabled"])
        self.assertTrue(
            attached["runtime_lifetime_classifier"][
                "live_observations_are_right_censored"
            ]
        )
        self.assertTrue(
            attached["runtime_lifetime_classifier"]["force_track_all_coverage_only"]
        )
        self.assertFalse(
            attached["runtime_lifetime_classifier"][
                "adaptive_sampled_rows_prevalence_claim_eligible"
            ]
        )

    def test_mode_parser_and_digest_distinguish_feature_parity_arms(self) -> None:
        mode, cleaned = wrapper.extract_runtime_mode(
            ["--runtime-lifetime-mode=off", "--raw-dir", "/tmp/evidence"]
        )
        self.assertEqual(mode, "off")
        self.assertEqual(cleaned, ["--raw-dir", "/tmp/evidence"])
        wrapper._RUNTIME_MODE = "off"
        off_digest = wrapper.runtime_implementation_digest()
        wrapper._RUNTIME_MODE = "adaptive"
        adaptive_digest = wrapper.runtime_implementation_digest()
        self.assertNotEqual(off_digest, adaptive_digest)

    def test_runtime_accounting_validation_rejects_inconsistent_evidence(self) -> None:
        stats = valid_stats(
            policy=5,
            adaptive_site_count=2,
            adaptive_cold_sites=1,
            adaptive_eligible_allocations=4,
            adaptive_training_allocations=3,
        )
        with self.assertRaises(wrapper.matrix.MatrixError):
            wrapper.validate_runtime_stats(stats, "adaptive")

        stats["adaptive_site_count"] = 1
        with self.assertRaises(wrapper.matrix.MatrixError):
            wrapper.validate_runtime_stats(stats, "adaptive")

        stats["adaptive_eligible_allocations"] = 3
        stats["adaptive_force_track_all_admitted_allocations"] = 3
        stats["adaptive_force_track_all_pressure_allocations"] = 3
        stats["adaptive_short_observations"] = 3
        stats["static_hint_abstained"] = 3
        wrapper.validate_runtime_stats(stats, "adaptive")

        stats["mapping_failures"] = 1
        with self.assertRaises(wrapper.matrix.MatrixError):
            wrapper.validate_runtime_stats(stats, "adaptive")
        stats["mapping_failures"] = 0

        stats["adaptive_short_observations"] = 2
        stats["static_hint_abstained"] = 2
        # Stats alone cannot distinguish a still-live confirmed Long from a
        # completed Long under ABI v2. Exact tracked-flow closure is deferred
        # to the site rows.
        wrapper.validate_runtime_stats(stats, "adaptive")
        incomplete = valid_site(
            requested_size=64,
            allocation_count=3,
            allocation_requested_bytes=192,
            allocation_payload_bytes=192,
            tracked_allocations=3,
            completed_outcomes=2,
            short_outcomes=2,
            short_requested_bytes=128,
            total_completed_age_bytes=64,
            maximum_completed_age_bytes=32,
            tracked_inflight=1,
            inflight_age_lower_bound_bytes=1,
        )
        with self.assertRaises(wrapper.matrix.MatrixError):
            wrapper.validate_runtime_site_rows([incomplete], stats)

        stats = valid_stats(
            policy=5,
            adaptive_site_count=1,
            adaptive_cold_sites=1,
            adaptive_eligible_allocations=1,
            adaptive_force_track_all_guard_bypasses=1,
        )
        with self.assertRaises(wrapper.matrix.MatrixError):
            wrapper.validate_runtime_stats(stats, "adaptive")

        stats["adaptive_force_track_all_guard_bypasses"] = 0
        stats["adaptive_eligible_allocations"] = 0
        stats["thp_advice_attempts"] = 1
        with self.assertRaises(wrapper.matrix.MatrixError):
            wrapper.validate_runtime_stats(stats, "adaptive")

        stats = valid_stats(
            policy=5,
            adaptive_pressure_bytes=4096,
            adaptive_force_track_all_pressure_allocations=1,
            adaptive_force_track_all_pressure_requested_bytes=8192,
        )
        with self.assertRaises(wrapper.matrix.MatrixError):
            wrapper.validate_runtime_stats(stats, "adaptive")

        stats = valid_stats(
            policy=5,
            adaptive_pressure_bytes=4096,
            adaptive_force_track_all_pressure_allocations=1,
            adaptive_force_track_all_pressure_requested_bytes=4096,
            adaptive_force_track_all_raw_pressure_allocations=2,
        )
        with self.assertRaises(wrapper.matrix.MatrixError):
            wrapper.validate_runtime_stats(stats, "adaptive")

    def test_attach_rejects_missing_coverage_telemetry(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            raw_dir = pathlib.Path(temporary)
            run_dir = raw_dir / "runs" / "fd" / "round-00" / "typeiso_coverage"
            run_dir.mkdir(parents=True)
            (run_dir / "stderr.bin").write_text("no telemetry\n", encoding="utf-8")
            (raw_dir / "results.json").write_text(
                json.dumps(
                    {
                        "measurements": [
                            {
                                "app": "fd",
                                "variant": "typeiso_coverage",
                                "round": 0,
                                "warmup": False,
                            }
                        ],
                        "summaries": [],
                    }
                ),
                encoding="utf-8",
            )

            with self.assertRaises(wrapper.matrix.MatrixError):
                wrapper.attach_runtime_stats(raw_dir)


if __name__ == "__main__":
    unittest.main()
