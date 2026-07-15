#!/usr/bin/env python3
"""Regressions for the executable RustSec expansion wave."""

from __future__ import annotations

import hashlib
import importlib.util
import json
import pathlib
import subprocess
import unittest


ROOT = pathlib.Path(__file__).resolve().parents[2]
CATALOG = ROOT / "evaluation" / "config" / "rustsec_heap_expansion_harnesses.json"
CORPUS = ROOT / "evaluation" / "config" / "rustsec_heap_security_corpus.json"
INVENTORY = ROOT / "evaluation" / "config" / "rustsec_heap_candidate_inventory.json"
REVIEW = ROOT / "evaluation" / "config" / "rustsec_temporal_reclaim_review.json"
RUNNER = ROOT / "evaluation" / "scripts" / "run_rustsec_heap_harness.py"
EVIDENCE = ROOT / "docs" / "evidence" / "rustsec-security-expansion-20260714"

spec = importlib.util.spec_from_file_location("rustsec_heap_expansion_runner", RUNNER)
assert spec is not None and spec.loader is not None
runner = importlib.util.module_from_spec(spec)
spec.loader.exec_module(runner)


class RustSecHeapExpansionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.catalog = runner.load_catalog(CATALOG)
        self.cases, self.scenarios = runner.index_catalog(self.catalog)

    def test_expansion_wave_has_distinct_new_strong_candidates(self) -> None:
        counts = self.catalog["counts"]
        expansion_ids = {case["advisory_id"] for case in self.cases.values()}
        current_ids = {
            case["advisory_id"]
            for case in json.loads(CORPUS.read_text(encoding="utf-8"))["cases"]
        }
        review = json.loads(REVIEW.read_text(encoding="utf-8"))
        strong = set(review["strong_candidates"]["cross_type_reuse"])
        strong.update(
            review["strong_candidates"]["tracked_reclaim_or_invalid_free"]
        )
        strong.update(
            review["strong_candidates"]["supplemental_rudra_source_evidence"]
        )
        first_wave = set(review["first_expansion_wave"]["advisory_ids"])

        self.assertEqual(counts["harness_case_count"], 5)
        self.assertEqual(counts["repository_scenario_count"], 7)
        self.assertEqual(counts["distinct_advisory_count"], 5)
        self.assertEqual(counts["published_scenario_count"], 5)
        self.assertEqual(counts["derived_reuse_scenario_count"], 2)
        self.assertEqual(len(self.cases), 5)
        self.assertEqual(len(self.scenarios), 7)
        self.assertEqual(len(expansion_ids), 5)
        self.assertTrue(expansion_ids.isdisjoint(current_ids))
        self.assertLessEqual(expansion_ids, strong)
        self.assertLessEqual(expansion_ids, first_wave)

    def test_every_local_execution_input_is_hash_pinned(self) -> None:
        for case in self.cases.values():
            for lock in case["lockfiles"].values():
                runner.validate_repo_file(lock["path"], lock["sha256"])
            for dependency_name in ("vulnerable_dependency", "patched_dependency"):
                dependency = case[dependency_name]
                if dependency["kind"] != "crates_io_archive":
                    continue
                runner.validate_sha256(
                    dependency["sha256"],
                    label=f"{case['case_id']} {dependency_name}",
                )
                self.assertGreater(dependency["bytes"], 0)
                self.assertEqual(
                    dependency["url"],
                    "https://static.crates.io/crates/"
                    f"{dependency['package']}/{dependency['package']}-"
                    f"{dependency['version']}.crate",
                )

            for scenario in case["scenarios"]:
                runner.validate_repo_file(
                    scenario["source_path"], scenario["source_sha256"]
                )
                patched_source = scenario.get("patched_source")
                if patched_source is not None:
                    runner.validate_repo_file(
                        patched_source["source_path"],
                        patched_source["source_sha256"],
                    )
                local_patch = scenario.get("local_patched_control")
                if local_patch is not None:
                    runner.validate_repo_file(
                        local_patch["path"], local_patch["sha256"]
                    )

    def test_rudra_adapters_bind_the_inventory_source_bytes(self) -> None:
        inventory = json.loads(INVENTORY.read_text(encoding="utf-8"))
        rudra_by_id = {
            entry["rustsec_id"]: entry
            for entry in inventory["rudra_pocs"]
            if entry["rustsec_id"] is not None
        }
        for case_id in ("RSH-044", "RSH-045"):
            case = self.cases[case_id]
            scenario = case["scenarios"][0]
            entry = rudra_by_id[case["advisory_id"]]
            self.assertEqual(
                scenario["origin_content_sha256"], entry["source_sha256"]
            )
            self.assertIn(entry["source_path"], scenario["origin_url"])

    def test_rsh041_manual_edge_excludes_subject_compiler_scopes(self) -> None:
        _case, scenario = self.scenarios["RSH-041-derived-reuse"]
        self.assertEqual(
            scenario["compiler_target_crates"], ["rsh-041-harness"]
        )
        exclusion = scenario["compiler_target_exclusion"]
        self.assertEqual(exclusion["subject_crate"], "oneringbuf")
        self.assertFalse(exclusion["compiler_automatic_victim_coverage"])

        source = (ROOT / scenario["source_path"]).read_text(encoding="utf-8")
        self.assertIn("if replacement_address == original_address", source)
        self.assertIn("std::mem::forget(r3)", source)

    def test_retained_evidence_matches_catalog_and_raw_matrices(self) -> None:
        catalog_sha256 = hashlib.sha256(CATALOG.read_bytes()).hexdigest()
        published = json.loads(
            (EVIDENCE / "published-summary.json").read_text(encoding="utf-8")
        )
        derived = json.loads(
            (EVIDENCE / "derived-reuse-summary.json").read_text(encoding="utf-8")
        )
        evidence_readme = (EVIDENCE / "README.md").read_text(encoding="utf-8")
        security_documentation = (
            ROOT / "docs" / "type-isolation-security-evaluation.md"
        ).read_text(encoding="utf-8")

        self.assertEqual(published["catalog_sha256"], catalog_sha256)
        self.assertEqual(derived["catalog"]["sha256"], catalog_sha256)
        self.assertEqual(published["catalog_case_count"], 5)
        self.assertEqual(published["experiment_file_count"], 10)
        self.assertFalse(published["mitigation_inferred"])
        for artifact in (
            CATALOG,
            EVIDENCE / "published-summary.json",
            EVIDENCE / "derived-reuse-summary.json",
        ):
            self.assertIn(artifact.name, evidence_readme)
            self.assertIn(
                artifact.relative_to(ROOT).as_posix(), security_documentation
            )
        self.assertIn("machine-readable", evidence_readme)
        self.assertIn("machine-readable artifact bindings", security_documentation)

        for record in published["experiment_inputs"] + derived[
            "raw_experiment_inputs"
        ]:
            path = ROOT / record["path"]
            payload = path.read_bytes()
            self.assertEqual(len(payload), record["bytes"])
            self.assertEqual(hashlib.sha256(payload).hexdigest(), record["sha256"])

        typeiso_specific = 0
        common_allocator_signals = 0
        for case in published["cases"]:
            arms = {
                (arm["archive_variant"], arm["allocator_variant"]): arm
                for arm in case["arms"]
            }
            self.assertEqual(
                arms[("vulnerable", "system")][
                    "expected_oracle_observation_count"
                ],
                3,
            )
            self.assertEqual(
                arms[("patched", "system")]["expected_oracle_observation_count"],
                3,
            )
            plain = arms[("vulnerable", "typed_plain")]
            typeiso = arms[("vulnerable", "typeiso")]
            typeiso_specific += typeiso["classification"] == "typeiso_specific_signal"
            common_allocator_signals += (
                plain["allocator_signal_count"] > 0
                and typeiso["allocator_signal_count"] > 0
            )
        self.assertEqual(typeiso_specific, 0)
        self.assertEqual(common_allocator_signals, 3)

        self.assertEqual(
            derived["counts"],
            {
                "compiler_automatic_victim_coverage_count": 0,
                "derived_reuse_scenario_count": 2,
                "source_vulnerability_detection_validated_count": 0,
                "source_vulnerability_mitigation_inferred_count": 0,
                "validated_cross_identity_reuse_edge_count": 2,
            },
        )
        for scenario in derived["scenarios"]:
            evaluation = scenario["edge_evaluation"]
            self.assertTrue(evaluation["validated"])
            self.assertTrue(evaluation["manual_victim_identity_annotation"])
            self.assertFalse(evaluation["compiler_automatic_victim_coverage"])
            self.assertFalse(evaluation["source_vulnerability_detection_validated"])

    def test_expansion_inputs_and_evidence_are_not_git_ignored(self) -> None:
        roots = (
            ROOT / "evaluation" / "harnesses" / "rustsec_heap_expansion",
            EVIDENCE,
        )
        paths = sorted(
            str(path.relative_to(ROOT))
            for root in roots
            for path in root.rglob("*")
            if path.is_file()
        )
        completed = subprocess.run(
            ["git", "check-ignore", "--stdin"],
            cwd=ROOT,
            input="\n".join(paths) + "\n",
            text=True,
            capture_output=True,
            check=False,
        )

        self.assertIn(completed.returncode, (0, 1), completed.stderr)
        self.assertEqual(completed.stdout, "")


if __name__ == "__main__":
    unittest.main()
