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


def valid_audit() -> dict:
    return {
        "summary": {
            "provider_override_installed": True,
            "body_clone_returned_to_rustc": True,
            "actual_semantic_scope_rewrite": True,
            "semantic_scope_unsolved_candidate_count": 1,
        },
        "rewrite_candidates": [ambiguous_row()],
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
        "buffers_distinct": True,
        "checksum": 123,
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
        "recovery_identity_matches": 0,
        "recovery_identity_mismatches": 0,
        "side_cache_corrupt_slots": 0,
    }


class MirAmbiguousCloneFallbackRunnerTests(unittest.TestCase):
    def test_validate_accepts_ambiguous_fail_closed_audit_and_runtime_fallback(self) -> None:
        evidence = runner.validate(valid_audit(), valid_runtime())
        self.assertEqual(evidence["audit"]["ambiguous_fail_closed_rows"], 1)
        self.assertEqual(evidence["runtime"]["clone_raw_alloc_no_metadata"], 1)

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
