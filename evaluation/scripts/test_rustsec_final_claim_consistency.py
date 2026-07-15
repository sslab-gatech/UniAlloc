#!/usr/bin/env python3
"""Cross-artifact regression tests for the final RustSec efficacy claim model."""

from __future__ import annotations

import collections
import hashlib
import json
import pathlib
import tarfile
import unittest


ROOT = pathlib.Path(__file__).resolve().parents[2]
LEDGER = ROOT / "evaluation/config/rustsec_heap_mechanism_results.json"
SCOPE = ROOT / "evaluation/config/rustsec_heap_complete_scope.json"
REPORT = (
    ROOT
    / "docs/evidence/rustsec-all-53-live-20260714/evaluable-49-report.json"
)
HISTORICAL_ATTEMPTS_REPORT = (
    ROOT / "docs/evidence/rustsec-all-53-live-20260714/all-53-report.json"
)
FIGURE_SUMMARY = ROOT / "docs/figures/rustsec-security-scope-20260714/summary.json"
ALL_53_HASH_MANIFEST = (
    ROOT / "docs/evidence/rustsec-all-53-live-20260714/artifact-hashes.json"
)
TERMINAL_HASH_MANIFEST = (
    ROOT
    / "docs/evidence/rustsec-security-complete-20260714/terminal-artifact-hashes.json"
)
TYPEISO_SNAPSHOT_SUMMARY = (
    ROOT
    / "docs/evidence/rustsec-security-complete-20260714/typeiso-frozen-snapshot-summary.json"
)
TYPEISO_IMPLEMENTATION_MANIFEST = (
    ROOT
    / "docs/evidence/rustsec-security-complete-20260714/"
    "typeiso-frozen-implementation-ce653fd5-manifest.json"
)
TYPEISO_EXPERIMENTS = {
    "RSH-002": ROOT / "docs/evidence/rustsec-rsh002-typeiso-20260714/experiment.json",
    "RSH-008": ROOT / "docs/evidence/rustsec-rsh008-typeiso-20260714/experiment.json",
    "RSH-041": ROOT
    / "docs/evidence/rustsec-security-expansion-20260714/raw/derived/RSH-041-derived-reuse-experiment.json",
    "RSH-042": ROOT
    / "docs/evidence/rustsec-security-expansion-20260714/raw/derived/RSH-042-derived-reuse-experiment.json",
    "RSH-052": ROOT / "docs/evidence/rustsec-rsh052-typeiso-20260714/experiment.json",
    "RSH-055": ROOT / "docs/evidence/rustsec-rsh055-typeiso-20260715/experiment.json",
    "RSH-064": ROOT / "docs/evidence/rustsec-rsh064-typeiso-20260714/experiment.json",
    "RSH-065": ROOT / "docs/evidence/rustsec-rsh065-typeiso-20260715/experiment.json",
    "RSH-066": ROOT / "docs/evidence/rustsec-rsh066-typeiso-20260715/experiment.json",
    "RSH-067": ROOT / "docs/evidence/rustsec-rsh067-typeiso-20260715/experiment.json",
    "RSH-068": ROOT / "docs/evidence/rustsec-rsh068-typeiso-20260715/experiment.json",
    "RSH-069": ROOT / "docs/evidence/rustsec-rsh069-typeiso-20260715/experiment.json",
}
TYPEISO_POSITIVE_CASES = set(TYPEISO_EXPERIMENTS)
EXPECTED_ATTRIBUTION_CASES = {
    "other_allocator_feature_only": {
        "RSH-001",
        "RSH-005",
        "RSH-009",
        "RSH-010",
        "RSH-011",
        "RSH-012",
        "RSH-013",
        "RSH-014",
        "RSH-018",
        "RSH-020",
        "RSH-021",
        "RSH-031",
        "RSH-043",
        "RSH-044",
        "RSH-045",
        "RSH-046",
        "RSH-047",
        "RSH-048",
        "RSH-051",
        "RSH-053",
        "RSH-057",
        "RSH-058",
        "RSH-060",
        "RSH-061",
        "RSH-062",
        "RSH-063",
        "RSH-070",
        "RSH-071",
        "RSH-072",
        "RSH-074",
        "RSH-076",
    },
    "type_isolation_only": TYPEISO_POSITIVE_CASES - {"RSH-002"},
    "multiple_allocator_mechanisms": {"RSH-002"},
    "no_allocator_signal_observed": {"RSH-049", "RSH-050", "RSH-075"},
    "not_yet_attributed": {"RSH-003", "RSH-006", "RSH-019"},
}
EXPECTED_REPORT_OUTCOME_CASES = {
    "detected": EXPECTED_ATTRIBUTION_CASES["other_allocator_feature_only"]
    | {"RSH-002"},
    "mitigated": EXPECTED_ATTRIBUTION_CASES["type_isolation_only"],
    "no_signal": EXPECTED_ATTRIBUTION_CASES["no_allocator_signal_observed"],
    "inconclusive": EXPECTED_ATTRIBUTION_CASES["not_yet_attributed"],
}
EXPECTED_ALL_53_MANIFEST_COUNTS = {
    "scope_case_count": 53,
    "evaluable_scope_case_count": 49,
    "executable_case_count": 49,
    "blocked_case_count": 4,
    "excluded_before_evaluation_case_count": 4,
    "raw_exact_allocator_signal_case_count": 43,
    "strict_positive_case_count": 43,
}
EXPECTED_TERMINAL_MANIFEST_COUNTS = {
    "reviewed_strong_candidates": 53,
    "evaluable_scope_cases": 49,
    "executable": 49,
    "blocked": 4,
    "excluded_before_evaluation": 4,
    "mechanism_result_rows": 62,
    "mechanism_detected_rows": 32,
    "mechanism_mitigated_rows": 12,
    "mechanism_no_signal_rows": 12,
    "mechanism_inconclusive_rows": 6,
    "strict_positive_mechanism_rows": 44,
    "strict_positive_cases": 43,
    "raw_exact_allocator_signal_cases": 43,
    "raw_exact_reclaim_check_scenarios": 31,
    "reclaim_checks_detected": 31,
    "recovery_layout_validation_detected": 1,
    "case_reclaim_checks_positive": 31,
    "case_type_isolation_positive": 12,
    "type_isolation_edge_covered": 12,
    "mechanism_overlap_observed_count": 1,
    "automatic_source_type_isolation_true_positive_cases": 0,
    "case_detected": 32,
    "case_mitigated": 11,
    "case_matched_no_signal": 3,
    "no_allocator_signal_observed": 3,
    "case_unresolved_allocator_mechanism": 3,
    "integrated_incomplete_or_inconclusive": 3,
}


def load(path: pathlib.Path) -> dict[str, object]:
    return json.loads(path.read_text(encoding="utf-8"))


def sha256_file(path: pathlib.Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def canonical_sha256(value: object) -> str:
    return hashlib.sha256(
        json.dumps(
            value, sort_keys=True, separators=(",", ":"), ensure_ascii=True
        ).encode("utf-8")
    ).hexdigest()


class RustSecFinalClaimConsistencyTests(unittest.TestCase):
    def assert_hash_manifest_integrity(
        self,
        manifest_path: pathlib.Path,
        *,
        entries_key: str,
        artifact_root: pathlib.Path,
    ) -> set[str]:
        manifest = load(manifest_path)
        entries = manifest[entries_key]
        self.assertIsInstance(entries, list)
        paths = [entry["path"] for entry in entries]
        self.assertEqual(len(paths), len(set(paths)), "duplicate manifest paths")
        self.assertEqual(paths, sorted(paths), "manifest paths must be deterministic")
        artifact_root = artifact_root.resolve()
        for entry in entries:
            relative = pathlib.PurePosixPath(entry["path"])
            self.assertFalse(relative.is_absolute(), entry["path"])
            self.assertNotIn("..", relative.parts, entry["path"])
            artifact = artifact_root.joinpath(*relative.parts).resolve()
            self.assertTrue(artifact.is_relative_to(artifact_root), entry["path"])
            self.assertTrue(artifact.is_file(), entry["path"])
            self.assertEqual(artifact.stat().st_size, entry["bytes"], entry["path"])
            self.assertEqual(sha256_file(artifact), entry["sha256"], entry["path"])
        return set(paths)

    def test_mechanism_and_case_counts_preserve_the_overlap(self) -> None:
        ledger = load(LEDGER)
        rows = ledger["results"]
        self.assertFalse(ledger["claim_grade"])
        self.assertEqual(len(rows), 62)
        self.assertEqual(
            collections.Counter(row["outcome"] for row in rows),
            {"detected": 32, "mitigated": 12, "no_signal": 12, "inconclusive": 6},
        )

        positives = [row for row in rows if row.get("true_positive") is True]
        self.assertEqual(len(positives), 44)
        self.assertEqual(len({row["case_id"] for row in positives}), 43)
        self.assertEqual(
            collections.Counter(row["mechanism"] for row in positives),
            {
                "reclaim_checks": 31,
                "recovery_layout_validation": 1,
                "type_isolation": 12,
            },
        )
        mechanisms_by_case: dict[str, set[str]] = collections.defaultdict(set)
        for row in positives:
            mechanisms_by_case[row["case_id"]].add(row["mechanism"])
        self.assertEqual(
            {
                case_id: mechanisms
                for case_id, mechanisms in mechanisms_by_case.items()
                if len(mechanisms) > 1
            },
            {"RSH-002": {"reclaim_checks", "type_isolation"}},
        )

        typeiso_rows = [
            row
            for row in positives
            if row["mechanism"] == "type_isolation"
        ]
        self.assertEqual({row["case_id"] for row in typeiso_rows}, TYPEISO_POSITIVE_CASES)
        for row in typeiso_rows:
            self.assertEqual(row["outcome"], "mitigated")
            self.assertIs(row["compiler_automatic_victim_coverage"], False)
            self.assertIs(row["source_vulnerability_detection_validated"], False)
            self.assertIs(row["automatic_source_coverage"], False)
            self.assertIs(row["source_level_true_positive"], False)
            self.assertIs(row["claim_grade"], False)
            self.assertEqual(
                row["positive_scope"], "exploit_enabling_cross_identity_reuse_edge"
            )
            synthetic = row["case_id"] in {"RSH-065", "RSH-069"}
            self.assertIs(row["synthetic_reduction"], synthetic)
            self.assertEqual(
                row["reduction_fidelity"],
                (
                    "synthetic_manual_reduction"
                    if synthetic
                    else "manual_derived_reduction"
                ),
            )

    def test_historical_all_53_report_cannot_masquerade_as_terminal_efficacy(self) -> None:
        report = load(HISTORICAL_ATTEMPTS_REPORT)
        self.assertEqual(report["report_role"], "historical_attempts_audit")
        self.assertEqual(
            report["superseded_by"],
            "docs/evidence/rustsec-all-53-live-20260714/evaluable-49-report.json",
        )
        self.assertIn("historical attempts audit", report["claim_boundary"])
        self.assertIn("superseded", report["claim_boundary"])
        self.assertEqual(
            report["report_sha256"],
            canonical_sha256(
                {
                    key: value
                    for key, value in report.items()
                    if key != "report_sha256"
                }
            ),
        )

    def test_scope_report_and_figure_use_the_same_case_partition(self) -> None:
        scope = load(SCOPE)
        report = load(REPORT)
        figure = load(FIGURE_SUMMARY)
        attribution = {
            "other_allocator_feature_only": 31,
            "type_isolation_only": 11,
            "multiple_allocator_mechanisms": 1,
            "no_allocator_signal_observed": 3,
            "not_yet_attributed": 3,
        }
        self.assertFalse(scope["claim_grade"])
        self.assertEqual(len(scope["cases"]), 49)
        self.assertEqual(len(scope["excluded_cases"]), 4)
        self.assertEqual(scope["counts"]["final_attribution"], attribution)
        self.assertEqual(scope["counts"]["other_feature_observed_count"], 32)
        self.assertEqual(scope["counts"]["type_isolation_observed_count"], 12)
        self.assertEqual(scope["counts"]["mechanism_overlap_observed_count"], 1)

        scope_case_ids = [row["case_id"] for row in scope["cases"]]
        self.assertEqual(len(scope_case_ids), len(set(scope_case_ids)))
        scope_partition = {
            key: [
                row["case_id"]
                for row in scope["cases"]
                if row["final_attribution"] == key
            ]
            for key in EXPECTED_ATTRIBUTION_CASES
        }
        self.assertEqual(
            {key: set(case_ids) for key, case_ids in scope_partition.items()},
            EXPECTED_ATTRIBUTION_CASES,
        )

        self.assertTrue(report["valid"])
        self.assertEqual(
            report["counts"]["outcome"],
            {"detected": 32, "mitigated": 11, "no_signal": 3, "inconclusive": 3},
        )
        self.assertEqual(report["counts"]["raw_exact_allocator_signal_case_count"], 43)
        self.assertEqual(report["counts"]["reclaim_exact_signal_case_count"], 31)
        self.assertEqual(report["counts"]["layout_validation_signal_case_count"], 1)
        self.assertEqual(report["counts"]["typeiso_reuse_denial_case_count"], 12)
        evidence_digests = {
            load(path)["typeiso_toolchain"]["force_build"]["implementation_sha256"]
            for path in TYPEISO_EXPERIMENTS.values()
        }
        self.assertEqual(len(evidence_digests), 1)
        self.assertEqual(
            report["counts"][
                "strict_typeiso_evidence_implementation_digest_counts"
            ],
            {next(iter(evidence_digests)): 12},
        )
        self.assertIn(
            "replay_arm_implementation_digest_counts", report["counts"]
        )
        self.assertNotIn("implementation_digest_arm_counts", report["counts"])
        report_outcome_cases: dict[str, set[str]] = collections.defaultdict(set)
        for row in report["cases"]:
            report_outcome_cases[row["outcome"]].add(row["case_id"])
        self.assertEqual(dict(report_outcome_cases), EXPECTED_REPORT_OUTCOME_CASES)

        bound_typeiso_results = {
            row["case_id"]: result
            for row in report["cases"]
            for result in row.get("strict_positive_mechanism_results", [])
            if result["mechanism"] == "type_isolation"
        }
        self.assertEqual(set(bound_typeiso_results), TYPEISO_POSITIVE_CASES)
        for case_id, result in bound_typeiso_results.items():
            self.assertEqual(
                result["implementation_sha256"], next(iter(evidence_digests))
            )
            self.assertIs(result["automatic_source_coverage"], False)
            self.assertIs(result["source_level_true_positive"], False)
            self.assertIs(result["claim_grade"], False)
            self.assertIs(
                result["synthetic_reduction"],
                case_id in {"RSH-065", "RSH-069"},
            )

        self.assertEqual(figure["case_attribution_counts"], attribution)
        self.assertEqual(figure["case_attribution_case_ids"], scope_partition)

        expected_mechanism_case_ids = {
            "type_isolation_edge_covered": [],
            "reclaim_checks_exact_detection": [],
            "recovery_layout_validation_exact_detection": [],
        }
        for row in scope["cases"]:
            results = row["mechanism_results"]
            if any(
                result["mechanism"] == "type_isolation"
                and result["outcome"] == "mitigated"
                and result.get("true_positive") is True
                and result.get("positive_scope")
                == "exploit_enabling_cross_identity_reuse_edge"
                and result.get("compiler_automatic_victim_coverage") is False
                and result.get("source_vulnerability_detection_validated") is False
                for result in results
            ):
                expected_mechanism_case_ids["type_isolation_edge_covered"].append(
                    row["case_id"]
                )
            if any(
                result["mechanism"] == "reclaim_checks"
                and result["outcome"] == "detected"
                and result.get("true_positive") is True
                for result in results
            ):
                expected_mechanism_case_ids[
                    "reclaim_checks_exact_detection"
                ].append(row["case_id"])
            if any(
                result["mechanism"] == "recovery_layout_validation"
                and result["outcome"] == "detected"
                and result.get("true_positive") is True
                for result in results
            ):
                expected_mechanism_case_ids[
                    "recovery_layout_validation_exact_detection"
                ].append(row["case_id"])
        self.assertEqual(
            figure["mechanism_coverage_case_ids"], expected_mechanism_case_ids
        )
        self.assertEqual(
            figure["mechanism_coverage_counts"],
            {
                "reclaim_checks_exact_detection": 31,
                "recovery_layout_validation_exact_detection": 1,
                "type_isolation_edge_covered": 12,
            },
        )
        self.assertEqual(figure["accounted_case_count"], 49)

    def test_final_hash_manifests_are_complete_and_content_addressed(self) -> None:
        all_53_paths = self.assert_hash_manifest_integrity(
            ALL_53_HASH_MANIFEST,
            entries_key="files",
            artifact_root=ALL_53_HASH_MANIFEST.parent,
        )
        all_53 = load(ALL_53_HASH_MANIFEST)
        self.assertEqual(all_53["counts"]["retained_file_count"], len(all_53_paths))
        for key, value in EXPECTED_ALL_53_MANIFEST_COUNTS.items():
            self.assertEqual(all_53["counts"].get(key), value, key)
        self.assertIn("rsh064/mechanism-result.json", all_53_paths)

        terminal_paths = self.assert_hash_manifest_integrity(
            TERMINAL_HASH_MANIFEST,
            entries_key="artifacts",
            artifact_root=ROOT,
        )
        terminal = load(TERMINAL_HASH_MANIFEST)
        for key, value in EXPECTED_TERMINAL_MANIFEST_COUNTS.items():
            self.assertEqual(terminal["counts"].get(key), value, key)
        self.assertIn(
            "docs/evidence/rustsec-all-53-live-20260714/rsh064/mechanism-result.json",
            terminal_paths,
        )

    def test_all_typeiso_edge_matrices_share_one_implementation_snapshot(self) -> None:
        implementation_digests: set[str] = set()
        experiment_hashes: dict[str, str] = {}
        for case_id, path in TYPEISO_EXPERIMENTS.items():
            experiment = load(path)
            edge = experiment["type_isolation_reuse_edge_evaluation"]
            self.assertTrue(edge["validated"], case_id)
            arm_digests = {
                arm["allocator_provenance"]["unialloc_implementation_sha256"]
                for arm in experiment["arms"]
            }
            self.assertEqual(len(arm_digests), 1, case_id)
            tool_digest = experiment["typeiso_toolchain"]["force_build"][
                "implementation_sha256"
            ]
            self.assertEqual(arm_digests, {tool_digest}, case_id)
            implementation_digests.add(tool_digest)
            experiment_hashes[case_id] = sha256_file(path)
        self.assertEqual(len(implementation_digests), 1)

        snapshot = load(TYPEISO_SNAPSHOT_SUMMARY)
        self.assertFalse(snapshot["claim_grade"])
        self.assertEqual(
            snapshot["snapshot_status"], "frozen_isolated_wip_evidence_snapshot"
        )
        self.assertEqual(snapshot["case_count"], 12)
        self.assertEqual(set(snapshot["case_ids"]), TYPEISO_POSITIVE_CASES)
        self.assertEqual(snapshot["manual_derived_edge_case_count"], 12)
        self.assertEqual(snapshot["automatic_source_true_positive_case_count"], 0)
        self.assertEqual(
            set(snapshot["synthetic_reduction_case_ids"]),
            {"RSH-065", "RSH-069"},
        )
        self.assertEqual(
            snapshot["implementation_sha256"], next(iter(implementation_digests))
        )
        self.assertEqual(
            {row["case"]: row["experiment_sha256"] for row in snapshot["results"]},
            experiment_hashes,
        )
        self.assertIn("does not assert equality", snapshot["claim_boundary"])

        implementation_manifest = load(TYPEISO_IMPLEMENTATION_MANIFEST)
        self.assertFalse(implementation_manifest["claim_grade"])
        self.assertEqual(
            implementation_manifest["implementation_sha256"],
            snapshot["implementation_sha256"],
        )
        implementation_files = implementation_manifest["files"]
        implementation_paths = [row["path"] for row in implementation_files]
        self.assertEqual(implementation_paths, sorted(implementation_paths))
        self.assertEqual(len(implementation_paths), len(set(implementation_paths)))
        self.assertEqual(
            implementation_manifest["file_count"], len(implementation_paths)
        )
        pass_record = next(
            row
            for row in implementation_files
            if row["path"]
            == "tools/unialloc-rustc-pass/unialloc-rustc-mir-rewrite-dry-run.rs"
        )
        self.assertEqual(pass_record["sha256"], snapshot["pass_sha256"])
        archive_record = implementation_manifest["archive"]
        archive_path = ROOT / archive_record["path"]
        self.assertEqual(archive_path.stat().st_size, archive_record["bytes"])
        self.assertEqual(sha256_file(archive_path), archive_record["sha256"])

        combined = hashlib.sha256()
        with tarfile.open(archive_path, "r:gz") as archive:
            members = [member for member in archive.getmembers() if member.isfile()]
            self.assertEqual([member.name for member in members], implementation_paths)
            for member, record in zip(members, implementation_files, strict=True):
                self.assertNotIn("..", pathlib.PurePosixPath(member.name).parts)
                handle = archive.extractfile(member)
                self.assertIsNotNone(handle)
                data = handle.read()
                self.assertEqual(len(data), record["bytes"], member.name)
                self.assertEqual(
                    hashlib.sha256(data).hexdigest(), record["sha256"], member.name
                )
                combined.update(member.name.encode())
                combined.update(b"\0")
                combined.update(data)
        self.assertEqual(
            combined.hexdigest(), implementation_manifest["implementation_sha256"]
        )


if __name__ == "__main__":
    unittest.main()
