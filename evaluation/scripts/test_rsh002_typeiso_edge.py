#!/usr/bin/env python3
"""Focused contracts for the derived RSH-002 Type Isolation edge."""

from __future__ import annotations

import importlib.util
import pathlib
import unittest


ROOT = pathlib.Path(__file__).resolve().parents[2]
CATALOG = ROOT / "evaluation" / "config" / "rustsec_heap_harnesses.json"
RUNNER = ROOT / "evaluation" / "scripts" / "run_rustsec_heap_harness.py"

spec = importlib.util.spec_from_file_location("rustsec_heap_harness", RUNNER)
assert spec is not None and spec.loader is not None
runner = importlib.util.module_from_spec(spec)
spec.loader.exec_module(runner)


class Rsh002TypeIsolationEdgeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.catalog = runner.load_catalog(CATALOG)
        cases, scenarios = runner.index_catalog(self.catalog)
        self.case = cases["RSH-002"]
        self.scenario = scenarios["RSH-002-derived-reuse"][1]

    def test_default_manual_mode_has_a_structured_automatic_probe_boundary(self) -> None:
        self.assertEqual(
            self.scenario["compiler_target_crates"], ["rsh-002-harness"]
        )
        exclusion = self.scenario["compiler_target_exclusion"]
        self.assertEqual(exclusion["subject_crate"], "bitvec")
        self.assertFalse(exclusion["compiler_automatic_victim_coverage"])
        self.assertIn("Manual-mode provenance", exclusion["reason"])
        self.assertIn("automatic mode disables the manual scope", exclusion["reason"])
        self.assertIn("default manual experiment", exclusion["claim_boundary"])
        self.assertIn("automatic-edge-identity probe", exclusion["claim_boundary"])

        annotation = self.scenario["type_isolation_edge_annotation"]
        self.assertEqual(
            annotation["kind"], "manual_exact_vulnerability_edge_identity"
        )
        self.assertEqual(annotation["expected_layout"], {"size": 1016, "align": 8})
        contract = annotation["automatic_compiler_coverage_contract"]
        self.assertEqual(contract["schema_version"], 1)
        self.assertEqual(contract["coverage_scope"], "source_shaped_derived")
        self.assertEqual(
            set(contract) - {"schema_version", "coverage_scope"},
            {"victim", "replacement"},
        )

    def test_patched_control_is_safe_same_identity_bitvec_reuse(self) -> None:
        runner.validate_repo_file(
            self.scenario["source_path"], self.scenario["source_sha256"]
        )
        patched = self.scenario["patched_source"]
        patched_path = runner.validate_repo_file(
            patched["source_path"], patched["source_sha256"]
        )
        source = patched_path.read_text(encoding="utf-8")

        self.assertGreaterEqual(source.count("BitVec::with_capacity(8065)"), 2)
        self.assertIn("drop(victim)", source)
        self.assertNotIn("BitBox", source)
        self.assertNotIn("struct Replacement", source)
        self.assertNotIn("drop(stale", source)
        self.assertIn("report_vulnerability_edge_reuse_denial", source)
        self.assertGreaterEqual(
            source.count("VICTIM_TYPE_ID,\n        VICTIM_MODULE_ID"), 4
        )

        oracle = self.scenario["oracle"]
        self.assertEqual(oracle["patched_control"], "same address grooming exits 0")

    def test_catalog_includes_the_layout_diagnostic_scenario(self) -> None:
        counts = self.catalog["counts"]
        self.assertEqual(counts["harness_case_count"], 22)
        self.assertEqual(counts["repository_scenario_count"], 28)
        self.assertEqual(counts["derived_adapter_scenario_count"], 4)
        self.assertEqual(counts["derived_reuse_scenario_count"], 2)


if __name__ == "__main__":
    unittest.main()
