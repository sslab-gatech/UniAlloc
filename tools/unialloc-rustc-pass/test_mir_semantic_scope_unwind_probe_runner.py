#!/usr/bin/env python3
"""Focused unit tests for the nested semantic-scope unwind runner."""

from __future__ import annotations

import importlib.util
from pathlib import Path
import sys
import unittest
from unittest import mock


ROOT = Path(__file__).resolve().parents[2]
RUNNER = ROOT / "tools/unialloc-rustc-pass/test_mir_semantic_scope_unwind_probe.py"
SPEC = importlib.util.spec_from_file_location("mir_semantic_scope_unwind_runner", RUNNER)
assert SPEC is not None and SPEC.loader is not None
runner = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = runner
SPEC.loader.exec_module(runner)


def applied_row(
    *,
    object_type: str,
    callee: str,
    type_id: int,
    mir_function: str = "probe::main",
    drop: bool = False,
    unwind_pop: bool = False,
) -> dict:
    return {
        "semantic_object_type": object_type,
        "callee": "TerminatorKind::Drop" if drop else callee,
        "type_id": type_id,
        "module_id": 0xC002,
        "flags": runner.TYPE_ISOLATED,
        "mir_function": mir_function,
        "lowering_kind": (
            "semantic_scope_drop_rewrite" if drop else "semantic_scope_enter_exit_rewrite"
        ),
        "rewrite_status": (
            "actual_semantic_scope_drop_rewrite_applied"
            if drop
            else "actual_semantic_scope_enter_exit_rewrite_applied"
        ),
        "semantic_scope_unwind_pop_inserted": unwind_pop,
        "metadata_pairing_contract": (
            "semantic_scope_drop_active_metadata"
            if drop
            else "semantic_scope_active_metadata"
        ),
    }


def valid_audit() -> dict:
    vec_type = "std::vec::Vec<PostUnwindPayload, std::alloc::Global>"
    return {
        "summary": {
            "provider_override_installed": True,
            "body_clone_returned_to_rustc": True,
            "actual_semantic_scope_rewrite": True,
            "semantic_scope_unwind_pop_inserted_count": 1,
        },
        "rewrite_candidates": [
            applied_row(
                object_type="std::vec::Vec<PanicOnClone, std::alloc::Global>",
                callee="Vec::<PanicOnClone>::extend_from_slice",
                type_id=11,
                mir_function="probe::trigger_panicking_vec_extend",
                unwind_pop=True,
            ),
            applied_row(
                object_type=vec_type,
                callee="Vec::<PostUnwindPayload>::extend::<NestedUnwindOnce>",
                type_id=22,
            ),
            applied_row(
                object_type=vec_type,
                callee="",
                type_id=22,
                drop=True,
            ),
        ],
    }


def valid_runtime() -> dict:
    return {
        "source": runner.PROBE_NAME,
        "panic_observed": True,
        "initial_main_depth": 0,
        "initial_overflow_depth": 0,
        "initial_represented_depth": 0,
        "post_unwind_main_depth": 0,
        "post_unwind_overflow_depth": 0,
        "post_unwind_represented_depth": 0,
        "restored_outer_main_depth": 1,
        "restored_outer_overflow_depth": 0,
        "restored_outer_represented_depth": 1,
        "final_main_depth": 0,
        "final_overflow_depth": 0,
        "final_represented_depth": 0,
        "post_unwind_address": 0x1000,
        "post_unwind_checksum": 123,
        "post_alloc_total_allocations": 1,
        "post_alloc_typed_allocations": 1,
        "post_alloc_fallback_allocations": 0,
        "post_drop_typed_deallocations": 1,
        "post_drop_fallback_deallocations": 0,
        "recovery_identity_mismatches": 0,
        "side_cache_corrupt_slots": 0,
        "type_rows": [
            {
                "type_id": 22,
                "allocations": 1,
                "allocated_bytes": 64,
                "deallocations": 0,
                "policy_flags_seen": runner.TYPE_ISOLATED,
            },
            {
                "type_id": 22,
                "allocations": 0,
                "allocated_bytes": 0,
                "deallocations": 1,
                "policy_flags_seen": runner.TYPE_ISOLATED,
            },
        ],
    }


class MirSemanticScopeUnwindRunnerTests(unittest.TestCase):
    def test_validate_accepts_nested_restore_and_post_unwind_pairing(self) -> None:
        evidence = runner.validate(valid_audit(), valid_runtime())
        self.assertEqual(evidence["audit"]["post_unwind_vec_type_id"], 22)
        self.assertEqual(
            evidence["runtime"]["post_unwind_vec_runtime"]["deallocations"], 1
        )

    def test_validate_requires_inner_unwind_pop(self) -> None:
        audit = valid_audit()
        audit["rewrite_candidates"][0]["semantic_scope_unwind_pop_inserted"] = False
        with self.assertRaisesRegex(AssertionError, "unwind pop"):
            runner.validate_audit(audit)

    def test_validate_requires_restore_to_outer_scope_not_zero_or_leaked_inner(self) -> None:
        runtime = valid_runtime()
        runtime["restored_outer_main_depth"] = 0
        with self.assertRaises(AssertionError):
            runner.validate_runtime(runtime, 22)

        runtime = valid_runtime()
        runtime["restored_outer_main_depth"] = 2
        runtime["restored_outer_represented_depth"] = 2
        with self.assertRaises(AssertionError):
            runner.validate_runtime(runtime, 22)

    def test_validate_requires_typed_post_unwind_allocation_drop_pair(self) -> None:
        runtime = valid_runtime()
        runtime["post_drop_typed_deallocations"] = 0
        runtime["post_drop_fallback_deallocations"] = 1
        with self.assertRaises(AssertionError):
            runner.validate_runtime(runtime, 22)

    def test_commit_binding_rejects_dirty_head_and_source_drift(self) -> None:
        with mock.patch.object(
            runner, "checked_output", return_value=" M unialloc/src/lib.rs"
        ):
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
