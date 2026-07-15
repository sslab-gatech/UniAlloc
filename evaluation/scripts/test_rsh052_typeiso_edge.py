#!/usr/bin/env python3
"""Focused regressions for the derived RSH-052 Type Isolation edge."""

from __future__ import annotations

import importlib.util
import json
import pathlib
import unittest


ROOT = pathlib.Path(__file__).resolve().parents[2]
CATALOG = (
    ROOT / "evaluation" / "config" / "rustsec_heap_strong_batch_a_harnesses.json"
)
RUNNER = ROOT / "evaluation" / "scripts" / "run_rustsec_heap_harness.py"
EVIDENCE = (
    ROOT / "docs" / "evidence" / "rustsec-typeiso-automatic-20260715"
)
RAW_EXPERIMENT = (
    EVIDENCE / "raw" / "RSH-052-experiment.json"
)

spec = importlib.util.spec_from_file_location("rustsec_heap_harness", RUNNER)
assert spec is not None and spec.loader is not None
runner = importlib.util.module_from_spec(spec)
spec.loader.exec_module(runner)


class Rsh052TypeIsolationEdgeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.catalog = runner.load_catalog(CATALOG)
        cases, scenarios = runner.index_catalog(self.catalog)
        self.case = cases["RSH-052"]
        self.scenario = scenarios["RSH-052-derived-reuse"][1]

    def test_batch_registers_one_derived_reuse_edge(self) -> None:
        self.assertEqual(self.catalog["counts"]["harness_case_count"], 7)
        self.assertEqual(self.catalog["counts"]["repository_scenario_count"], 8)
        self.assertEqual(self.catalog["counts"]["published_scenario_count"], 7)
        self.assertEqual(self.catalog["counts"]["derived_reuse_scenario_count"], 1)

        self.assertEqual(self.scenario["adapter_kind"], "derived_adapter")
        self.assertEqual(
            self.scenario["classification_role"], "derived_reuse_experiment"
        )
        self.assertEqual(self.scenario["provenance"], "derived_adapter")
        self.assertEqual(self.scenario["oracle"]["tool"], "native_address_trace")

    def test_edge_is_exactly_annotated_and_hash_pinned(self) -> None:
        annotation = self.scenario["type_isolation_edge_annotation"]
        self.assertEqual(
            annotation["kind"], "manual_exact_vulnerability_edge_identity"
        )
        self.assertEqual(annotation["expected_layout"], {"size": 64, "align": 1})
        self.assertEqual(annotation["victim_type_id"], 0x5253_4834_0000_0001)
        self.assertEqual(annotation["victim_module_id"], 0x5253_4834_0000_0002)
        self.assertIn("Box<str>", " ".join(annotation["annotation_scope"]))

        runner.validate_repo_file(
            self.scenario["source_path"], self.scenario["source_sha256"]
        )
        patched = self.scenario["patched_source"]
        runner.validate_repo_file(patched["source_path"], patched["source_sha256"])

    def test_vulnerable_edge_and_same_identity_control_keep_distinct_roles(self) -> None:
        vulnerable = (ROOT / self.scenario["source_path"]).read_text(encoding="utf-8")
        patched_meta = self.scenario["patched_source"]
        patched = (ROOT / patched_meta["source_path"]).read_text(encoding="utf-8")

        self.assertIn("struct Replacement([u8; PAYLOAD_SIZE]);", vulnerable)
        self.assertIn("drop(old)", vulnerable)
        self.assertIn("report_vulnerability_edge_reuse_denial", vulnerable)
        self.assertIn("Box::new(Replacement", vulnerable)

        self.assertNotIn("struct Replacement", patched)
        self.assertIn("drop(old)", patched)
        self.assertIn("report_vulnerability_edge_reuse_denial", patched)
        self.assertIn("replacement.get_or_intern(PAYLOAD)", patched)
        self.assertIn(".resolve(replacement_symbol)", patched)
        self.assertGreaterEqual(
            patched.count("VICTIM_TYPE_ID,\n        VICTIM_MODULE_ID"), 3
        )

    def test_frozen_evidence_exports_one_bounded_mitigation(self) -> None:
        summary_path = EVIDENCE / "derived-reuse-summary.json"
        mechanism_path = EVIDENCE / "mechanism-results.json"
        summary = json.loads(summary_path.read_text(encoding="utf-8"))
        mechanism = json.loads(mechanism_path.read_text(encoding="utf-8"))

        self.assertEqual(
            summary["counts"]["validated_cross_identity_reuse_edge_count"], 12
        )
        self.assertEqual(len(summary["scenarios"]), 12)
        scenario = next(
            row for row in summary["scenarios"] if row["case_id"] == "RSH-052"
        )
        self.assertEqual(scenario["scenario_id"], "RSH-052-derived-reuse")
        self.assertTrue(scenario["edge_evaluation"]["validated"])
        self.assertFalse(
            scenario["edge_evaluation"]["vulnerability_specific_detection_signal"]
        )
        self.assertEqual(scenario["unexpected_arm_count"], 0)

        raw_record = scenario["raw_experiment"]
        self.assertEqual(raw_record["bytes"], RAW_EXPERIMENT.stat().st_size)
        self.assertEqual(raw_record["sha256"], runner.sha256_file(RAW_EXPERIMENT))

        self.assertEqual(mechanism["counts"], {"mitigated": 12})
        self.assertEqual(len(mechanism["results"]), 12)
        result = next(
            row for row in mechanism["results"] if row["case_id"] == "RSH-052"
        )
        self.assertFalse(result["claim_grade"])
        self.assertFalse(result["vulnerability_specific_detection_signal"])
        self.assertFalse(result["full_source_vulnerability_detection"])
        self.assertTrue(result["validated_mitigation"])
        self.assertNotIn("true_positive", result)
        self.assertEqual(
            result["result_semantics"],
            "causal_compiler_bound_reuse_edge_mitigation",
        )
        self.assertEqual(result["failed_checks"], [])
        self.assertTrue(all(result["checks"].values()))

        self.assertEqual(
            mechanism["derived_reuse_input"]["sha256"],
            runner.sha256_file(summary_path),
        )
if __name__ == "__main__":
    unittest.main()
