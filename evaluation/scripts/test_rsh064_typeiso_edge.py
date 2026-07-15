#!/usr/bin/env python3
"""Focused regressions for the derived RSH-064 Type Isolation edge."""

from __future__ import annotations

import importlib.util
import json
import pathlib
import unittest


ROOT = pathlib.Path(__file__).resolve().parents[2]
RUNNER = ROOT / "evaluation" / "scripts" / "run_rsh064_neon_witness.py"
CATALOG = (
    ROOT / "evaluation" / "config" / "rustsec_heap_neon_node_harnesses.json"
)
EVIDENCE = (
    ROOT / "docs" / "evidence" / "rustsec-typeiso-automatic-20260715"
)
EXPORTER = ROOT / "evaluation" / "scripts" / "export_rustsec_mechanism_results.py"

spec = importlib.util.spec_from_file_location("rsh064_neon_runner", RUNNER)
assert spec is not None and spec.loader is not None
runner = importlib.util.module_from_spec(spec)
spec.loader.exec_module(runner)

exporter_spec = importlib.util.spec_from_file_location(
    "rustsec_mechanism_exporter", EXPORTER
)
assert exporter_spec is not None and exporter_spec.loader is not None
exporter = importlib.util.module_from_spec(exporter_spec)
exporter_spec.loader.exec_module(exporter)


class Rsh064TypeIsolationEdgeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.catalog = runner.load_catalog(CATALOG)
        _cases, scenarios = runner.harness.index_catalog(self.catalog)
        self.scenario = scenarios["RSH-064-derived-reuse"][1]

    def test_automatic_edge_uses_concrete_victim_scope_and_safe_control(self) -> None:
        vulnerable = (ROOT / self.scenario["source_path"]).read_text(
            encoding="utf-8"
        )
        patched_meta = self.scenario["patched_source"]
        patched = (ROOT / patched_meta["source_path"]).read_text(encoding="utf-8")

        self.assertIn("fn materialize_victim(seed: &[u8; PAYLOAD_SIZE])", vulnerable)
        self.assertIn("fn reclaim_victim<T>", vulnerable)
        self.assertIn("black_box(materialize)", vulnerable)
        self.assertIn("black_box(reclaim)", vulnerable)
        self.assertIn("ExternalView", vulnerable)
        self.assertIn("Box::new(Replacement", vulnerable)

        self.assertIn("fn materialize_payload(seed: &[u8; PAYLOAD_SIZE])", patched)
        self.assertIn("fn reclaim_payload<T>", patched)
        self.assertNotIn("ExternalView", patched)
        self.assertNotIn("unsafe", patched)

    def test_frozen_evidence_exports_one_bounded_mitigation(self) -> None:
        experiment_path = EVIDENCE / "raw" / "RSH-064-experiment.json"
        summary_path = EVIDENCE / "derived-reuse-summary.json"
        mechanism_path = EVIDENCE / "mechanism-results.json"
        experiment = json.loads(experiment_path.read_text(encoding="utf-8"))
        summary = json.loads(summary_path.read_text(encoding="utf-8"))
        mechanism = json.loads(mechanism_path.read_text(encoding="utf-8"))

        edge = experiment["type_isolation_reuse_edge_evaluation"]
        self.assertTrue(experiment["orchestration_success"])
        self.assertEqual(experiment["unexpected_arm_count"], 0)
        self.assertTrue(edge["validated"])
        self.assertEqual(
            edge["status"], "cross_identity_reuse_edge_blocked_and_reported"
        )
        self.assertFalse(edge["vulnerability_specific_detection_signal"])

        summary_scenario = next(
            row for row in summary["scenarios"] if row["case_id"] == "RSH-064"
        )
        arms = {
            (arm["archive_variant"], arm["allocator_variant"]): arm
            for arm in summary_scenario["arms"]
        }
        self.assertEqual(
            arms[("vulnerable", "system")]["address_reuse_observation_count"],
            3,
        )
        self.assertEqual(
            arms[("vulnerable", "typed_plain")][
                "address_reuse_observation_count"
            ],
            3,
        )
        self.assertEqual(
            arms[("vulnerable", "typeiso")]["address_reuse_observation_count"],
            0,
        )
        self.assertEqual(
            arms[("vulnerable", "typeiso")][
                "matching_reuse_denial_event_count"
            ],
            3,
        )
        self.assertEqual(
            arms[("vulnerable", "typeiso")]["bound_replacement_site_count"],
            3,
        )
        for variant in ("system", "typed_plain", "typeiso"):
            self.assertEqual(
                arms[("patched", variant)]["address_reuse_observation_count"],
                3,
            )
            self.assertEqual(
                arms[("patched", variant)][
                    "matching_reuse_denial_event_count"
                ],
                0,
            )

        raw_record = summary_scenario["raw_experiment"]
        self.assertEqual(raw_record["bytes"], experiment_path.stat().st_size)
        self.assertEqual(raw_record["sha256"], runner.sha256_file(experiment_path))
        typeiso_toolchain = experiment["typeiso_toolchain"]
        driver_build = typeiso_toolchain["driver_build"]
        force_build = typeiso_toolchain["force_build"]
        self.assertTrue(driver_build["success"])
        self.assertRegex(driver_build["pass_source_sha256"], r"^[0-9a-f]{64}$")
        self.assertEqual(
            typeiso_toolchain["driver_sha256"], driver_build["wrapper_sha256"]
        )
        self.assertTrue(force_build["success"])
        implementation_digests = {
            arm["allocator_provenance"]["unialloc_implementation_sha256"]
            for arm in experiment["arms"]
        }
        self.assertEqual(
            implementation_digests, {force_build["implementation_sha256"]}
        )

        self.assertEqual(mechanism["counts"], {"mitigated": 12})
        self.assertEqual(len(mechanism["results"]), 12)
        result = next(
            row for row in mechanism["results"] if row["case_id"] == "RSH-064"
        )
        recomputed = exporter.evaluate_type_isolation_edge(
            summary_scenario, minimum_repetitions=3
        )
        recomputed["evidence"] = mechanism["derived_reuse_input"]
        self.assertEqual(result, recomputed)
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
