#!/usr/bin/env python3
"""Focused unit tests for the ambiguous Clone fallback probe runner."""

from __future__ import annotations

import importlib.util
from pathlib import Path
import sys
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[2]
RUNNER = ROOT / "tools/unialloc-rustc-pass/test_mir_ambiguous_clone_fallback_probe.py"
SPEC = importlib.util.spec_from_file_location("mir_ambiguous_clone_runner", RUNNER)
assert SPEC is not None and SPEC.loader is not None
runner = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = runner
SPEC.loader.exec_module(runner)


def ambiguous_row(**overrides: object) -> dict:
    row = {
        "mir_function": "rustc_driver_mir_ambiguous_clone_fallback_probe::main",
        "callee": "<std::result::Result<std::vec::Vec<ProducerPayload>, std::string::String> as std::clone::Clone>::clone",
        "destination_type": "std::result::Result<std::vec::Vec<ProducerPayload>, std::string::String>",
        "lowering_kind": "semantic_scope_unsolved_heap_object_candidate",
        "rewrite_status": "semantic_scope_rewrite_skipped_ambiguous_heap_object_type",
        "replacement_resolution_status": "rustc_middle_multiple_heap_object_types_not_lowered",
        "replacement_preview": "Skipped semantic-scope lowering because the Clone result has multiple supported heap owners: std::string::String, std::vec::Vec<ProducerPayload>",
    }
    row.update(overrides)
    return row


def supported_row(
    *, function_name: str, drop: bool = False, type_id: int = 11
) -> dict:
    return {
        "mir_function": function_name,
        "callee": "TerminatorKind::Drop" if drop else "Vec::with_capacity",
        "destination_type": "std::vec::Vec<ProducerPayload, std::alloc::Global>",
        "semantic_object_type": "std::vec::Vec<ProducerPayload, std::alloc::Global>",
        "type_id": type_id,
        "module_id": 0xC002,
        "flags": runner.TYPE_ISOLATED,
        "lowering_kind": (
            "semantic_scope_drop_rewrite"
            if drop
            else "semantic_scope_enter_exit_rewrite"
        ),
        "rewrite_status": (
            "actual_semantic_scope_drop_rewrite_applied"
            if drop
            else "actual_semantic_scope_enter_exit_rewrite_applied"
        ),
    }


def valid_audit() -> dict:
    return {
        "summary": {
            "provider_override_installed": True,
            "body_clone_returned_to_rustc": True,
            "actual_semantic_scope_rewrite": True,
            "semantic_scope_unsolved_candidate_count": 1,
        },
        "rewrite_candidates": [
            ambiguous_row(),
            supported_row(function_name="supported_seed_protected_buffer"),
            supported_row(function_name="supported_recover_protected_buffer"),
        ],
    }


def valid_runtime() -> dict:
    return {
        "source": "rustc_driver_mir_ambiguous_clone_fallback_probe",
        "clone_function": "Result::clone",
        "result_variant": "Ok",
        "same_layout_bytes": 64,
        "source_len": 4,
        "cloned_len": 4,
        "source_buffer": 0x1000,
        "cloned_buffer": 0x2000,
        "protected_buffer": 0x3000,
        "recovered_buffer": 0x3000,
        "buffers_distinct": True,
        "fallback_avoided_protected_buffer": True,
        "typed_recovery_preserved": True,
        "checksum": 123,
        "seed_type_id": 11,
        "recovery_type_id": 11,
        "seed_typed_allocations": 1,
        "seed_typed_deallocations": 1,
        "seed_typed_cache_inserts": 1,
        "clone_typed_allocations": 0,
        "clone_typed_deallocations": 0,
        "clone_fallback_allocations": 1,
        "clone_fallback_deallocations_before_drop": 0,
        "clone_raw_alloc_no_metadata": 1,
        "clone_raw_alloc_no_metadata_bytes": 256,
        "clone_raw_realloc_no_metadata": 0,
        "clone_recorded_old_realloc_fallback": 0,
        "drop_fallback_deallocations": 1,
        "drop_raw_dealloc_no_metadata": 1,
        "recovery_typed_allocations": 1,
        "recovery_typed_deallocations": 1,
        "recovery_typed_cache_hits": 1,
        "recovery_typed_cache_inserts": 1,
        "recovery_identity_matches": 0,
        "recovery_identity_mismatches": 0,
        "side_cache_corrupt_slots": 0,
    }


class MirAmbiguousCloneFallbackRunnerTests(unittest.TestCase):
    def test_validate_accepts_ambiguous_fail_closed_audit_and_runtime_fallback(self) -> None:
        evidence = runner.validate(valid_audit(), valid_runtime())
        self.assertEqual(evidence["audit"]["ambiguous_fail_closed_rows"], 1)
        self.assertEqual(evidence["runtime"]["clone_raw_alloc_no_metadata"], 1)
        self.assertTrue(evidence["runtime"]["fallback_avoided_protected_buffer"])
        self.assertTrue(evidence["runtime"]["typed_recovery_preserved"])
        self.assertEqual(
            evidence["audit"]["supported_controls"]["type_id"], 11
        )
        for function_name in runner.SUPPORTED_FUNCTIONS:
            function_evidence = evidence["audit"]["supported_controls"][
                "functions"
            ][function_name]
            self.assertEqual(function_evidence["allocation_scope_rows"], 1)
            self.assertEqual(
                function_evidence["target_drop_or_deallocation_rows"], 0
            )
            self.assertTrue(function_evidence["allocation_side_recovery_required"])

    def test_validate_rejects_ambiguous_clone_when_scope_is_applied(self) -> None:
        audit = valid_audit()
        audit["rewrite_candidates"].append(
            ambiguous_row(
                lowering_kind="semantic_scope_enter_exit_rewrite",
                rewrite_status="actual_semantic_scope_enter_exit_rewrite_applied",
                replacement_resolution_status="lowered",
            )
        )
        with self.assertRaisesRegex(AssertionError, "must not receive"):
            runner.validate_audit(audit)

    def test_validate_rejects_missing_raw_fallback_allocation(self) -> None:
        runtime = valid_runtime()
        runtime["clone_raw_alloc_no_metadata"] = 0
        with self.assertRaises(AssertionError):
            runner.validate_runtime(runtime)

    def test_validate_rejects_typed_metadata_for_ambiguous_clone(self) -> None:
        runtime = valid_runtime()
        runtime["clone_typed_allocations"] = 1
        with self.assertRaises(AssertionError):
            runner.validate_runtime(runtime)

    def test_validate_rejects_fallback_reusing_protected_typed_address(self) -> None:
        runtime = valid_runtime()
        runtime["cloned_buffer"] = runtime["protected_buffer"]
        with self.assertRaisesRegex(AssertionError, "protected typed cache"):
            runner.validate_runtime(runtime)

    def test_validate_rejects_supported_recovery_missing_protected_address(self) -> None:
        runtime = valid_runtime()
        runtime["recovered_buffer"] = 0x4000
        with self.assertRaisesRegex(AssertionError, "supported typed recovery"):
            runner.validate_runtime(runtime)

    def test_validate_rejects_tampered_ambiguous_classification(self) -> None:
        audit = valid_audit()
        audit["rewrite_candidates"][0]["replacement_resolution_status"] = "lowered"
        with self.assertRaisesRegex(AssertionError, "expected one ambiguous"):
            runner.validate_audit(audit)

    def test_validate_rejects_duplicate_ambiguous_classification_row(self) -> None:
        audit = valid_audit()
        audit["rewrite_candidates"].append(ambiguous_row())
        with self.assertRaisesRegex(AssertionError, "exactly one ambiguous Clone"):
            runner.validate_audit(audit)

    def test_validate_rejects_helper_target_drop_scope(self) -> None:
        audit = valid_audit()
        audit["rewrite_candidates"].append(
            supported_row(
                function_name="supported_recover_protected_buffer", drop=True
            )
        )
        with self.assertRaisesRegex(
            AssertionError, "must not have target Drop/deallocation"
        ):
            runner.validate_audit(audit)

    def test_validate_accepts_preserved_raw_allocation_side_recovery_shape(self) -> None:
        audit = valid_audit()
        for function_name in runner.SUPPORTED_FUNCTIONS:
            push = supported_row(function_name=function_name)
            push["callee"] = "Vec::push"
            audit["rewrite_candidates"].append(push)
        audit["rewrite_candidates"].append(
            {
                "mir_function": "main",
                "callee": "TerminatorKind::Drop",
                "semantic_object_type": (
                    "std::result::Result<std::vec::Vec<ProducerPayload>, "
                    "std::string::String>"
                ),
                "type_id": 99,
                "module_id": 0xC002,
                "flags": runner.TYPE_ISOLATED,
                "lowering_kind": "semantic_scope_drop_rewrite",
                "rewrite_status": "actual_semantic_scope_drop_rewrite_applied",
            }
        )
        evidence = runner.validate(audit, valid_runtime())
        for function_name in runner.SUPPORTED_FUNCTIONS:
            self.assertEqual(
                evidence["audit"]["supported_controls"]["functions"][
                    function_name
                ]["target_drop_or_deallocation_rows"],
                0,
            )

    def test_validate_rejects_duplicate_supported_scope(self) -> None:
        audit = valid_audit()
        audit["rewrite_candidates"].append(
            supported_row(function_name="supported_seed_protected_buffer")
        )
        with self.assertRaisesRegex(
            AssertionError, "exactly one supported Vec allocation scope candidate"
        ):
            runner.validate_audit(audit)

    def test_validate_rejects_wrong_kind_duplicate_with_capacity_candidate(self) -> None:
        audit = valid_audit()
        duplicate = supported_row(
            function_name="supported_seed_protected_buffer"
        )
        duplicate["lowering_kind"] = "direct_allocator_call_rewrite"
        duplicate["rewrite_status"] = "actual_allocator_call_replacement_applied"
        audit["rewrite_candidates"].append(duplicate)
        with self.assertRaisesRegex(
            AssertionError, "exactly one supported Vec allocation scope candidate"
        ):
            runner.validate_audit(audit)

    def test_validate_rejects_duplicate_planned_supported_scope(self) -> None:
        audit = valid_audit()
        planned = supported_row(function_name="supported_seed_protected_buffer")
        planned["rewrite_status"] = "semantic_scope_enter_exit_rewrite_planned"
        audit["rewrite_candidates"].append(planned)
        with self.assertRaisesRegex(
            AssertionError, "exactly one supported Vec allocation scope candidate"
        ):
            runner.validate_audit(audit)

    def test_validate_rejects_duplicate_planned_supported_drop(self) -> None:
        audit = valid_audit()
        planned = supported_row(
            function_name="supported_recover_protected_buffer", drop=True
        )
        planned["rewrite_status"] = "semantic_scope_drop_rewrite_planned"
        audit["rewrite_candidates"].append(planned)
        with self.assertRaisesRegex(
            AssertionError, "must not have target Drop/deallocation"
        ):
            runner.validate_audit(audit)

    def test_validate_rejects_helper_target_deallocation_row(self) -> None:
        audit = valid_audit()
        deallocation = supported_row(
            function_name="supported_seed_protected_buffer"
        )
        deallocation.update(
            {
                "callee": "__unialloc_dealloc_layout_with_metadata_hints",
                "lowering_kind": "direct_allocator_call_rewrite",
                "rewrite_status": "actual_allocator_call_replacement_applied",
                "metadata_pairing_contract": "direct_metadata_deallocation",
            }
        )
        audit["rewrite_candidates"].append(deallocation)
        with self.assertRaisesRegex(
            AssertionError, "must not have target Drop/deallocation"
        ):
            runner.validate_audit(audit)

    def test_validate_rejects_deallocation_exposed_only_by_replacement_symbol(self) -> None:
        audit = valid_audit()
        audit["rewrite_candidates"].append(
            {
                "mir_function": "supported_seed_protected_buffer",
                "callee": "opaque_operation",
                "lowering_kind": "opaque_operation",
                "rewrite_status": "opaque_operation",
                "replacement_symbol": "__unialloc_dealloc_layout_with_metadata_hints",
                "metadata_pairing_contract": "opaque_operation",
            }
        )
        with self.assertRaisesRegex(
            AssertionError, "must not have target Drop/deallocation"
        ):
            runner.validate_audit(audit)

    def test_validate_rejects_sole_supported_scope_not_actually_applied(self) -> None:
        audit = valid_audit()
        for row in audit["rewrite_candidates"]:
            if (
                row.get("mir_function") == "supported_seed_protected_buffer"
                and row.get("lowering_kind") == "semantic_scope_enter_exit_rewrite"
            ):
                row["rewrite_status"] = "semantic_scope_enter_exit_rewrite_planned"
        with self.assertRaisesRegex(AssertionError, "was not actually applied"):
            runner.validate_audit(audit)

    def test_validate_rejects_supported_seed_recovery_identity_drift(self) -> None:
        audit = valid_audit()
        for row in audit["rewrite_candidates"]:
            if row.get("mir_function") == "supported_recover_protected_buffer":
                row["type_id"] = 22
        with self.assertRaisesRegex(AssertionError, "share one compiler type_id"):
            runner.validate_audit(audit)

    def test_commit_binding_rejects_dirty_head_and_source_drift(self) -> None:
        with mock.patch.object(runner, "checked_output", return_value=" M unialloc/src/lib.rs"):
            with self.assertRaisesRegex(AssertionError, "clean working tree"):
                runner.source_binding_snapshot("nightly-test", "rustc")

        start = {
            "git_head": "a" * 40,
            "git_status": "",
            "clean_head": True,
            "toolchain": "nightly-test",
            "scoped_fingerprint_sha256": "one",
        }
        runner.assert_source_binding_stable(start, dict(start))
        end = dict(start)
        end["scoped_fingerprint_sha256"] = "two"
        with self.assertRaisesRegex(AssertionError, "drifted"):
            runner.assert_source_binding_stable(start, end)


if __name__ == "__main__":
    unittest.main()
