#!/usr/bin/env python3
"""Regression coverage for the allocator-visible RSH-060 witness."""

from __future__ import annotations

import hashlib
import json
import pathlib
import unittest


ROOT = pathlib.Path(__file__).resolve().parents[2]
CATALOG = (
    ROOT / "evaluation" / "config" / "rustsec_heap_strong_batch_c_harnesses.json"
)
SOURCE = (
    ROOT
    / "evaluation"
    / "harnesses"
    / "rustsec_heap_expansion"
    / "RSH-060"
    / "partial_panic.rs"
)
EVIDENCE = ROOT / "docs" / "evidence" / "rustsec-rsh060-partial-panic-20260714"


class Rsh060PartialPanicTests(unittest.TestCase):
    def setUp(self) -> None:
        self.catalog = json.loads(CATALOG.read_text(encoding="utf-8"))
        self.case = next(
            case for case in self.catalog["cases"] if case["case_id"] == "RSH-060"
        )
        self.scenario = next(
            scenario
            for scenario in self.case["scenarios"]
            if scenario["scenario_id"] == "RSH-060-partial-panic"
        )

    def test_catalog_registers_a_distinct_reclaim_candidate(self) -> None:
        scenario_ids = [
            scenario["scenario_id"]
            for case in self.catalog["cases"]
            for scenario in case.get("scenarios", [])
        ]
        counts = self.catalog["counts"]
        self.assertEqual(counts["repository_scenario_count"], len(scenario_ids))
        self.assertEqual(len(scenario_ids), len(set(scenario_ids)))
        self.assertEqual(
            counts["published_scenario_count"], counts["executable_case_count"]
        )
        self.assertIn("RSH-060-partial-panic", scenario_ids)
        self.assertEqual(
            self.scenario["classification_role"],
            "allocator_boundary_detection_candidate",
        )
        self.assertEqual(self.scenario["oracle"]["tool"], "asan")
        self.assertIn("double-free", self.scenario["oracle"]["vulnerable"])
        self.assertIn("panic", self.scenario["oracle"]["patched_control"])

    def test_witness_is_hash_pinned_and_preserves_the_partial_yield_edge(self) -> None:
        payload = SOURCE.read_bytes()
        self.assertEqual(
            hashlib.sha256(payload).hexdigest(), self.scenario["source_sha256"]
        )

        source = payload.decode("utf-8")
        self.assertIn("TooDee::from_vec(2, 2", source)
        self.assertIn("matrix.insert_row(0, PartialThenPanic", source)
        self.assertIn("Some(OwnedValue", source)
        self.assertIn("intentional iterator panic after one yielded item", source)
        self.assertIn("fn len(&self) -> usize", source)
        self.assertIn("2", source.split("fn len(&self) -> usize", 1)[1])

    def test_retained_matrix_establishes_feature_matched_detection(self) -> None:
        experiment = json.loads(
            (EVIDENCE / "experiment.json").read_text(encoding="utf-8")
        )
        self.assertTrue(experiment["orchestration_success"])
        self.assertEqual(experiment["unexpected_arm_count"], 0)
        self.assertEqual(experiment["repetitions_requested"], 3)

        arms = {
            (arm["archive_variant"], arm["allocator_variant"]): arm
            for arm in experiment["arms"]
        }
        baseline = arms[("vulnerable", "system")]
        plain = arms[("vulnerable", "reclaim_plain")]
        checks = arms[("vulnerable", "reclaim_checks")]
        self.assertEqual(baseline["repetition_summary"]["tool_finding_count"], 3)
        self.assertEqual(
            baseline["oracle_validation"]["tool_finding_signatures"],
            ["asan_double_free"] * 3,
        )
        self.assertEqual(
            plain["oracle_validation"]["native_diagnostic_signatures"],
            ["rust_panic_observed"] * 3,
        )
        self.assertEqual(
            checks["oracle_validation"]["native_diagnostic_signatures"],
            ["unialloc_pointer_already_released_check"] * 3,
        )
        for allocator in ("system", "reclaim_plain", "reclaim_checks"):
            patched = arms[("patched", allocator)]
            self.assertEqual(patched["repetition_summary"]["tool_finding_count"], 0)

        exported = json.loads(
            (EVIDENCE / "mechanism-results.json").read_text(encoding="utf-8")
        )
        result = next(
            row
            for row in exported["results"]
            if row["scenario_id"] == "RSH-060-partial-panic"
        )
        self.assertEqual(result["outcome"], "detected")
        self.assertEqual(
            result["reason"], "feature_matched_exact_allocator_diagnostic"
        )
        self.assertTrue(all(result["checks"].values()))


if __name__ == "__main__":
    unittest.main()
