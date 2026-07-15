#!/usr/bin/env python3
"""Tests for the evaluable-scope RustSec runner and exclusion audit."""

from __future__ import annotations

import contextlib
import csv
import importlib.util
import io
import json
import pathlib
import sys
import tempfile
import unittest


ROOT = pathlib.Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "evaluation" / "scripts" / "run_rustsec_complete_scope.py"
spec = importlib.util.spec_from_file_location("rustsec_complete_scope_runner", SCRIPT)
assert spec is not None and spec.loader is not None
runner = importlib.util.module_from_spec(spec)
spec.loader.exec_module(runner)


class RustSecCompleteScopeRunnerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = pathlib.Path(self.temporary.name)
        self.scope = self.root / "scope.json"
        self.catalog = self.root / "catalog.json"
        self.replay = self.root / "replay.json"
        self.blocked = self.root / "blocked.json"
        self.output_json = self.root / "report.json"
        self.output_csv = self.root / "report.csv"
        self.log_dir = self.root / "logs"
        self.binary = self.root / "frozen-binary"
        self.binary.write_bytes(b"frozen allocator binary\n")
        self.binary_sha256 = runner.sweep.sha256_file(self.binary)
        self.implementation_sha256 = "b" * 64
        self._write_fixture()

    @staticmethod
    def case_id(index: int) -> str:
        return "RSH-064" if index == 49 else f"RSH-{index:03d}"

    @staticmethod
    def excluded_case_ids() -> tuple[str, ...]:
        return ("RSH-054", "RSH-056", "RSH-059", "RSH-073")

    def write_json(self, path: pathlib.Path, value: object) -> None:
        path.write_text(json.dumps(value, indent=2) + "\n", encoding="utf-8")

    def _write_fixture(self) -> None:
        scope_cases: list[dict[str, object]] = []
        excluded_cases: list[dict[str, object]] = []
        catalog_cases: list[dict[str, object]] = []
        strict_ledger = self.root / "strict-ledger.json"
        strict_ledger.write_text('{"validated":true}\n', encoding="utf-8")
        strict_ledger_artifact = {
            "path": str(strict_ledger),
            "bytes": strict_ledger.stat().st_size,
            "sha256": runner.sweep.sha256_file(strict_ledger),
        }
        for index in range(1, 50):
            case_id = self.case_id(index)
            selected = f"{case_id}-selected"
            scenarios = [selected]
            if index == 1:
                scenarios = [f"{case_id}-first", selected]
            mechanism_result: dict[str, object] = {
                "mechanism": "reclaim_checks",
                "scenario_id": selected,
                "outcome": "detected",
            }
            if index in (13, 20):
                mechanism_result.update(
                    {
                        "reason": "feature_matched_exact_allocator_diagnostic",
                        "true_positive": True,
                        "positive_scope": "duplicate_reclaim_event_in_integrated_witness",
                        "negative_boundary": None,
                        "claim_scope": "exact duplicate-reclaim detection",
                        "result_semantics": "exact_diagnostic_true_positive",
                        "repetitions": 3,
                        "evidence": strict_ledger_artifact,
                        "allocator_signal_signatures": {
                            "unialloc_pointer_already_released_check": 3
                        },
                        "checks": {
                            "baseline_reproduced_in_all_repetitions": True,
                            "exact_allocator_signal_in_all_repetitions": True,
                            "feature_matched_reclaim_ablation_valid": True,
                            "patched_reclaim_plain_control_valid": True,
                            "patched_system_control_valid": True,
                            "patched_treatment_signal_free": True,
                            "vulnerable_reclaim_plain_completed_in_all_repetitions": True,
                            "vulnerable_reclaim_plain_exact_signal_absent": True,
                            "vulnerable_treatment_completed_in_all_repetitions": True,
                            "vulnerable_treatment_status_valid": True,
                        },
                        "feature_provenance": {
                            name: {"unialloc_implementation_sha256": "c" * 64}
                            for name in (
                                "patched_reclaim_checks",
                                "patched_reclaim_plain",
                                "vulnerable_reclaim_checks",
                                "vulnerable_reclaim_plain",
                            )
                        },
                        "feature_provenance_failures": [],
                    }
                )
            scope_cases.append(
                {
                    "case_id": case_id,
                    "advisory_id": f"RUSTSEC-TEST-{index:04d}",
                    "package": f"crate-{index}",
                    "primary_primitive": "use_after_free" if index == 1 else "double_free",
                    "integration": {
                        "status": "executable",
                        "scenario_ids": scenarios,
                        "blocker_reason": None,
                    },
                    "mechanism_results": [mechanism_result],
                }
            )
            catalog_scenarios = []
            for scenario_id in scenarios:
                catalog_scenarios.append(
                    {
                        "scenario_id": scenario_id,
                        "source_path": f"evaluation/harnesses/{case_id}/main.rs",
                        "oracle": {
                            "tool": "asan",
                            "vulnerable": f"root cause oracle for {scenario_id}",
                        },
                    }
                )
            catalog_cases.append(
                {"case_id": case_id, "scenarios": catalog_scenarios}
            )

        for index, case_id in enumerate(self.excluded_case_ids(), start=50):
            excluded_cases.append(
                {
                    "case_id": case_id,
                    "advisory_id": f"RUSTSEC-TEST-{index:04d}",
                    "package": f"excluded-crate-{index}",
                    "primary_primitive": "double_free",
                    "integration": {
                        "status": "blocked",
                        "scenario_ids": [],
                        "blocker_reason": f"native dependency {index} is unavailable",
                    },
                    "mechanism_results": [],
                    "scope_exclusion": {
                        "code": "no_executable_reproduction",
                        "reason": f"native dependency {index} is unavailable",
                        "evidence": [f"excluded/{case_id}.log"],
                    },
                }
            )
        self.write_json(
            self.scope,
            {
                "schema_version": 1,
                "source": "test-scope",
                "counts": {
                    "strong_candidate_count": 49,
                    "excluded_non_evaluable_count": 4,
                    "reviewed_strong_candidate_count": 53,
                    "integration_status": {"executable": 49},
                    "excluded_integration_status": {"blocked": 4},
                },
                "cases": scope_cases,
                "excluded_cases": excluded_cases,
            },
        )
        self.write_json(
            self.catalog,
            {"schema_version": 1, "source": "test-catalog", "cases": catalog_cases},
        )
        scope_sha256 = runner.sweep.sha256_file(self.scope)
        replay_cases: list[dict[str, object]] = []
        for index in range(1, 50):
            case_id = self.case_id(index)
            signals = {
                "address_reuse_observed": False,
                "asan": False,
                "miri_ub": False,
                "pointer_already_released": index in (1, 13, 20),
                "reuse_denial_report": None,
                "rust_panic": index in (1, 13, 20),
            }
            live_arms = [
                {
                    "case_id": case_id,
                    "scenario_id": f"{case_id}-selected",
                    "mechanism": "reclaim_checks",
                    "frozen_outcome": "detected",
                    "archive_variant": "vulnerable",
                    "allocator_variant": "reclaim_checks",
                    "attempt_status": "completed",
                    "exit_code": 101 if index in (1, 13, 20) else 0,
                    "timed_out": False,
                    "binary_path": str(self.binary),
                    "recorded_binary_sha256": self.binary_sha256,
                    "actual_binary_sha256": self.binary_sha256,
                    "frozen_unialloc_implementation_sha256": (
                        self.implementation_sha256
                    ),
                    "signals": signals,
                }
            ]
            if index in (13, 20):
                live_arms.append(
                    {
                        **live_arms[0],
                        "allocator_variant": "reclaim_plain",
                        "exit_code": 0,
                        "signals": {
                            **signals,
                            "pointer_already_released": False,
                            "rust_panic": False,
                        },
                    }
                )
            replay_cases.append(
                {
                    "case_id": case_id,
                    "advisory_id": f"RUSTSEC-TEST-{index:04d}",
                    "package": f"crate-{index}",
                    "primary_primitive": (
                        "use_after_free" if index == 1 else "double_free"
                    ),
                    "scenario_id": f"{case_id}-selected",
                    "mechanism": "reclaim_checks",
                    "frozen_outcome": "detected",
                    "frozen_reason": "synthetic exact diagnostic",
                    "live_attempt_status": "completed",
                    "live_arms": live_arms,
                }
            )
        self.write_json(
            self.replay,
            {
                "schema_version": 1,
                "source": "test-replay",
                "scope_sha256": scope_sha256,
                "cases": replay_cases,
            },
        )
        blocked_root = self.root / "blocked-evidence"
        blocked_root.mkdir()
        blocked_cases: list[dict[str, object]] = []
        for index, case_id in enumerate(self.excluded_case_ids(), start=50):
            evidence_path = blocked_root / f"{case_id}.log"
            evidence_path.write_text(f"attempted {case_id}\n", encoding="utf-8")
            blocked_cases.append(
                {
                    "case_id": case_id,
                    "advisory_id": f"RUSTSEC-TEST-{index:04d}",
                    "package": f"excluded-crate-{index}",
                    "primary_primitive": "double_free",
                    "prior_status": "blocked",
                    "current_status": "blocked",
                    "blocker_code": f"blocker-{index}",
                    "root_blocker": f"root blocker for {case_id}",
                    "conclusion": f"terminal probe conclusion for {case_id}",
                    "probes": [
                        {
                            "case_id": case_id,
                            "label": f"{case_id}-probe",
                            "command": f"probe {case_id}",
                            "exit_code": 1,
                            "outcome": f"probe outcome for {case_id}",
                            "evidence": [
                                {
                                    "path": evidence_path.name,
                                    "bytes": evidence_path.stat().st_size,
                                    "sha256": runner.sweep.sha256_file(evidence_path),
                                }
                            ],
                        }
                    ],
                }
            )
        self.write_json(
            self.blocked,
            {
                "schema_version": 1,
                "source": "test-blocked-attempts",
                "root": str(blocked_root),
                "cases": blocked_cases,
            },
        )

    def jobs(self) -> dict[str, dict[str, object]]:
        return runner.direct_verify_jobs(self.replay, self.blocked)

    def build(self, *, dry_run: bool = False) -> tuple[dict[str, object], bool]:
        return runner.build_report(
            scope_path=self.scope,
            catalog_paths=[self.catalog],
            jobs=self.jobs(),
            plan_path=None,
            output_json=self.output_json,
            output_csv=self.output_csv,
            log_dir=self.log_dir,
            dry_run=dry_run,
            execute_unsafe=False,
            default_timeout_seconds=10,
        )

    def strict_typeiso_result(
        self,
        *,
        case_id: str,
        scenario_id: str,
    ) -> dict[str, object]:
        experiment_path = self.root / f"{case_id}-typeiso-experiment.json"
        self.write_json(
            experiment_path,
            {
                "schema_version": 1,
                "claim_grade": False,
                "type_isolation_reuse_edge_evaluation": {"validated": True},
                "typeiso_toolchain": {
                    "force_build": {
                        "implementation_sha256": self.implementation_sha256
                    }
                },
                "arms": [
                    {
                        "allocator_provenance": {
                            "unialloc_implementation_sha256": (
                                self.implementation_sha256
                            )
                        }
                    }
                ],
            },
        )
        summary_path = self.root / f"{case_id}-typeiso-summary.json"
        self.write_json(
            summary_path,
            {
                "schema_version": 1,
                "claim_grade": False,
                "raw_experiment_inputs": [
                    {
                        "case_id": case_id,
                        "path": str(experiment_path),
                        "bytes": experiment_path.stat().st_size,
                        "sha256": runner.sweep.sha256_file(experiment_path),
                    }
                ],
            },
        )
        return {
            "case_id": case_id,
            "advisory_id": f"RUSTSEC-TEST-{int(case_id[-3:]):04d}",
            "mechanism": "type_isolation",
            "scenario_id": scenario_id,
            "outcome": "mitigated",
            "true_positive": True,
            "positive_scope": "exploit_enabling_cross_identity_reuse_edge",
            "negative_boundary": None,
            "reason": "matched_cross_identity_reuse_edge_blocked_and_reported",
            "result_semantics": "causal_mitigation_true_positive",
            "claim_scope": "derived cross-identity reuse edge only",
            "repetitions": 3,
            "source_vulnerability_detection_validated": False,
            "compiler_automatic_victim_coverage": False,
            "evidence": {
                "path": str(summary_path),
                "bytes": summary_path.stat().st_size,
                "sha256": runner.sweep.sha256_file(summary_path),
            },
            "checks": {
                name: True for name in runner.STRICT_TYPEISO_CHECKS
            },
            "failed_checks": [],
            "missing_arms": [],
            "evidence_gaps": [],
        }

    def strict_recovery_layout_result(
        self,
        *,
        case_id: str,
        scenario_id: str,
    ) -> dict[str, object]:
        evidence_path = self.root / "strict-ledger.json"
        evidence = {
            "path": str(evidence_path),
            "bytes": evidence_path.stat().st_size,
            "sha256": runner.sweep.sha256_file(evidence_path),
        }
        return {
            "case_id": case_id,
            "advisory_id": f"RUSTSEC-TEST-{int(case_id[-3:]):04d}",
            "mechanism": "recovery_layout_validation",
            "scenario_id": scenario_id,
            "outcome": "detected",
            "true_positive": True,
            "positive_scope": "allocation_deallocation_layout_mismatch_edge",
            "negative_boundary": None,
            "reason": "matched_exact_recovery_layout_diagnostic",
            "result_semantics": "exact_diagnostic_true_positive",
            "claim_scope": "exact same-pointer recovery layout mismatch edge",
            "repetitions": 3,
            "evidence": evidence,
            "ground_truth_evidence": evidence,
            "allocator_signal_signatures": {
                "unialloc_recovery_deallocation_layout_mismatch": 3
            },
            "baseline_finding_signatures": {"miri_undefined_behavior": 3},
            "ground_truth_validation": {
                "valid": True,
                "reason": "matched_miri_source_controls",
                "vulnerable_finding_count": 3,
                "patched_clean_exit_count": 3,
            },
            "checks": {
                name: True for name in runner.STRICT_RECOVERY_LAYOUT_CHECKS
            },
        }

    @staticmethod
    def case(report: dict[str, object], case_id: str) -> dict[str, object]:
        return next(case for case in report["cases"] if case["case_id"] == case_id)

    def test_durable_rsh031_result_satisfies_strict_layout_contract(self) -> None:
        fragment_path = (
            ROOT
            / "docs"
            / "evidence"
            / "rustsec-rsh031-layout-20260715"
            / "mechanism-results.json"
        )
        result = json.loads(fragment_path.read_text(encoding="utf-8"))["results"][0]
        case = {
            "case_id": "RSH-031",
            "advisory_id": "RUSTSEC-2023-0017",
            "mechanism_results": [result],
            "_representative_mechanism_result": result,
            "_mechanism": "recovery_layout_validation",
            "_scope_outcome": "detected",
            "_representative_scenario_id": "RSH-031-derived-layout-validation",
        }

        validated, evidence, result_index = runner.validate_strict_current_result(
            case=case
        )

        self.assertIs(validated, result)
        self.assertEqual(result_index, 0)
        self.assertEqual(evidence["sha256"], result["evidence"]["sha256"])

    def test_direct_verification_keeps_exclusions_out_of_efficacy_rows(self) -> None:
        report, valid = self.build()

        self.assertTrue(valid)
        self.assertTrue(report["valid"])
        self.assertEqual(report["counts"]["case_count"], 49)
        self.assertEqual(
            report["counts"]["scope_status"], {"executable": 49}
        )
        self.assertEqual(report["counts"]["status"], {"completed": 49})
        self.assertEqual(report["counts"]["live_arm_count"], 51)
        self.assertEqual(report["counts"]["probe_count"], 0)
        self.assertEqual(report["counts"]["reclaim_exact_signal_case_count"], 3)
        self.assertEqual(report["counts"]["layout_validation_signal_case_count"], 0)
        self.assertEqual(report["counts"]["typeiso_reuse_denial_case_count"], 0)
        self.assertEqual(
            report["counts"]["replay_arm_implementation_digest_counts"],
            {self.implementation_sha256: 51},
        )
        self.assertEqual(
            report["counts"][
                "strict_typeiso_evidence_implementation_digest_counts"
            ],
            {},
        )
        self.assertEqual(report["counts"]["scope_review_required_count"], 0)
        first = self.case(report, "RSH-001")
        self.assertEqual(first["representative_scenario_id"], "RSH-001-selected")
        self.assertEqual(
            first["root_cause"], "root cause oracle for RSH-001-selected"
        )
        self.assertEqual(
            first["implementation_digests"], {self.implementation_sha256: 1}
        )
        neon = self.case(report, "RSH-064")
        self.assertEqual(neon["scope_status"], "executable")
        self.assertEqual(neon["status"], "completed")
        excluded = report["excluded_audit"]
        self.assertEqual(excluded["counts"]["case_count"], 4)
        self.assertEqual(excluded["counts"]["status"], {"blocked": 4})
        self.assertEqual(excluded["counts"]["probe_count"], 4)
        self.assertEqual(
            {row["case_id"] for row in excluded["cases"]},
            set(self.excluded_case_ids()),
        )
        self.assertTrue(
            set(self.excluded_case_ids()).isdisjoint(
                row["case_id"] for row in report["cases"]
            )
        )
        with self.output_csv.open(encoding="utf-8", newline="") as handle:
            csv_rows = list(csv.DictReader(handle))
        self.assertEqual(len(csv_rows), 49)
        self.assertIn("layout_validation_signal_observed", csv_rows[0])
        self.assertTrue(
            set(self.excluded_case_ids()).isdisjoint(
                row["case_id"] for row in csv_rows
            )
        )

    def test_strict_result_overlay_preserves_frozen_replay_observation(self) -> None:
        base_scope = self.root / "base-scope.json"
        base = json.loads(self.scope.read_text(encoding="utf-8"))
        for case_id in ("RSH-013", "RSH-020"):
            base_case = next(
                case for case in base["cases"] if case["case_id"] == case_id
            )
            base_case["mechanism_results"][0]["outcome"] = "inconclusive"
            base_case["mechanism_results"][0]["true_positive"] = False
            base_case["mechanism_results"][0]["positive_scope"] = None
            base_case["mechanism_results"][0]["result_semantics"] = "evidence_gap"
        self.write_json(base_scope, base)

        replay = json.loads(self.replay.read_text(encoding="utf-8"))
        replay["scope_sha256"] = runner.sweep.sha256_file(base_scope)
        for case_id in ("RSH-013", "RSH-020"):
            replay_case = next(
                case for case in replay["cases"] if case["case_id"] == case_id
            )
            replay_case["frozen_outcome"] = "inconclusive"
            for arm in replay_case["live_arms"]:
                arm["frozen_outcome"] = "inconclusive"
        self.write_json(self.replay, replay)

        report, valid = runner.build_report(
            scope_path=self.scope,
            base_scope_path=base_scope,
            catalog_paths=[self.catalog],
            jobs=self.jobs(),
            plan_path=None,
            output_json=self.output_json,
            output_csv=self.output_csv,
            log_dir=self.log_dir,
            dry_run=False,
            execute_unsafe=False,
            default_timeout_seconds=10,
        )

        self.assertTrue(valid)
        current_scope = json.loads(self.scope.read_text())["cases"]
        for case_id in ("RSH-013", "RSH-020"):
            with self.subTest(case_id=case_id):
                current = self.case(report, case_id)
                self.assertEqual(current["outcome"], "detected")
                self.assertTrue(current["reclaim_exact_signal_observed"])
                self.assertTrue(current["raw_exact_allocator_signal"])
                self.assertEqual(current["frozen_replay_outcome"], "inconclusive")
                self.assertEqual(
                    current["strict_result_override"]["reason"],
                    "feature_matched_exact_allocator_diagnostic",
                )
                self.assertEqual(
                    current["strict_result_override"]["evidence"]["sha256"],
                    next(
                        case for case in current_scope if case["case_id"] == case_id
                    )["mechanism_results"][0]["evidence"]["sha256"],
                )

    def test_scope_selects_one_positive_result_among_negative_rows(self) -> None:
        payload = json.loads(self.scope.read_text(encoding="utf-8"))
        case = next(row for row in payload["cases"] if row["case_id"] == "RSH-008")
        case["integration"]["scenario_ids"] = [
            "RSH-008-derived-reuse",
            "RSH-008-published",
        ]
        positive = self.strict_typeiso_result(
            case_id="RSH-008",
            scenario_id="RSH-008-derived-reuse",
        )
        negative = {
            "case_id": "RSH-008",
            "advisory_id": "RUSTSEC-TEST-0008",
            "mechanism": "type_isolation",
            "scenario_id": "RSH-008-published",
            "outcome": "inconclusive",
            "true_positive": False,
            "positive_scope": None,
            "negative_boundary": None,
            "result_semantics": "evidence_gap",
        }
        case["mechanism_results"] = [negative, positive]
        self.write_json(self.scope, payload)

        _scope, cases, _excluded = runner.load_scope(self.scope)

        selected = cases["RSH-008"]
        self.assertEqual(selected["_scope_outcome"], "mitigated")
        self.assertEqual(selected["_mechanism"], "type_isolation")
        self.assertEqual(
            selected["_representative_scenario_id"],
            "RSH-008-derived-reuse",
        )
        self.assertEqual(selected["_representative_mechanism_result"], positive)

    def test_scope_rejects_multiple_positive_results_for_one_mechanism(self) -> None:
        payload = json.loads(self.scope.read_text(encoding="utf-8"))
        case = next(row for row in payload["cases"] if row["case_id"] == "RSH-008")
        case["integration"]["scenario_ids"] = ["edge-a", "edge-b"]
        case["mechanism_results"] = [
            self.strict_typeiso_result(case_id="RSH-008", scenario_id="edge-a"),
            self.strict_typeiso_result(case_id="RSH-008", scenario_id="edge-b"),
        ]
        self.write_json(self.scope, payload)

        with self.assertRaisesRegex(
            runner.CompleteScopeError, "multiple positive results for one mechanism"
        ):
            runner.load_scope(self.scope)

    def test_scope_keeps_distinct_mechanism_positives_with_reclaim_representative(self) -> None:
        payload = json.loads(self.scope.read_text(encoding="utf-8"))
        case = next(row for row in payload["cases"] if row["case_id"] == "RSH-013")
        recovery_scenario = "RSH-013-derived-layout-validation"
        typeiso_scenario = "RSH-013-derived-reuse"
        case["integration"]["scenario_ids"].append(recovery_scenario)
        case["integration"]["scenario_ids"].append(typeiso_scenario)
        recovery = self.strict_recovery_layout_result(
            case_id="RSH-013",
            scenario_id=recovery_scenario,
        )
        typeiso = self.strict_typeiso_result(
            case_id="RSH-013",
            scenario_id=typeiso_scenario,
        )
        case["mechanism_results"].append(recovery)
        case["mechanism_results"].append(typeiso)
        self.write_json(self.scope, payload)

        _scope, cases, _excluded = runner.load_scope(self.scope)

        selected = cases["RSH-013"]
        self.assertEqual(selected["_scope_outcome"], "detected")
        self.assertEqual(selected["_mechanism"], "reclaim_checks")
        self.assertEqual(
            selected["_representative_scenario_id"],
            "RSH-013-selected",
        )
        self.assertEqual(
            selected["_representative_mechanism_result"]["mechanism"],
            "reclaim_checks",
        )
        self.assertEqual(
            {row["mechanism"] for row in selected["_positive_mechanism_results"]},
            {
                "reclaim_checks",
                "recovery_layout_validation",
                "type_isolation",
            },
        )

    def test_scope_prefers_recovery_layout_before_type_isolation(self) -> None:
        payload = json.loads(self.scope.read_text(encoding="utf-8"))
        case = next(row for row in payload["cases"] if row["case_id"] == "RSH-008")
        recovery_scenario = "RSH-008-derived-layout-validation"
        typeiso_scenario = "RSH-008-derived-reuse"
        case["integration"]["scenario_ids"] = [typeiso_scenario, recovery_scenario]
        case["mechanism_results"] = [
            self.strict_typeiso_result(
                case_id="RSH-008",
                scenario_id=typeiso_scenario,
            ),
            self.strict_recovery_layout_result(
                case_id="RSH-008",
                scenario_id=recovery_scenario,
            ),
        ]
        self.write_json(self.scope, payload)

        _scope, cases, _excluded = runner.load_scope(self.scope)

        selected = cases["RSH-008"]
        self.assertEqual(selected["_mechanism"], "recovery_layout_validation")
        self.assertEqual(selected["_scope_outcome"], "detected")
        self.assertEqual(
            selected["_representative_scenario_id"], recovery_scenario
        )

    def test_overlap_validation_binds_all_distinct_positive_mechanisms(self) -> None:
        payload = json.loads(self.scope.read_text(encoding="utf-8"))
        case = next(row for row in payload["cases"] if row["case_id"] == "RSH-013")
        recovery_scenario = "RSH-013-derived-layout-validation"
        typeiso_scenario = "RSH-013-derived-reuse"
        case["integration"]["scenario_ids"].append(recovery_scenario)
        case["integration"]["scenario_ids"].append(typeiso_scenario)
        case["mechanism_results"].append(
            self.strict_recovery_layout_result(
                case_id="RSH-013",
                scenario_id=recovery_scenario,
            )
        )
        case["mechanism_results"].append(
            self.strict_typeiso_result(
                case_id="RSH-013",
                scenario_id=typeiso_scenario,
            )
        )
        self.write_json(self.scope, payload)
        _scope, cases, _excluded = runner.load_scope(self.scope)
        current = cases["RSH-013"]
        record = {
            "reclaim_exact_signal_observed": False,
            "layout_validation_signal_observed": False,
            "typeiso_reuse_denial_observed": False,
            "raw_exact_allocator_signal": False,
        }

        runner.validate_and_bind_positive_mechanism_results(
            case=current,
            record=record,
        )

        self.assertTrue(record["reclaim_exact_signal_observed"])
        self.assertTrue(record["layout_validation_signal_observed"])
        self.assertTrue(record["typeiso_reuse_denial_observed"])
        self.assertTrue(record["raw_exact_allocator_signal"])
        self.assertEqual(
            [row["mechanism"] for row in record["strict_positive_mechanism_results"]],
            ["reclaim_checks", "recovery_layout_validation", "type_isolation"],
        )
        self.assertTrue(
            all(
                row["true_positive"]
                for row in record["strict_positive_mechanism_results"]
            )
        )

    def test_typeiso_result_overlay_preserves_historical_arms(self) -> None:
        payload = json.loads(self.scope.read_text(encoding="utf-8"))
        case = next(row for row in payload["cases"] if row["case_id"] == "RSH-008")
        case["integration"]["scenario_ids"] = [
            "RSH-008-derived-reuse",
            "RSH-008-published",
        ]
        case["mechanism_results"] = [
            {
                "case_id": "RSH-008",
                "advisory_id": "RUSTSEC-TEST-0008",
                "mechanism": "type_isolation",
                "scenario_id": "RSH-008-published",
                "outcome": "inconclusive",
                "true_positive": False,
                "positive_scope": None,
                "result_semantics": "evidence_gap",
            },
            self.strict_typeiso_result(
                case_id="RSH-008",
                scenario_id="RSH-008-derived-reuse",
            ),
        ]
        self.write_json(self.scope, payload)
        _scope, cases, _excluded = runner.load_scope(self.scope)
        current = cases["RSH-008"]
        current["_catalog_scenario"] = {
            "catalog_path": "derived-catalog.json",
            "source_path": "derived.rs",
            "oracle_tool": "allocator_stats",
            "root_cause": "cross-identity address reuse",
        }
        historical_arms = [{"historical": True}]
        record = {
            "case_id": "RSH-008",
            "outcome": "inconclusive",
            "mechanism": "type_isolation",
            "representative_scenario_id": "RSH-008-published",
            "conclusion": "compiler_critical_site_coverage_not_validated",
            "arms": historical_arms,
            "live_arm_count": 5,
            "typeiso_reuse_denial_observed": False,
        }

        runner.apply_strict_result_override(case=current, record=record)
        runner.validate_and_bind_positive_mechanism_results(
            case=current, record=record
        )

        self.assertEqual(record["outcome"], "mitigated")
        self.assertEqual(record["mechanism"], "type_isolation")
        self.assertEqual(
            record["representative_scenario_id"], "RSH-008-derived-reuse"
        )
        self.assertEqual(record["frozen_replay_outcome"], "inconclusive")
        self.assertEqual(
            record["frozen_replay_representative_scenario_id"],
            "RSH-008-published",
        )
        self.assertIs(record["arms"], historical_arms)
        self.assertEqual(record["live_arm_count"], 5)
        self.assertTrue(record["typeiso_reuse_denial_observed"])
        self.assertTrue(record["raw_exact_allocator_signal"])
        self.assertEqual(
            record["strict_result_override"]["result_semantics"],
            "causal_mitigation_true_positive",
        )
        bound = record["strict_positive_mechanism_results"]
        self.assertEqual(len(bound), 1)
        self.assertEqual(bound[0]["claim_scope"], "derived cross-identity reuse edge only")
        self.assertFalse(bound[0]["compiler_automatic_victim_coverage"])
        self.assertFalse(bound[0]["source_vulnerability_detection_validated"])
        self.assertFalse(bound[0]["automatic_source_coverage"])
        self.assertFalse(bound[0]["source_level_true_positive"])
        self.assertFalse(bound[0]["claim_grade"])
        self.assertEqual(
            bound[0]["implementation_sha256"], self.implementation_sha256
        )
        self.assertEqual(bound[0]["reduction_fidelity"], "manual_derived_reduction")
        self.assertFalse(bound[0]["synthetic_reduction"])

    def test_recovery_layout_overlay_exposes_exact_case_signal(self) -> None:
        payload = json.loads(self.scope.read_text(encoding="utf-8"))
        case = next(row for row in payload["cases"] if row["case_id"] == "RSH-031")
        derived_scenario = "RSH-031-derived-layout-validation"
        case["integration"]["scenario_ids"].append(derived_scenario)
        case["mechanism_results"] = [
            {
                "case_id": "RSH-031",
                "advisory_id": "RUSTSEC-TEST-0031",
                "mechanism": "reclaim_checks",
                "scenario_id": "RSH-031-selected",
                "outcome": "no_signal",
                "true_positive": False,
                "positive_scope": None,
                "negative_boundary": "no duplicate-reclaim signal",
                "result_semantics": "matched_negative_without_exact_reclaim_diagnostic",
            },
            self.strict_recovery_layout_result(
                case_id="RSH-031",
                scenario_id=derived_scenario,
            ),
        ]
        self.write_json(self.scope, payload)
        _scope, cases, _excluded = runner.load_scope(self.scope)
        current = cases["RSH-031"]
        current["_catalog_scenario"] = {
            "catalog_path": "derived-catalog.json",
            "source_path": "derived-layout.rs",
            "oracle_tool": "miri+allocator_stats",
            "root_cause": "allocation/deallocation layout mismatch",
        }
        historical_arms = [{"historical": True}]
        record = {
            "case_id": "RSH-031",
            "outcome": "no_signal",
            "mechanism": "reclaim_checks",
            "representative_scenario_id": "RSH-031-selected",
            "conclusion": "no duplicate-reclaim signal",
            "arms": historical_arms,
            "live_arm_count": 4,
            "reclaim_exact_signal_observed": False,
            "layout_validation_signal_observed": False,
            "typeiso_reuse_denial_observed": False,
        }

        runner.apply_strict_result_override(case=current, record=record)

        self.assertEqual(record["outcome"], "detected")
        self.assertEqual(record["mechanism"], "recovery_layout_validation")
        self.assertTrue(record["layout_validation_signal_observed"])
        self.assertFalse(record["reclaim_exact_signal_observed"])
        self.assertFalse(record["typeiso_reuse_denial_observed"])
        self.assertTrue(record["raw_exact_allocator_signal"])
        self.assertIs(record["arms"], historical_arms)
        self.assertEqual(
            record["strict_result_override"]["allocator_signal_signatures"],
            {"unialloc_recovery_deallocation_layout_mismatch": 3},
        )
        self.assertTrue(
            record["strict_result_override"]["ground_truth_validation"]["valid"]
        )

    def test_result_overlay_rejects_unsubstantiated_scope_change(self) -> None:
        base_scope = self.root / "base-scope.json"
        base = json.loads(self.scope.read_text(encoding="utf-8"))
        base_case = next(
            case for case in base["cases"] if case["case_id"] == "RSH-014"
        )
        base_case["mechanism_results"][0]["outcome"] = "inconclusive"
        self.write_json(base_scope, base)

        replay = json.loads(self.replay.read_text(encoding="utf-8"))
        replay["scope_sha256"] = runner.sweep.sha256_file(base_scope)
        replay_case = next(
            case for case in replay["cases"] if case["case_id"] == "RSH-014"
        )
        replay_case["frozen_outcome"] = "inconclusive"
        for arm in replay_case["live_arms"]:
            arm["frozen_outcome"] = "inconclusive"
        self.write_json(self.replay, replay)

        with self.assertRaisesRegex(
            runner.CompleteScopeError, "strict result override"
        ):
            runner.build_report(
                scope_path=self.scope,
                base_scope_path=base_scope,
                catalog_paths=[self.catalog],
                jobs=self.jobs(),
                plan_path=None,
                output_json=self.output_json,
                output_csv=self.output_csv,
                log_dir=self.log_dir,
                dry_run=False,
                execute_unsafe=False,
                default_timeout_seconds=10,
            )

    def test_dry_run_emits_selected_rows_without_ingesting_live_artifacts(self) -> None:
        self.replay.unlink()
        self.blocked.unlink()
        jobs = {
            "executable_replay": {
                "job_id": "executable_replay",
                "action": "run",
                "artifact_path": self.replay,
                "command": [sys.executable, "-c", "raise SystemExit(99)"],
                "expected_returncodes": [0],
            },
            "blocked_attempts": {
                "job_id": "blocked_attempts",
                "action": "run",
                "artifact_path": self.blocked,
                "command": [sys.executable, "-c", "raise SystemExit(99)"],
                "expected_returncodes": [0],
            },
        }

        report, valid = runner.build_report(
            scope_path=self.scope,
            catalog_paths=[self.catalog],
            jobs=jobs,
            plan_path=None,
            output_json=self.output_json,
            output_csv=self.output_csv,
            log_dir=self.log_dir,
            dry_run=True,
            execute_unsafe=False,
            default_timeout_seconds=10,
        )

        self.assertTrue(valid)
        self.assertEqual(len(report["cases"]), 49)
        self.assertEqual(
            self.case(report, "RSH-001")["outcome"],
            "planned_executable_replay",
        )
        self.assertEqual(
            report["excluded_audit"]["counts"]["case_count"], 4
        )
        self.assertEqual(
            {row["outcome"] for row in report["excluded_audit"]["cases"]},
            {"planned_exclusion_probe"},
        )
        self.assertFalse(self.replay.exists())
        self.assertFalse(self.blocked.exists())

    def test_scope_counts_and_partition_fail_closed(self) -> None:
        original = json.loads(self.scope.read_text(encoding="utf-8"))
        mutations = (
            (
                "missing-evaluable",
                {**original, "cases": original["cases"][:-1]},
                "strong_candidate_count",
            ),
            (
                "duplicate",
                {
                    **original,
                    "cases": [*original["cases"][:-1], original["cases"][0]],
                },
                "duplicate case",
            ),
            (
                "missing-excluded",
                {**original, "excluded_cases": original["excluded_cases"][:-1]},
                "excluded_non_evaluable_count",
            ),
            (
                "partition-overlap",
                {
                    **original,
                    "excluded_cases": [
                        *original["excluded_cases"][:-1],
                        {
                            **original["excluded_cases"][-1],
                            "case_id": original["cases"][0]["case_id"],
                        },
                    ],
                },
                "overlap",
            ),
            (
                "reviewed-total",
                {
                    **original,
                    "counts": {
                        **original["counts"],
                        "reviewed_strong_candidate_count": 52,
                    },
                },
                "reviewed_strong_candidate_count",
            ),
        )
        for label, payload, pattern in mutations:
            with self.subTest(label=label):
                self.write_json(self.scope, payload)
                with self.assertRaisesRegex(runner.CompleteScopeError, pattern):
                    runner.load_scope(self.scope)
        self.write_json(self.scope, original)

    def test_legacy_base_scope_uses_its_recorded_counts(self) -> None:
        current = json.loads(self.scope.read_text(encoding="utf-8"))
        legacy_cases = [*current["cases"], *current["excluded_cases"]]
        legacy_scope = self.root / "legacy-base-scope.json"
        legacy_payload = {
            "schema_version": 1,
            "source": "legacy-test-scope",
            "counts": {
                "strong_candidate_count": 53,
                "integration_status": {"blocked": 4, "executable": 49},
            },
            "cases": legacy_cases,
        }
        self.write_json(legacy_scope, legacy_payload)

        payload, cases = runner.load_base_scope(legacy_scope)

        self.assertEqual(payload["source"], "legacy-test-scope")
        self.assertEqual(len(cases), 53)
        self.assertEqual(cases["RSH-054"]["_scope_outcome"], "blocked")

        legacy_payload["counts"]["strong_candidate_count"] = 54
        self.write_json(legacy_scope, legacy_payload)
        with self.assertRaisesRegex(runner.CompleteScopeError, "54 cases"):
            runner.load_base_scope(legacy_scope)

    def test_replay_rejects_missing_duplicate_and_wrong_scenario(self) -> None:
        scope_sha256 = runner.sweep.sha256_file(self.scope)
        _scope, cases, _excluded = runner.load_scope(self.scope)
        original = json.loads(self.replay.read_text(encoding="utf-8"))
        mutations = (
            (
                "missing",
                {**original, "cases": original["cases"][:-1]},
                "coverage mismatch",
            ),
            (
                "duplicate",
                {
                    **original,
                    "cases": [*original["cases"][:-1], original["cases"][0]],
                },
                "duplicate case",
            ),
            (
                "scenario",
                {
                    **original,
                    "cases": [
                        {**original["cases"][0], "scenario_id": "RSH-001-first"},
                        *original["cases"][1:],
                    ],
                },
                "scenario mismatch",
            ),
        )
        for label, payload, pattern in mutations:
            with self.subTest(label=label):
                self.write_json(self.replay, payload)
                with self.assertRaisesRegex(runner.CompleteScopeError, pattern):
                    runner.validate_executable_replay(
                        self.replay,
                        scope_sha256=scope_sha256,
                        cases=cases,
                    )
        self.write_json(self.replay, original)

    def test_replay_rejects_binary_hash_drift(self) -> None:
        self.binary.write_bytes(b"changed binary\n")

        with self.assertRaisesRegex(runner.CompleteScopeError, "binary hash mismatch"):
            self.build()

    def test_replay_marks_cleaned_binary_as_archived_digest_evidence(self) -> None:
        self.binary.unlink()

        report, valid = self.build()

        self.assertTrue(valid)
        self.assertEqual(report["counts"]["binary_available_arm_count"], 0)
        self.assertEqual(
            report["counts"]["archived_binary_unavailable_arm_count"], 51
        )
        arm = self.case(report, "RSH-001")["arms"][0]
        self.assertFalse(arm["binary_available"])
        self.assertEqual(arm["binary_validation"], "archived_digest_only")
        self.assertIn("not local live-binary verification", report["claim_boundary"])

    def test_blocked_attempts_validate_exclusions_only(self) -> None:
        _scope, _cases, excluded = runner.load_scope(self.scope)
        original = json.loads(self.blocked.read_text(encoding="utf-8"))
        scope_row = {
            **original["cases"][0],
            "case_id": "RSH-064",
            "advisory_id": "RUSTSEC-TEST-0049",
            "package": "crate-49",
            "probes": [
                {
                    **original["cases"][0]["probes"][0],
                    "case_id": "RSH-064",
                }
            ],
        }
        mutations = (
            (
                "missing",
                {**original, "cases": original["cases"][:-1]},
                "coverage mismatch",
            ),
            (
                "no-probes",
                {
                    **original,
                    "cases": [
                        {**original["cases"][0], "probes": []},
                        *original["cases"][1:],
                    ],
                },
                "no attempted probes",
            ),
            (
                "efficacy-row",
                {**original, "cases": [*original["cases"], scope_row]},
                "coverage mismatch",
            ),
        )
        for label, payload, pattern in mutations:
            with self.subTest(label=label):
                self.write_json(self.blocked, payload)
                with self.assertRaisesRegex(runner.CompleteScopeError, pattern):
                    runner.validate_blocked_attempts(
                        self.blocked,
                        excluded_cases=excluded,
                    )
        self.write_json(self.blocked, original)

    def test_blocked_attempts_preserve_supplemental_transition_separately(self) -> None:
        _scope, cases, excluded = runner.load_scope(self.scope)
        payload = json.loads(self.blocked.read_text(encoding="utf-8"))
        template = payload["cases"][0]
        transition = {
            **template,
            "case_id": "RSH-064",
            "advisory_id": "RUSTSEC-TEST-0049",
            "package": "crate-49",
            "primary_primitive": "double_free",
            "current_status": "newly_executable",
            "blocker_code": "external_host_resolved",
            "root_blocker": "the external host is now available",
            "conclusion": "the former blocker was resolved",
            "probes": [
                {
                    **template["probes"][0],
                    "case_id": "RSH-064",
                }
            ],
        }
        payload["cases"].append(transition)
        self.write_json(self.blocked, payload)

        records = runner.validate_blocked_attempts(
            self.blocked,
            excluded_cases=excluded,
            historical_transition_cases={"RSH-064": cases["RSH-064"]},
        )

        self.assertEqual(set(records), {*self.excluded_case_ids(), "RSH-064"})
        self.assertEqual(records["RSH-064"]["scope_status"], "historical_transition")
        self.assertEqual(records["RSH-064"]["outcome"], "newly_executable")
        self.assertEqual(records["RSH-064"]["probe_count"], 1)

    def test_plan_requires_scope_binding_and_both_unique_jobs(self) -> None:
        scope_sha256 = runner.sweep.sha256_file(self.scope)
        base_jobs = [
            {
                "job_id": "executable_replay",
                "action": "verify",
                "artifact_path": str(self.replay),
                "expected_sha256": runner.sweep.sha256_file(self.replay),
            },
            {
                "job_id": "blocked_attempts",
                "action": "verify",
                "artifact_path": str(self.blocked),
                "expected_sha256": runner.sweep.sha256_file(self.blocked),
            },
        ]
        plan = self.root / "plan.json"
        mutations = (
            (
                "scope",
                {"schema_version": 1, "scope_sha256": "a" * 64, "jobs": base_jobs},
                "scope SHA-256 mismatch",
            ),
            (
                "missing",
                {
                    "schema_version": 1,
                    "scope_sha256": scope_sha256,
                    "jobs": base_jobs[:-1],
                },
                "missing jobs",
            ),
            (
                "duplicate",
                {
                    "schema_version": 1,
                    "scope_sha256": scope_sha256,
                    "jobs": [base_jobs[0], base_jobs[0]],
                },
                "duplicate command-plan job",
            ),
        )
        for label, payload, pattern in mutations:
            with self.subTest(label=label):
                self.write_json(plan, payload)
                with self.assertRaisesRegex(runner.CompleteScopeError, pattern):
                    runner.load_plan(plan, scope_sha256=scope_sha256)

    def test_run_job_requires_opt_in_and_records_logs(self) -> None:
        marker = self.root / "ran"
        jobs = self.jobs()
        jobs["executable_replay"] = {
            "job_id": "executable_replay",
            "action": "run",
            "artifact_path": self.replay,
            "command": [
                sys.executable,
                "-c",
                f"from pathlib import Path; Path({str(marker)!r}).write_text('ran')",
            ],
            "expected_returncodes": [0],
        }

        with self.assertRaisesRegex(runner.CompleteScopeError, "execute-unsafe"):
            runner.run_plan_jobs(
                jobs,
                dry_run=False,
                execute_unsafe=False,
                default_timeout_seconds=10,
                log_dir=self.log_dir,
            )
        records, valid = runner.run_plan_jobs(
            jobs,
            dry_run=False,
            execute_unsafe=True,
            default_timeout_seconds=10,
            log_dir=self.log_dir,
        )
        self.assertTrue(valid)
        self.assertEqual(len(records), 2)
        self.assertEqual(marker.read_text(), "ran")
        run_record = next(
            record for record in records if record["job_id"] == "executable_replay"
        )
        self.assertEqual(run_record["status"], "completed")
        self.assertTrue(run_record["logs"]["stdout"]["sha256"])

    def test_cli_direct_mode_writes_matching_json_and_csv(self) -> None:
        stdout = io.StringIO()
        with contextlib.redirect_stdout(stdout):
            status = runner.main(
                [
                    "--scope",
                    str(self.scope),
                    "--catalog",
                    str(self.catalog),
                    "--executable-replay",
                    str(self.replay),
                    "--blocked-attempts",
                    str(self.blocked),
                    "--output-json",
                    str(self.output_json),
                    "--output-csv",
                    str(self.output_csv),
                ]
            )

        self.assertEqual(status, 0)
        self.assertEqual(json.loads(stdout.getvalue()), json.loads(self.output_json.read_text()))
        with self.output_csv.open(encoding="utf-8", newline="") as handle:
            self.assertEqual(len(list(csv.DictReader(handle))), 49)


if __name__ == "__main__":
    unittest.main()
