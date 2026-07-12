#!/usr/bin/env python3
"""Focused unit tests for the Vec realloc identity probe runner."""

from __future__ import annotations

import importlib.util
from pathlib import Path
import sys
import unittest
from unittest import mock


ROOT = Path(__file__).resolve().parents[2]
RUNNER = ROOT / "tools/unialloc-rustc-pass/test_mir_vec_realloc_identity_probe.py"
SPEC = importlib.util.spec_from_file_location("mir_vec_realloc_runner", RUNNER)
assert SPEC is not None and SPEC.loader is not None
runner = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = runner
SPEC.loader.exec_module(runner)


def audit_row(
    *,
    object_type: str,
    type_id: int,
    callee: str,
    drop: bool = False,
) -> dict:
    return {
        "semantic_object_type": object_type,
        "type_id": type_id,
        "module_id": 0xC002,
        "flags": runner.TYPE_ISOLATED,
        "placement_hint": runner.CROSS_THREAD_RECOVERY,
        "callee": "TerminatorKind::Drop" if drop else callee,
        "lowering_kind": (
            "semantic_scope_drop_rewrite" if drop else "semantic_scope_enter_exit_rewrite"
        ),
        "rewrite_status": (
            "actual_semantic_scope_drop_rewrite_applied"
            if drop
            else "actual_semantic_scope_enter_exit_rewrite_applied"
        ),
    }


def valid_audit() -> dict:
    producer = "std::vec::Vec<ProducerPayload, std::alloc::Global>"
    consumer = "std::vec::Vec<ConsumerPayload, std::alloc::Global>"
    return {
        "summary": {
            "provider_override_installed": True,
            "body_clone_returned_to_rustc": True,
            "actual_semantic_scope_rewrite": True,
            "semantic_scope_rewrite_applied_count": 6,
            "semantic_scope_unsolved_candidate_count": 0,
            "semantic_scope_drop_unsolved_candidate_count": 0,
            "cross_thread_recovery_hint_count": 6,
        },
        "rewrite_candidates": [
            audit_row(object_type=producer, type_id=11, callee="Vec::with_capacity"),
            audit_row(object_type=producer, type_id=11, callee="Vec::reserve_exact"),
            audit_row(object_type=producer, type_id=11, callee="Vec::push"),
            audit_row(object_type=producer, type_id=11, callee="", drop=True),
            audit_row(object_type=consumer, type_id=22, callee="Vec::with_capacity"),
            audit_row(object_type=consumer, type_id=22, callee="", drop=True),
        ],
    }


def valid_runtime() -> dict:
    return {
        "capacity_growth_forced": True,
        "wrong_type_reuse_blocked": True,
        "producer_buffer_recovered": True,
        "same_layout_element_bytes": 64,
        "initial_capacity": 1,
        "final_capacity": 8,
        "growth_moved_buffer": False,
        "growth_typed_allocations": 1,
        "growth_typed_deallocations": 0,
        "growth_raw_realloc_no_metadata": 0,
        "growth_recorded_old_metadata_fallback": 0,
        "producer_final_buffer": 0x1000,
        "consumer_buffer": 0x2000,
        "recovered_producer_buffer": 0x1000,
        "typed_allocations": 4,
        "typed_deallocations": 3,
        "typed_cache_hits": 1,
        "semantic_type_stats_dropped_events": 0,
        "recovery_identity_matches": 0,
        "recovery_identity_mismatches": 0,
        "side_cache_corrupt_slots": 0,
        "type_rows": [
            {
                "type_id": 11,
                "allocations": 3,
                "allocated_bytes": 1088,
                "deallocations": 2,
                "cache_hits": 1,
                "cache_inserts": 2,
                "cache_bypasses": 0,
                "policy_flags_seen": runner.TYPE_ISOLATED,
                "observed_alloc_size": 512,
                "observed_dealloc_size": 512,
            },
            {
                "type_id": 22,
                "allocations": 1,
                "allocated_bytes": 512,
                "deallocations": 1,
                "cache_hits": 0,
                "cache_inserts": 1,
                "cache_bypasses": 0,
                "policy_flags_seen": runner.TYPE_ISOLATED,
                "observed_alloc_size": 512,
                "observed_dealloc_size": 512,
            },
        ],
    }


class MirVecReallocIdentityRunnerTests(unittest.TestCase):
    def test_validate_accepts_in_place_growth_and_allocation_side_recovery(self) -> None:
        evidence = runner.validate(valid_audit(), valid_runtime())
        self.assertEqual(evidence["producer_type_id"], 11)
        self.assertEqual(evidence["consumer_type_id"], 22)
        self.assertFalse(evidence["growth_moved_buffer_observed"])
        self.assertEqual(evidence["recovery_identity_matches_observed"], 0)

    def test_validate_rejects_wrong_type_alias_and_untyped_growth(self) -> None:
        runtime = valid_runtime()
        runtime["consumer_buffer"] = runtime["producer_final_buffer"]
        with self.assertRaises(AssertionError):
            runner.validate(valid_audit(), runtime)

        runtime = valid_runtime()
        runtime["growth_raw_realloc_no_metadata"] = 1
        with self.assertRaises(AssertionError):
            runner.validate(valid_audit(), runtime)

    def test_validate_requires_growth_and_drop_rewrite_coverage(self) -> None:
        audit = valid_audit()
        audit["rewrite_candidates"] = [
            row
            for row in audit["rewrite_candidates"]
            if "reserve_exact" not in str(row.get("callee"))
        ]
        with self.assertRaisesRegex(AssertionError, "reserve_exact"):
            runner.validate(audit, valid_runtime())

        audit = valid_audit()
        audit["rewrite_candidates"] = [
            row
            for row in audit["rewrite_candidates"]
            if not (
                "ProducerPayload" in str(row.get("semantic_object_type"))
                and row.get("lowering_kind") == "semantic_scope_drop_rewrite"
            )
        ]
        with self.assertRaisesRegex(AssertionError, "Drop"):
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
