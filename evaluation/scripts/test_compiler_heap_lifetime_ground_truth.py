#!/usr/bin/env python3
"""Unit tests for compiler/runtime heap-lifetime ground-truth joining."""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path
import sys
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "evaluation" / "scripts" / "compiler_heap_lifetime_ground_truth.py"
spec = importlib.util.spec_from_file_location("compiler_heap_lifetime_ground_truth", SCRIPT)
assert spec is not None and spec.loader is not None
analysis = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = analysis
spec.loader.exec_module(analysis)


def runtime_row(
    callsite: int,
    *,
    requested_size: int = 64,
    align: int = 8,
    short: int = 0,
    long: int = 0,
    indeterminate: int = 0,
    inflight: int = 0,
    bypassed: int = 0,
    minimum_payload_capacity: int | None = None,
    maximum_payload_capacity: int | None = None,
    allocation_payload_bytes: int | None = None,
    module_id: int = 3000,
) -> dict[str, object]:
    tracked = short + long + indeterminate + inflight
    completed = short + long + indeterminate
    allocations = tracked + bypassed
    if minimum_payload_capacity is None:
        minimum_payload_capacity = requested_size
    if maximum_payload_capacity is None:
        maximum_payload_capacity = minimum_payload_capacity
    allocation_payload_bytes = (
        allocation_payload_bytes
        if allocation_payload_bytes is not None
        else allocations * minimum_payload_capacity
    )
    return {
        "callsite": callsite,
        "type_id": 2000 + callsite,
        "module_id": module_id,
        "requested_size": requested_size,
        "align": align,
        "minimum_payload_capacity": minimum_payload_capacity,
        "maximum_payload_capacity": maximum_payload_capacity,
        "allocation_count": allocations,
        "allocation_requested_bytes": allocations * requested_size,
        "allocation_payload_bytes": allocation_payload_bytes,
        "tracked_allocations": tracked,
        "bypassed_allocations": bypassed,
        "completed_outcomes": completed,
        "short_outcomes": short,
        "long_outcomes": long,
        "censored_outcomes": indeterminate,
        "short_requested_bytes": short * requested_size,
        "long_requested_bytes": long * requested_size,
        "censored_requested_bytes": indeterminate * requested_size,
        "total_completed_age_bytes": completed * 100,
        "maximum_completed_age_bytes": 100 if completed else 0,
        "tracked_inflight": inflight,
        "inflight_age_lower_bound_bytes": inflight * 50,
        "first_allocation_pressure": 10 if allocations else 0,
        "last_allocation_pressure": 110 if allocations else 0,
        "predictor_key_ambiguous": False,
        "predictor_flags_seen": 1,
        "predictor_placement_hint_bits_union": 0,
        "latest_prediction": 0,
        "latest_static_prior": 0,
    }


def compiler_features(
    *,
    backedge: bool,
    requested_size: int | None = None,
    requested_align: int | None = None,
) -> dict[str, object]:
    record: dict[str, object] = {
        "analysis_phase": "post_borrowck_pre_optimization",
        "schema_version": 1,
        "analysis_only": True,
        "classification_rule_applied": False,
        "owner_place": "_1",
        "owner_place_basis": "semantic_scope_destination",
        "runtime_join_key_complete": (
            requested_size is not None and requested_align is not None
        ),
        "runtime_join_key": {
            "callsite": None,
            "type_id": None,
            "module_id": None,
            "requested_size_bytes": requested_size,
            "requested_align_bytes": requested_align,
        },
        "requested_layout_basis": (
            "rustc_exact_box_layout" if requested_size is not None else "dynamic_unknown"
        ),
        "owner_move_count": 0,
        "return_sink": False,
        "escape_sink": False,
        "store_sink": False,
        "allocation_in_natural_loop": backedge,
        "reachable_backedge_after_allocation": backedge,
        "function_has_yield_or_await": False,
        "reachable_yield_or_await": False,
        "receiver_owned_allocation": False,
        "exact_drop_path": not backedge,
        "conditional_drop_path": False,
        "cleanup_drop_path": False,
        "normal_drop_blocks": ["bb2"] if not backedge else [],
        "cleanup_drop_blocks": [],
        "return_sink_blocks": [],
        "escape_sink_blocks": [],
        "store_sink_blocks": [],
        "reachable_normal_blocks": ["bb0", "bb1"],
        "reachable_cleanup_blocks": [],
        "normal_successor_count": 2,
        "cleanup_successor_count": 0,
    }
    return record


def compiler_row(
    callsite: int,
    *,
    backedge: bool,
    requested_size: int | None = None,
    requested_align: int | None = None,
    function: str | None = None,
) -> dict[str, object]:
    features = compiler_features(
        backedge=backedge,
        requested_size=requested_size,
        requested_align=requested_align,
    )
    features["runtime_join_key"] = {
        "callsite": callsite,
        "type_id": 2000 + callsite,
        "module_id": 3000,
        "requested_size_bytes": requested_size,
        "requested_align_bytes": requested_align,
    }
    return {
        "lowering_kind": "semantic_scope_enter_exit_rewrite",
        "callsite": callsite,
        "type_id": 2000 + callsite,
        "module_id": 3000,
        "mir_function": function or f"fixture::{callsite}",
        "destination_type": "alloc::boxed::Box<[u8]>",
        "callee": "alloc::boxed::Box::new",
        "lifetime_hint": 0,
        "lifetime_hint_confidence": 0,
        "lifetime_hint_basis": "automatic_heap_missing_terminal_unknown",
        "lifetime_analysis_features": features,
    }


def write_runtime(
    path: Path,
    rows: list[dict[str, object]],
    *,
    truncated: bool = False,
    force_track_all: bool = False,
) -> None:
    envelope = {
        "source": "unialloc-lifetime-adaptive-site-snapshot-v1",
        "abi_version": 1,
        "required_rows": len(rows) + int(truncated),
        "captured_rows": len(rows),
        "truncated": truncated,
        "rows": rows,
    }
    if not force_track_all:
        path.write_text(json.dumps(envelope), encoding="utf-8")
        return
    stats = {
        "adaptive_force_track_all": True,
        "adaptive_force_track_all_maximum_allocations": 1_000_000,
        "adaptive_force_track_all_maximum_requested_bytes": 4 * 1024**3,
        "adaptive_force_track_all_admitted_allocations": sum(
            int(row["tracked_allocations"]) for row in rows
        ),
        "adaptive_force_track_all_admitted_requested_bytes": sum(
            int(row["allocation_requested_bytes"]) for row in rows
        ),
        "adaptive_force_track_all_guard_bypasses": 0,
        "adaptive_force_track_all_pressure_allocations": sum(
            int(row["allocation_count"]) for row in rows
        ),
        "adaptive_force_track_all_pressure_requested_bytes": sum(
            int(row["allocation_requested_bytes"]) for row in rows
        ),
        "adaptive_force_track_all_raw_pressure_allocations": 0,
        "adaptive_force_track_all_raw_pressure_requested_bytes": 0,
        "adaptive_force_track_all_raw_reallocation_pressure_allocations": 0,
        "adaptive_force_track_all_raw_reallocation_pressure_requested_bytes": 0,
        "adaptive_pressure_bytes": sum(
            int(row["allocation_requested_bytes"]) for row in rows
        ),
        "adaptive_cold_bypassed_allocations": 0,
        "adaptive_short_bypassed_allocations": 0,
        "adaptive_site_table_bypasses": 0,
        "adaptive_observation_table_bypasses": 0,
        "adaptive_observation_site_count": len(rows),
        "adaptive_eligible_allocations": sum(
            int(row["allocation_count"]) for row in rows
        ),
        "thp_extent_mappings": 0,
        "thp_advice_attempts": 0,
        "thp_advice_successes": 0,
        "thp_advice_errors": 0,
        "nohugepage_advice_failures": 0,
        "mapping_failures": 0,
    }
    path.write_text(
        "\n".join(
            (
                analysis.RUNTIME_STATS_PREFIX + json.dumps(stats),
                analysis.RUNTIME_PREFIX + json.dumps(envelope),
            )
        )
        + "\n",
        encoding="utf-8",
    )


def write_audit(path: Path, rows: list[dict[str, object]]) -> None:
    path.write_text(json.dumps({"rewrite_candidates": rows}), encoding="utf-8")


class CompilerHeapLifetimeGroundTruthTests(unittest.TestCase):
    def test_runtime_validation_keeps_indeterminate_and_live_censoring_separate(self) -> None:
        row = runtime_row(1, short=2, long=3, indeterminate=4, inflight=5, bypassed=6)
        merged = analysis.merge_runtime_rows([row])
        aggregate = next(iter(merged.values()))

        self.assertEqual(aggregate["long_outcomes"], 3)
        self.assertEqual(aggregate["censored_outcomes"], 4)
        self.assertEqual(aggregate["tracked_inflight"], 5)
        self.assertEqual(aggregate["bypassed_allocations"], 6)

        broken = dict(row)
        broken["completed_outcomes"] = 10
        with self.assertRaises(analysis.GroundTruthError):
            analysis.validate_runtime_row(broken)

    def test_runtime_validation_accepts_mixed_payload_capacities_within_bounds(self) -> None:
        row = runtime_row(
            1,
            long=2,
            minimum_payload_capacity=64,
            maximum_payload_capacity=128,
            allocation_payload_bytes=192,
        )
        validated = analysis.validate_runtime_row(row)
        self.assertEqual(validated["minimum_payload_capacity"], 64)
        self.assertEqual(validated["maximum_payload_capacity"], 128)

        broken = dict(row)
        broken["allocation_payload_bytes"] = 257
        with self.assertRaises(analysis.GroundTruthError):
            analysis.validate_runtime_row(broken)

        unknown_module = analysis.validate_runtime_row(runtime_row(2, long=1, module_id=0))
        self.assertEqual(unknown_module["key"].module_id, 0)

    def test_static_identity_expands_to_exact_runtime_sizes(self) -> None:
        runtime = analysis.merge_runtime_rows(
            [
                runtime_row(1, requested_size=64, long=4),
                runtime_row(1, requested_size=128, long=4),
            ]
        )
        compiler_record = analysis.compiler_record(
            compiler_row(1, backedge=True)
        )
        summary = analysis.application_summary(
            "ripgrep",
            runtime,
            {compiler_record["key"]: compiler_record},
            minimum_decisive_outcomes=1,
            minimum_long_byte_share=0.8,
            minimum_feature_sites=1,
        )

        self.assertEqual(summary["runtime_exact_site_count"], 2)
        self.assertEqual(summary["matched_runtime_exact_site_count"], 2)
        self.assertEqual(summary["exact_five_field_match_count"], 0)
        self.assertEqual(summary["compiler_layout_abstention_runtime_site_count"], 2)
        self.assertFalse(summary["label_collection"]["unbiased_prevalence_claim_eligible"])

    def test_compiler_exact_layout_mismatch_fails_closed(self) -> None:
        runtime = analysis.merge_runtime_rows([runtime_row(1, requested_size=128, long=1)])
        compiler_record = analysis.compiler_record(
            compiler_row(
                1,
                backedge=True,
                requested_size=64,
                requested_align=8,
            )
        )
        with self.assertRaises(analysis.GroundTruthError):
            analysis.application_summary(
                "ripgrep",
                runtime,
                {compiler_record["key"]: compiler_record},
                minimum_decisive_outcomes=1,
                minimum_long_byte_share=0.8,
                minimum_feature_sites=1,
            )

    def test_duplicate_compiler_identity_with_conflicting_features_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            write_audit(root / "one.json", [compiler_row(1, backedge=True)])
            write_audit(root / "two.json", [compiler_row(1, backedge=False)])
            with self.assertRaises(analysis.GroundTruthError):
                analysis.load_compiler_audits([root])

    def test_long_ranking_and_feature_correlation_use_decisive_bytes_only(self) -> None:
        runtime = analysis.merge_runtime_rows(
            [
                runtime_row(1, long=8, indeterminate=20, inflight=30, bypassed=2),
                runtime_row(2, short=8),
            ]
        )
        compiler = {}
        for row in (
            compiler_row(1, backedge=True),
            compiler_row(2, backedge=False),
        ):
            record = analysis.compiler_record(row)
            compiler[record["key"]] = record
        summary = analysis.application_summary(
            "ripgrep",
            runtime,
            compiler,
            minimum_decisive_outcomes=3,
            minimum_long_byte_share=0.8,
            minimum_feature_sites=1,
        )

        self.assertEqual(len(summary["long_dominant_candidates"]), 1)
        correlation = next(
            row
            for row in summary["feature_correlations"]
            if row["feature"] == "reachable_backedge_after_allocation"
        )
        self.assertEqual(correlation["long_share_difference_percentage_points"], 100.0)
        self.assertTrue(correlation["selection_conditioned"])
        self.assertFalse(summary["label_collection"]["unbiased_prevalence_claim_eligible"])
        candidate = summary["long_dominant_candidates"][0]
        self.assertEqual(candidate["derived"]["completed_indeterminate_outcomes"], 20)
        self.assertEqual(candidate["derived"]["live_right_censored_objects"], 30)

    def test_manifest_pipeline_requires_complete_snapshots_and_renders_boundaries(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            applications = []
            for index, app in enumerate(("ripgrep", "fd", "oxipng"), 1):
                runtime_path = root / f"{app}-runtime.json"
                audit_path = root / f"{app}-audit.json"
                write_runtime(
                    runtime_path,
                    [runtime_row(index, long=4)],
                    force_track_all=True,
                )
                write_audit(audit_path, [compiler_row(index, backedge=True)])
                applications.append(
                    {
                        "app": app,
                        "runtime_site_snapshot_files": [runtime_path.name],
                        "compiler_audit_roots": [audit_path.name],
                    }
                )
            manifest = root / "manifest.json"
            manifest.write_text(
                json.dumps({"schema_version": 1, "applications": applications}),
                encoding="utf-8",
            )
            summary = analysis.analyze_manifest(
                manifest,
                required_apps={"ripgrep", "fd", "oxipng"},
                minimum_decisive_outcomes=1,
                minimum_long_byte_share=0.8,
                minimum_feature_sites=1,
            )

            markdown = analysis.render_markdown(summary)
            self.assertEqual(summary["aggregate"]["runtime_exact_site_count"], 3)
            self.assertEqual(
                summary["heuristic_promotion_decision"]["status"],
                "CANDIDATES_REQUIRE_HELD_OUT_VALIDATION",
            )
            self.assertFalse(
                summary["heuristic_promotion_decision"][
                    "production_static_rule_promoted"
                ]
            )
            self.assertTrue(
                summary["aggregate"]["label_collection"][
                    "unbiased_prevalence_claim_eligible"
                ]
            )
            self.assertEqual(
                summary["external_baseline_gate"]["google_tcmalloc_temeraire_hpaa"],
                "not_measured_in_this_harness_requires_separate_verified_"
                "fixed_revision_official_bazel_arm",
            )
            self.assertIn("Access hotness is unavailable", markdown)
            self.assertIn("Exact Drop establishes a scoped owner path", markdown)

            write_runtime(root / "ripgrep-runtime.json", [runtime_row(1, long=4)], truncated=True)
            with self.assertRaises(analysis.GroundTruthError):
                analysis.analyze_manifest(
                    manifest,
                    required_apps={"ripgrep", "fd", "oxipng"},
                    minimum_decisive_outcomes=1,
                    minimum_long_byte_share=0.8,
                    minimum_feature_sites=1,
                )

    def test_empty_candidate_set_records_an_explicit_abstention_decision(self) -> None:
        aggregate = {
            "long_dominant_candidates": [],
        }
        decision = analysis.heuristic_promotion_decision(
            aggregate,
            minimum_decisive_outcomes=3,
            minimum_long_byte_share=0.8,
        )
        self.assertEqual(decision["status"], "ABSTAIN")
        self.assertFalse(decision["production_static_rule_promoted"])
        self.assertEqual(decision["eligible_training_candidate_count"], 0)
        self.assertEqual(
            decision["safe_runtime_action"],
            "preserve Unknown placement for unvalidated sites",
        )


if __name__ == "__main__":
    unittest.main()
