#!/usr/bin/env python3
"""Focused regressions for the derived RSH-008 Type Isolation edge."""

from __future__ import annotations

import importlib.util
import json
import pathlib
import unittest


ROOT = pathlib.Path(__file__).resolve().parents[2]
CATALOG = ROOT / "evaluation" / "config" / "rustsec_heap_harnesses.json"
RUNNER = ROOT / "evaluation" / "scripts" / "run_rustsec_heap_harness.py"
EVIDENCE = ROOT / "docs" / "evidence" / "rustsec-rsh008-typeiso-20260714"

spec = importlib.util.spec_from_file_location("rustsec_heap_harness", RUNNER)
assert spec is not None and spec.loader is not None
runner = importlib.util.module_from_spec(spec)
spec.loader.exec_module(runner)


class Rsh008TypeIsolationEdgeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.catalog = runner.load_catalog(CATALOG)
        cases, scenarios = runner.index_catalog(self.catalog)
        self.case = cases["RSH-008"]
        self.scenario = scenarios["RSH-008-derived-reuse"][1]

    def test_compiler_routing_preserves_the_manual_victim_identity(self) -> None:
        self.assertEqual(
            self.scenario["compiler_target_crates"], ["rsh-008-harness"]
        )
        annotation = self.scenario["type_isolation_edge_annotation"]
        self.assertEqual(
            annotation["kind"], "manual_exact_vulnerability_edge_identity"
        )
        self.assertEqual(annotation["expected_layout"], {"size": 48, "align": 8})
        self.assertIn("LruEntry", " ".join(annotation["annotation_scope"]))
        runner.validate_repo_file(
            self.scenario["source_path"], self.scenario["source_sha256"]
        )
        patched = self.scenario["patched_source"]
        runner.validate_repo_file(patched["source_path"], patched["source_sha256"])

    def test_frozen_matrix_binds_three_denials_and_passes_controls(self) -> None:
        preflight = json.loads((EVIDENCE / "preflight.json").read_text())
        experiment = json.loads((EVIDENCE / "experiment.json").read_text())

        self.assertEqual(preflight["scenario_id"], "RSH-008-derived-reuse")
        self.assertEqual(preflight["harness_target_crates"], ["rsh_008_harness"])
        self.assertEqual(len(preflight["planned_arms"]), 6)
        self.assertTrue(experiment["orchestration_success"])
        self.assertEqual(experiment["unexpected_arm_count"], 0)
        self.assertEqual(experiment["repetitions_requested"], 3)

        arms = {
            (arm["archive_variant"], arm["allocator_variant"]): arm
            for arm in experiment["arms"]
        }
        self.assertEqual(len(arms), 6)
        for archive in ("vulnerable", "patched"):
            for allocator in ("system", "typed_plain", "typeiso"):
                arm = arms[(archive, allocator)]
                self.assertEqual(arm["repetition_summary"]["exit_codes"], [0, 0, 0])
                self.assertEqual(arm["target_crates"], ["rsh_008_harness"])

        for allocator in ("system", "typed_plain"):
            arm = arms[("vulnerable", allocator)]
            self.assertEqual(
                arm["repetition_summary"]["address_reuse_observation_count"], 3
            )
        treatment = arms[("vulnerable", "typeiso")]
        self.assertEqual(
            treatment["repetition_summary"]["address_reuse_observation_count"], 0
        )
        self.assertEqual(
            treatment["reuse_denial_evidence"][
                "matching_reuse_denial_event_count"
            ],
            3,
        )
        self.assertEqual(
            treatment["reuse_denial_evidence"]["bound_replacement_site_count"], 3
        )

        for allocator in ("system", "typed_plain", "typeiso"):
            arm = arms[("patched", allocator)]
            self.assertEqual(
                arm["repetition_summary"]["address_reuse_observation_count"], 3
            )
            self.assertTrue(arm["oracle_validation"]["expected_oracle_observed"])
            if allocator != "system":
                self.assertEqual(
                    arm["reuse_denial_evidence"][
                        "matching_reuse_denial_event_count"
                    ],
                    0,
                )

        edge = experiment["type_isolation_reuse_edge_evaluation"]
        self.assertTrue(edge["validated"])
        self.assertTrue(edge["typeiso_reuse_edge_blocked_and_reported"])
        self.assertFalse(edge["compiler_automatic_victim_coverage"])
        self.assertFalse(edge["source_vulnerability_detection_validated"])

    def test_frozen_export_is_one_bounded_true_positive(self) -> None:
        experiment_path = EVIDENCE / "experiment.json"
        summary_path = EVIDENCE / "derived-reuse-summary.json"
        mechanism_path = EVIDENCE / "mechanism-results.json"
        summary = json.loads(summary_path.read_text())
        mechanism = json.loads(mechanism_path.read_text())

        scenario = summary["scenarios"][0]
        self.assertEqual(scenario["scenario_id"], "RSH-008-derived-reuse")
        self.assertEqual(
            scenario["raw_experiment"]["sha256"],
            runner.sha256_file(experiment_path),
        )
        self.assertEqual(
            summary["counts"]["validated_cross_identity_reuse_edge_count"], 1
        )

        self.assertEqual(mechanism["counts"], {"mitigated": 1})
        result = mechanism["results"][0]
        self.assertTrue(result["true_positive"])
        self.assertEqual(result["outcome"], "mitigated")
        self.assertEqual(
            result["result_semantics"], "causal_mitigation_true_positive"
        )
        self.assertEqual(result["failed_checks"], [])
        self.assertEqual(result["evidence_gaps"], [])
        self.assertTrue(all(result["checks"].values()))
        self.assertFalse(result["compiler_automatic_victim_coverage"])
        self.assertFalse(result["source_vulnerability_detection_validated"])
        self.assertEqual(
            mechanism["derived_reuse_input"]["sha256"],
            runner.sha256_file(summary_path),
        )

        readme = (EVIDENCE / "README.md").read_text()
        for artifact_name in (
            "preflight.json",
            "experiment.json",
            "derived-reuse-summary.json",
            "mechanism-results.json",
        ):
            self.assertIn(f"`{artifact_name}`", readme)
        self.assertIn(self.scenario["source_path"], readme)
        self.assertIn(self.scenario["patched_source"]["source_path"], readme)
        self.assertIn("machine-readable records are the provenance authority", readme)
        self.assertIn("manual victim identity", readme)
        self.assertIn("source-vulnerability detection", readme)


if __name__ == "__main__":
    unittest.main()
