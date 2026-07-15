#!/usr/bin/env python3

from __future__ import annotations

import hashlib
import json
import unittest
from pathlib import Path


REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
SUITE_PATH = (
    REPOSITORY_ROOT / "evaluation" / "config" / "type_isolation_primary_suite.json"
)
READINESS_PATH = (
    REPOSITORY_ROOT
    / "docs"
    / "evidence"
    / "type-isolation-primary-suite-readiness-20260714.json"
)
TARGET_ORDER = (
    "collections",
    "oxipng",
    "redb",
    "polars",
    "swc",
    "rustpython",
    "actix_web",
)
IMPLEMENTATION_REVISION = "f5d0c19c1cc5b56fdac3282d69333dd8c85d4cf2"
IMPLEMENTATION_SHA256 = (
    "cab1e580c08e2b16308bae75501716049ba428b040fb04bf305269c9ba9eaf01"
)
RSS_WORK_MODELS = {
    "collections": "workload_native_adaptive_iterations",
    "oxipng": "fixed_work",
    "redb": "fixed_work",
    "polars": "fixed_work",
    "swc": "workload_native_adaptive_iterations",
    "rustpython": "workload_native_adaptive_iterations",
    "actix_web": "workload_native_adaptive_iterations",
}
PRE_AMENDMENT_SUITE_SHA256 = (
    "96fa64009550d52f89549dd2124e3b1a402a42b7f1fd1750888600dd67bb8a6f"
)
ANALYSIS_SUITE_SHA256 = (
    "ba386a779e45e0884e02cf8825c69985d1b5cf68d57366af78e7f77cfa8f6ccb"
)
PRE_AMENDMENT_SUITE_PATH = (
    REPOSITORY_ROOT / "docs/evidence/type-isolation-primary-suite-20260714/"
    "pre-amendment-suite-manifest-96fa640.json"
)
IMPLEMENTATION_SNAPSHOT_PATH = (
    REPOSITORY_ROOT / "docs/evidence/type-isolation-primary-suite-20260714/"
    "canonical-implementation-snapshot-f5d0c19.json"
)


class TypeIsolationPrimarySuiteTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.suite = json.loads(SUITE_PATH.read_text(encoding="utf-8"))
        cls.readiness = json.loads(READINESS_PATH.read_text(encoding="utf-8"))

    def test_target_and_variant_contract_is_fixed(self) -> None:
        self.assertEqual("type-isolation-primary-v1", self.suite["suite_id"])
        self.assertEqual("analysis_contract_frozen", self.suite["status"])
        self.assertEqual(
            ["unialloc", "typed_plain", "typeiso_perf"],
            self.suite["variant_order"],
        )
        self.assertEqual(
            list(TARGET_ORDER), [target["id"] for target in self.suite["targets"]]
        )
        self.assertNotIn("fd", TARGET_ORDER)

    def test_allocator_and_pass_implementation_is_exactly_frozen(self) -> None:
        implementation = self.suite["implementation"]
        self.assertEqual(IMPLEMENTATION_REVISION, implementation["git_revision"])
        self.assertEqual(IMPLEMENTATION_SHA256, implementation["canonical_sha256"])
        self.assertEqual(131, implementation["canonical_file_count"])
        self.assertEqual(4_426_670, implementation["canonical_size_bytes"])
        self.assertEqual(
            ["unialloc", "alloc_macros"], implementation["canonical_roots"]
        )
        self.assertEqual(
            ["tools/unialloc-rustc-pass/unialloc-rustc-mir-rewrite-dry-run.rs"],
            implementation["canonical_explicit_files"],
        )

    def test_canonical_snapshot_keeps_capture_audit_scope_explicit(self) -> None:
        snapshot = json.loads(IMPLEMENTATION_SNAPSHOT_PATH.read_text(encoding="utf-8"))
        self.assertEqual("campaign_source_snapshot", snapshot["status"])
        self.assertEqual(
            ["collections", "oxipng"], snapshot["applies_to_measured_targets"]
        )
        self.assertIn("only the Collections and Oxipng", snapshot["scope_note"])
        self.assertEqual(
            IMPLEMENTATION_SHA256, snapshot["implementation_digest"]["value"]
        )

    def test_each_target_has_four_or_five_unique_harnesses(self) -> None:
        for target in self.suite["targets"]:
            with self.subTest(target=target["id"]):
                harnesses = target["harnesses"]
                ids = [harness["id"] for harness in harnesses]
                self.assertGreaterEqual(len(harnesses), 4)
                self.assertLessEqual(len(harnesses), 5)
                self.assertEqual(len(ids), len(set(ids)))
                self.assertTrue(
                    all(
                        harness["metric_direction"]
                        in {"lower_is_better", "higher_is_better"}
                        for harness in harnesses
                    )
                )
                self.assertGreaterEqual(
                    sum(
                        harness["aggregate_role"] == "primary" for harness in harnesses
                    ),
                    4,
                )

    def test_rss_work_model_is_explicit_for_every_target(self) -> None:
        self.assertEqual(
            RSS_WORK_MODELS,
            {
                target["id"]: target["rss_work_model"]
                for target in self.suite["targets"]
            },
        )
        self.assertTrue(
            all(
                target["readiness"] == "exact_f5d0c19_campaign_frozen"
                for target in self.suite["targets"]
            )
        )
        self.assertTrue(
            all(
                harness["performance_unit"] and harness["performance_source"]
                for target in self.suite["targets"]
                for harness in target["harnesses"]
            )
        )

    def test_rss_interpretation_amendment_preserves_preregistration(self) -> None:
        amendment = self.suite["analysis_amendments"][0]
        self.assertEqual(
            PRE_AMENDMENT_SUITE_SHA256,
            amendment["original_campaign_manifest_sha256"],
        )
        self.assertFalse(amendment["benchmark_definitions_changed"])
        self.assertFalse(amendment["sampling_budget_changed"])
        self.assertEqual("fixed_work", amendment["core_comparable_rss_work_model"])
        self.assertEqual(
            "diagnostic_only", amendment["adaptive_rss_comparison_eligibility"]
        )
        self.assertEqual(
            PRE_AMENDMENT_SUITE_SHA256,
            hashlib.sha256(PRE_AMENDMENT_SUITE_PATH.read_bytes()).hexdigest(),
        )

    def test_comparison_and_aggregation_contracts_remain_separate(self) -> None:
        families = {
            family["id"]: (family["subject"], family["reference"])
            for family in self.suite["comparison_families"]
        }
        self.assertEqual(
            {
                "compiler_route": ("typed_plain", "unialloc"),
                "policy_increment": ("typeiso_perf", "typed_plain"),
                "end_to_end": ("typeiso_perf", "unialloc"),
            },
            families,
        )
        aggregation = self.suite["aggregation"]
        self.assertIn("equal-weight", aggregation["target"])
        self.assertIn("equal-weight", aggregation["suite"])
        self.assertIn("discard the first", aggregation["paper_reproduction"])

    def test_compiler_route_equivalence_margin_is_predeclared(self) -> None:
        gate = self.suite["compiler_route_equivalence_gate"]
        self.assertEqual("paired_execution_cost_ratio", gate["metric"])
        self.assertEqual("typed_plain", gate["subject"])
        self.assertEqual("unialloc", gate["reference"])
        self.assertEqual(0.85, gate["minimum_ratio"])
        self.assertEqual(1.15, gate["maximum_ratio"])
        self.assertEqual("harness", gate["failure_scope"])
        self.assertEqual("attribution_classification", gate["role"])
        self.assertEqual("retain_measurements", gate["on_failure"])
        self.assertNotIn(
            "typed_plain passes the predeclared compiler-route equivalence margin",
            self.suite["data_eligibility_gates"],
        )

    def test_overview_and_target_detail_figure_contracts_are_explicit(self) -> None:
        contract = self.suite["figure_contract"]
        self.assertEqual({"overview", "target_detail"}, set(contract))
        self.assertEqual("rows", contract["overview"]["harness_axis"])
        self.assertIn("ratio to UniAlloc", contract["overview"]["performance_panel"])
        self.assertIn("ratio to UniAlloc", contract["overview"]["rss_panel"])
        self.assertEqual("rows", contract["target_detail"]["harness_axis"])
        self.assertIn("absolute peak MiB", contract["target_detail"]["rss_panel"])

    def test_committee_overview_predeclares_two_harnesses_per_target(self) -> None:
        for target in self.suite["targets"]:
            with self.subTest(target=target["id"]):
                selected = sorted(
                    harness["overview_rank"]
                    for harness in target["harnesses"]
                    if "overview_rank" in harness
                )
                self.assertEqual([1, 2], selected)
                self.assertTrue(
                    all(
                        harness.get("overview_label")
                        for harness in target["harnesses"]
                        if "overview_rank" in harness
                    )
                )

    def test_overview_contract_is_title_free_horizontal_bars(self) -> None:
        overview = self.suite["figure_contract"]["overview"]
        self.assertEqual("grouped horizontal median bars", overview["encoding"])
        self.assertEqual("log2 cost ratio centered at 1x", overview["ratio_axis"])
        self.assertEqual(2, overview["selected_harnesses_per_target"])
        self.assertEqual(
            {
                "title": False,
                "subtitle": False,
                "panel_title": False,
                "footer": False,
            },
            overview["canvas_text"],
        )

    def test_fd_is_retained_only_as_a_diagnostic_exclusion(self) -> None:
        exclusions = self.suite["diagnostic_exclusions"]
        self.assertEqual(["fd"], [entry["target"] for entry in exclusions])
        self.assertEqual("compiler_path_diagnostic", exclusions[0]["cohort"])
        serialized_targets = json.dumps(self.suite["targets"])
        self.assertNotIn('"fd"', serialized_targets)

    def test_existing_runner_paths_resolve(self) -> None:
        for target in self.suite["targets"]:
            runner = target["runner"]
            if runner is None:
                continue
            with self.subTest(target=target["id"]):
                self.assertTrue((REPOSITORY_ROOT / runner).is_file(), runner)

    def test_readiness_audit_records_the_completed_measurement_boundary(self) -> None:
        report = self.readiness
        self.assertEqual(self.suite["suite_id"], report["suite_id"])
        self.assertEqual(7, report["required_target_count"])
        self.assertEqual(7, report["eligible_target_count"])
        self.assertEqual(34, report["harness_count"])
        self.assertEqual(102, report["warmup_process_count"])
        self.assertEqual(510, report["measured_process_count"])
        self.assertEqual(
            "presentation_eligible_with_attribution_limits", report["status"]
        )
        self.assertEqual("complete_with_attribution_limits", report["result"]["status"])
        self.assertEqual(
            list(TARGET_ORDER), [target["id"] for target in report["targets"]]
        )
        self.assertTrue(all(target["eligible"] is True for target in report["targets"]))
        self.assertEqual(14, report["figure_bundle"]["selected_overview_harness_count"])
        self.assertEqual(ANALYSIS_SUITE_SHA256, report["suite_manifest"]["sha256"])
        self.assertEqual(
            hashlib.sha256(SUITE_PATH.read_bytes()).hexdigest(),
            report["suite_manifest"]["sha256"],
        )


if __name__ == "__main__":
    unittest.main()
