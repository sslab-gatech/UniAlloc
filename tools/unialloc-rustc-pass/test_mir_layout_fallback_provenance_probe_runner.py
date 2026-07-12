#!/usr/bin/env python3
"""Focused tests for the Layout fallback provenance probe validator."""

from __future__ import annotations

import importlib.util
from pathlib import Path
import sys
import unittest
from unittest import mock


ROOT = Path(__file__).resolve().parents[2]
RUNNER = ROOT / "tools/unialloc-rustc-pass/test_mir_layout_fallback_provenance_probe.py"
SPEC = importlib.util.spec_from_file_location("mir_layout_fallback_runner", RUNNER)
assert SPEC is not None and SPEC.loader is not None
runner = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = runner
SPEC.loader.exec_module(runner)

POSITIVE_TYPE_ID = 0x1111
FALLBACK_TYPE_ID = 0x2222
MODULE_ID = 0xC002_DA00_0000_0001
POSITIVE_ALLOC_CALLSITE = 0xA110
POSITIVE_DEALLOC_CALLSITE = 0xDEA1
FALLBACK_ALLOC_CALLSITE = 0xFA11


def direct_row(**overrides: object) -> dict:
    row = {
        "lowering_kind": "direct_allocator_call_rewrite",
        "mir_function": "run_expect_positive_control",
        "semantic_object_type": "std::alloc::Layout::new::<[u64; 4_usize]>",
        "type_id": POSITIVE_TYPE_ID,
        "type_id_basis": "rustc_middle_mir_layout_transformer_heap_object_type",
        "module_id": MODULE_ID,
        "callsite": POSITIVE_ALLOC_CALLSITE,
        "rewrite_status": "actual_allocator_call_replacement_applied",
        "replacement_symbol": "__unialloc_alloc_layout_with_metadata",
    }
    row.update(overrides)
    return row


def valid_audit() -> dict:
    return {
        "summary": {
            "provider_override_installed": True,
            "body_clone_returned_to_rustc": True,
            "actual_allocator_call_replacement": True,
            "direct_layout_allocator_rewrite_applied_count": 3,
        },
        "rewrite_candidates": [
            direct_row(),
            direct_row(
                callsite=POSITIVE_DEALLOC_CALLSITE,
                replacement_symbol="__unialloc_dealloc_layout_with_metadata",
            ),
            direct_row(
                mir_function="run_fallback_negative_control",
                semantic_object_type=runner.UNKNOWN_HEAP_OBJECT_TYPE,
                type_id=FALLBACK_TYPE_ID,
                type_id_basis="direct_allocator_callsite_key",
                callsite=FALLBACK_ALLOC_CALLSITE,
            ),
        ],
    }


def runtime_row(
    *,
    type_id: int,
    callsite: int,
    allocations: int,
    deallocations: int,
    alloc_size: int = 0,
    alloc_align: int = 0,
    dealloc_size: int = 0,
    dealloc_align: int = 0,
) -> dict:
    return {
        "type_id": type_id,
        "module_id": MODULE_ID,
        "callsite": callsite,
        "allocations": allocations,
        "deallocations": deallocations,
        "observed_alloc_size": alloc_size,
        "observed_alloc_align": alloc_align,
        "observed_dealloc_size": dealloc_size,
        "observed_dealloc_align": dealloc_align,
        "policy_flags_seen": 1,
    }


def valid_runtime() -> dict:
    return {
        "source": runner.PROBE_NAME,
        "manual_metadata_abi_calls": False,
        "positive_layout_size": 32,
        "positive_layout_align": 64,
        "fallback_layout_size": 37,
        "fallback_layout_align": 1,
        "fallback_pointer": 0x1000,
        "positive_checksum": 1,
        "fallback_checksum": 2,
        "typed_allocations": 2,
        "typed_deallocations": 1,
        "fallback_allocations": 0,
        "recovery_identity_mismatches": 0,
        "side_cache_corrupt_slots": 0,
        "type_rows": [
            runtime_row(
                type_id=POSITIVE_TYPE_ID,
                callsite=POSITIVE_ALLOC_CALLSITE,
                allocations=1,
                deallocations=0,
                alloc_size=32,
                alloc_align=64,
            ),
            runtime_row(
                type_id=POSITIVE_TYPE_ID,
                callsite=POSITIVE_DEALLOC_CALLSITE,
                allocations=0,
                deallocations=1,
                dealloc_size=32,
                dealloc_align=64,
            ),
            runtime_row(
                type_id=FALLBACK_TYPE_ID,
                callsite=FALLBACK_ALLOC_CALLSITE,
                allocations=1,
                deallocations=0,
                alloc_size=37,
                alloc_align=1,
            ),
        ],
    }


class MirLayoutFallbackProvenanceRunnerTests(unittest.TestCase):
    def test_validate_accepts_typed_expect_and_callsite_fallback(self) -> None:
        evidence = runner.validate(valid_audit(), valid_runtime())
        self.assertEqual(evidence["positive_type_id"], POSITIVE_TYPE_ID)
        self.assertEqual(evidence["fallback_callsite_type_id"], FALLBACK_TYPE_ID)

    def test_fail_first_rejects_fallback_inheriting_source_layout_identity(self) -> None:
        audit = valid_audit()
        fallback = audit["rewrite_candidates"][2]
        fallback["semantic_object_type"] = "std::alloc::Layout::new::<[u64; 4_usize]>"
        fallback["type_id"] = POSITIVE_TYPE_ID
        fallback["type_id_basis"] = "rustc_middle_mir_layout_transformer_heap_object_type"
        with self.assertRaises(AssertionError):
            runner.validate(audit, valid_runtime())

    def test_rejects_runtime_fallback_row_with_positive_type_id(self) -> None:
        runtime = valid_runtime()
        runtime["type_rows"][2]["type_id"] = POSITIVE_TYPE_ID
        with self.assertRaises(AssertionError):
            runner.validate(valid_audit(), runtime)

    def test_fail_first_rejects_wrong_positive_dealloc_alignment(self) -> None:
        runtime = valid_runtime()
        runtime["type_rows"][1]["observed_dealloc_align"] = 32
        with self.assertRaises(AssertionError):
            runner.validate(valid_audit(), runtime)

    def test_rejects_missing_actual_rewrite(self) -> None:
        audit = valid_audit()
        audit["rewrite_candidates"][2]["rewrite_status"] = (
            "provider_override_body_clone_returned_rewrite_planned"
        )
        with self.assertRaises(AssertionError):
            runner.validate(audit, valid_runtime())

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
