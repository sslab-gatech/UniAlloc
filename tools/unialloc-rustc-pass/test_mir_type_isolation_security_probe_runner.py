#!/usr/bin/env python3
"""Focused unit tests for the MIR type-isolation security probe runner."""

from __future__ import annotations

import copy
import importlib.util
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock


ROOT = Path(__file__).resolve().parents[2]
RUNNER = ROOT / "tools/unialloc-rustc-pass/test_mir_type_isolation_security_probe.py"
SPEC = importlib.util.spec_from_file_location("mir_typeiso_security_runner", RUNNER)
assert SPEC is not None and SPEC.loader is not None
runner = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = runner
SPEC.loader.exec_module(runner)


def ready_clone_classification_audit() -> dict:
    rows = [
        {
            "mir_function": "fixture::clone_single_heap",
            "lowering_kind": "semantic_scope_enter_exit_rewrite",
            "rewrite_status": "semantic_scope_enter_exit_rewrite_planned",
            "semantic_object_type": "std::vec::Vec<u8>",
            "destination_type": "std::option::Option<std::vec::Vec<u8>>",
        }
    ]
    for function_name, destination_type in (
        ("clone_nonheap_token", "fixture::NonHeapToken"),
        ("clone_nonheap_option", "std::option::Option<u8>"),
        ("clone_nonheap_result", "std::result::Result<u16, u32>"),
        ("clone_nonowning_reference", "&std::vec::Vec<u8>"),
    ):
        rows.append(
            {
                "mir_function": f"fixture::{function_name}",
                "lowering_kind": "semantic_scope_non_heap_object_skipped",
                "rewrite_status": "semantic_scope_rewrite_skipped_non_heap_object_type",
                "destination_type": destination_type,
            }
        )
    rows.append(
        {
            "mir_function": "fixture::clone_ambiguous_result",
            "lowering_kind": "semantic_scope_unsolved_heap_object_candidate",
            "rewrite_status": "semantic_scope_rewrite_skipped_ambiguous_heap_object_type",
            "replacement_resolution_status": "rustc_middle_multiple_heap_object_types_not_lowered",
            "semantic_object_type": (
                "multiple_heap_owners(std::string::String,std::vec::Vec<u8>)"
            ),
            "destination_type": (
                "std::result::Result<std::vec::Vec<u8>, std::string::String>"
            ),
        }
    )
    for function_name, destination_type in (
        ("clone_arc_vec_owner", "fixture::ArcVecOwner"),
        ("clone_rc_vec_owner", "fixture::RcVecOwner"),
    ):
        rows.append(
            {
                "mir_function": f"fixture::{function_name}",
                "lowering_kind": "semantic_scope_enter_exit_rewrite",
                "rewrite_status": "semantic_scope_enter_exit_rewrite_planned",
                "semantic_object_type": "std::vec::Vec<u8>",
                "destination_type": destination_type,
            }
        )
    for function_name, destination_type in (
        (
            "clone_standalone_arc",
            "std::sync::Arc<fixture::NonHeapToken>",
        ),
        (
            "clone_standalone_rc",
            "std::rc::Rc<fixture::NonHeapToken>",
        ),
    ):
        rows.append(
            {
                "mir_function": f"fixture::{function_name}",
                "lowering_kind": "semantic_scope_non_heap_object_skipped",
                "rewrite_status": "semantic_scope_rewrite_skipped_non_heap_object_type",
                "replacement_resolution_status": (
                    "rustc_middle_no_supported_heap_owner_not_lowered"
                ),
                "metadata_pairing_contract": "audit_only_no_supported_heap_owner",
                "destination_type": destination_type,
            }
        )
    rows.append(
        {
            "mir_function": "fixture::clone_multi_owner_headers",
            "lowering_kind": "semantic_scope_unsolved_heap_object_candidate",
            "rewrite_status": "semantic_scope_rewrite_skipped_ambiguous_heap_object_type",
            "replacement_resolution_status": (
                "rustc_middle_multiple_heap_object_types_not_lowered"
            ),
            "semantic_object_type": (
                "multiple_heap_owners(std::string::String,std::vec::Vec<u8>)"
            ),
            "destination_type": "fixture::Headers",
        }
    )
    rows.append(
        {
            "mir_function": "fixture::clone_nested_vec_owner",
            "lowering_kind": "semantic_scope_enter_exit_rewrite",
            "rewrite_status": "semantic_scope_enter_exit_rewrite_planned",
            "semantic_object_type": "std::vec::Vec<u8>",
            "destination_type": "fixture::NestedVecOwner",
        }
    )
    rows.append(
        {
            "mir_function": "fixture::clone_raw_pointer_wrapper",
            "lowering_kind": "semantic_scope_unsolved_heap_object_candidate",
            "rewrite_status": "semantic_scope_rewrite_skipped_unresolved_heap_object_type",
            "replacement_resolution_status": "rustc_middle_heap_object_type_not_solved",
            "semantic_object_type": "<unknown-heap-object-type>",
            "destination_type": "fixture::RawPointerWrapper",
        }
    )
    rows.append(
        {
            "mir_function": "fixture::clone_const_generic",
            "lowering_kind": "semantic_scope_unsolved_heap_object_candidate",
            "rewrite_status": "semantic_scope_rewrite_skipped_unresolved_heap_object_type",
            "replacement_resolution_status": "rustc_middle_heap_object_type_not_solved",
            "semantic_object_type": "<unknown-heap-object-type>",
            "destination_type": "fixture::ConstGenericClone<N>",
        }
    )
    for function_name, destination_type in (
        (
            "drop_multi_owner_struct",
            "fixture::MultiOwnerStruct<std::vec::Vec<u8>, std::string::String>",
        ),
        (
            "drop_multi_owner_enum",
            "fixture::MultiOwnerEnum<std::vec::Vec<u8>, std::string::String>",
        ),
    ):
        rows.append(
            {
                "mir_function": f"fixture::{function_name}",
                "lowering_kind": "semantic_scope_drop_multiple_heap_owners_skipped",
                "rewrite_status": "semantic_scope_drop_rewrite_skipped_multiple_heap_owners",
                "replacement_resolution_status": (
                    "rustc_middle_drop_multiple_heap_owners_not_lowered"
                ),
                "metadata_pairing_contract": "audit_only_multiple_heap_owner_drop_type",
                "semantic_object_type": (
                    "multiple_heap_owners(std::string::String,std::vec::Vec<u8>)"
                ),
                "destination_type": destination_type,
            }
        )
    return {
        "summary": {"semantic_scope_unsolved_candidate_count": 4},
        "rewrite_candidates": rows,
    }


def ready_actual_refcounted_clone_audit() -> dict:
    rows = []
    for function_name, destination_type in (
        ("clone_arc_vec_owner", "fixture::ArcVecCloneOwner"),
        ("clone_rc_vec_owner", "fixture::RcVecCloneOwner"),
    ):
        rows.append(
            {
                "mir_function": f"fixture::{function_name}",
                "lowering_kind": "semantic_scope_enter_exit_rewrite",
                "rewrite_status": "actual_semantic_scope_enter_exit_rewrite_applied",
                "replacement_resolution_status": (
                    "resolved_unialloc_semantic_scope_push_local_pop"
                ),
                "semantic_object_type": "std::vec::Vec<u8>",
                "destination_type": destination_type,
            }
        )
    for function_name, destination_type in (
        (
            "clone_standalone_arc",
            "std::sync::Arc<fixture::RefCountedPayload>",
        ),
        (
            "clone_standalone_rc",
            "std::rc::Rc<fixture::RefCountedPayload>",
        ),
    ):
        rows.append(
            {
                "mir_function": f"fixture::{function_name}",
                "lowering_kind": "semantic_scope_non_heap_object_skipped",
                "rewrite_status": "semantic_scope_rewrite_skipped_non_heap_object_type",
                "replacement_resolution_status": (
                    "rustc_middle_no_supported_heap_owner_not_lowered"
                ),
                "metadata_pairing_contract": "audit_only_no_supported_heap_owner",
                "destination_type": destination_type,
            }
        )
    return {"rewrite_candidates": rows}


def ready_generic_drop_audit() -> dict:
    return {
        "rewrite_candidates": [
            {
                "mir_function": (
                    "rustc_driver_mir_type_isolation_security_probe::generic_drop"
                ),
                "lowering_kind": (
                    "semantic_scope_drop_generic_type_parameter_skipped"
                ),
                "rewrite_status": (
                    "semantic_scope_drop_rewrite_skipped_generic_type_parameter"
                ),
                "replacement_resolution_status": (
                    "rustc_middle_drop_generic_type_parameter_not_lowered"
                ),
                "metadata_pairing_contract": "audit_only_generic_drop_type",
                "semantic_object_type": "<unknown-heap-object-type>",
                "destination_type": "T",
            }
        ]
    }


def ready_generic_drop_runtime() -> dict:
    return {
        "generic_drop_typed_deallocations_delta": 4,
        "generic_drop_fallback_deallocations_delta": 0,
        "generic_drop_typed_cache_inserts_delta": 4,
    }


def preserved_raw_generic_drop_omission_audit() -> dict:
    """Minimized from the preserved 2e3c0e2 actual-rustc target audit."""
    drop_callsites = (
        13708296833535806698,
        2539143866788732867,
        17702369971882354328,
        10831918827053851400,
    )
    return {
        "summary": {
            "provider_override_installed": True,
            "body_clone_returned_to_rustc": True,
            "semantic_scope_drop_candidate_count": 4,
            "semantic_scope_drop_rewrite_applied_count": 4,
            "semantic_scope_drop_generic_type_parameter_skipped_count": 0,
        },
        "rewrite_candidates": [
            {
                "mir_function": "producer_box",
                "lowering_kind": "semantic_scope_enter_exit_rewrite",
                "rewrite_status": "semantic_scope_enter_exit_rewrite_applied",
                "semantic_object_type": "std::boxed::Box<ProducerPayload>",
            },
            *[
                {
                    "mir_function": "main",
                    "lowering_kind": "semantic_scope_drop_rewrite",
                    "rewrite_status": "actual_semantic_scope_drop_rewrite_applied",
                    "semantic_object_type": "std::string::String",
                    "destination_type": "std::string::String",
                    "callsite": callsite,
                }
                for callsite in drop_callsites
            ],
            {
                "mir_function": "main::{closure#0}",
                "lowering_kind": "semantic_scope_drop_non_heap_object_skipped",
                "rewrite_status": "semantic_scope_drop_rewrite_skipped_non_heap_object_type",
                "destination_type": "std::array::IntoIter<usize, 4_usize>",
            },
        ],
    }


class MirTypeIsolationSecurityRunnerTests(unittest.TestCase):
    def test_generic_drop_recovery_accepts_preserved_raw_provider_omission(self) -> None:
        evidence = runner.validate_generic_drop_recovery_requirement(
            preserved_raw_generic_drop_omission_audit(), ready_generic_drop_runtime()
        )
        self.assertTrue(evidence["generic_drop_recovery_validated"])
        self.assertEqual(evidence["generic_drop_audit_only_skip_count"], 0)
        self.assertEqual(
            evidence["generic_drop_provider_exposure"],
            "not_exposed_by_optimized_mir_provider",
        )

    def test_generic_drop_recovery_rejects_unbound_synthetic_omission(self) -> None:
        with self.assertRaisesRegex(AssertionError, "provider-omission evidence"):
            runner.validate_generic_drop_recovery_requirement(
                {"rewrite_candidates": []}, ready_generic_drop_runtime()
            )

    def test_generic_drop_recovery_rejects_summary_only_drop_evidence(self) -> None:
        audit = preserved_raw_generic_drop_omission_audit()
        audit["rewrite_candidates"] = [
            row
            for row in audit["rewrite_candidates"]
            if row.get("lowering_kind") != "semantic_scope_drop_rewrite"
        ]
        with self.assertRaisesRegex(AssertionError, "row-level actual Drop evidence"):
            runner.validate_generic_drop_recovery_requirement(
                audit, ready_generic_drop_runtime()
            )

    def test_generic_drop_recovery_accepts_exact_audit_and_runtime_delta(self) -> None:
        evidence = runner.validate_generic_drop_recovery_requirement(
            ready_generic_drop_audit(), ready_generic_drop_runtime()
        )
        self.assertTrue(evidence["generic_drop_recovery_validated"])
        self.assertEqual(evidence["generic_drop_audit_only_skip_count"], 1)

    def test_generic_drop_recovery_accepts_bare_function_name(self) -> None:
        bare_name = ready_generic_drop_audit()
        bare_name["rewrite_candidates"][0]["mir_function"] = "generic_drop"
        bare_evidence = runner.validate_generic_drop_recovery_requirement(
            bare_name, ready_generic_drop_runtime()
        )
        self.assertTrue(bare_evidence["generic_drop_recovery_validated"])

    def test_generic_drop_recovery_rejects_duplicate_or_applied_scope(self) -> None:
        duplicate = ready_generic_drop_audit()
        duplicate["rewrite_candidates"].append(
            copy.deepcopy(duplicate["rewrite_candidates"][0])
        )
        with self.assertRaisesRegex(AssertionError, "exactly one"):
            runner.validate_generic_drop_recovery_requirement(
                duplicate, ready_generic_drop_runtime()
            )

        applied = ready_generic_drop_audit()
        applied["rewrite_candidates"].append(
            {
                "mir_function": (
                    "rustc_driver_mir_type_isolation_security_probe::generic_drop"
                ),
                "lowering_kind": "semantic_scope_enter_exit_rewrite",
                "rewrite_status": "semantic_scope_enter_exit_rewrite_applied",
                "metadata_pairing_contract": "scope_enter_original_call_scope_exit",
            }
        )
        with self.assertRaisesRegex(AssertionError, "applied/planned"):
            runner.validate_generic_drop_recovery_requirement(
                applied, ready_generic_drop_runtime()
            )

    def test_generic_drop_recovery_rejects_contract_and_runtime_delta_drift(self) -> None:
        wrong_contract = ready_generic_drop_audit()
        wrong_contract["rewrite_candidates"][0]["metadata_pairing_contract"] = (
            "unexpected_contract"
        )
        with self.assertRaisesRegex(AssertionError, "audit-only pairing contract"):
            runner.validate_generic_drop_recovery_requirement(
                wrong_contract, ready_generic_drop_runtime()
            )

        for field, value in (
            ("generic_drop_typed_deallocations_delta", 3),
            ("generic_drop_fallback_deallocations_delta", 1),
            ("generic_drop_typed_cache_inserts_delta", 3),
        ):
            with self.subTest(field=field):
                runtime = ready_generic_drop_runtime()
                runtime[field] = value
                with self.assertRaisesRegex(AssertionError, "generic Drop"):
                    runner.validate_generic_drop_recovery_requirement(
                        ready_generic_drop_audit(), runtime
                    )

    def test_clone_candidate_classification_accepts_expected_partition(self) -> None:
        evidence = runner.validate_clone_candidate_classification(
            ready_clone_classification_audit()
        )
        self.assertEqual(evidence["single_heap_scope_rows"], 1)
        self.assertEqual(evidence["nested_heap_scope_rows"], 1)
        self.assertEqual(evidence["non_heap_skipped_count"], 6)
        self.assertEqual(evidence["ambiguous_unsolved_count"], 1)
        self.assertEqual(evidence["headers_multi_owner_unsolved_count"], 1)
        self.assertEqual(evidence["raw_pointer_unresolved_count"], 1)
        self.assertEqual(evidence["const_generic_unresolved_count"], 1)
        self.assertEqual(evidence["total_unsolved_count"], 4)
        self.assertEqual(
            evidence["refcounted_single_owner_rows_by_function"],
            {"clone_arc_vec_owner": 1, "clone_rc_vec_owner": 1},
        )
        self.assertEqual(
            evidence["refcounted_no_owner_rows_by_function"],
            {"clone_standalone_arc": 1, "clone_standalone_rc": 1},
        )
        self.assertEqual(
            evidence["multi_owner_drop_rows_by_function"],
            {"drop_multi_owner_struct": 1, "drop_multi_owner_enum": 1},
        )

    def test_actual_refcounted_clone_classification_accepts_exact_partition(self) -> None:
        evidence = runner.validate_actual_refcounted_clone_classification(
            ready_actual_refcounted_clone_audit()
        )
        self.assertEqual(
            evidence["actual_single_owner_rows_by_function"],
            {"clone_arc_vec_owner": 1, "clone_rc_vec_owner": 1},
        )
        self.assertEqual(
            evidence["actual_no_owner_rows_by_function"],
            {"clone_standalone_arc": 1, "clone_standalone_rc": 1},
        )

    def test_actual_refcounted_clone_classification_rejects_forged_rows(self) -> None:
        mutations = (
            (
                "clone_arc_vec_owner",
                "rewrite_status",
                "semantic_scope_enter_exit_rewrite_planned",
                "did not apply",
            ),
            (
                "clone_rc_vec_owner",
                "semantic_object_type",
                "std::rc::Rc<fixture::RefCountedPayload>",
                "sole Vec owner",
            ),
            (
                "clone_standalone_arc",
                "lowering_kind",
                "semantic_scope_enter_exit_rewrite",
                "audit-only no-owner",
            ),
        )
        for function_name, field, value, message in mutations:
            with self.subTest(function_name=function_name, field=field):
                audit = ready_actual_refcounted_clone_audit()
                row = next(
                    row
                    for row in audit["rewrite_candidates"]
                    if row["mir_function"].endswith(f"::{function_name}")
                )
                row[field] = value
                with self.assertRaisesRegex(AssertionError, message):
                    runner.validate_actual_refcounted_clone_classification(audit)

    def test_clone_candidate_classification_rejects_multi_owner_drop_as_applied(self) -> None:
        audit = ready_clone_classification_audit()
        row = next(
            row
            for row in audit["rewrite_candidates"]
            if row["mir_function"].endswith("::drop_multi_owner_struct")
        )
        row["lowering_kind"] = "semantic_scope_drop_rewrite"
        row["rewrite_status"] = "actual_semantic_scope_drop_rewrite_applied"
        row["replacement_resolution_status"] = "resolved_unialloc_semantic_scope_push_local_pop"
        with self.assertRaisesRegex(AssertionError, "fail closed"):
            runner.validate_clone_candidate_classification(audit)

    def test_clone_candidate_classification_rejects_nonheap_as_unsolved(self) -> None:
        audit = ready_clone_classification_audit()
        row = next(
            row
            for row in audit["rewrite_candidates"]
            if row["mir_function"].endswith("::clone_nonheap_option")
        )
        row["lowering_kind"] = "semantic_scope_unsolved_heap_object_candidate"
        row["rewrite_status"] = "semantic_scope_rewrite_skipped_unresolved_heap_object_type"
        audit["summary"]["semantic_scope_unsolved_candidate_count"] = 5
        with self.assertRaisesRegex(AssertionError, "non-heap skip"):
            runner.validate_clone_candidate_classification(audit)

    def test_clone_candidate_classification_rejects_ambiguous_as_applied(self) -> None:
        audit = ready_clone_classification_audit()
        row = next(
            row
            for row in audit["rewrite_candidates"]
            if row["mir_function"].endswith("::clone_ambiguous_result")
        )
        row["lowering_kind"] = "semantic_scope_enter_exit_rewrite"
        row["rewrite_status"] = "semantic_scope_enter_exit_rewrite_planned"
        row["replacement_resolution_status"] = "not_requested_dry_run"
        audit["summary"]["semantic_scope_unsolved_candidate_count"] = 3
        with self.assertRaisesRegex(AssertionError, "fail-closed"):
            runner.validate_clone_candidate_classification(audit)

    def test_clone_candidate_classification_requires_single_owner_positive(self) -> None:
        audit = copy.deepcopy(ready_clone_classification_audit())
        audit["rewrite_candidates"] = [
            row
            for row in audit["rewrite_candidates"]
            if not row["mir_function"].endswith("::clone_single_heap")
        ]
        with self.assertRaisesRegex(AssertionError, "clone_single_heap"):
            runner.validate_clone_candidate_classification(audit)

    def test_clone_candidate_classification_rejects_raw_pointer_as_nonheap(self) -> None:
        audit = ready_clone_classification_audit()
        row = next(
            row
            for row in audit["rewrite_candidates"]
            if row["mir_function"].endswith("::clone_raw_pointer_wrapper")
        )
        row["lowering_kind"] = "semantic_scope_non_heap_object_skipped"
        row["rewrite_status"] = "semantic_scope_rewrite_skipped_non_heap_object_type"
        audit["summary"]["semantic_scope_unsolved_candidate_count"] = 3
        with self.assertRaisesRegex(AssertionError, "fail-closed"):
            runner.validate_clone_candidate_classification(audit)

    def test_clone_candidate_classification_rejects_const_generic_as_nonheap(self) -> None:
        audit = ready_clone_classification_audit()
        row = next(
            row
            for row in audit["rewrite_candidates"]
            if row["mir_function"].endswith("::clone_const_generic")
        )
        row["lowering_kind"] = "semantic_scope_non_heap_object_skipped"
        row["rewrite_status"] = "semantic_scope_rewrite_skipped_non_heap_object_type"
        audit["summary"]["semantic_scope_unsolved_candidate_count"] = 3
        with self.assertRaisesRegex(AssertionError, "fail-closed"):
            runner.validate_clone_candidate_classification(audit)

    def test_allocation_side_recovery_rejects_target_drop_or_deallocation_scope(self) -> None:
        runtime = {
            "recovery_identity_matches": 0,
            "recovery_identity_mismatches": 0,
        }
        audit = {"rewrite_candidates": []}
        evidence = runner.validate_allocation_side_recovery_requirement(audit, runtime)
        self.assertTrue(evidence["allocation_side_recovery_required"])

        target_rows = (
            {
                "semantic_object_type": "std::boxed::Box<ProducerPayload>",
                "lowering_kind": "semantic_scope_drop_rewrite",
                "callee": "TerminatorKind::Drop",
            },
            {
                "destination_type": "std::boxed::Box<ConsumerPayload>",
                "lowering_kind": "semantic_scope_enter_exit_rewrite",
                "callee": "__rust_dealloc",
            },
        )
        for row in target_rows:
            with self.subTest(row=row):
                with self.assertRaises(AssertionError):
                    runner.validate_allocation_side_recovery_requirement(
                        {"rewrite_candidates": [row]}, runtime
                    )

    def test_allocation_side_recovery_rejects_active_requested_identity(self) -> None:
        with self.assertRaisesRegex(
            AssertionError, "without any target drop/deallocation scope"
        ):
            runner.validate_allocation_side_recovery_requirement(
                {"rewrite_candidates": []},
                {
                    "recovery_identity_matches": 1,
                    "recovery_identity_mismatches": 0,
                },
            )

    def test_commit_binding_rejects_dirty_head_and_source_drift(self) -> None:
        with mock.patch.object(runner, "checked_output", return_value=" M unialloc/src/lib.rs"):
            with self.assertRaisesRegex(AssertionError, "clean working tree"):
                runner.source_binding_snapshot("nightly-test", "rustc")

        start = {
            "git_head": "a" * 40,
            "git_status": "",
            "clean_head": True,
            "toolchain": "nightly-test",
            "rustc_verbose_version": "rustc test",
            "sysroot": "/tmp/sysroot",
            "scoped_paths": ["source.rs"],
            "scoped_file_count": 1,
            "scoped_file_hashes": {"source.rs": "one"},
            "scoped_fingerprint_sha256": "one",
        }
        runner.assert_source_binding_stable(start, dict(start))
        end = dict(start)
        end["scoped_fingerprint_sha256"] = "two"
        with self.assertRaisesRegex(AssertionError, "drifted"):
            runner.assert_source_binding_stable(start, end)

    def test_output_inside_repo_is_rejected_and_build_inputs_are_scoped(self) -> None:
        with self.assertRaisesRegex(AssertionError, "outside repository"):
            runner.reject_output_inside_repo(ROOT / "evaluation" / "raw" / "probe-output")
        with tempfile.TemporaryDirectory() as directory:
            runner.reject_output_inside_repo(Path(directory) / "probe-output")

        scoped = {str(path) for path in runner.SOURCE_BINDING_PATHS}
        self.assertIn("unialloc/build.rs", scoped)
        self.assertIn("alloc_macros/src", scoped)
        self.assertIn(str(RUNNER.relative_to(ROOT)), scoped)


if __name__ == "__main__":
    unittest.main()
