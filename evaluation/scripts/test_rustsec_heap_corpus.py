#!/usr/bin/env python3
"""Focused regressions for the RustSec/Rudra heap-security corpus audit."""

from __future__ import annotations

import copy
import importlib.util
import json
import pathlib
import subprocess
import tempfile
import unittest


ROOT = pathlib.Path(__file__).resolve().parents[2]
AUDIT_PATH = ROOT / "evaluation" / "scripts" / "audit_rustsec_heap_corpus.py"
MANIFEST_PATH = ROOT / "evaluation" / "config" / "rustsec_heap_security_corpus.json"
HARNESS_CATALOG_PATH = ROOT / "evaluation" / "config" / "rustsec_heap_harnesses.json"
DOCUMENTATION_PATH = ROOT / "docs" / "type-isolation-security-evaluation.md"
HARNESS_ROOT = ROOT / "evaluation" / "harnesses" / "rustsec_heap"

spec = importlib.util.spec_from_file_location("unialloc_rustsec_heap_audit", AUDIT_PATH)
assert spec is not None and spec.loader is not None
audit = importlib.util.module_from_spec(spec)
spec.loader.exec_module(audit)


class RustSecHeapCorpusAuditTests(unittest.TestCase):
    def setUp(self) -> None:
        self.manifest = json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))
        self.harness_catalog = json.loads(
            HARNESS_CATALOG_PATH.read_text(encoding="utf-8")
        )

    def audit_copy(self, mutate=None, *, catalog_mutate=None, **audit_kwargs):
        value = copy.deepcopy(self.manifest)
        if mutate is not None:
            mutate(value)
        with tempfile.TemporaryDirectory() as raw_tmp:
            root = pathlib.Path(raw_tmp)
            path = root / "manifest.json"
            if catalog_mutate is not None:
                catalog = copy.deepcopy(self.harness_catalog)
                catalog_mutate(catalog)
                catalog_path = root / "rustsec_heap_harnesses.json"
                catalog_path.write_text(
                    json.dumps(catalog, indent=2, sort_keys=True) + "\n",
                    encoding="utf-8",
                )
                value["harness_bundle"]["catalog_sha256"] = audit.file_sha256(
                    catalog_path
                )
                audit_kwargs["harness_catalog"] = catalog_path
            path.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8")
            return audit.audit_manifest(path, **audit_kwargs)

    def test_repository_manifest_is_a_passing_non_claim_grade_catalog(self) -> None:
        report = audit.audit_manifest(MANIFEST_PATH)

        self.assertTrue(report["passed"], report["blockers"])
        self.assertTrue(report["catalog_ready"])
        self.assertFalse(report["claim_grade"])
        self.assertIsNone(report["snapshot_verified"])
        self.assertEqual(report["summary"]["case_count"], 40)
        self.assertEqual(report["summary"]["cohorts"], {"rudra_poc": 19, "rustsec_extended": 21})
        self.assertEqual(report["summary"]["execution_readiness"]["poc_source_available"], 18)
        self.assertEqual(
            report["summary"]["execution_readiness"][
                "pinned_harness_source_available"
            ],
            22,
        )
        self.assertEqual(
            report["summary"]["harness_bundle"]["counts"],
            {
                "derived_adapter_scenario_count": 4,
                "derived_reuse_scenario_count": 2,
                "harness_case_count": 22,
                "pinned_rudra_source_scenario_count": 18,
                "published_or_upstream_scenario_count": 24,
                "repository_scenario_count": 28,
                "source_ready_distinct_advisory_count": 40,
                "total_source_ready_scenario_count": 46,
            },
        )
        self.assertEqual(
            report["summary"]["harness_bundle"]["distinct_advisory_count"], 40
        )
        self.assertEqual(
            report["summary"]["published_witness_type_isolation_effects"],
            {"no_direct_effect": 40},
        )
        self.assertEqual(
            report["summary"]["derived_type_isolation_experiment_effects"],
            {"conditional_reuse_edge_mitigation": 3, "no_direct_effect": 37},
        )
        self.assertEqual(
            report["summary"]["unialloc_boundary_effects"],
            {"conditional_detection": 13, "no_direct_effect": 27},
        )
        self.assertGreaterEqual(report["summary"]["control_roles"]["negative_control"], 40 // 3)

    def test_repository_harness_evidence_is_not_git_ignored(self) -> None:
        paths = sorted(
            str(path.relative_to(ROOT))
            for path in HARNESS_ROOT.rglob("*")
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

    def test_rsh008_derived_edge_has_manual_identity_and_safe_control(self) -> None:
        case = next(
            case
            for case in self.harness_catalog["cases"]
            if case["case_id"] == "RSH-008"
        )
        scenario = next(
            scenario
            for scenario in case["scenarios"]
            if scenario["scenario_id"] == "RSH-008-derived-reuse"
        )

        annotation = scenario["type_isolation_edge_annotation"]
        self.assertEqual(
            annotation["kind"], "manual_exact_vulnerability_edge_identity"
        )
        self.assertEqual(annotation["expected_layout"], {"size": 48, "align": 8})
        self.assertIn("LruEntry", " ".join(annotation["annotation_scope"]))
        self.assertEqual(
            scenario["compiler_target_crates"], ["rsh-008-harness"]
        )
        self.assertEqual(
            scenario["oracle"]["patched_control"],
            "same address grooming exits 0",
        )

        patched = scenario["patched_source"]
        patched_path = ROOT / patched["source_path"]
        self.assertTrue(patched_path.is_file())
        self.assertEqual(audit.file_sha256(patched_path), patched["source_sha256"])
        patched_source = patched_path.read_text(encoding="utf-8")
        self.assertIn("with_vulnerability_edge_identity", patched_source)
        self.assertIn("report_vulnerability_edge_reuse_denial", patched_source)

        vulnerable_path = ROOT / scenario["source_path"]
        vulnerable_source = vulnerable_path.read_text(encoding="utf-8")
        self.assertIn("black_box(stale_value_address)", vulnerable_source)
        self.assertNotIn('println!("{value}")', vulnerable_source)

    def test_documented_catalog_matches_manifest(self) -> None:
        documentation = DOCUMENTATION_PATH.read_text(encoding="utf-8")
        profiles = self.manifest["mechanism_profiles"]
        catalog_heading = "## Complete frozen 40-case pilot catalog"
        self.assertEqual(documentation.count(catalog_heading), 1)
        catalog_section = documentation.split(catalog_heading, maxsplit=1)[1]
        catalog_section = catalog_section.split("\n## ", maxsplit=1)[0]
        documented_rows = [
            line
            for line in catalog_section.splitlines()
            if line.startswith("| RSH-")
        ]

        self.assertEqual(len(documented_rows), len(self.manifest["cases"]))
        for case in self.manifest["cases"]:
            profile = profiles[case["assessment_profile"]]
            if (
                profile["derived_type_isolation_experiment_effect"]
                == "conditional_reuse_edge_mitigation"
            ):
                effect = (
                    "published: no direct; derived A-to-B: conditional "
                    "reuse-edge block"
                )
            elif profile["unialloc_boundary_effect"] == "conditional_detection":
                effect = (
                    "Type Isolation: no direct; UniAlloc boundary: "
                    "conditional detection"
                )
            else:
                effect = "expected no direct effect"
            expected = (
                f"| {case['case_id']} | "
                f"[{case['advisory_id']}]({case['advisory']['rustsec_url']}) / "
                f"`{case['crate']}` | `{case['taxonomy']['primary_primitive']}` | "
                f"{effect} | `{case['execution']['readiness']}` |"
            )
            self.assertIn(expected, documented_rows)

    def test_duplicate_advisory_id_is_rejected(self) -> None:
        def mutate(value):
            value["cases"][1]["advisory_id"] = value["cases"][0]["advisory_id"]
            value["cases"][1]["advisory"]["rustsec_url"] = value["cases"][0]["advisory"]["rustsec_url"]
            value["cases"][1]["advisory"]["snapshot_path"] = value["cases"][0]["advisory"]["snapshot_path"]

        report = self.audit_copy(mutate)

        self.assertFalse(report["passed"])
        self.assertIn("duplicate advisory_id", "\n".join(report["blockers"]))

    def test_source_snapshot_requires_full_commit_pin(self) -> None:
        report = self.audit_copy(
            lambda value: value["source_snapshots"]["rustsec_advisory_db"].__setitem__(
                "commit", "9f3e138"
            )
        )

        self.assertFalse(report["passed"])
        self.assertIn("full 40-hex pin", "\n".join(report["blockers"]))

    def test_profile_requires_explicit_boundary(self) -> None:
        report = self.audit_copy(
            lambda value: value["mechanism_profiles"]["reuse_dependent_uaf"].__setitem__(
                "boundaries", []
            )
        )

        self.assertFalse(report["passed"])
        self.assertIn("must declare boundaries", "\n".join(report["blockers"]))

    def test_reuse_mitigation_candidate_must_be_temporal_uaf(self) -> None:
        def mutate(value):
            candidate = next(
                case for case in value["cases"] if case["assessment_profile"] == "reuse_dependent_uaf"
            )
            candidate["taxonomy"]["dimension"] = "spatial"
            candidate["taxonomy"]["primary_primitive"] = "out_of_bounds_write"

        report = self.audit_copy(mutate)

        self.assertFalse(report["passed"])
        blockers = "\n".join(report["blockers"])
        self.assertIn("reuse-edge experiment must be temporal", blockers)
        self.assertIn("reuse-edge experiment must be a UAF", blockers)

    def test_profile_rejects_incompatible_primary_primitive(self) -> None:
        def mutate(value):
            candidate = next(
                case
                for case in value["cases"]
                if case["assessment_profile"] == "uninitialized_exposure"
            )
            candidate["taxonomy"]["primary_primitive"] = "double_free"

        report = self.audit_copy(mutate)

        self.assertFalse(report["passed"])
        self.assertIn(
            "primary_primitive is incompatible with assessment_profile",
            "\n".join(report["blockers"]),
        )

    def test_detection_candidate_cannot_be_attributed_to_type_isolation(self) -> None:
        def mutate(value):
            profile = value["mechanism_profiles"]["allocator_visible_double_free"]
            profile["derived_type_isolation_experiment_effect"] = (
                "conditional_reuse_edge_mitigation"
            )

        report = self.audit_copy(mutate)

        self.assertFalse(report["passed"])
        self.assertIn(
            "detection hypothesis cannot be attributed to a derived Type Isolation experiment",
            "\n".join(report["blockers"]),
        )

    def test_detection_candidate_requires_boundary_effect(self) -> None:
        def mutate(value):
            profile = value["mechanism_profiles"]["allocator_visible_double_free"]
            profile["unialloc_boundary_effect"] = "no_direct_effect"

        report = self.audit_copy(mutate)

        self.assertFalse(report["passed"])
        self.assertIn(
            "detection hypothesis must name a conditional UniAlloc boundary effect",
            "\n".join(report["blockers"]),
        )

    def test_schema_frozen_quota_floor_is_enforced(self) -> None:
        report = self.audit_copy(
            lambda value: value["selection_protocol"]["minimum_strata"].__setitem__(
                "negative_controls", 30
            )
        )

        self.assertFalse(report["passed"])
        self.assertIn(
            "minimum_strata must match the schema-version-1 frozen floors",
            "\n".join(report["blockers"]),
        )

    def test_empty_corpus_cannot_self_authorize_zero_quotas(self) -> None:
        def mutate(value):
            value["cases"] = []
            value["selection_protocol"]["minimum_strata"] = {}
            value["source_snapshots"]["rustsec_advisory_db"]["selected_file_count"] = 0
            value["source_snapshots"]["rustsec_advisory_db"][
                "selected_case_candidate_screen_count"
            ] = 0
            value["source_snapshots"]["rustsec_advisory_db"][
                "selected_case_outside_candidate_screen_count"
            ] = 0
            value["source_snapshots"]["rudra_poc"]["selected_file_count"] = 0

        report = self.audit_copy(mutate)

        self.assertFalse(report["passed"])
        blockers = "\n".join(report["blockers"])
        self.assertIn("schema-version-1 frozen floors", blockers)
        self.assertIn("case count is below the schema-version-1 minimum", blockers)

    def test_patched_replay_pin_is_rejected(self) -> None:
        def mutate(value):
            value["cases"][0]["execution"]["replay_pin"] = "0.1.3"

        report = self.audit_copy(mutate)

        self.assertFalse(report["passed"])
        self.assertIn(
            "replay pin is patched or explicitly unaffected",
            "\n".join(report["blockers"]),
        )

    def test_caret_version_requirements_follow_cargo_compatibility(self) -> None:
        self.assertTrue(audit.semver_matches_requirement("0.22.2", "^0.22.2"))
        self.assertTrue(audit.semver_matches_requirement("0.22.9", "^0.22.2"))
        self.assertFalse(audit.semver_matches_requirement("0.23.0", "^0.22.2"))

    def test_partial_version_bounds_follow_rustsec_ranges(self) -> None:
        self.assertTrue(audit.semver_matches_requirement("0.7.1", "< 0.8"))
        self.assertFalse(audit.semver_matches_requirement("0.8.0", "< 0.8"))

    def test_tampered_harness_catalog_hash_is_rejected(self) -> None:
        report = self.audit_copy(
            lambda value: value["harness_bundle"].__setitem__(
                "catalog_sha256", "0" * 64
            )
        )

        self.assertFalse(report["passed"])
        self.assertIn(
            "harness catalog SHA-256 disagrees",
            "\n".join(report["blockers"]),
        )

    def test_tampered_harness_source_hash_is_rejected(self) -> None:
        def mutate(catalog):
            catalog["cases"][0]["scenarios"][0]["source_sha256"] = "0" * 64

        report = self.audit_copy(catalog_mutate=mutate)

        self.assertFalse(report["passed"])
        self.assertIn("source SHA-256 mismatch", "\n".join(report["blockers"]))

    def test_harness_readiness_cross_reference_mismatch_is_rejected(self) -> None:
        def mutate(value):
            case = next(
                case
                for case in value["cases"]
                if case["execution"]["readiness"]
                == "pinned_harness_source_available"
            )
            case["execution"]["readiness"] = "fresh_harness_required"

        report = self.audit_copy(mutate)

        self.assertFalse(report["passed"])
        blockers = "\n".join(report["blockers"])
        self.assertIn("repository harness readiness and reproduction kind must agree", blockers)
        self.assertIn("catalog case IDs disagree with pinned corpus readiness rows", blockers)

    def test_harness_count_drift_is_rejected(self) -> None:
        def mutate(catalog):
            catalog["counts"]["repository_scenario_count"] = 26

        report = self.audit_copy(catalog_mutate=mutate)

        self.assertFalse(report["passed"])
        blockers = "\n".join(report["blockers"])
        self.assertIn("frozen 22/28/24/4/2/18/46/40 split", blockers)
        self.assertIn("declared repository_scenario_count disagrees", blockers)

    def test_derived_non_reuse_adapter_remains_a_negative_control(self) -> None:
        case = next(
            case for case in self.harness_catalog["cases"] if case["case_id"] == "RSH-026"
        )
        scenario = case["scenarios"][0]

        self.assertEqual(scenario["adapter_kind"], "derived_adapter")
        self.assertEqual(scenario["classification_role"], "published_negative_control")
        report = audit.audit_manifest(MANIFEST_PATH)
        self.assertTrue(report["passed"], report["blockers"])

    def test_boundary_detection_role_requires_an_eligible_profile(self) -> None:
        def mutate(catalog):
            case = next(
                case for case in catalog["cases"] if case["case_id"] == "RSH-003"
            )
            case["scenarios"][0]["classification_role"] = (
                "allocator_boundary_detection_candidate"
            )

        report = self.audit_copy(catalog_mutate=mutate)

        self.assertFalse(report["passed"])
        self.assertIn(
            "boundary-detection role requires a conditional UniAlloc boundary effect",
            "\n".join(report["blockers"]),
        )

    def test_subject_patch_requires_result_hashes_for_each_selected_variant(self) -> None:
        def mutate(catalog):
            case = next(case for case in catalog["cases"] if case["case_id"] == "RSH-001")
            del case["scenarios"][0]["subject_patches"][0][
                "resulting_source_files_by_variant"
            ]["patched"]

        report = self.audit_copy(catalog_mutate=mutate)

        self.assertFalse(report["passed"])
        self.assertIn(
            "result variants must equal apply_variants", "\n".join(report["blockers"])
        )

    def test_subject_patch_must_apply_to_both_dependency_variants(self) -> None:
        def mutate(catalog):
            case = next(case for case in catalog["cases"] if case["case_id"] == "RSH-001")
            patch = case["scenarios"][0]["subject_patches"][0]
            patch["apply_variants"] = ["vulnerable"]

        report = self.audit_copy(catalog_mutate=mutate)

        self.assertFalse(report["passed"])
        self.assertIn(
            "apply_variants must equal vulnerable and patched",
            "\n".join(report["blockers"]),
        )

    def test_upstream_patched_case_rejects_a_mixed_local_patch_policy(self) -> None:
        def mutate(catalog):
            case = next(case for case in catalog["cases"] if case["case_id"] == "RSH-022")
            scenario = case["scenarios"][0]
            scenario["local_patched_control"] = {
                "kind": "hashed_local_patch",
                "label": "ambiguous mixed control",
                "path": scenario["source_path"],
                "sha256": scenario["source_sha256"],
                "resulting_source_files": {"src/array_queue.rs": "0" * 64},
            }

        report = self.audit_copy(catalog_mutate=mutate)

        self.assertFalse(report["passed"])
        self.assertIn(
            "upstream patched archive cannot be mixed with scenario-local patches",
            "\n".join(report["blockers"]),
        )

    def test_malformed_manifest_root_returns_a_blocker(self) -> None:
        with tempfile.TemporaryDirectory() as raw_tmp:
            path = pathlib.Path(raw_tmp) / "manifest.json"
            path.write_text("[]\n", encoding="utf-8")
            report = audit.audit_manifest(path)

        self.assertFalse(report["passed"])
        self.assertEqual(report["blockers"], ["manifest root must be an object"])

    def test_malformed_source_snapshot_returns_blockers_without_crashing(self) -> None:
        report = self.audit_copy(
            lambda value: value["source_snapshots"].__setitem__(
                "rustsec_advisory_db", []
            )
        )

        self.assertFalse(report["passed"])
        self.assertIn(
            "source snapshot rustsec_advisory_db must be an object",
            "\n".join(report["blockers"]),
        )

    def test_malformed_referenced_profile_returns_blockers_without_crashing(self) -> None:
        report = self.audit_copy(
            lambda value: value["mechanism_profiles"].__setitem__(
                "reuse_dependent_uaf", []
            )
        )

        self.assertFalse(report["passed"])
        self.assertIn(
            "mechanism profile reuse_dependent_uaf must be an object",
            "\n".join(report["blockers"]),
        )

    def test_malformed_version_range_returns_blockers_without_crashing(self) -> None:
        report = self.audit_copy(
            lambda value: value["cases"][0]["advisory"].__setitem__(
                "patched_versions", [3]
            )
        )

        self.assertFalse(report["passed"])
        self.assertIn(
            "patched_versions entries must be nonempty strings",
            "\n".join(report["blockers"]),
        )

    def test_malformed_case_survives_source_verification_path(self) -> None:
        def mutate(value):
            value["cases"][0] = "invalid case"
            value["cases"][1]["advisory"] = []

        report = self.audit_copy(mutate, rustsec_db=ROOT)

        self.assertFalse(report["passed"])
        blockers = "\n".join(report["blockers"])
        self.assertIn("case[0] must be an object", blockers)
        self.assertIn("case[1] advisory must be an object", blockers)

    def test_rudra_case_requires_pinned_poc_metadata(self) -> None:
        def mutate(value):
            case = next(case for case in value["cases"] if case["cohort"] == "rudra_poc")
            case["poc"] = None

        report = self.audit_copy(mutate)

        self.assertFalse(report["passed"])
        self.assertIn("Rudra cohort requires poc metadata", "\n".join(report["blockers"]))

    def test_rudra_case_requires_analyzer_classification(self) -> None:
        def mutate(value):
            case = next(case for case in value["cases"] if case["cohort"] == "rudra_poc")
            case["poc"]["rudra_analyzers"] = []

        report = self.audit_copy(mutate)

        self.assertFalse(report["passed"])
        self.assertIn(
            "Rudra analyzer classification is required",
            "\n".join(report["blockers"]),
        )

    def test_absolute_rustsec_snapshot_path_is_rejected(self) -> None:
        def mutate(value):
            case = value["cases"][0]
            case["advisory"]["snapshot_path"] = (
                f"/tmp/{case['advisory_id']}.md"
            )

        report = self.audit_copy(mutate)

        self.assertFalse(report["passed"])
        self.assertIn(
            "advisory snapshot_path must be a strict POSIX relative path",
            "\n".join(report["blockers"]),
        )

    def test_traversing_rustsec_snapshot_path_is_rejected(self) -> None:
        def mutate(value):
            case = value["cases"][0]
            case["advisory"]["snapshot_path"] = (
                f"../crates/{case['crate']}/{case['advisory_id']}.md"
            )

        report = self.audit_copy(mutate)

        self.assertFalse(report["passed"])
        self.assertIn(
            "advisory snapshot_path must be a strict POSIX relative path",
            "\n".join(report["blockers"]),
        )

    def test_rustsec_snapshot_url_is_bound_to_the_exact_pinned_path(self) -> None:
        def mutate(value):
            value["cases"][0]["advisory"]["snapshot_url"] = (
                "https://example.invalid/advisory.md"
            )

        report = self.audit_copy(mutate)

        self.assertFalse(report["passed"])
        self.assertIn(
            "advisory snapshot URL must bind to its source commit, crate, and advisory ID",
            "\n".join(report["blockers"]),
        )

    def test_absolute_rudra_snapshot_path_is_rejected(self) -> None:
        def mutate(value):
            case = next(case for case in value["cases"] if case.get("poc"))
            poc = case["poc"]
            poc["snapshot_path"] = f"/tmp/{poc['id']}-{poc['target_crate']}.rs"

        report = self.audit_copy(mutate)

        self.assertFalse(report["passed"])
        self.assertIn(
            "Rudra snapshot_path must be a strict POSIX relative path",
            "\n".join(report["blockers"]),
        )

    def test_traversing_rudra_snapshot_path_is_rejected(self) -> None:
        def mutate(value):
            case = next(case for case in value["cases"] if case.get("poc"))
            poc = case["poc"]
            poc["snapshot_path"] = (
                f"poc/{poc['id']}-escape/../../../tmp/{poc['target_crate']}.rs"
            )

        report = self.audit_copy(mutate)

        self.assertFalse(report["passed"])
        self.assertIn(
            "Rudra snapshot_path must be a strict POSIX relative path",
            "\n".join(report["blockers"]),
        )

    def test_rudra_snapshot_url_is_bound_to_the_exact_pinned_path(self) -> None:
        def mutate(value):
            case = next(case for case in value["cases"] if case.get("poc"))
            case["poc"]["snapshot_url"] = "https://example.invalid/poc.rs"

        report = self.audit_copy(mutate)

        self.assertFalse(report["passed"])
        self.assertIn(
            "Rudra snapshot URL must bind to its source commit, PoC ID, and target crate",
            "\n".join(report["blockers"]),
        )

    def test_clean_exit_oracle_cannot_be_claimed_in_classification_manifest(self) -> None:
        def mutate(value):
            value["cases"][0]["execution"]["treatment_oracle"] = "clean_exit"

        report = self.audit_copy(mutate)

        self.assertFalse(report["passed"])
        self.assertIn("treatment oracle must remain preregistered-before-run", "\n".join(report["blockers"]))

    def test_case_rows_cannot_self_promote_to_claim_grade(self) -> None:
        report = self.audit_copy(
            lambda value: value["cases"][0]["execution"].__setitem__("claim_grade", True)
        )

        self.assertFalse(report["passed"])
        self.assertIn("classification row must remain claim_grade=false", "\n".join(report["blockers"]))

    def test_selected_files_digest_binds_paths_and_bytes(self) -> None:
        with tempfile.TemporaryDirectory() as raw_tmp:
            root = pathlib.Path(raw_tmp)
            (root / "a").write_bytes(b"one")
            (root / "b").write_bytes(b"two")
            first = audit.selected_files_sha256(root, ["a", "b"])
            reordered = audit.selected_files_sha256(root, ["b", "a"])
            (root / "b").write_bytes(b"changed")
            changed = audit.selected_files_sha256(root, ["a", "b"])

        self.assertEqual(first, reordered)
        self.assertNotEqual(first, changed)

    def test_snapshot_resolution_rejects_symlink_escape(self) -> None:
        with (
            tempfile.TemporaryDirectory() as root_tmp,
            tempfile.TemporaryDirectory() as outside_tmp,
        ):
            root = pathlib.Path(root_tmp)
            outside = pathlib.Path(outside_tmp)
            (outside / "advisory.md").write_text("outside\n", encoding="utf-8")
            (root / "crates").symlink_to(outside, target_is_directory=True)

            with self.assertRaisesRegex(
                ValueError, "escapes the pinned snapshot root"
            ):
                audit.confined_snapshot_path(root, "crates/advisory.md")

    def test_strict_snapshot_paths_reject_empty_and_dot_segments(self) -> None:
        self.assertIsNone(audit.strict_posix_relative_path("crates//advisory.md"))
        self.assertIsNone(audit.strict_posix_relative_path("crates/./advisory.md"))


if __name__ == "__main__":
    unittest.main()
