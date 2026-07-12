#!/usr/bin/env python3
"""Focused negative tests for the nonzero realloc shrink validator."""
from __future__ import annotations
import copy
import importlib.util
from pathlib import Path
import sys
import unittest

ROOT = Path(__file__).resolve().parents[2]
RUNNER = ROOT / "tools/unialloc-rustc-pass/test_mir_realloc_shrink_probe.py"
SPEC = importlib.util.spec_from_file_location("mir_realloc_shrink_runner", RUNNER)
assert SPEC is not None and SPEC.loader is not None
runner = importlib.util.module_from_spec(SPEC); sys.modules[SPEC.name] = runner; SPEC.loader.exec_module(runner)


def row(op: str, callsite: int) -> dict:
    symbol = f"__unialloc_{op}_layout_with_metadata"
    delegated = op in {"realloc", "dealloc"}
    return {"mir_function": f"probe::{runner.HELPER}", "lowering_kind": "direct_allocator_call_rewrite",
            "rewrite_status": "actual_allocator_call_replacement_applied", "replacement_symbol": symbol,
            "replacement_resolution_status": "resolved" + symbol[1:],
            "type_id": 0 if delegated else 11, "module_id": 0 if delegated else 22,
            "callsite": callsite, "flags": 0 if delegated else runner.TYPE_ISOLATED,
            "lifetime_hint": 0, "placement_hint": 0,
            "type_id_basis": "direct_allocator_recovery_delegated" if delegated else "direct_allocator_callsite_key"}


def valid_audit() -> dict:
    return {"summary": {"provider_override_installed": True, "body_clone_returned_to_rustc": True,
                         "actual_allocator_call_replacement_requested": True,
                         "actual_allocator_call_replacement": True},
            "rewrite_candidates": [row("alloc", 101), row("realloc", 102), row("dealloc", 103)]}


def raw_semantic_scope_helper_row() -> dict:
    """Preserved shape of the legitimate fourth row from the combined probe audit."""
    return {
        "mir_function": runner.HELPER,
        "callee": "realloc_shrink_type_rows_json",
        "destination_type": "std::string::String",
        "lowering_kind": "semantic_scope_enter_exit_rewrite",
        "rewrite_status": "semantic_scope_enter_exit_rewrite_planned",
        "replacement_symbol": "__unialloc_semantic_scope_push",
        "replacement_resolution_status": "not_requested_dry_run",
        "type_id": 11507945832468554002,
        "module_id": 13835860698770440193,
        "flags": runner.TYPE_ISOLATED,
        "callsite": 13804096419549101735,
    }


def runtime_row(callsite: int, allocations: int, deallocations: int, alloc_size: int, dealloc_size: int) -> dict:
    return {"type_id": 11, "module_id": 22, "callsite": callsite, "allocations": allocations,
            "deallocations": deallocations, "observed_alloc_size": alloc_size,
            "observed_alloc_align": 64 if alloc_size else 0, "observed_dealloc_size": dealloc_size,
            "observed_dealloc_align": 64 if dealloc_size else 0, "policy_flags_seen": runner.TYPE_ISOLATED}


def valid_runtime() -> dict:
    return {"source": runner.PROBE_NAME, "realloc_shrink": {"enabled": True, "old_size": 63,
            "new_size": 57, "align": 64, "old_pointer": 0x1000, "new_pointer": 0x1000,
            "pointer_reused": True, "pointer_aligned": True, "payload_preserved": True,
            "realloc_typed_allocations": 1, "realloc_typed_deallocations": 1,
            "final_typed_deallocations": 1, "realloc_fallback_allocations": 0,
            "realloc_raw_realloc_no_metadata": 0, "final_raw_dealloc_no_metadata": 0,
            "recovery_identity_matches": 0, "recovery_identity_mismatches": 0,
            "side_cache_corrupt_slots": 0, "recovery_record_after_final_dealloc": False,
            "type_rows": [runtime_row(101,1,1,63,63), runtime_row(102,1,0,57,0), runtime_row(103,0,1,0,57)]}}


class ReallocShrinkValidatorTests(unittest.TestCase):
    def test_accepts_contract_valid_same_class_shrink(self) -> None:
        evidence = runner.validate(valid_audit(), valid_runtime()); self.assertEqual(evidence["compiler_type_id"], 11)

    def test_accepts_preserved_semantic_scope_row_in_same_helper(self) -> None:
        audit = valid_audit()
        audit["rewrite_candidates"].append(raw_semantic_scope_helper_row())
        evidence = runner.validate(audit, valid_runtime())
        self.assertEqual(evidence["compiler_type_id"], 11)

    def test_rejects_duplicate_direct_row_even_with_semantic_scope_row(self) -> None:
        audit = valid_audit()
        audit["rewrite_candidates"].append(raw_semantic_scope_helper_row())
        audit["rewrite_candidates"].append(row("alloc", 104))
        with self.assertRaisesRegex(AssertionError, "exactly three"):
            runner.validate(audit, valid_runtime())

    def test_rejects_missing_or_tampered_rewrite_row(self) -> None:
        audit = valid_audit(); audit["rewrite_candidates"].pop()
        with self.assertRaisesRegex(AssertionError, "exactly three"): runner.validate(audit, valid_runtime())
        for field, value, message in (("rewrite_status", "not_requested_dry_run", "rewrite_status"),
                                      ("replacement_resolution_status", "lowered", "resolution"),
                                      ("type_id", 99, "realloc delegated type_id")):
            with self.subTest(field=field):
                audit = valid_audit(); audit["rewrite_candidates"][1][field] = value
                with self.assertRaisesRegex(AssertionError, message): runner.validate(audit, valid_runtime())

    def test_rejects_fake_pointer_alignment_or_payload(self) -> None:
        for field, value, message in (("new_pointer", 0x2000, "pointer_reused"),
                                      ("pointer_aligned", False, "pointer_aligned"),
                                      ("payload_preserved", False, "payload_preserved")):
            with self.subTest(field=field):
                runtime = valid_runtime(); runtime["realloc_shrink"][field] = value
                with self.assertRaisesRegex(AssertionError, message): runner.validate(valid_audit(), runtime)
        runtime = valid_runtime(); runtime["realloc_shrink"]["old_pointer"] = 0x1001; runtime["realloc_shrink"]["new_pointer"] = 0x1001
        with self.assertRaisesRegex(AssertionError, "pointer alignment"): runner.validate(valid_audit(), runtime)

    def test_rejects_fake_identity_or_runtime_accounting(self) -> None:
        runtime = valid_runtime(); runtime["realloc_shrink"]["type_rows"][1]["type_id"] = 99
        with self.assertRaisesRegex(AssertionError, "runtime realloc type_id"): runner.validate(valid_audit(), runtime)
        for field in ("realloc_fallback_allocations", "realloc_raw_realloc_no_metadata",
                      "recovery_identity_mismatches", "side_cache_corrupt_slots"):
            with self.subTest(field=field):
                runtime = valid_runtime(); runtime["realloc_shrink"][field] = 1
                with self.assertRaises(AssertionError): runner.validate(valid_audit(), runtime)

    def test_rejects_non_neutral_or_local_delegated_rows(self) -> None:
        for field, value, message in (("module_id", 22, "realloc delegated module_id"),
                                      ("flags", runner.TYPE_ISOLATED, "realloc delegated flags"),
                                      ("lifetime_hint", 1, "realloc delegated lifetime_hint"),
                                      ("placement_hint", 1, "realloc delegated placement_hint"),
                                      ("type_id_basis", "direct_allocator_callsite_key", "realloc delegated basis")):
            with self.subTest(field=field):
                audit = valid_audit(); audit["rewrite_candidates"][1][field] = value
                with self.assertRaisesRegex(AssertionError, message):
                    runner.validate(audit, valid_runtime())
        audit = valid_audit()
        audit["rewrite_candidates"][1]["replacement_symbol"] += "_local"
        audit["rewrite_candidates"][1]["replacement_resolution_status"] += "_local"
        with self.assertRaisesRegex(AssertionError, "realloc recovery ABI"):
            runner.validate(audit, valid_runtime())

if __name__ == "__main__": unittest.main()
