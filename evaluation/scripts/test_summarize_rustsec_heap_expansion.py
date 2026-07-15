#!/usr/bin/env python3
"""Tests for the RustSec heap-expansion experiment summarizer."""

from __future__ import annotations

import contextlib
import importlib.util
import io
import json
import pathlib
import tempfile
import unittest


ROOT = pathlib.Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "evaluation" / "scripts" / "summarize_rustsec_heap_expansion.py"
spec = importlib.util.spec_from_file_location("rustsec_heap_expansion_summary", SCRIPT)
assert spec is not None and spec.loader is not None
summary = importlib.util.module_from_spec(spec)
spec.loader.exec_module(summary)


def observation(
    *,
    tool: str | None = None,
    native: str | None = None,
    clean: bool = False,
    sanitizer: bool | None = None,
    panic: bool | None = None,
) -> dict[str, object]:
    return {
        "repetition": 1,
        "exit_code": 0 if clean else 1,
        "timed_out": False,
        "clean_exit": clean,
        "tool_finding_signature": tool,
        "native_diagnostic_signature": native,
        "rust_panic_or_assertion_observed": (
            native == "rust_panic_observed" if panic is None else panic
        ),
        "sanitizer_finding_observed": bool(tool) if sanitizer is None else sanitizer,
    }


def run_record(
    *,
    stderr: str = "",
    exit_code: int = 1,
    timed_out: bool = False,
    critical_checkpoint: str | None = None,
) -> dict[str, object]:
    result: dict[str, object] = {
        "exit_code": exit_code,
        "timed_out": timed_out,
        "stdout": "",
        "stderr": stderr,
    }
    if critical_checkpoint is not None:
        result["critical_checkpoint"] = critical_checkpoint
    return result


def synthetic_run(value: dict[str, object], repetition: int) -> dict[str, object]:
    native = value.get("native_diagnostic_signature")
    panic = value.get("rust_panic_or_assertion_observed") is True
    if native == "unialloc_pointer_already_released_check":
        stderr = (
            f"thread 'main' ({1000 + repetition}) panicked at "
            "/tmp/test/unialloc/src/alloc_api/type_isolation.rs:123:45:\n"
            "pointer already released\n"
        )
    elif native == "rust_panic_observed" or panic:
        stderr = (
            f"thread 'main' ({1000 + repetition}) panicked at "
            f"/tmp/run-{repetition}/work/project/src/witness.rs:27:13:\n"
            "test panic checkpoint\n"
            "note: run with `RUST_BACKTRACE=1` to display a backtrace\n"
        )
    else:
        stderr = ""
    return run_record(
        stderr=stderr,
        exit_code=int(value["exit_code"]),
        timed_out=bool(value["timed_out"]),
    )


def cargo_metadata_attestation(
    allocator: str,
    *,
    features: list[str] | None = None,
    manifest_path: pathlib.Path | None = None,
    package_count: int = 1,
) -> dict[str, object]:
    expected_features = features or {
        "unialloc": ["stats"],
        "reclaim_plain": ["stats"],
        "reclaim_checks": ["reclaim_checks", "stats"],
    }[allocator]
    contract: dict[str, object] = {
        "schema_version": 1,
        "package_count": package_count,
        "resolve_node_count": 1,
        "package_id": (
            f"path+{(ROOT / 'unialloc').resolve().as_uri()}#0.1.0"
        ),
        "manifest_path": str(
            manifest_path or (ROOT / "unialloc" / "Cargo.toml").resolve()
        ),
        "package_source": None,
        "expected_features": expected_features,
        "resolved_features": expected_features,
        "default_features_expected": False,
        "default_feature_resolved": False,
    }
    return {
        **contract,
        "cargo_metadata_stdout_sha256": "a" * 64,
        "feature_contract_sha256": summary.canonical_sha256(contract),
    }


def retoken_attestation(value: dict[str, object]) -> None:
    contract = {
        key: field
        for key, field in value.items()
        if key not in {
            "cargo_metadata_stdout_sha256",
            "feature_contract_sha256",
        }
    }
    value["feature_contract_sha256"] = summary.canonical_sha256(contract)


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
        "catalog_features": catalog_features or [],
        "effective_features": effective_features or [],
        "default_features_enabled": True,
        "override_applied": override_applied,
        "override_allocator_variant": override_allocator_variant,
        "override_source": (
            "scenario.allocator_cargo_feature_overrides"
            if override_applied
            else None
        ),
    }


def arm(
    archive: str,
    allocator: str,
    observations: list[dict[str, object]],
    *,
    case_id: str = "RSH-041",
    stats: list[dict[str, int]] | None = None,
    rewrites: dict[str, int] | None = None,
    blockers: list[str] | None = None,
    denial_count: int = 0,
    runs: list[dict[str, object]] | None = None,
    execution_mode: str | None = None,
    implementation_sha256: str = "d" * 64,
    subject_features: dict[str, object] | None = None,
) -> dict[str, object]:
    normalized_observations = []
    for index, record in enumerate(observations, start=1):
        normalized_observations.append({**record, "repetition": index})
    value: dict[str, object] = {
        "case_id": case_id,
        "archive_variant": archive,
        "allocator_variant": allocator,
        "execution_mode": execution_mode
        or ("asan" if allocator == "system" else "native_diagnostic"),
        "execution_status": "completed",
        "oracle_validation": {"repetition_observations": normalized_observations},
        "runs": runs
        if runs is not None
        else [
            synthetic_run(record, index)
            for index, record in enumerate(normalized_observations, start=1)
        ],
        "efficacy_blockers": blockers or [],
        "pass_audit_summary": rewrites or {},
    }
    if allocator in {"unialloc", "reclaim_plain", "reclaim_checks"}:
        subject = subject_features or subject_feature_record(allocator)
        provenance_subject = json.loads(json.dumps(subject))
        fingerprint_subject = json.loads(json.dumps(subject))
        fingerprint_payload = {
            "archive_variant": archive,
            "allocator_variant": allocator,
            "unialloc_implementation_sha256": implementation_sha256,
            "subject_cargo_features": fingerprint_subject,
        }
        value["allocator_provenance"] = {
            "cargo_metadata_attestation": cargo_metadata_attestation(allocator),
            "unialloc_implementation_sha256": implementation_sha256,
            "subject_cargo_features": provenance_subject,
        }
        value["fingerprint_payload"] = fingerprint_payload
        value["fingerprint"] = summary.canonical_sha256(fingerprint_payload)
    if stats is not None:
        value["runtime_stats_by_repetition"] = [
            {"repetition": index, "status": "validated", "stats": record}
            for index, record in enumerate(stats, start=1)
        ]
    if denial_count:
        value["reuse_denial_evidence"] = {
            "matching_reuse_denial_event_count": denial_count
        }
    return value


class RustSecHeapExpansionSummaryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = pathlib.Path(self.temporary.name)
        self.catalog = self.root / "catalog.json"
        self.catalog.write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "source": "test",
                    "claim_grade": False,
                    "counts": {},
                    "cases": [
                        {
                            "case_id": "RSH-041",
                            "advisory_id": "RUSTSEC-TEST-0001",
                            "crate": "demo",
                            "scenarios": [{"scenario_id": "RSH-041-upstream"}],
                        },
                        {
                            "case_id": "RSH-042",
                            "advisory_id": "RUSTSEC-TEST-0002",
                            "crate": "unrun",
                            "scenarios": [{"scenario_id": "RSH-042-upstream"}],
                        },
                    ],
                }
            ),
            encoding="utf-8",
        )

    def write_experiment(
        self, name: str, arms: list[dict[str, object]], *, scenario: str = "RSH-041-upstream"
    ) -> pathlib.Path:
        path = self.root / name
        path.write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "source": "test-experiment",
                    "scenario_id": scenario,
                    "arms": arms,
                }
            ),
            encoding="utf-8",
        )
        return path

    def test_artifact_record_uses_repository_relative_path(self) -> None:
        self.assertEqual(
            summary.artifact_record(SCRIPT)["path"],
            str(SCRIPT.relative_to(ROOT)),
        )

    def test_resolves_missing_catalog_advisory_from_corpus(self) -> None:
        catalog = json.loads(self.catalog.read_text(encoding="utf-8"))
        catalog["cases"][0]["advisory_id"] = None
        self.catalog.write_text(json.dumps(catalog), encoding="utf-8")
        corpus = self.root / "corpus.json"
        corpus.write_text(
            json.dumps(
                {
                    "cases": [
                        {
                            "case_id": "RSH-041",
                            "advisory_id": "RUSTSEC-TEST-0041",
                        }
                    ]
                }
            ),
            encoding="utf-8",
        )

        result = summary.summarize(self.catalog, [], corpus_path=corpus)

        self.assertEqual(
            self.case(result, "RSH-041")["advisory_id"],
            "RUSTSEC-TEST-0041",
        )
        self.assertEqual(result["corpus_input"]["sha256"], summary.artifact_record(corpus)["sha256"])

    @staticmethod
    def case(result: dict[str, object], case_id: str) -> dict[str, object]:
        return next(case for case in result["cases"] if case["case_id"] == case_id)

    @staticmethod
    def result_arm(
        case: dict[str, object], archive: str, allocator: str
    ) -> dict[str, object]:
        return next(
            value
            for value in case["arms"]
            if value["archive_variant"] == archive
            and value["allocator_variant"] == allocator
        )

    def test_merges_system_and_typed_files_and_classifies_ablation(self) -> None:
        system = self.write_experiment(
            "system.json",
            [
                arm(
                    "vulnerable",
                    "system",
                    [
                        observation(tool="asan_double_free"),
                        observation(tool="asan_double_free"),
                    ],
                ),
                arm(
                    "patched",
                    "system",
                    [observation(clean=True), observation(clean=True)],
                ),
            ],
        )
        typed = self.write_experiment(
            "typed.json",
            [
                arm(
                    "vulnerable",
                    "typed_plain",
                    [observation(clean=True), observation(clean=True)],
                    stats=[
                        {"typed_allocations": 4, "fallback_allocations": 1},
                        {"typed_allocations": 5, "fallback_allocations": 0},
                    ],
                    rewrites={
                        "semantic_rewrites_applied": 2,
                        "total_compiler_rewrites_applied": 3,
                    },
                    blockers=["critical_site_coverage_not_validated"],
                ),
                arm(
                    "vulnerable",
                    "typeiso",
                    [
                        observation(native="unialloc_pointer_already_released_check"),
                        observation(native="unialloc_pointer_already_released_check"),
                    ],
                    stats=[{"typed_allocations": 6, "fallback_allocations": 2}],
                    rewrites={"total_compiler_rewrites_applied": 4},
                ),
            ],
        )

        result = summary.summarize(
            self.catalog,
            [f"RSH-041={system}", f"RSH-041={typed}"],
        )
        self.assertRegex(result["catalog_sha256"], r"^[0-9a-f]{64}$")
        self.assertEqual(
            [record["case_id"] for record in result["experiment_inputs"]],
            ["RSH-041", "RSH-041"],
        )
        self.assertEqual(
            {record["path"] for record in result["experiment_inputs"]},
            {str(system.resolve()), str(typed.resolve())},
        )
        self.assertTrue(
            all(
                len(record["sha256"]) == 64
                for record in result["experiment_inputs"]
            )
        )
        case = self.case(result, "RSH-041")
        vulnerable_system = self.result_arm(case, "vulnerable", "system")
        patched_system = self.result_arm(case, "patched", "system")
        typed_plain = self.result_arm(case, "vulnerable", "typed_plain")
        typeiso = self.result_arm(case, "vulnerable", "typeiso")

        self.assertEqual(case["experiment_count"], 2)
        self.assertEqual(case["classification"], "typeiso_specific_signal")
        self.assertEqual(vulnerable_system["classification"], "baseline_reproduced")
        self.assertEqual(vulnerable_system["baseline_finding_count"], 2)
        self.assertEqual(
            vulnerable_system["baseline_finding_signatures"],
            {"asan_double_free": 2},
        )
        self.assertEqual(patched_system["patched_clean_count"], 2)
        self.assertEqual(patched_system["clean_exit_count"], 2)
        self.assertEqual(case["scenario_id"], "RSH-041-upstream")
        self.assertEqual(case["scenario_ids"], ["RSH-041-upstream"])
        self.assertEqual(
            vulnerable_system["scenario_id"], "RSH-041-upstream"
        )
        self.assertEqual(patched_system["classification"], "insufficient_evidence")
        self.assertEqual(typed_plain["classification"], "direct_effect_absent")
        self.assertEqual(typed_plain["typed_allocation_count"], 9)
        self.assertEqual(typed_plain["fallback_allocation_count"], 1)
        self.assertEqual(
            typed_plain["compiler_rewrite_counts"]["total_compiler_rewrites_applied"],
            3,
        )
        self.assertEqual(
            typed_plain["eligibility_blockers"],
            ["critical_site_coverage_not_validated"],
        )
        self.assertEqual(typeiso["classification"], "typeiso_specific_signal")
        self.assertEqual(typeiso["native_diagnostic_count"], 2)
        self.assertFalse(typeiso["mitigation_inferred"])

        unrun = self.case(result, "RSH-042")
        self.assertEqual(unrun["classification"], "insufficient_evidence")
        self.assertEqual(unrun["arms"], [])

    def test_typeiso_signal_shared_with_typed_plain_is_not_specific(self) -> None:
        experiment = self.write_experiment(
            "shared.json",
            [
                arm(
                    "vulnerable",
                    "typed_plain",
                    [
                        observation(
                            native="unialloc_pointer_already_released_check"
                        )
                    ],
                ),
                arm(
                    "vulnerable",
                    "typeiso",
                    [
                        observation(
                            native="unialloc_pointer_already_released_check"
                        )
                    ],
                ),
            ],
        )

        result = summary.summarize(self.catalog, [f"RSH-041={experiment}"])
        case = self.case(result, "RSH-041")
        self.assertEqual(
            self.result_arm(case, "vulnerable", "typeiso")["classification"],
            "allocator_signal_observed",
        )
        self.assertEqual(case["classification"], "allocator_signal_observed")

    def test_records_matched_safe_panic_as_expected_patched_control(self) -> None:
        patched = arm(
            "patched",
            "system",
            [
                observation(panic=True),
                observation(panic=True),
                observation(panic=True),
            ],
        )
        patched["oracle_validation"].update(
            {
                "expected_oracle_observed": True,
                "status": "patched_safe_panic_control_observed",
            }
        )
        experiment = self.write_experiment("safe-panic.json", [patched])

        result = summary.summarize(self.catalog, [f"RSH-041={experiment}"])
        rendered = self.result_arm(self.case(result, "RSH-041"), "patched", "system")
        self.assertEqual(rendered["patched_clean_count"], 0)
        self.assertEqual(rendered["expected_oracle_observation_count"], 3)
        self.assertEqual(
            rendered["oracle_statuses"],
            {"patched_safe_panic_control_observed": 1},
        )
        self.assertTrue(rendered["native_diagnostic_revalidation"]["valid"])
        self.assertEqual(len(rendered["patched_control_fingerprints"]), 1)
        fingerprint, count = next(
            iter(rendered["patched_control_fingerprints"].items())
        )
        self.assertEqual(count, 3)
        normalized = rendered["native_outcome_fingerprint_records"][fingerprint][
            "normalized_outcome"
        ]
        self.assertEqual(normalized["returncode"], 1)
        self.assertEqual(
            normalized["critical_checkpoint"],
            {
                "panic_source": "project/src/witness.rs",
                "panic_message": "test panic checkpoint",
            },
        )

    def test_different_panic_reasons_have_different_fingerprints(self) -> None:
        first, first_error = summary.normalized_native_outcome(
            run_record(
                stderr=(
                    "thread 'main' (1001) panicked at "
                    "/tmp/one/work/project/src/witness.rs:10:2:\n"
                    "first panic reason\n"
                ),
                exit_code=101,
            )
        )
        second, second_error = summary.normalized_native_outcome(
            run_record(
                stderr=(
                    "thread 'main' (2002) panicked at "
                    "/tmp/two/work/project/src/witness.rs:99:7:\n"
                    "second panic reason\n"
                ),
                exit_code=101,
            )
        )

        self.assertIsNone(first_error)
        self.assertIsNone(second_error)
        self.assertNotEqual(
            summary.outcome_fingerprint(first),
            summary.outcome_fingerprint(second),
        )

    def test_repeated_panic_before_destructor_abort_has_one_checkpoint(self) -> None:
        outcome, error = summary.normalized_native_outcome(
            run_record(
                stderr=(
                    "thread 'main' (1001) panicked at src/witness.rs:27:13:\n"
                    "intentional drop panic\n"
                    "thread 'main' (1001) panicked at src/witness.rs:27:13:\n"
                    "intentional drop panic\n"
                    "thread 'main' (1001) panicked at "
                    "/rustc/example/library/core/src/panicking.rs:233:5:\n"
                    "panic in a destructor during cleanup\n"
                    "thread caused non-unwinding panic. aborting.\n"
                ),
                exit_code=-6,
            )
        )

        self.assertIsNone(error)
        self.assertEqual(
            outcome["critical_checkpoint"],
            {
                "panic_source": "src/witness.rs",
                "panic_message": "intentional drop panic",
            },
        )

    def test_destructor_abort_keeps_distinct_checkpoints_ambiguous(self) -> None:
        outcome, error = summary.normalized_native_outcome(
            run_record(
                stderr=(
                    "thread 'main' (1001) panicked at src/first.rs:10:2:\n"
                    "first panic reason\n"
                    "thread 'main' (1001) panicked at src/second.rs:20:3:\n"
                    "second panic reason\n"
                    "thread 'main' (1001) panicked at "
                    "/rustc/example/library/core/src/panicking.rs:233:5:\n"
                    "panic in a destructor during cleanup\n"
                    "thread caused non-unwinding panic. aborting.\n"
                ),
                exit_code=-6,
            )
        )

        self.assertIsNone(outcome)
        self.assertEqual(error, "missing_or_ambiguous_panic_checkpoint")

    def test_missing_parseable_panic_checkpoint_has_no_fingerprint(self) -> None:
        experiment = self.write_experiment(
            "missing-checkpoint.json",
            [
                arm(
                    "patched",
                    "reclaim_checks",
                    [observation(native="rust_panic_observed", panic=True)],
                    runs=[
                        run_record(
                            stderr=(
                                "thread 'main' panicked at an unknown checkpoint\n"
                                "same panic reason\n"
                            ),
                            exit_code=1,
                        )
                    ],
                )
            ],
        )

        result = summary.summarize(self.catalog, [f"RSH-041={experiment}"])
        rendered = self.result_arm(
            self.case(result, "RSH-041"), "patched", "reclaim_checks"
        )
        self.assertEqual(rendered["native_outcome_fingerprints"], {})
        self.assertEqual(
            rendered["native_outcome_fingerprint_failures"],
            {"missing_or_ambiguous_panic_checkpoint": 1},
        )

    def test_records_repeated_synchronous_native_signal_separately(self) -> None:
        observations = [
            {
                **observation(native=None, panic=False),
                "exit_code": -11,
            }
            for _ in range(3)
        ]
        experiment = self.write_experiment(
            "native-signal.json",
            [
                arm(
                    "vulnerable",
                    "reclaim_checks",
                    observations,
                    runs=[run_record(exit_code=-11) for _ in range(3)],
                )
            ],
        )

        result = summary.summarize(self.catalog, [f"RSH-041={experiment}"])
        rendered = self.result_arm(
            self.case(result, "RSH-041"), "vulnerable", "reclaim_checks"
        )
        self.assertEqual(rendered["native_outcome_fingerprints"], {})
        self.assertEqual(
            rendered["native_abnormal_signal_signatures"],
            {"posix_signal:SIGSEGV": 3},
        )
        self.assertEqual(
            rendered["native_outcome_fingerprint_failures"],
            {"non_panic_native_signal": 3},
        )

    def test_native_signal_normalization_rejects_ambiguous_exit_classes(self) -> None:
        self.assertIsNone(
            summary.normalized_native_abnormal_signal(run_record(exit_code=139))
        )
        self.assertIsNone(
            summary.normalized_native_abnormal_signal(run_record(exit_code=-9))
        )
        self.assertIsNone(
            summary.normalized_native_abnormal_signal(
                run_record(exit_code=-11, timed_out=True)
            )
        )

    def test_mixed_panic_fingerprints_remain_explicit(self) -> None:
        runs = [
            run_record(
                stderr=(
                    "thread 'main' (1001) panicked at src/witness.rs:10:2:\n"
                    "first panic reason\n"
                ),
                exit_code=1,
            ),
            run_record(
                stderr=(
                    "thread 'main' (1002) panicked at src/witness.rs:11:3:\n"
                    "second panic reason\n"
                ),
                exit_code=1,
            ),
        ]
        experiment = self.write_experiment(
            "mixed-fingerprints.json",
            [
                arm(
                    "patched",
                    "reclaim_checks",
                    [
                        observation(native="rust_panic_observed", panic=True),
                        observation(native="rust_panic_observed", panic=True),
                    ],
                    runs=runs,
                )
            ],
        )

        result = summary.summarize(self.catalog, [f"RSH-041={experiment}"])
        rendered = self.result_arm(
            self.case(result, "RSH-041"), "patched", "reclaim_checks"
        )
        self.assertEqual(len(rendered["patched_control_fingerprints"]), 2)
        self.assertEqual(
            sorted(rendered["patched_control_fingerprints"].values()), [1, 1]
        )

    def test_panic_fingerprint_ignores_temp_path_line_pid_and_address_drift(
        self,
    ) -> None:
        first, first_error = summary.normalized_native_outcome(
            run_record(
                stderr=(
                    "thread 'main' (1001) panicked at "
                    "/tmp/one/work/subject/demo-1.0/src/lib.rs:10:2:\n"
                    "pointer was 0x1234\n"
                ),
                exit_code=101,
            )
        )
        second, second_error = summary.normalized_native_outcome(
            run_record(
                stderr=(
                    "thread 'main' (9876) panicked at "
                    "/home/user/build/work/subject/demo-1.0/src/lib.rs:999:77:\n"
                    "pointer was 0xfeedbeef\n"
                ),
                exit_code=101,
            )
        )

        self.assertIsNone(first_error)
        self.assertIsNone(second_error)
        self.assertEqual(first, second)
        self.assertEqual(
            first["critical_checkpoint"],
            {
                "panic_source": "subject/demo-1.0/src/lib.rs",
                "panic_message": "pointer was <address>",
            },
        )

    def test_current_parser_mismatch_removes_allocator_signal(self) -> None:
        experiment = self.write_experiment(
            "parser-mismatch.json",
            [
                arm(
                    "vulnerable",
                    "reclaim_checks",
                    [
                        observation(
                            native="unialloc_pointer_already_released_check",
                            panic=True,
                        )
                    ],
                    runs=[
                        run_record(
                            stderr=(
                                "thread 'main' (1001) panicked at "
                                "src/witness.rs:10:2:\n"
                                "pointer already released\n"
                            ),
                            exit_code=1,
                        )
                    ],
                )
            ],
        )

        result = summary.summarize(self.catalog, [f"RSH-041={experiment}"])
        rendered = self.result_arm(
            self.case(result, "RSH-041"), "vulnerable", "reclaim_checks"
        )
        self.assertFalse(rendered["native_diagnostic_revalidation"]["valid"])
        self.assertEqual(rendered["allocator_signal_count"], 0)
        self.assertIn(
            "native_diagnostic_source_revalidation_failed",
            rendered["eligibility_blockers"],
        )

    def test_records_matched_compile_error_codes(self) -> None:
        patched = arm("patched", "system", [])
        patched["oracle_validation"].update(
            {
                "expected_oracle_observed": True,
                "status": "patched_control_compile_rejection_observed",
                "matched_compile_error_codes": ["E0599"],
            }
        )
        experiment = self.write_experiment("compile-rejection.json", [patched])

        result = summary.summarize(self.catalog, [f"RSH-041={experiment}"])
        rendered = self.result_arm(self.case(result, "RSH-041"), "patched", "system")
        self.assertEqual(rendered["matched_compile_error_codes"], {"E0599": 1})

    def test_plain_rust_panic_is_recorded_but_is_not_an_allocator_signal(self) -> None:
        experiment = self.write_experiment(
            "panic-only.json",
            [
                arm(
                    "vulnerable",
                    "typed_plain",
                    [observation(native="rust_panic_observed")],
                ),
                arm(
                    "vulnerable",
                    "typeiso",
                    [observation(native="rust_panic_observed")],
                ),
            ],
        )

        result = summary.summarize(self.catalog, [f"RSH-041={experiment}"])
        case = self.case(result, "RSH-041")
        typeiso = self.result_arm(case, "vulnerable", "typeiso")
        self.assertEqual(typeiso["native_diagnostic_count"], 1)
        self.assertEqual(typeiso["allocator_signal_count"], 0)
        self.assertEqual(len(typeiso["native_outcome_fingerprints"]), 1)
        self.assertEqual(
            list(typeiso["native_outcome_fingerprints"].values()), [1]
        )
        self.assertEqual(typeiso["classification"], "insufficient_evidence")
        self.assertEqual(case["classification"], "insufficient_evidence")

    def test_reclaim_checks_exact_diagnostic_is_an_allocator_signal(self) -> None:
        experiment = self.write_experiment(
            "reclaim-checks.json",
            [
                arm(
                    "vulnerable",
                    "reclaim_checks",
                    [
                        observation(
                            native="unialloc_pointer_already_released_check"
                        )
                    ],
                ),
                arm("patched", "reclaim_checks", [observation(clean=True)]),
            ],
        )

        result = summary.summarize(self.catalog, [f"RSH-041={experiment}"])
        case = self.case(result, "RSH-041")
        reclaim = self.result_arm(case, "vulnerable", "reclaim_checks")
        self.assertEqual(reclaim["classification"], "allocator_signal_observed")
        self.assertEqual(
            reclaim["allocator_signal_signatures"],
            {"unialloc_pointer_already_released_check": 1},
        )

    def test_reclaim_feature_attestations_are_canonical_and_rendered(self) -> None:
        experiment = self.write_experiment(
            "reclaim-feature-attestations.json",
            [
                arm("vulnerable", "reclaim_plain", [observation(clean=True)]),
                arm(
                    "vulnerable",
                    "reclaim_checks",
                    [
                        observation(
                            native="unialloc_pointer_already_released_check"
                        )
                    ],
                ),
            ],
        )

        result = summary.summarize(self.catalog, [f"RSH-041={experiment}"])
        case = self.case(result, "RSH-041")
        self.assertEqual(
            [value["allocator_variant"] for value in case["arms"]],
            ["reclaim_plain", "reclaim_checks"],
        )
        for allocator, expected_features in (
            ("reclaim_plain", ["stats"]),
            ("reclaim_checks", ["reclaim_checks", "stats"]),
        ):
            rendered = self.result_arm(case, "vulnerable", allocator)
            attestation = rendered["cargo_metadata_attestation"]
            self.assertTrue(attestation["required"])
            self.assertTrue(attestation["valid"])
            self.assertEqual(attestation["observation_count"], 1)
            self.assertRegex(attestation["token"], r"^[0-9a-f]{64}$")
            self.assertEqual(attestation["failure_counts"], {})
            self.assertEqual(
                attestation["record"]["manifest_path"],
                "unialloc/Cargo.toml",
            )
            self.assertEqual(
                attestation["record"]["expected_features"], expected_features
            )
            self.assertEqual(
                attestation["record"]["resolved_features"], expected_features
            )
            self.assertFalse(
                attestation["record"]["default_features_expected"]
            )
            self.assertFalse(attestation["record"]["default_feature_resolved"])
            self.assertEqual(attestation["record"]["package_count"], 1)
            self.assertEqual(attestation["record"]["resolve_node_count"], 1)
            provenance = rendered["direct_allocator_provenance"]
            self.assertTrue(provenance["required"])
            self.assertTrue(provenance["valid"])
            self.assertEqual(
                rendered["unialloc_implementation_sha256"], "d" * 64
            )
            self.assertEqual(
                rendered["subject_cargo_features"]["allocator_variant"],
                allocator,
            )
            self.assertEqual(provenance["failure_counts"], {})

    def test_wrong_cargo_metadata_provenance_suppresses_allocator_signal(self) -> None:
        vulnerable = arm(
            "vulnerable",
            "reclaim_checks",
            [observation(native="unialloc_pointer_already_released_check")],
        )
        attestation = vulnerable["allocator_provenance"][
            "cargo_metadata_attestation"
        ]
        attestation["manifest_path"] = "/tmp/evil/unialloc/Cargo.toml"
        retoken_attestation(attestation)
        experiment = self.write_experiment("wrong-provenance.json", [vulnerable])

        result = summary.summarize(self.catalog, [f"RSH-041={experiment}"])
        rendered = self.result_arm(
            self.case(result, "RSH-041"), "vulnerable", "reclaim_checks"
        )
        cargo = rendered["cargo_metadata_attestation"]
        self.assertFalse(cargo["valid"])
        self.assertIsNone(cargo["token"])
        self.assertEqual(cargo["observation_count"], 0)
        self.assertEqual(
            cargo["failure_counts"],
            {"cargo metadata manifest does not bind repository UniAlloc": 1},
        )
        self.assertEqual(rendered["allocator_signal_count"], 0)
        self.assertEqual(rendered["allocator_signal_signatures"], {})
        self.assertEqual(
            rendered["source_revalidated_allocator_signal_signatures"],
            {"unialloc_pointer_already_released_check": 1},
        )
        self.assertIn(
            "cargo_metadata_attestation_invalid",
            rendered["eligibility_blockers"],
        )

    def test_mixed_cargo_metadata_provenance_suppresses_merged_signal(self) -> None:
        first_arm = arm(
            "vulnerable",
            "reclaim_checks",
            [observation(native="unialloc_pointer_already_released_check")],
        )
        second_arm = arm(
            "vulnerable",
            "reclaim_checks",
            [observation(native="unialloc_pointer_already_released_check")],
        )
        second_attestation = second_arm["allocator_provenance"][
            "cargo_metadata_attestation"
        ]
        second_attestation["package_id"] = (
            f"path+{(ROOT / 'unialloc').resolve().as_uri()}#9.9.9"
        )
        retoken_attestation(second_attestation)
        first = self.write_experiment("provenance-one.json", [first_arm])
        second = self.write_experiment("provenance-two.json", [second_arm])

        result = summary.summarize(
            self.catalog,
            [f"RSH-041={first}", f"RSH-041={second}"],
        )
        rendered = self.result_arm(
            self.case(result, "RSH-041"), "vulnerable", "reclaim_checks"
        )
        cargo = rendered["cargo_metadata_attestation"]
        self.assertFalse(cargo["valid"])
        self.assertIsNone(cargo["token"])
        self.assertEqual(cargo["observation_count"], 2)
        self.assertEqual(
            cargo["failure_counts"],
            {"mixed cargo feature contract tokens": 1},
        )
        self.assertEqual(len(cargo["observed_feature_contract_tokens"]), 2)
        self.assertEqual(len(cargo["observed_records"]), 2)
        self.assertEqual(rendered["allocator_signal_count"], 0)
        self.assertEqual(
            rendered["source_revalidated_allocator_signal_signatures"],
            {"unialloc_pointer_already_released_check": 2},
        )

    def test_direct_provenance_digest_must_match_fingerprint(self) -> None:
        vulnerable = arm(
            "vulnerable",
            "reclaim_checks",
            [observation(native="unialloc_pointer_already_released_check")],
        )
        vulnerable["allocator_provenance"][
            "unialloc_implementation_sha256"
        ] = "e" * 64
        experiment = self.write_experiment(
            "implementation-fingerprint-mismatch.json", [vulnerable]
        )

        result = summary.summarize(self.catalog, [f"RSH-041={experiment}"])
        rendered = self.result_arm(
            self.case(result, "RSH-041"), "vulnerable", "reclaim_checks"
        )
        provenance = rendered["direct_allocator_provenance"]
        self.assertFalse(provenance["valid"])
        self.assertIsNone(rendered["unialloc_implementation_sha256"])
        self.assertEqual(
            provenance["failure_counts"],
            {"UniAlloc implementation sha256 differs from fingerprint": 1},
        )
        self.assertEqual(rendered["allocator_signal_count"], 0)

    def test_missing_direct_provenance_fails_closed(self) -> None:
        vulnerable = arm(
            "vulnerable",
            "reclaim_checks",
            [observation(native="unialloc_pointer_already_released_check")],
        )
        del vulnerable["allocator_provenance"]
        experiment = self.write_experiment(
            "missing-direct-provenance.json", [vulnerable]
        )

        result = summary.summarize(self.catalog, [f"RSH-041={experiment}"])
        rendered = self.result_arm(
            self.case(result, "RSH-041"), "vulnerable", "reclaim_checks"
        )
        provenance = rendered["direct_allocator_provenance"]
        self.assertFalse(provenance["valid"])
        self.assertEqual(
            provenance["failure_counts"],
            {"missing direct allocator provenance": 1},
        )
        self.assertIn(
            "direct_allocator_provenance_invalid",
            rendered["eligibility_blockers"],
        )
        self.assertEqual(rendered["allocator_signal_count"], 0)

    def test_mixed_implementation_digests_fail_closed(self) -> None:
        first = self.write_experiment(
            "implementation-one.json",
            [
                arm(
                    "vulnerable",
                    "reclaim_checks",
                    [
                        observation(
                            native="unialloc_pointer_already_released_check"
                        )
                    ],
                    implementation_sha256="d" * 64,
                )
            ],
        )
        second = self.write_experiment(
            "implementation-two.json",
            [
                arm(
                    "vulnerable",
                    "reclaim_checks",
                    [
                        observation(
                            native="unialloc_pointer_already_released_check"
                        )
                    ],
                    implementation_sha256="e" * 64,
                )
            ],
        )

        result = summary.summarize(
            self.catalog,
            [f"RSH-041={first}", f"RSH-041={second}"],
        )
        rendered = self.result_arm(
            self.case(result, "RSH-041"), "vulnerable", "reclaim_checks"
        )
        provenance = rendered["direct_allocator_provenance"]
        self.assertFalse(provenance["valid"])
        self.assertEqual(
            provenance["failure_counts"],
            {"mixed UniAlloc implementation sha256 values": 1},
        )
        self.assertEqual(len(provenance["observed_unialloc_implementation_sha256"]), 2)
        self.assertEqual(rendered["allocator_signal_count"], 0)

    def test_subject_cargo_features_must_match_fingerprint(self) -> None:
        vulnerable = arm(
            "vulnerable",
            "reclaim_plain",
            [observation(clean=True)],
        )
        vulnerable["allocator_provenance"]["subject_cargo_features"][
            "effective_features"
        ] = ["tampered"]
        experiment = self.write_experiment(
            "subject-feature-fingerprint-mismatch.json", [vulnerable]
        )

        result = summary.summarize(self.catalog, [f"RSH-041={experiment}"])
        rendered = self.result_arm(
            self.case(result, "RSH-041"), "vulnerable", "reclaim_plain"
        )
        provenance = rendered["direct_allocator_provenance"]
        self.assertFalse(provenance["valid"])
        self.assertIsNone(rendered["subject_cargo_features"])
        self.assertEqual(
            provenance["failure_counts"],
            {"subject Cargo features differ from fingerprint": 1},
        )

    def test_package_id_must_bind_repository_unialloc(self) -> None:
        vulnerable = arm(
            "vulnerable",
            "reclaim_checks",
            [observation(native="unialloc_pointer_already_released_check")],
        )
        attestation = vulnerable["allocator_provenance"][
            "cargo_metadata_attestation"
        ]
        attestation["package_id"] = "registry+https://example.invalid#unialloc@0.1.0"
        retoken_attestation(attestation)
        experiment = self.write_experiment("registry-package.json", [vulnerable])

        result = summary.summarize(self.catalog, [f"RSH-041={experiment}"])
        rendered = self.result_arm(
            self.case(result, "RSH-041"), "vulnerable", "reclaim_checks"
        )
        cargo = rendered["cargo_metadata_attestation"]
        self.assertFalse(cargo["valid"])
        self.assertEqual(
            cargo["failure_counts"],
            {"cargo metadata package id does not bind repository UniAlloc": 1},
        )

    def test_reuse_denial_is_typeiso_specific_only_with_completed_ablation(self) -> None:
        experiment = self.write_experiment(
            "denial.json",
            [
                arm("vulnerable", "typed_plain", [observation(clean=True)]),
                arm(
                    "vulnerable",
                    "typeiso",
                    [observation(clean=False)],
                    denial_count=3,
                ),
            ],
        )

        result = summary.summarize(self.catalog, [f"RSH-041={experiment}"])
        case = self.case(result, "RSH-041")
        typeiso = self.result_arm(case, "vulnerable", "typeiso")
        self.assertEqual(typeiso["classification"], "typeiso_specific_signal")
        self.assertEqual(
            typeiso["native_diagnostic_signatures"],
            {"cross_identity_reuse_denial": 3},
        )

    def test_runtime_stats_wrong_identity_denials_are_allocator_signals(self) -> None:
        experiment = self.write_experiment(
            "stats-denial.json",
            [
                arm("vulnerable", "typed_plain", [observation(clean=True)]),
                arm(
                    "vulnerable",
                    "typeiso",
                    [observation(clean=True), observation(clean=True)],
                    stats=[
                        {
                            "typed_allocations": 2,
                            "fallback_allocations": 0,
                            "typed_cache_wrong_identity_denials": 1,
                        },
                        {
                            "typed_allocations": 3,
                            "fallback_allocations": 0,
                            "typed_cache_wrong_identity_denials": 1,
                        },
                    ],
                ),
            ],
        )

        result = summary.summarize(self.catalog, [f"RSH-041={experiment}"])
        case = self.case(result, "RSH-041")
        typeiso = self.result_arm(case, "vulnerable", "typeiso")
        self.assertEqual(typeiso["classification"], "typeiso_specific_signal")
        self.assertEqual(
            typeiso["native_diagnostic_signatures"],
            {"typed_cache_wrong_identity_denial": 2},
        )

    def test_clean_typed_exit_without_reproduced_baseline_is_insufficient(self) -> None:
        experiment = self.write_experiment(
            "clean-only.json",
            [arm("vulnerable", "typeiso", [observation(clean=True)])],
        )

        result = summary.summarize(self.catalog, [f"RSH-041={experiment}"])
        case = self.case(result, "RSH-041")
        typeiso = self.result_arm(case, "vulnerable", "typeiso")
        self.assertEqual(typeiso["classification"], "insufficient_evidence")
        self.assertFalse(result["mitigation_inferred"])
        self.assertIn("clean exits never establish mitigation", result["boundary"])

    def test_multiple_scenarios_for_one_case_are_not_rendered_as_unique(self) -> None:
        catalog = json.loads(self.catalog.read_text(encoding="utf-8"))
        catalog["cases"][0]["scenarios"].append(
            {"scenario_id": "RSH-041-alternative"}
        )
        self.catalog.write_text(json.dumps(catalog), encoding="utf-8")
        first = self.write_experiment(
            "first-scenario.json",
            [arm("vulnerable", "system", [observation(tool="asan_double_free")])],
        )
        second = self.write_experiment(
            "second-scenario.json",
            [arm("vulnerable", "system", [observation(tool="asan_double_free")])],
            scenario="RSH-041-alternative",
        )

        result = summary.summarize(
            self.catalog,
            [f"RSH-041={first}", f"RSH-041={second}"],
        )
        case = self.case(result, "RSH-041")
        rendered = self.result_arm(case, "vulnerable", "system")
        expected = ["RSH-041-alternative", "RSH-041-upstream"]
        self.assertIsNone(case["scenario_id"])
        self.assertEqual(case["scenario_ids"], expected)
        self.assertIsNone(rendered["scenario_id"])
        self.assertEqual(rendered["scenario_ids"], expected)

    def test_rejects_duplicate_experiment_path(self) -> None:
        experiment = self.write_experiment(
            "duplicate.json",
            [arm("vulnerable", "system", [observation(tool="asan_double_free")])],
        )

        with self.assertRaisesRegex(summary.SummaryError, "duplicate experiment path"):
            summary.summarize(
                self.catalog,
                [f"RSH-041={experiment}", f"RSH-041={experiment}"],
            )

    def test_selects_allocator_arms_from_separate_campaign_artifacts(self) -> None:
        old = self.write_experiment(
            "old-full-campaign.json",
            [
                arm(
                    "vulnerable",
                    "system",
                    [observation(tool="asan_double_free")],
                ),
                arm("patched", "system", [observation(clean=True)]),
                arm(
                    "vulnerable",
                    "reclaim_checks",
                    [
                        observation(
                            native="unialloc_pointer_already_released_check"
                        )
                    ],
                ),
            ],
        )
        direct = self.write_experiment(
            "new-direct-campaign.json",
            [
                arm("vulnerable", "system", [observation(clean=True)]),
                arm("vulnerable", "reclaim_plain", [observation(clean=True)]),
                arm(
                    "vulnerable",
                    "reclaim_checks",
                    [
                        observation(
                            native="unialloc_pointer_already_released_check"
                        )
                    ],
                ),
            ],
        )

        result = summary.summarize(
            self.catalog,
            [],
            experiment_allocator_specs=[
                f"RSH-041:system={old}",
                f"RSH-041:reclaim_plain,reclaim_checks={direct}",
            ],
        )
        case = self.case(result, "RSH-041")
        self.assertEqual(case["experiment_count"], 2)
        self.assertEqual(
            [
                (value["archive_variant"], value["allocator_variant"])
                for value in case["arms"]
            ],
            [
                ("vulnerable", "system"),
                ("vulnerable", "reclaim_plain"),
                ("vulnerable", "reclaim_checks"),
                ("patched", "system"),
            ],
        )
        self.assertEqual(
            [
                {
                    "allocator_selection": record["allocator_selection"],
                    "requested_allocators": record["requested_allocators"],
                    "selected_allocators": record["selected_allocators"],
                }
                for record in result["experiment_inputs"]
            ],
            [
                {
                    "allocator_selection": "explicit",
                    "requested_allocators": ["system"],
                    "selected_allocators": ["system"],
                },
                {
                    "allocator_selection": "explicit",
                    "requested_allocators": [
                        "reclaim_plain",
                        "reclaim_checks",
                    ],
                    "selected_allocators": [
                        "reclaim_plain",
                        "reclaim_checks",
                    ],
                },
            ],
        )
        reclaim = self.result_arm(case, "vulnerable", "reclaim_checks")
        self.assertEqual(reclaim["experiment_count"], 1)
        self.assertEqual(reclaim["allocator_signal_count"], 1)

    def test_rejects_invalid_allocator_selections(self) -> None:
        path = self.root / "unused.json"
        for value, pattern in (
            (f"RSH-041:={path}", "invalid --experiment-allocator value"),
            (f"RSH-041:bogus={path}", "unknown allocator selection"),
            (
                f"RSH-041:system,system={path}",
                "duplicate allocator in experiment selection",
            ),
        ):
            with self.subTest(value=value):
                with self.assertRaisesRegex(summary.SummaryError, pattern):
                    summary.parse_experiment_allocator_spec(value)

    def test_rejects_selected_allocator_absent_from_artifact(self) -> None:
        experiment = self.write_experiment(
            "selection-absent.json",
            [arm("vulnerable", "system", [observation(clean=True)])],
        )

        with self.assertRaisesRegex(summary.SummaryError, "selected allocator absent"):
            summary.summarize(
                self.catalog,
                [],
                experiment_allocator_specs=[
                    f"RSH-041:reclaim_plain={experiment}"
                ],
            )

    def test_rejects_duplicate_path_with_different_allocator_selections(self) -> None:
        experiment = self.write_experiment(
            "duplicate-selection.json",
            [
                arm("vulnerable", "system", [observation(clean=True)]),
                arm("vulnerable", "reclaim_plain", [observation(clean=True)]),
            ],
        )

        with self.assertRaisesRegex(summary.SummaryError, "duplicate experiment path"):
            summary.summarize(
                self.catalog,
                [],
                experiment_allocator_specs=[
                    f"RSH-041:system={experiment}",
                    f"RSH-041:reclaim_plain={experiment}",
                ],
            )

    def test_allocator_selection_cannot_bypass_case_or_scenario_binding(self) -> None:
        wrong_case = self.write_experiment(
            "selection-wrong-case.json",
            [
                arm("vulnerable", "system", [observation(clean=True)]),
                arm(
                    "vulnerable",
                    "reclaim_plain",
                    [observation(clean=True)],
                    case_id="RSH-042",
                ),
            ],
        )
        wrong_scenario_arm = arm(
            "vulnerable", "reclaim_plain", [observation(clean=True)]
        )
        wrong_scenario_arm["scenario_id"] = "RSH-042-upstream"
        wrong_scenario = self.write_experiment(
            "selection-wrong-scenario.json",
            [
                arm("vulnerable", "system", [observation(clean=True)]),
                wrong_scenario_arm,
            ],
        )

        for path, pattern in (
            (wrong_case, "does not match assignment"),
            (wrong_scenario, "does not match experiment"),
        ):
            with self.subTest(path=path):
                with self.assertRaisesRegex(summary.SummaryError, pattern):
                    summary.summarize(
                        self.catalog,
                        [],
                        experiment_allocator_specs=[f"RSH-041:system={path}"],
                    )

    def test_rejects_case_assignment_mismatch(self) -> None:
        experiment = self.write_experiment(
            "mismatch.json",
            [
                arm(
                    "vulnerable",
                    "system",
                    [observation(tool="asan_double_free")],
                    case_id="RSH-042",
                )
            ],
        )

        with self.assertRaisesRegex(summary.SummaryError, "does not match assignment"):
            summary.summarize(self.catalog, [f"RSH-041={experiment}"])

    def test_cli_writes_same_json_it_prints(self) -> None:
        experiment = self.write_experiment(
            "cli.json",
            [arm("vulnerable", "system", [observation(tool="asan_double_free")])],
        )
        output = self.root / "summary.json"
        stdout = io.StringIO()

        with contextlib.redirect_stdout(stdout):
            status = summary.main(
                [
                    "--catalog",
                    str(self.catalog),
                    "--experiment",
                    f"RSH-041={experiment}",
                    "--output",
                    str(output),
                ]
            )

        self.assertEqual(status, 0)
        self.assertEqual(json.loads(stdout.getvalue()), json.loads(output.read_text()))


if __name__ == "__main__":
    unittest.main()
