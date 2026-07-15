#!/usr/bin/env python3
"""Tests for strict RustSec allocator-mechanism result export."""

from __future__ import annotations

import importlib.util
import io
import pathlib
import unittest
from contextlib import redirect_stderr


ROOT = pathlib.Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "evaluation/scripts/export_rustsec_mechanism_results.py"
spec = importlib.util.spec_from_file_location("mechanism_results", SCRIPT)
assert spec is not None and spec.loader is not None
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


def cargo_metadata_attestation(features: list[str]) -> dict[str, object]:
    contract: dict[str, object] = {
        "schema_version": 1,
        "package_count": 1,
        "resolve_node_count": 1,
        "package_id": (
            f"path+{(ROOT / 'unialloc').resolve().as_uri()}#0.1.0"
        ),
        "manifest_path": str((ROOT / "unialloc/Cargo.toml").resolve()),
        "package_source": None,
        "expected_features": features,
        "resolved_features": features,
        "default_features_expected": False,
        "default_feature_resolved": False,
    }
    token = module.canonical_sha256(contract)
    record = {
        **contract,
        "manifest_path": "unialloc/Cargo.toml",
        "feature_contract_sha256": token,
    }
    return {
        "required": True,
        "valid": True,
        "token": token,
        "observation_count": 1,
        "failure_counts": {},
        "record": record,
        "observed_feature_contract_tokens": {token: 1},
        "observed_records": {token: record},
        "cargo_metadata_stdout_sha256": {"3" * 64: 1},
    }


def subject_feature_record(
    allocator: str,
    *,
    catalog_features: list[str] | None = None,
    effective_features: list[str] | None = None,
    override_applied: bool = False,
    override_allocator_variant: str | None = None,
) -> dict[str, object]:
    return {
        "allocator_variant": allocator,
        "crate": "demo",
        "catalog_features": sorted(catalog_features or []),
        "effective_features": sorted(effective_features or []),
        "default_features_enabled": True,
        "override_applied": override_applied,
        "override_allocator_variant": override_allocator_variant,
        "override_source": (
            "scenario.allocator_cargo_feature_overrides"
            if override_applied
            else None
        ),
    }


def direct_summary_provenance(
    subject: dict[str, object], implementation_sha256: str
) -> dict[str, object]:
    token = module.canonical_sha256(subject)
    return {
        "required": True,
        "valid": True,
        "observation_count": 1,
        "failure_counts": {},
        "unialloc_implementation_sha256": implementation_sha256,
        "subject_cargo_features_sha256": token,
        "subject_cargo_features": subject,
        "observed_unialloc_implementation_sha256": {
            implementation_sha256: 1
        },
        "observed_subject_cargo_feature_tokens": {token: 1},
        "observed_subject_cargo_features": {token: subject},
    }


def set_direct_subject(
    value: dict[str, object], subject: dict[str, object]
) -> None:
    implementation_sha256 = str(value["unialloc_implementation_sha256"])
    value["subject_cargo_features"] = subject
    value["direct_allocator_provenance"] = direct_summary_provenance(
        subject, implementation_sha256
    )


def retoken_summary_attestation(value: dict[str, object]) -> None:
    record = value["record"]
    contract = {
        key: field
        for key, field in record.items()
        if key != "feature_contract_sha256"
    }
    contract["manifest_path"] = str((ROOT / "unialloc/Cargo.toml").resolve())
    token = module.canonical_sha256(contract)
    record["feature_contract_sha256"] = token
    value["token"] = token
    value["observed_feature_contract_tokens"] = {token: 1}
    value["observed_records"] = {token: record}


def arm(
    archive: str,
    allocator: str,
    *,
    repetitions: int = 3,
    baseline: dict[str, int] | None = None,
    signals: dict[str, int] | None = None,
    native: dict[str, int] | None = None,
    clean: int = 0,
    clean_exit_count: int = 0,
    expected: int = 0,
    compile_errors: dict[str, int] | None = None,
    control_fingerprints: dict[str, int] | None = None,
    outcome_fingerprints: dict[str, int] | None = None,
    oracle_statuses: dict[str, int] | None = None,
    statuses: list[str] | None = None,
    implementation_sha256: str = "d" * 64,
    subject_features: dict[str, object] | None = None,
) -> dict[str, object]:
    result = {
        "archive_variant": archive,
        "allocator_variant": allocator,
        "experiment_count": 1,
        "execution_statuses": statuses or ["completed"],
        "repetition_count": repetitions,
        "baseline_finding_signatures": baseline or {},
        "allocator_signal_signatures": signals or {},
        "native_diagnostic_signatures": native or {},
        "patched_clean_count": clean,
        "clean_exit_count": clean_exit_count,
        "expected_oracle_observation_count": expected,
        "matched_compile_error_codes": compile_errors or {},
        "patched_control_fingerprints": control_fingerprints or {},
        "native_outcome_fingerprints": outcome_fingerprints or {},
        "native_outcome_fingerprint_failures": {},
        "oracle_statuses": oracle_statuses or {},
    }
    if allocator in {"reclaim_plain", "reclaim_checks"}:
        subject = subject_features or subject_feature_record(allocator)
        result["unialloc_implementation_sha256"] = implementation_sha256
        result["subject_cargo_features"] = subject
        result["direct_allocator_provenance"] = direct_summary_provenance(
            subject, implementation_sha256
        )
    if allocator == "reclaim_plain":
        result["cargo_metadata_attestation"] = cargo_metadata_attestation(
            ["stats"]
        )
    elif allocator == "reclaim_checks":
        result["cargo_metadata_attestation"] = cargo_metadata_attestation(
            ["reclaim_checks", "stats"]
        )
    return result


def matched_case(
    *, treatment_signals: int = 3, safe_panic: bool = False
) -> dict[str, object]:
    patched_system = arm("patched", "system", clean=3)
    patched_plain = arm("patched", "reclaim_plain", clean=3)
    patched_treatment = arm("patched", "reclaim_checks", clean=3)
    if safe_panic:
        patched_system = arm(
            "patched",
            "system",
            expected=3,
            control_fingerprints={"panic:patched-checkpoint": 3},
        )
        patched_treatment = arm(
            "patched",
            "reclaim_checks",
            native={"rust_panic_observed": 3},
            control_fingerprints={"panic:patched-checkpoint": 3},
        )
        patched_plain = arm(
            "patched",
            "reclaim_plain",
            native={"rust_panic_observed": 3},
            control_fingerprints={"panic:patched-checkpoint": 3},
        )
    return {
        "case_id": "RSH-043",
        "advisory_id": "RUSTSEC-TEST-0001",
        "scenario_id": "RSH-043-matched",
        "arms": [
            arm(
                "vulnerable",
                "system",
                baseline={"asan_double_free": 3},
                expected=3,
            ),
            arm(
                "vulnerable",
                "reclaim_checks",
                signals={"unialloc_pointer_already_released_check": treatment_signals}
                if treatment_signals
                else {},
                clean_exit_count=3 if treatment_signals == 0 else 0,
            ),
            arm(
                "vulnerable",
                "reclaim_plain",
                clean_exit_count=3,
            ),
            patched_system,
            patched_plain,
            patched_treatment,
        ],
    }


def derived_arm(
    archive: str,
    allocator: str,
    *,
    repetitions: int = 3,
    reuse: int = 0,
    denials: int = 0,
    bound_reports: int = 0,
    expected_oracle: bool = True,
    status: str = "completed",
    exit_code: int = 0,
) -> dict[str, object]:
    return {
        "archive_variant": archive,
        "allocator_variant": allocator,
        "execution_status": status,
        "exit_codes": [exit_code] * repetitions,
        "address_reuse_observation_count": reuse,
        "matching_reuse_denial_event_count": denials,
        "bound_replacement_site_count": bound_reports,
        "expected_oracle_observed": expected_oracle,
    }


def matched_derived_scenario() -> dict[str, object]:
    return {
        "advisory_id": "RUSTSEC-TEST-0002",
        "case_id": "RSH-041",
        "scenario_id": "RSH-041-derived-reuse",
        "repetitions_requested": 3,
        "orchestration_success": True,
        "unexpected_arm_count": 0,
        # Deliberately false: the exporter must derive the result from arms.
        "edge_evaluation": {
            "baseline_vulnerability_and_address_reuse_reproduced": False,
            "patched_system_control_reproduced": False,
            "typed_plain_address_reuse_ablation_reproduced": False,
            "typeiso_reuse_edge_blocked_and_reported": False,
            "vulnerability_specific_detection_signal": False,
            "validated": False,
            "claim_scope": "one exact A-to-B edge",
            "compiler_automatic_victim_coverage": False,
            "source_vulnerability_detection_validated": False,
        },
        "arms": [
            derived_arm("vulnerable", "system", reuse=3),
            derived_arm("vulnerable", "typed_plain", reuse=3),
            derived_arm(
                "vulnerable",
                "typeiso",
                reuse=0,
                denials=3,
                bound_reports=3,
                expected_oracle=False,
                exit_code=101,
            ),
            derived_arm("patched", "system", reuse=3),
            derived_arm("patched", "typed_plain", reuse=3),
            derived_arm("patched", "typeiso", reuse=3),
        ],
    }


def matched_diagnostic_only_derived_scenario() -> dict[str, object]:
    scenario = matched_derived_scenario()
    scenario["typed_allocator_expected_oracle"] = False
    for arm in scenario["arms"]:
        if arm["allocator_variant"] in {"typed_plain", "typeiso"}:
            arm["expected_oracle_observed"] = False
    vulnerable_system = scenario["arms"][0]
    vulnerable_system["exit_codes"] = [1, 1, 1]
    return scenario


class MechanismResultTests(unittest.TestCase):
    def test_exports_derived_reuse_summary_without_reclaim_summary(self) -> None:
        evidence = {
            "path": "derived-reuse-summary.json",
            "bytes": 123,
            "sha256": "a" * 64,
        }

        result = module.export(
            None,
            summary_artifact=None,
            minimum_repetitions=3,
            derived_reuse_summary={
                "schema_version": 1,
                "scenarios": [matched_derived_scenario()],
            },
            derived_reuse_artifact=evidence,
        )

        self.assertNotIn("summary_input", result)
        self.assertEqual(result["derived_reuse_input"], evidence)
        self.assertEqual(result["counts"], {"mitigated": 1})
        self.assertEqual(len(result["results"]), 1)
        self.assertEqual(result["results"][0]["outcome"], "mitigated")
        self.assertEqual(result["results"][0]["evidence"], evidence)

    def test_derived_only_export_rejects_malformed_scenario(self) -> None:
        with self.assertRaisesRegex(
            module.ResultError,
            "derived reuse scenario requires advisory, case, and scenario ids",
        ):
            module.export(
                None,
                summary_artifact=None,
                minimum_repetitions=3,
                derived_reuse_summary={
                    "schema_version": 1,
                    "scenarios": [{"arms": []}],
                },
                derived_reuse_artifact={
                    "path": "derived-reuse-summary.json",
                    "bytes": 2,
                    "sha256": "b" * 64,
                },
            )

    def test_cli_rejects_invocation_without_any_input(self) -> None:
        stderr = io.StringIO()
        with redirect_stderr(stderr):
            status = module.main([])

        self.assertEqual(status, 2)
        self.assertIn(
            "at least one of --summary or --derived-reuse-summary is required",
            stderr.getvalue(),
        )

    def test_exports_detected_only_for_fully_matched_exact_signal(self) -> None:
        result = module.evaluate_reclaim_case(matched_case(), minimum_repetitions=3)
        self.assertEqual(result["outcome"], "detected")
        self.assertTrue(all(result["checks"].values()))
        self.assertTrue(result["true_positive"])
        self.assertEqual(
            result["result_semantics"], "exact_diagnostic_true_positive"
        )
        self.assertEqual(
            result["positive_scope"],
            "duplicate_reclaim_event_in_integrated_witness",
        )

    def test_accepts_matched_safe_panic_patched_control(self) -> None:
        result = module.evaluate_reclaim_case(
            matched_case(safe_panic=True), minimum_repetitions=3
        )
        self.assertEqual(result["outcome"], "detected")
        self.assertEqual(
            result["patched_control_mode"], "matched_safe_panic_fingerprint"
        )

    def test_rejects_baseline_without_expected_oracle_in_every_repetition(self) -> None:
        case = matched_case()
        baseline = case["arms"][0]
        baseline["expected_oracle_observation_count"] = 2

        result = module.evaluate_reclaim_case(case, minimum_repetitions=3)

        self.assertEqual(result["outcome"], "inconclusive")
        self.assertFalse(result["checks"]["baseline_reproduced_in_all_repetitions"])

    def test_rejects_mixed_baseline_signatures_even_when_counts_sum_to_repetitions(
        self,
    ) -> None:
        case = matched_case()
        baseline = case["arms"][0]
        baseline["baseline_finding_signatures"] = {
            "asan_double_free": 2,
            "asan_heap_use_after_free": 1,
        }

        result = module.evaluate_reclaim_case(case, minimum_repetitions=3)

        self.assertEqual(result["outcome"], "inconclusive")
        self.assertFalse(result["checks"]["baseline_reproduced_in_all_repetitions"])

    def test_rejects_incomplete_vulnerable_treatment_as_no_signal(self) -> None:
        case = matched_case(treatment_signals=0)
        treatment = case["arms"][1]
        treatment["execution_statuses"] = ["completed", "unexpected"]

        result = module.evaluate_reclaim_case(case, minimum_repetitions=3)

        self.assertEqual(result["outcome"], "inconclusive")
        self.assertFalse(
            result["checks"]["vulnerable_treatment_completed_in_all_repetitions"]
        )

    def test_accepts_stable_clean_vulnerable_treatment_as_no_signal(self) -> None:
        result = module.evaluate_reclaim_case(
            matched_case(treatment_signals=0), minimum_repetitions=3
        )

        self.assertEqual(result["outcome"], "no_signal")
        self.assertEqual(result["vulnerable_treatment_mode"], "stable_clean_exit")

    def test_rejects_abnormal_vulnerable_treatment_as_no_signal(self) -> None:
        case = matched_case(treatment_signals=0)
        treatment = case["arms"][1]
        treatment["clean_exit_count"] = 0
        treatment["oracle_statuses"] = {"native_abnormal_exit_inconclusive": 1}

        result = module.evaluate_reclaim_case(case, minimum_repetitions=3)

        self.assertEqual(result["outcome"], "inconclusive")
        self.assertFalse(
            result["checks"]["vulnerable_treatment_completed_in_all_repetitions"]
        )

    def test_accepts_only_control_equivalent_safe_panic_as_no_signal(self) -> None:
        case = matched_case(treatment_signals=0, safe_panic=True)
        fingerprint = {"panic-sha256": 3}
        treatment = case["arms"][1]
        treatment.update(
            {
                "clean_exit_count": 0,
                "native_diagnostic_signatures": {"rust_panic_observed": 3},
                "native_outcome_fingerprints": fingerprint,
            }
        )
        for value in case["arms"]:
            if value["archive_variant"] == "patched":
                value["patched_control_fingerprints"] = fingerprint

        result = module.evaluate_reclaim_case(case, minimum_repetitions=3)

        self.assertEqual(result["outcome"], "no_signal")
        self.assertEqual(
            result["vulnerable_treatment_mode"],
            "matched_safe_panic_control_fingerprint",
        )

    def test_accepts_matched_vulnerable_plain_panic_as_no_signal(self) -> None:
        case = matched_case(treatment_signals=0)
        fingerprint = {"panic:vulnerable-checkpoint": 3}
        for allocator in ("reclaim_plain", "reclaim_checks"):
            vulnerable = next(
                value
                for value in case["arms"]
                if value["archive_variant"] == "vulnerable"
                and value["allocator_variant"] == allocator
            )
            vulnerable.update(
                {
                    "clean_exit_count": 0,
                    "native_diagnostic_signatures": {
                        "rust_panic_observed": 3
                    },
                    "native_outcome_fingerprints": fingerprint,
                }
            )

        result = module.evaluate_reclaim_case(case, minimum_repetitions=3)

        self.assertEqual(result["outcome"], "no_signal")
        self.assertEqual(
            result["vulnerable_treatment_mode"],
            "matched_vulnerable_plain_panic_fingerprint",
        )
        self.assertFalse(result["true_positive"])
        self.assertEqual(
            result["result_semantics"],
            "matched_negative_without_exact_reclaim_diagnostic",
        )
        self.assertEqual(
            result["negative_boundary"],
            "no_exact_reclaim_diagnostic_in_feature_matched_integrated_witness",
        )

    def test_rejects_different_vulnerable_plain_and_treatment_panics(self) -> None:
        case = matched_case(treatment_signals=0)
        for allocator, fingerprint in (
            ("reclaim_plain", {"panic:plain": 3}),
            ("reclaim_checks", {"panic:checks": 3}),
        ):
            vulnerable = next(
                value
                for value in case["arms"]
                if value["archive_variant"] == "vulnerable"
                and value["allocator_variant"] == allocator
            )
            vulnerable.update(
                {
                    "clean_exit_count": 0,
                    "native_diagnostic_signatures": {
                        "rust_panic_observed": 3
                    },
                    "native_outcome_fingerprints": fingerprint,
                }
            )

        result = module.evaluate_reclaim_case(case, minimum_repetitions=3)

        self.assertEqual(result["outcome"], "inconclusive")
        self.assertFalse(
            result["checks"]["vulnerable_treatment_completed_in_all_repetitions"]
        )

    def test_rejects_matched_native_signal_without_source_bound_fingerprint(
        self,
    ) -> None:
        case = matched_case(treatment_signals=0)
        signal_signature = {"posix_signal:SIGSEGV": 3}
        for allocator in ("reclaim_plain", "reclaim_checks"):
            vulnerable = next(
                value
                for value in case["arms"]
                if value["archive_variant"] == "vulnerable"
                and value["allocator_variant"] == allocator
            )
            vulnerable.update(
                {
                    "clean_exit_count": 0,
                    "native_abnormal_signal_signatures": signal_signature,
                    "oracle_statuses": {
                        "native_abnormal_exit_inconclusive": 1
                    },
                }
            )

        result = module.evaluate_reclaim_case(case, minimum_repetitions=3)

        self.assertEqual(result["outcome"], "inconclusive")
        self.assertFalse(
            result["checks"]["vulnerable_treatment_completed_in_all_repetitions"]
        )
        self.assertEqual(
            result["vulnerable_treatment_native_abnormal_signal_signatures"],
            signal_signature,
        )

    def test_accepts_source_bound_matched_native_signal_as_no_signal(self) -> None:
        case = matched_case(treatment_signals=0)
        signal_signature = {"posix_signal:SIGSEGV": 3}
        checkpoint_fingerprint = {"checkpoint:stale-deref:source-line": 3}
        for allocator in ("reclaim_plain", "reclaim_checks"):
            vulnerable = next(
                value
                for value in case["arms"]
                if value["archive_variant"] == "vulnerable"
                and value["allocator_variant"] == allocator
            )
            vulnerable.update(
                {
                    "clean_exit_count": 0,
                    "native_abnormal_signal_signatures": signal_signature,
                    "native_outcome_fingerprints": checkpoint_fingerprint,
                    "oracle_statuses": {
                        "native_abnormal_exit_inconclusive": 1
                    },
                }
            )

        result = module.evaluate_reclaim_case(case, minimum_repetitions=3)

        self.assertEqual(result["outcome"], "no_signal")
        self.assertEqual(
            result["vulnerable_treatment_mode"],
            "matched_vulnerable_plain_native_abnormal_signal",
        )

    def test_rejects_mismatched_normalized_native_signals(self) -> None:
        case = matched_case(treatment_signals=0)
        for allocator, signal_signature in (
            ("reclaim_plain", {"posix_signal:SIGSEGV": 3}),
            ("reclaim_checks", {"posix_signal:SIGABRT": 3}),
        ):
            vulnerable = next(
                value
                for value in case["arms"]
                if value["archive_variant"] == "vulnerable"
                and value["allocator_variant"] == allocator
            )
            vulnerable.update(
                {
                    "clean_exit_count": 0,
                    "native_abnormal_signal_signatures": signal_signature,
                    "native_outcome_fingerprint_failures": {
                        "non_panic_native_signal": 3
                    },
                    "oracle_statuses": {
                        "native_abnormal_exit_inconclusive": 1
                    },
                }
            )

        result = module.evaluate_reclaim_case(case, minimum_repetitions=3)

        self.assertEqual(result["outcome"], "inconclusive")
        self.assertFalse(
            result["checks"]["vulnerable_treatment_completed_in_all_repetitions"]
        )

    def test_rejects_partial_normalized_native_signal(self) -> None:
        case = matched_case(treatment_signals=0)
        for allocator in ("reclaim_plain", "reclaim_checks"):
            vulnerable = next(
                value
                for value in case["arms"]
                if value["archive_variant"] == "vulnerable"
                and value["allocator_variant"] == allocator
            )
            vulnerable.update(
                {
                    "clean_exit_count": 0,
                    "native_abnormal_signal_signatures": {
                        "posix_signal:SIGSEGV": 2
                    },
                    "native_outcome_fingerprint_failures": {
                        "non_panic_native_signal": 3
                    },
                    "oracle_statuses": {
                        "native_abnormal_exit_inconclusive": 1
                    },
                }
            )

        result = module.evaluate_reclaim_case(case, minimum_repetitions=3)

        self.assertEqual(result["outcome"], "inconclusive")

    def test_rejects_unbounded_native_abnormal_exit_class(self) -> None:
        case = matched_case(treatment_signals=0)
        for allocator in ("reclaim_plain", "reclaim_checks"):
            vulnerable = next(
                value
                for value in case["arms"]
                if value["archive_variant"] == "vulnerable"
                and value["allocator_variant"] == allocator
            )
            vulnerable.update(
                {
                    "clean_exit_count": 0,
                    "native_abnormal_signal_signatures": {
                        "generic_abnormal_exit": 3
                    },
                    "native_outcome_fingerprint_failures": {
                        "non_panic_native_signal": 3
                    },
                    "oracle_statuses": {
                        "native_abnormal_exit_inconclusive": 1
                    },
                }
            )

        result = module.evaluate_reclaim_case(case, minimum_repetitions=3)

        self.assertEqual(result["outcome"], "inconclusive")

    def test_patched_controls_still_gate_matched_vulnerable_panic(self) -> None:
        case = matched_case(treatment_signals=0)
        fingerprint = {"panic:vulnerable-checkpoint": 3}
        for allocator in ("reclaim_plain", "reclaim_checks"):
            vulnerable = next(
                value
                for value in case["arms"]
                if value["archive_variant"] == "vulnerable"
                and value["allocator_variant"] == allocator
            )
            vulnerable.update(
                {
                    "clean_exit_count": 0,
                    "native_diagnostic_signatures": {
                        "rust_panic_observed": 3
                    },
                    "native_outcome_fingerprints": fingerprint,
                }
            )
        patched = next(
            value
            for value in case["arms"]
            if value["archive_variant"] == "patched"
            and value["allocator_variant"] == "reclaim_checks"
        )
        patched["patched_clean_count"] = 2

        result = module.evaluate_reclaim_case(case, minimum_repetitions=3)

        self.assertEqual(result["outcome"], "inconclusive")
        self.assertFalse(result["checks"]["patched_treatment_signal_free"])

    def test_rejects_unmatched_vulnerable_safe_panic_as_no_signal(self) -> None:
        case = matched_case(treatment_signals=0, safe_panic=True)
        treatment = case["arms"][1]
        treatment.update(
            {
                "clean_exit_count": 0,
                "native_diagnostic_signatures": {"rust_panic_observed": 3},
                "native_outcome_fingerprints": {"panic-vulnerable": 3},
            }
        )

        result = module.evaluate_reclaim_case(case, minimum_repetitions=3)

        self.assertEqual(result["outcome"], "inconclusive")
        self.assertFalse(
            result["checks"]["vulnerable_treatment_completed_in_all_repetitions"]
        )

    def test_rejects_vulnerable_treatment_below_minimum_repetitions(self) -> None:
        case = matched_case(treatment_signals=0)
        baseline = case["arms"][0]
        treatment = case["arms"][1]
        baseline["repetition_count"] = 2
        baseline["expected_oracle_observation_count"] = 2
        baseline["baseline_finding_signatures"] = {"asan_double_free": 2}
        treatment["repetition_count"] = 2

        result = module.evaluate_reclaim_case(case, minimum_repetitions=3)

        self.assertEqual(result["outcome"], "inconclusive")
        self.assertFalse(
            result["checks"]["vulnerable_treatment_completed_in_all_repetitions"]
        )

    def test_rejects_nonclean_control_without_panic_fingerprint(self) -> None:
        case = matched_case(safe_panic=True)
        for value in case["arms"]:
            if value["archive_variant"] == "patched":
                value["patched_control_fingerprints"] = {}

        result = module.evaluate_reclaim_case(case, minimum_repetitions=3)

        self.assertEqual(result["outcome"], "inconclusive")
        self.assertFalse(result["checks"]["patched_system_control_valid"])
        self.assertEqual(
            result["patched_control_mode"],
            "patched_system_control_fingerprint_missing",
        )

    def test_rejects_nonclean_control_with_different_panic_fingerprint(self) -> None:
        case = matched_case(safe_panic=True)
        patched_treatment = case["arms"][-1]
        patched_treatment["patched_control_fingerprints"] = {
            "panic:different-checkpoint": 3
        }

        result = module.evaluate_reclaim_case(case, minimum_repetitions=3)

        self.assertEqual(result["outcome"], "inconclusive")
        self.assertTrue(result["checks"]["patched_system_control_valid"])
        self.assertFalse(result["checks"]["patched_treatment_signal_free"])
        self.assertEqual(
            result["patched_control_mode"],
            "patched_treatment_control_fingerprint_mismatch",
        )

    def test_rejects_treatment_panic_when_system_control_exits_cleanly(self) -> None:
        case = matched_case(treatment_signals=0)
        case["arms"] = [
            value
            for value in case["arms"]
            if not (
                value["archive_variant"] == "patched"
                and value["allocator_variant"] == "reclaim_checks"
            )
        ] + [
            arm(
                "patched",
                "reclaim_checks",
                native={"rust_panic_observed": 3},
            )
        ]

        result = module.evaluate_reclaim_case(case, minimum_repetitions=3)

        self.assertEqual(result["outcome"], "inconclusive")
        self.assertFalse(result["checks"]["patched_treatment_signal_free"])
        self.assertEqual(
            result["patched_control_mode"], "patched_treatment_outcome_mismatch"
        )

    def test_accepts_matched_compile_rejection_patched_control(self) -> None:
        case = matched_case()
        case["arms"] = [
            value
            for value in case["arms"]
            if value["archive_variant"] != "patched"
        ] + [
            arm(
                "patched",
                "system",
                repetitions=0,
                expected=1,
                compile_errors={"E0599": 1},
            ),
            arm(
                "patched",
                "reclaim_plain",
                repetitions=0,
                compile_errors={"E0599": 1},
            ),
            arm(
                "patched",
                "reclaim_checks",
                repetitions=0,
                compile_errors={"E0599": 1},
            ),
        ]
        result = module.evaluate_reclaim_case(case, minimum_repetitions=3)
        self.assertEqual(result["outcome"], "detected")
        self.assertEqual(result["patched_control_mode"], "matched_compile_rejection")

    def test_partial_signal_is_inconclusive_without_complete_outcome_mode(self) -> None:
        result = module.evaluate_reclaim_case(
            matched_case(treatment_signals=2), minimum_repetitions=3
        )
        self.assertEqual(result["outcome"], "inconclusive")

    def test_missing_patched_arm_is_inconclusive(self) -> None:
        case = matched_case()
        case["arms"] = [
            value
            for value in case["arms"]
            if not (
                value["archive_variant"] == "patched"
                and value["allocator_variant"] == "reclaim_checks"
            )
        ]
        result = module.evaluate_reclaim_case(case, minimum_repetitions=3)
        self.assertEqual(result["outcome"], "inconclusive")

    def test_requires_all_six_feature_matched_reclaim_arms(self) -> None:
        case = matched_case()
        case["arms"] = [
            value
            for value in case["arms"]
            if not (
                value["archive_variant"] == "vulnerable"
                and value["allocator_variant"] == "reclaim_plain"
            )
        ]

        result = module.evaluate_reclaim_case(case, minimum_repetitions=3)

        self.assertEqual(result["outcome"], "inconclusive")
        self.assertEqual(result["reason"], "missing_matched_arms")
        self.assertEqual(result["missing_arms"], ["vulnerable_reclaim_plain"])

    def test_rejects_duplicate_feature_matched_reclaim_arm(self) -> None:
        case = matched_case()
        case["arms"].append(arm("vulnerable", "reclaim_plain", clean_exit_count=3))

        with self.assertRaisesRegex(module.ResultError, "duplicate summarized arm"):
            module.evaluate_reclaim_case(case, minimum_repetitions=3)

    def test_wrong_unialloc_manifest_path_fails_feature_provenance(self) -> None:
        case = matched_case()
        plain = next(
            value
            for value in case["arms"]
            if value["archive_variant"] == "vulnerable"
            and value["allocator_variant"] == "reclaim_plain"
        )
        plain["cargo_metadata_attestation"]["record"]["manifest_path"] = (
            "/tmp/lookalike/unialloc/Cargo.toml"
        )

        result = module.evaluate_reclaim_case(case, minimum_repetitions=3)

        self.assertEqual(result["outcome"], "inconclusive")
        self.assertFalse(
            result["checks"]["feature_matched_reclaim_ablation_valid"]
        )
        self.assertIn(
            "vulnerable_reclaim_plain.cargo_metadata_attestation."
            "record.manifest_path:mismatch",
            result["feature_provenance_failures"],
        )

    def test_wrong_resolved_features_fail_feature_provenance(self) -> None:
        case = matched_case()
        plain = next(
            value
            for value in case["arms"]
            if value["archive_variant"] == "patched"
            and value["allocator_variant"] == "reclaim_plain"
        )
        plain["cargo_metadata_attestation"]["record"]["resolved_features"] = [
            "reclaim_checks",
            "stats",
        ]

        result = module.evaluate_reclaim_case(case, minimum_repetitions=3)

        self.assertEqual(result["outcome"], "inconclusive")
        self.assertIn(
            "patched_reclaim_plain.cargo_metadata_attestation."
            "record.resolved_features:mismatch",
            result["feature_provenance_failures"],
        )

    def test_mixed_feature_contract_tokens_fail_closed(self) -> None:
        case = matched_case()
        plain = next(
            value
            for value in case["arms"]
            if value["archive_variant"] == "patched"
            and value["allocator_variant"] == "reclaim_plain"
        )
        plain["cargo_metadata_attestation"]["token"] = "4" * 64
        plain["cargo_metadata_attestation"]["record"][
            "feature_contract_sha256"
        ] = "4" * 64

        result = module.evaluate_reclaim_case(case, minimum_repetitions=3)

        self.assertEqual(result["outcome"], "inconclusive")
        self.assertIn(
            "reclaim_plain_attestations.feature_contract:mixed",
            result["feature_provenance_failures"],
        )

    def test_summarizer_attestation_failure_fails_closed(self) -> None:
        case = matched_case()
        treatment = case["arms"][1]
        treatment["cargo_metadata_attestation"].update(
            {
                "valid": False,
                "failure_counts": {"mixed_feature_contract": 1},
            }
        )

        result = module.evaluate_reclaim_case(case, minimum_repetitions=3)

        self.assertEqual(result["outcome"], "inconclusive")
        self.assertFalse(
            result["checks"]["feature_matched_reclaim_ablation_valid"]
        )

    def test_mixed_unialloc_implementation_digests_fail_closed(self) -> None:
        case = matched_case()
        treatment = next(
            value
            for value in case["arms"]
            if value["archive_variant"] == "vulnerable"
            and value["allocator_variant"] == "reclaim_checks"
        )
        subject = treatment["subject_cargo_features"]
        treatment["unialloc_implementation_sha256"] = "e" * 64
        treatment["direct_allocator_provenance"] = direct_summary_provenance(
            subject, "e" * 64
        )

        result = module.evaluate_reclaim_case(case, minimum_repetitions=3)

        self.assertEqual(result["outcome"], "inconclusive")
        self.assertIn(
            "reclaim_attestations.unialloc_implementation_sha256:mixed",
            result["feature_provenance_failures"],
        )

    def test_missing_unialloc_implementation_digest_fails_closed(self) -> None:
        case = matched_case()
        plain = next(
            value
            for value in case["arms"]
            if value["archive_variant"] == "patched"
            and value["allocator_variant"] == "reclaim_plain"
        )
        del plain["unialloc_implementation_sha256"]

        result = module.evaluate_reclaim_case(case, minimum_repetitions=3)

        self.assertEqual(result["outcome"], "inconclusive")
        self.assertIn(
            "patched_reclaim_plain.unialloc_implementation_sha256:invalid",
            result["feature_provenance_failures"],
        )

    def test_plain_and_checks_subject_features_must_match_per_archive(self) -> None:
        for archive in ("vulnerable", "patched"):
            with self.subTest(archive=archive):
                case = matched_case()
                plain = next(
                    value
                    for value in case["arms"]
                    if value["archive_variant"] == archive
                    and value["allocator_variant"] == "reclaim_plain"
                )
                set_direct_subject(
                    plain,
                    subject_feature_record(
                        "reclaim_plain", effective_features=["plain-only"]
                    ),
                )

                result = module.evaluate_reclaim_case(
                    case, minimum_repetitions=3
                )

                self.assertEqual(result["outcome"], "inconclusive")
                self.assertIn(
                    f"{archive}_reclaim_subject_cargo_features:mismatch",
                    result["feature_provenance_failures"],
                )

    def test_rsh075_mirrored_subject_override_is_feature_matched(self) -> None:
        case = matched_case()
        case["case_id"] = "RSH-075"
        case["scenario_id"] = "RSH-075-upstream-regression"
        for value in case["arms"]:
            allocator = value["allocator_variant"]
            if allocator not in {"reclaim_plain", "reclaim_checks"}:
                continue
            set_direct_subject(
                value,
                subject_feature_record(
                    allocator,
                    catalog_features=["sqlite", "__with_asan_tests"],
                    effective_features=["sqlite"],
                    override_applied=True,
                    override_allocator_variant="reclaim_checks",
                ),
            )

        result = module.evaluate_reclaim_case(case, minimum_repetitions=3)

        self.assertEqual(result["outcome"], "detected")
        self.assertNotIn(
            "vulnerable_reclaim_subject_cargo_features:mismatch",
            result["feature_provenance_failures"],
        )
        self.assertNotIn(
            "patched_reclaim_subject_cargo_features:mismatch",
            result["feature_provenance_failures"],
        )

    def test_self_consistent_non_repo_package_id_fails_closed(self) -> None:
        case = matched_case()
        plain = next(
            value
            for value in case["arms"]
            if value["archive_variant"] == "vulnerable"
            and value["allocator_variant"] == "reclaim_plain"
        )
        attestation = plain["cargo_metadata_attestation"]
        attestation["record"]["package_id"] = (
            "registry+https://github.com/rust-lang/crates.io-index#unialloc@0.1.0"
        )
        retoken_summary_attestation(attestation)

        result = module.evaluate_reclaim_case(case, minimum_repetitions=3)

        self.assertEqual(result["outcome"], "inconclusive")
        self.assertIn(
            "vulnerable_reclaim_plain.cargo_metadata_attestation."
            "record.package_id:non_repo_identity",
            result["feature_provenance_failures"],
        )

    def test_rejects_exact_signal_in_reclaim_plain_ablation(self) -> None:
        case = matched_case()
        plain = next(
            value
            for value in case["arms"]
            if value["archive_variant"] == "vulnerable"
            and value["allocator_variant"] == "reclaim_plain"
        )
        plain["allocator_signal_signatures"] = {
            "unialloc_pointer_already_released_check": 3
        }

        result = module.evaluate_reclaim_case(case, minimum_repetitions=3)

        self.assertEqual(result["outcome"], "inconclusive")
        self.assertFalse(
            result["checks"]["vulnerable_reclaim_plain_exact_signal_absent"]
        )

    def test_rejects_signal_in_patched_reclaim_plain_control(self) -> None:
        case = matched_case()
        plain = next(
            value
            for value in case["arms"]
            if value["archive_variant"] == "patched"
            and value["allocator_variant"] == "reclaim_plain"
        )
        plain["allocator_signal_signatures"] = {
            "unialloc_pointer_already_released_check": 3
        }

        result = module.evaluate_reclaim_case(case, minimum_repetitions=3)

        self.assertEqual(result["outcome"], "inconclusive")
        self.assertFalse(result["checks"]["patched_reclaim_plain_control_valid"])

    def test_emits_unique_case_scenario_binding(self) -> None:
        result = module.evaluate_reclaim_case(matched_case(), minimum_repetitions=3)

        self.assertEqual(result["scenario_id"], "RSH-043-matched")

    def test_accepts_unique_arm_level_scenario_binding(self) -> None:
        case = matched_case()
        del case["scenario_id"]
        for value in case["arms"]:
            value["scenario_ids"] = ["RSH-043-arm-bound"]

        result = module.evaluate_reclaim_case(case, minimum_repetitions=3)

        self.assertEqual(result["scenario_id"], "RSH-043-arm-bound")
        self.assertEqual(result["outcome"], "detected")

    def test_missing_scenario_binding_is_inconclusive(self) -> None:
        case = matched_case()
        del case["scenario_id"]

        result = module.evaluate_reclaim_case(case, minimum_repetitions=3)

        self.assertEqual(result["outcome"], "inconclusive")
        self.assertEqual(result["reason"], "missing_scenario_binding")
        self.assertFalse(result["true_positive"])
        self.assertEqual(result["result_semantics"], "evidence_gap")

    def test_ambiguous_scenario_binding_is_inconclusive(self) -> None:
        case = matched_case()
        case["scenario_ids"] = ["RSH-043-matched", "RSH-043-second"]

        result = module.evaluate_reclaim_case(case, minimum_repetitions=3)

        self.assertEqual(result["outcome"], "inconclusive")
        self.assertEqual(result["reason"], "ambiguous_scenario_binding")
        self.assertEqual(
            result["scenario_ids"], ["RSH-043-matched", "RSH-043-second"]
        )

    def test_exports_type_isolation_as_edge_scoped_mitigation(self) -> None:
        scenario = matched_derived_scenario()

        result = module.evaluate_type_isolation_edge(
            scenario, minimum_repetitions=3
        )
        self.assertEqual(result["outcome"], "mitigated")
        self.assertEqual(
            result["evidence_scope"], "exploit_enabling_cross_identity_reuse_edge"
        )
        self.assertFalse(result["source_vulnerability_detection_validated"])
        self.assertFalse(result["automatic_source_coverage"])
        self.assertFalse(result["source_level_true_positive"])
        self.assertFalse(result["claim_grade"])
        self.assertFalse(result["synthetic_reduction"])
        self.assertEqual(result["reduction_fidelity"], "manual_derived_reduction")
        self.assertTrue(result["true_positive"])
        self.assertEqual(
            result["result_semantics"],
            "causal_mitigation_true_positive",
        )
        self.assertEqual(
            result["positive_scope"],
            "exploit_enabling_cross_identity_reuse_edge",
        )

    def test_synthetic_typeiso_reduction_requires_explicit_claim_marker(self) -> None:
        scenario = matched_derived_scenario()
        scenario["case_id"] = "RSH-065"

        missing_marker = module.evaluate_type_isolation_edge(
            scenario, minimum_repetitions=3
        )

        self.assertEqual(missing_marker["outcome"], "inconclusive")
        self.assertTrue(missing_marker["synthetic_reduction"])
        self.assertIn(
            "scenario.claim_scope:synthetic_reduction_marker_missing",
            missing_marker["evidence_gaps"],
        )

        scenario["edge_evaluation"]["claim_scope"] = (
            "derived RSH-065 manually modeled cross-identity edge only"
        )
        validated = module.evaluate_type_isolation_edge(
            scenario, minimum_repetitions=3
        )
        self.assertEqual(validated["outcome"], "mitigated")
        self.assertEqual(
            validated["reduction_fidelity"], "synthetic_manual_reduction"
        )

    def test_exports_diagnostic_only_typed_arms_with_asan_system_baseline(self) -> None:
        result = module.evaluate_type_isolation_edge(
            matched_diagnostic_only_derived_scenario(), minimum_repetitions=3
        )

        self.assertEqual(result["outcome"], "mitigated")
        self.assertTrue(result["true_positive"])
        self.assertTrue(result["checks"]["typed_allocator_oracle_policy_valid"])
        self.assertTrue(
            result["checks"][
                "vulnerable_typed_plain_oracle_contract_satisfied"
            ]
        )

    def test_rejects_invalid_typed_allocator_oracle_policy(self) -> None:
        scenario = matched_diagnostic_only_derived_scenario()
        scenario["typed_allocator_expected_oracle"] = "false"

        result = module.evaluate_type_isolation_edge(
            scenario, minimum_repetitions=3
        )

        self.assertEqual(result["outcome"], "inconclusive")
        self.assertIn(
            "scenario.typed_allocator_expected_oracle:invalid_boolean",
            result["evidence_gaps"],
        )

    def test_typeiso_export_ignores_spoofed_positive_edge_booleans(self) -> None:
        scenario = matched_derived_scenario()
        scenario["edge_evaluation"].update(
            {
                "baseline_vulnerability_and_address_reuse_reproduced": True,
                "patched_system_control_reproduced": True,
                "typed_plain_address_reuse_ablation_reproduced": True,
                "typeiso_reuse_edge_blocked_and_reported": True,
                "validated": True,
            }
        )
        scenario["arms"][0]["address_reuse_observation_count"] = 0

        result = module.evaluate_type_isolation_edge(
            scenario, minimum_repetitions=3
        )

        self.assertEqual(result["outcome"], "inconclusive")
        self.assertIn(
            "system_reuse_observed_in_all_repetitions", result["failed_checks"]
        )

    def test_typeiso_export_requires_all_six_matched_arms(self) -> None:
        scenario = matched_derived_scenario()
        scenario["arms"] = [
            value
            for value in scenario["arms"]
            if not (
                value["archive_variant"] == "patched"
                and value["allocator_variant"] == "typed_plain"
            )
        ]

        result = module.evaluate_type_isolation_edge(
            scenario, minimum_repetitions=3
        )

        self.assertEqual(result["outcome"], "inconclusive")
        self.assertEqual(result["reason"], "missing_matched_arms")
        self.assertEqual(result["missing_arms"], ["patched_typed_plain"])

    def test_typeiso_export_requires_bound_report_in_every_repetition(self) -> None:
        scenario = matched_derived_scenario()
        scenario["arms"][2]["bound_replacement_site_count"] = 2

        result = module.evaluate_type_isolation_edge(
            scenario, minimum_repetitions=3
        )

        self.assertEqual(result["outcome"], "inconclusive")
        self.assertIn(
            "typeiso_report_bound_in_all_repetitions", result["failed_checks"]
        )

    def test_typeiso_export_rejects_patched_typeiso_denial(self) -> None:
        scenario = matched_derived_scenario()
        scenario["arms"][-1]["matching_reuse_denial_event_count"] = 1

        result = module.evaluate_type_isolation_edge(
            scenario, minimum_repetitions=3
        )

        self.assertEqual(result["outcome"], "inconclusive")
        self.assertIn("patched_typeiso_control_valid", result["failed_checks"])

    def test_typeiso_export_requires_same_identity_reuse_in_patched_controls(self) -> None:
        scenario = matched_derived_scenario()
        for arm in scenario["arms"]:
            if arm["archive_variant"] == "patched":
                arm["address_reuse_observation_count"] = 0

        result = module.evaluate_type_isolation_edge(
            scenario, minimum_repetitions=3
        )

        self.assertEqual(result["outcome"], "inconclusive")
        self.assertFalse(result["true_positive"])
        self.assertEqual(
            {
                "patched_system_control_valid",
                "patched_typed_plain_control_valid",
                "patched_typeiso_control_valid",
            },
            set(result["failed_checks"]),
        )

    def test_typeiso_export_reports_missing_raw_count_as_evidence_gap(self) -> None:
        scenario = matched_derived_scenario()
        del scenario["arms"][2]["bound_replacement_site_count"]

        result = module.evaluate_type_isolation_edge(
            scenario, minimum_repetitions=3
        )

        self.assertEqual(result["outcome"], "inconclusive")
        self.assertEqual(
            result["reason"], "derived_reuse_arm_evidence_missing_or_invalid"
        )
        self.assertIn(
            "vulnerable_typeiso.bound_replacement_site_count:missing",
            result["evidence_gaps"],
        )

    def test_typeiso_export_rejects_incomplete_exit_code_list(self) -> None:
        scenario = matched_derived_scenario()
        scenario["arms"][1]["exit_codes"] = [0, 0]

        result = module.evaluate_type_isolation_edge(
            scenario, minimum_repetitions=3
        )

        self.assertEqual(result["outcome"], "inconclusive")
        self.assertIn(
            "vulnerable_typed_plain_completed_in_all_repetitions",
            result["failed_checks"],
        )

    def test_typeiso_export_allows_extra_nonrequired_allocator_arms(self) -> None:
        scenario = matched_derived_scenario()
        scenario["arms"].extend(
            [
                derived_arm("vulnerable", "unialloc", reuse=3),
                derived_arm("patched", "unialloc", reuse=3),
            ]
        )

        result = module.evaluate_type_isolation_edge(
            scenario, minimum_repetitions=3
        )

        self.assertEqual(result["outcome"], "mitigated")

    def test_rejects_duplicate_derived_reuse_arm(self) -> None:
        scenario = {
            "advisory_id": "RUSTSEC-TEST-0002",
            "case_id": "RSH-041",
            "scenario_id": "RSH-041-derived-reuse",
            "repetitions_requested": 3,
            "edge_evaluation": {},
            "arms": [
                {
                    "archive_variant": "vulnerable",
                    "allocator_variant": "system",
                },
                {
                    "archive_variant": "vulnerable",
                    "allocator_variant": "system",
                },
            ],
        }

        with self.assertRaisesRegex(module.ResultError, "duplicate derived reuse arm"):
            module.evaluate_type_isolation_edge(scenario, minimum_repetitions=3)


if __name__ == "__main__":
    unittest.main()
