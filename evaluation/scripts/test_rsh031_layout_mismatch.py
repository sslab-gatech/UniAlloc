#!/usr/bin/env python3
"""Focused contract tests for the RSH-031 recovery-layout diagnostic."""

from __future__ import annotations

import hashlib
import importlib.util
import json
import pathlib
import unittest


ROOT = pathlib.Path(__file__).resolve().parents[2]
HARNESS_DIR = ROOT / "evaluation" / "harnesses" / "rustsec_heap" / "RSH-031"
SCENARIO_PATH = HARNESS_DIR / "derived_layout_scenario.json"
CATALOG_PATH = ROOT / "evaluation" / "config" / "rustsec_heap_harnesses.json"
RUNTIME_PATH = ROOT / "unialloc" / "src" / "alloc_api" / "type_isolation.rs"
CACHE_PATH = ROOT / "unialloc" / "src" / "cache" / "mod.rs"
ALLOC_API_PATH = ROOT / "unialloc" / "src" / "alloc_api" / "mod.rs"
LIB_PATH = ROOT / "unialloc" / "src" / "lib.rs"
RUNNER_PATH = ROOT / "evaluation" / "scripts" / "run_rsh031_layout_mismatch.py"
BASELINE_PATH = (
    ROOT
    / "evaluation"
    / "raw"
    / "rustsec-reclaim-checks-base-20260714"
    / "RSH-031-upstream"
    / "experiment.json"
)
BUNDLE_DIR = ROOT / "docs" / "evidence" / "rustsec-rsh031-layout-20260715"

RUNNER_SPEC = importlib.util.spec_from_file_location("run_rsh031_layout_mismatch", RUNNER_PATH)
assert RUNNER_SPEC is not None and RUNNER_SPEC.loader is not None
RUNNER = importlib.util.module_from_spec(RUNNER_SPEC)
RUNNER_SPEC.loader.exec_module(RUNNER)


def sha256(path: pathlib.Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


class Rsh031LayoutMismatchTests(unittest.TestCase):
    def test_durable_bundle_binds_treatment_and_miri_ground_truth(self) -> None:
        experiment_path = BUNDLE_DIR / "experiment.json"
        mechanism_path = BUNDLE_DIR / "mechanism-results.json"
        ground_truth_path = BUNDLE_DIR / "upstream-miri-experiment.json"
        experiment = json.loads(experiment_path.read_text(encoding="utf-8"))
        mechanism = json.loads(mechanism_path.read_text(encoding="utf-8"))
        result = mechanism["results"][0]

        self.assertTrue(experiment["checks"]["valid"])
        self.assertEqual(mechanism["counts"]["true_positive"], 1)
        self.assertEqual(result["mechanism"], "recovery_layout_validation")
        self.assertEqual(result["outcome"], "detected")
        self.assertTrue(result["true_positive"])
        for field, path in (
            ("evidence", experiment_path),
            ("ground_truth_evidence", ground_truth_path),
        ):
            self.assertEqual(result[field]["sha256"], sha256(path))
            self.assertEqual(result[field]["bytes"], path.stat().st_size)

    def test_runner_validates_pinned_miri_source_controls(self) -> None:
        validation = RUNNER.upstream_miri_ground_truth(BASELINE_PATH, 3)
        self.assertEqual(
            validation,
            {
                "valid": True,
                "reason": "matched_miri_source_controls",
                "vulnerable_finding_count": 3,
                "patched_clean_exit_count": 3,
            },
        )

    def test_derived_pair_is_hash_bound_and_policy_independent(self) -> None:
        scenario = json.loads(SCENARIO_PATH.read_text(encoding="utf-8"))
        vulnerable = ROOT / scenario["source_path"]
        patched = ROOT / scenario["patched_source"]["source_path"]

        self.assertEqual(scenario["case_id"], "RSH-031")
        self.assertEqual(scenario["advisory_id"], "RUSTSEC-2023-0017")
        self.assertEqual(scenario["mechanism"], "recovery_layout_validation")
        self.assertEqual(scenario["mechanism_class"], "other_allocator_feature")
        self.assertEqual(scenario["expected_variants"], ["typed_plain", "typeiso"])
        self.assertEqual(sha256(vulnerable), scenario["source_sha256"])
        self.assertEqual(sha256(patched), scenario["patched_source"]["source_sha256"])
        self.assertEqual(
            scenario["expected_vulnerable_signal"],
            {
                "recovery_deallocation_layout_mismatches": 1,
                "last_requested_size": 1009,
                "last_requested_align": 1,
                "last_recorded_size": 1009,
                "last_recorded_align": 256,
            },
        )
        self.assertEqual(
            scenario["expected_patched_signal"],
            {"recovery_deallocation_layout_mismatches": 0},
        )

    def test_upstream_miri_and_patched_control_supply_source_ground_truth(self) -> None:
        catalog = json.loads(CATALOG_PATH.read_text(encoding="utf-8"))
        case = next(case for case in catalog["cases"] if case["case_id"] == "RSH-031")
        scenario = next(
            row for row in case["scenarios"] if row["scenario_id"] == "RSH-031-upstream"
        )

        self.assertEqual(scenario["oracle"]["tool"], "miri")
        self.assertIn("layout mismatch", scenario["oracle"]["vulnerable"])
        self.assertEqual(
            sha256(ROOT / scenario["source_path"]), scenario["source_sha256"]
        )
        self.assertEqual(
            sha256(ROOT / scenario["patched_source"]["source_path"]),
            scenario["patched_source"]["source_sha256"],
        )

    def test_derived_layout_validation_is_registered_in_the_case_catalog(self) -> None:
        catalog = json.loads(CATALOG_PATH.read_text(encoding="utf-8"))
        case = next(case for case in catalog["cases"] if case["case_id"] == "RSH-031")
        registered = next(
            row
            for row in case["scenarios"]
            if row["scenario_id"] == "RSH-031-derived-layout-validation"
        )
        descriptor = json.loads(SCENARIO_PATH.read_text(encoding="utf-8"))

        self.assertEqual(registered["classification_role"], descriptor["classification_role"])
        self.assertEqual(registered["mechanism"], "recovery_layout_validation")
        self.assertEqual(registered["source_path"], descriptor["source_path"])
        self.assertEqual(registered["source_sha256"], descriptor["source_sha256"])
        self.assertEqual(registered["patched_source"], descriptor["patched_source"])

    def test_runtime_records_only_fail_closed_deallocation_layout_mismatches(self) -> None:
        runtime = RUNTIME_PATH.read_text(encoding="utf-8")
        cache = CACHE_PATH.read_text(encoding="utf-8")
        alloc_api = ALLOC_API_PATH.read_text(encoding="utf-8")
        lib = LIB_PATH.read_text(encoding="utf-8")

        self.assertIn("SemanticAllocationLayoutValidationSnapshot", runtime)
        self.assertIn("semantic_allocation_layout_validation_snapshot", runtime)
        self.assertIn("mismatch.is_layout_mismatch(requested)", runtime)
        self.assertIn(
            "record_recovery_deallocation_layout_mismatch(layout, mismatch);", runtime
        )
        self.assertEqual(
            cache.count("record_recovery_deallocation_layout_mismatch(layout, mismatch);"),
            2,
        )
        self.assertIn("semantic_allocation_layout_validation_snapshot", alloc_api)
        self.assertIn("semantic_allocation_layout_validation_snapshot", lib)
        self.assertIn(
            "rsh031_recovery_layout_mismatch_is_exact_bound_and_policy_independent",
            runtime,
        )

    def test_vulnerable_and_patched_sources_use_the_same_record_identity(self) -> None:
        vulnerable = (HARNESS_DIR / "derived_layout_vulnerable.rs").read_text(
            encoding="utf-8"
        )
        patched = (HARNESS_DIR / "derived_layout_patched.rs").read_text(
            encoding="utf-8"
        )
        for identity in (
            "0x5253_4830_3331_0001",
            "0x5253_4830_3331_0002",
            "0x5253_4830_3331_0003",
        ):
            self.assertIn(identity, vulnerable)
            self.assertIn(identity, patched)
        self.assertIn("Vec::<u8>::from_raw_parts(ptr, 0, SIZE)", vulnerable)
        self.assertIn("const DEALLOCATION_ALIGN: usize = 1;", vulnerable)
        self.assertIn("const ALLOCATION_ALIGN: usize = 256;", vulnerable)
        self.assertIn("const ALIGN: usize = 256;", patched)


if __name__ == "__main__":
    unittest.main()
