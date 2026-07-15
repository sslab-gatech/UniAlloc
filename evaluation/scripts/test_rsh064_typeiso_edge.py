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
EVIDENCE = ROOT / "docs" / "evidence" / "rustsec-rsh064-typeiso-20260714"
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

    def test_manual_edge_uses_indirect_victim_scope_and_safe_control(self) -> None:
        vulnerable = (ROOT / self.scenario["source_path"]).read_text(
            encoding="utf-8"
        )
        patched_meta = self.scenario["patched_source"]
        patched = (ROOT / patched_meta["source_path"]).read_text(encoding="utf-8")

        self.assertIn("fn materialize_victim<T: Clone", vulnerable)
        self.assertIn("fn reclaim_victim<T>", vulnerable)
        self.assertIn("black_box(materialize)", vulnerable)
        self.assertIn("black_box(reclaim)", vulnerable)
        self.assertIn("ExternalView", vulnerable)
        self.assertIn("Box::new(Replacement", vulnerable)

        self.assertIn("fn materialize_payload<T: Clone", patched)
        self.assertIn("fn reclaim_payload<T>", patched)
        self.assertNotIn("ExternalView", patched)
        self.assertNotIn("unsafe", patched)

    def test_frozen_evidence_exports_one_bounded_true_positive(self) -> None:
        experiment_path = EVIDENCE / "experiment.json"
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
        self.assertTrue(edge["vulnerability_specific_detection_signal"])

        arms = {
            (arm["archive_variant"], arm["allocator_variant"]): arm
            for arm in summary["scenarios"][0]["arms"]
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

        raw_record = summary["scenarios"][0]["raw_experiment"]
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

        self.assertEqual(mechanism["counts"], {"mitigated": 1})
        self.assertEqual(len(mechanism["results"]), 1)
        result = mechanism["results"][0]
        recomputed = exporter.evaluate_type_isolation_edge(
            summary["scenarios"][0], minimum_repetitions=3
        )
        recomputed["evidence"] = mechanism["derived_reuse_input"]
        self.assertEqual(result, recomputed)
        self.assertTrue(result["true_positive"])
        self.assertEqual(result["outcome"], "mitigated")
        self.assertEqual(
            result["result_semantics"], "causal_mitigation_true_positive"
        )
        self.assertEqual(result["failed_checks"], [])
        self.assertTrue(all(result["checks"].values()))
        self.assertEqual(
            mechanism["derived_reuse_input"]["sha256"],
            runner.sha256_file(summary_path),
        )

        readme = (EVIDENCE / "README.md").read_text(encoding="utf-8")
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
        self.assertIn("manually attributed", readme)
        self.assertIn("Automatic compiler", readme)


if __name__ == "__main__":
    unittest.main()
