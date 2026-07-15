#!/usr/bin/env python3
"""Cross-artifact regression tests for the final RustSec efficacy claim model."""

from __future__ import annotations

import collections
import hashlib
import json
import pathlib
import statistics
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
TYPEISO_AUTOMATIC_ROOT = (
    ROOT / "docs/evidence/rustsec-typeiso-automatic-20260715"
)
TYPEISO_AUTOMATIC_SUMMARY = TYPEISO_AUTOMATIC_ROOT / "derived-reuse-summary.json"
TYPEISO_AUTOMATIC_RESULTS = TYPEISO_AUTOMATIC_ROOT / "mechanism-results.json"
TYPEISO_PERFORMANCE_SCREEN = TYPEISO_AUTOMATIC_ROOT / "performance-screen/summary.json"
TYPEISO_EXPERIMENTS = {
    case_id: ROOT
    / "docs/evidence/rustsec-typeiso-automatic-20260715/raw"
    / f"{case_id}-experiment.json"
    for case_id in (
        "RSH-002",
        "RSH-008",
        "RSH-041",
        "RSH-042",
        "RSH-052",
        "RSH-055",
        "RSH-064",
        "RSH-065",
        "RSH-066",
        "RSH-067",
        "RSH-068",
        "RSH-069",
    )
}
TYPEISO_MITIGATED_CASES = set(TYPEISO_EXPERIMENTS)
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
    "type_isolation_only": TYPEISO_MITIGATED_CASES - {"RSH-002"},
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
    "case_type_isolation_mitigated": 12,
    "type_isolation_edge_covered": 12,
    "mechanism_overlap_observed_count": 1,
    "automatic_full_source_vulnerability_detection_cases": 0,
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

        positives = [
            row
            for row in rows
            if (
                row.get("mechanism") == "type_isolation"
                and row.get("validated_mitigation") is True
                and "true_positive" not in row
            )
            or (
                row.get("mechanism") != "type_isolation"
                and row.get("true_positive") is True
                and "validated_mitigation" not in row
            )
        ]
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
        self.assertEqual({row["case_id"] for row in typeiso_rows}, TYPEISO_MITIGATED_CASES)
        for row in typeiso_rows:
            self.assertEqual(row["outcome"], "mitigated")
            self.assertIs(row["validated_mitigation"], True)
            self.assertNotIn("true_positive", row)
            self.assertEqual(
                row["result_semantics"],
                "causal_compiler_bound_reuse_edge_mitigation",
            )
            compiler_automatic = row["compiler_automatic_victim_coverage"]
            self.assertIsInstance(compiler_automatic, bool)
            automatic_probe = row.get("automatic_edge_identity_probe", False)
            self.assertIsInstance(automatic_probe, bool)
            self.assertIs(compiler_automatic, automatic_probe)
            manual_annotation = row.get("manual_victim_identity_annotation")
            if automatic_probe:
                self.assertIs(manual_annotation, False)
            else:
                self.assertIn(manual_annotation, (None, True))
            self.assertIs(row["source_vulnerability_detection_validated"], False)
            self.assertIn(
                row.get("vulnerability_specific_detection_signal"),
                (None, False),
            )
            self.assertIn(
                row.get("full_source_vulnerability_detection"),
                (None, False),
            )
            self.assertIs(row["automatic_source_coverage"], False)
            self.assertNotIn("source_level_true_positive", row)
            self.assertIs(row["claim_grade"], False)
            self.assertEqual(
                row["positive_scope"], "exploit_enabling_cross_identity_reuse_edge"
            )
            synthetic = row["case_id"] in {"RSH-065", "RSH-069"}
            self.assertIs(row["synthetic_reduction"], synthetic)
            self.assertEqual(
                row["reduction_fidelity"],
                (
                    "compiler_automatic_synthetic_reduction"
                    if automatic_probe and synthetic
                    else "compiler_automatic_derived_reduction"
                    if automatic_probe
                    else "synthetic_manual_reduction"
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
        self.assertEqual(set(bound_typeiso_results), TYPEISO_MITIGATED_CASES)
        for case_id, result in bound_typeiso_results.items():
            self.assertEqual(
                result["implementation_sha256"], next(iter(evidence_digests))
            )
            self.assertIs(result["automatic_source_coverage"], False)
            self.assertIs(result["validated_mitigation"], True)
            self.assertNotIn("true_positive", result)
            self.assertNotIn("source_level_true_positive", result)
            self.assertIs(result["claim_grade"], False)
            self.assertIn(
                result.get("vulnerability_specific_detection_signal"),
                (None, False),
            )
            self.assertIn(
                result.get("full_source_vulnerability_detection"),
                (None, False),
            )
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
                and result.get("validated_mitigation") is True
                and "true_positive" not in result
                and result.get("positive_scope")
                == "exploit_enabling_cross_identity_reuse_edge"
                and isinstance(
                    result.get("compiler_automatic_victim_coverage"), bool
                )
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
        self.assertEqual(
            terminal["strict_typeiso_evidence_implementation_digest_counts"],
            load(REPORT)["counts"][
                "strict_typeiso_evidence_implementation_digest_counts"
            ],
        )
        self.assertIn(
            "docs/evidence/rustsec-all-53-live-20260714/rsh064/mechanism-result.json",
            terminal_paths,
        )
        performance_screen_root = TYPEISO_PERFORMANCE_SCREEN.parent
        performance_screen_paths = {
            path.relative_to(ROOT).as_posix()
            for path in performance_screen_root.rglob("*")
            if path.is_file()
        }
        screen = load(TYPEISO_PERFORMANCE_SCREEN)
        expected_performance_screen_paths = {
            TYPEISO_PERFORMANCE_SCREEN.relative_to(ROOT).as_posix(),
            (performance_screen_root / "README.md").relative_to(ROOT).as_posix(),
            *(artifact["path"] for artifact in screen["raw_artifacts"]),
        }
        self.assertEqual(
            performance_screen_paths,
            expected_performance_screen_paths,
            "performance-screen directory contains an unpinned or missing file",
        )
        self.assertTrue(
            performance_screen_paths <= terminal_paths,
            sorted(performance_screen_paths - terminal_paths),
        )
        self.assertEqual(
            terminal["counts"]["performance_screen_artifacts"],
            len(performance_screen_paths),
        )
        automatic_bundle_paths = {
            path.relative_to(ROOT).as_posix()
            for path in TYPEISO_AUTOMATIC_ROOT.rglob("*")
            if path.is_file()
        }
        terminal_automatic_paths = {
            path
            for path in terminal_paths
            if path.startswith(
                TYPEISO_AUTOMATIC_ROOT.relative_to(ROOT).as_posix() + "/"
            )
        }
        self.assertEqual(
            terminal_automatic_paths,
            automatic_bundle_paths,
            "automatic Type Isolation bundle contains an unpinned or missing file",
        )
        self.assertEqual(
            terminal["counts"]["automatic_typeiso_bundle_artifacts"],
            len(automatic_bundle_paths),
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

        summary = load(TYPEISO_AUTOMATIC_SUMMARY)
        self.assertFalse(summary["claim_grade"])
        self.assertEqual(
            summary["counts"]["validated_cross_identity_reuse_edge_count"], 12
        )
        self.assertEqual(
            summary["counts"]["compiler_automatic_victim_coverage_count"], 12
        )
        self.assertEqual(
            summary["counts"]["vulnerability_specific_detection_signal_count"], 0
        )
        self.assertEqual(
            summary["counts"]["full_source_vulnerability_detection_count"], 0
        )
        self.assertIn(
            "causal mitigation of compiler-bound measured cross-identity reuse edges",
            summary["boundary"],
        )
        self.assertEqual(
            {
                row["case_id"]: row["sha256"]
                for row in summary["raw_experiment_inputs"]
            },
            experiment_hashes,
        )
        self.assertEqual(
            {row["case_id"] for row in summary["scenarios"]},
            TYPEISO_MITIGATED_CASES,
        )
        for scenario in summary["scenarios"]:
            self.assertIs(scenario["automatic_edge_identity_probe"], True)
            edge = scenario["edge_evaluation"]
            self.assertIs(edge["compiler_automatic_victim_coverage"], True)
            self.assertIs(
                edge["causal_compiler_bound_reuse_edge_mitigation"], True
            )
            self.assertIs(edge["vulnerability_specific_detection_signal"], False)
            self.assertIs(edge["source_vulnerability_detection_validated"], False)

        mechanism_results = load(TYPEISO_AUTOMATIC_RESULTS)["results"]
        self.assertEqual(len(mechanism_results), 12)
        self.assertEqual(
            {row["case_id"] for row in mechanism_results},
            TYPEISO_MITIGATED_CASES,
        )
        for row in mechanism_results:
            self.assertEqual(row["mechanism"], "type_isolation")
            self.assertEqual(row["outcome"], "mitigated")
            self.assertIs(row["validated_mitigation"], True)
            self.assertNotIn("true_positive", row)
            self.assertEqual(
                row["result_semantics"],
                "causal_compiler_bound_reuse_edge_mitigation",
            )
            self.assertIs(row["vulnerability_specific_detection_signal"], False)
            self.assertIs(row["full_source_vulnerability_detection"], False)

    def test_compiler_pass_performance_screen_is_recomputable(self) -> None:
        screen = load(TYPEISO_PERFORMANCE_SCREEN)
        self.assertIs(screen["passed"], True)
        self.assertIs(screen["claim_grade"], False)
        self.assertEqual(screen["protocol"]["warmups_per_run"], 3)
        self.assertEqual(screen["protocol"]["measured_repetitions_per_run"], 15)
        self.assertEqual(screen["protocol"]["cpu_list"], "20")
        self.assertEqual(screen["protocol"]["numa_node"], 0)
        self.assertEqual(
            screen["identity_gates"]["google_tcmalloc_revision"],
            "12f255231938d30493186b0a037feedd70f5a1c1",
        )
        self.assertEqual(screen["identity_gates"]["hpaa_active"], 1)
        self.assertEqual(screen["identity_gates"]["malloc_provider_is_self"], 1)
        self.assertIs(
            screen["identity_gates"]["per_target_tcmalloc_marker_gate"], True
        )
        self.assertIs(screen["identity_gates"]["performance_stats_disabled"], True)

        raw: dict[str, dict[str, object]] = {}
        for artifact in screen["raw_artifacts"]:
            path = ROOT / artifact["path"]
            self.assertEqual(path.stat().st_size, artifact["bytes"])
            self.assertEqual(sha256_file(path), artifact["sha256"])
            raw[artifact["id"]] = load(path)
        self.assertEqual(len(raw), 10)

        candidate_digests = {
            raw[name]["implementation_sha256"]
            for name in raw
            if name.startswith("candidate_")
        }
        self.assertEqual(len(candidate_digests), 1)
        candidate_pass_hashes = {
            raw[name]["pass_source_sha256"]
            for name in raw
            if name.startswith("candidate_")
        }
        self.assertEqual(
            candidate_pass_hashes,
            {
                sha256_file(
                    ROOT
                    / "tools/unialloc-rustc-pass/"
                    "unialloc-rustc-mir-rewrite-dry-run.rs"
                )
            },
        )

        def paired_ratios(
            document: dict[str, object], app: str, metric: str
        ) -> list[float]:
            rows = [
                row
                for row in document["measurements"]
                if row["app"] == app and row["warmup"] is False
            ]
            indexed = {
                (row["variant"], row["round"]): row
                for row in rows
            }
            rounds = sorted(
                row["round"]
                for row in rows
                if row["variant"] == "typed_plain"
            )
            return [
                indexed[("typeiso_perf", round_index)][metric]
                / indexed[("typed_plain", round_index)][metric]
                for round_index in rounds
            ]

        for app, prefix in (("oxipng", "broad"), ("ripgrep", "ripgrep_long")):
            measurements: dict[str, dict[str, list[float]]] = {
                "wall": {"baseline": [], "candidate": []},
                "peak_rss": {"baseline": [], "candidate": []},
            }
            for order in ("a", "b"):
                for label, metric in (
                    ("wall", "wall_seconds"),
                    ("peak_rss", "peak_rss_kib"),
                ):
                    for version in ("baseline", "candidate"):
                        measurements[label][version].extend(
                            paired_ratios(
                                raw[f"{version}_{prefix}_{order}"], app, metric
                            )
                        )
            workload = "ripgrep_long" if app == "ripgrep" else app
            for label, values in measurements.items():
                baseline = values["baseline"]
                candidate = values["candidate"]
                self.assertEqual(len(baseline), 30)
                self.assertEqual(len(candidate), 30)
                delta = (
                    statistics.median(candidate) / statistics.median(baseline) - 1
                )
                recorded = screen["workloads"][workload][label]
                self.assertAlmostEqual(
                    delta,
                    recorded["candidate_vs_baseline_ratio_of_ratios_delta"],
                )
            wall_delta = screen["workloads"][workload]["wall"][
                "candidate_vs_baseline_ratio_of_ratios_delta"
            ]
            self.assertLessEqual(
                wall_delta,
                screen["thresholds"]["maximum_wall_ratio_of_ratios_regression"],
            )

        ripgrep_rss = screen["workloads"]["ripgrep_long"]["peak_rss"]
        self.assertLessEqual(
            ripgrep_rss["candidate_max_peak_rss_kib"],
            ripgrep_rss["baseline_max_peak_rss_kib"],
        )
        self.assertLessEqual(
            screen["workloads"]["oxipng"]["peak_rss"][
                "candidate_vs_baseline_ratio_of_ratios_delta"
            ],
            screen["thresholds"][
                "maximum_oxipng_rss_ratio_of_ratios_regression"
            ],
        )


if __name__ == "__main__":
    unittest.main()
