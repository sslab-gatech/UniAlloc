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
    return {
        "summary": {"semantic_scope_unsolved_candidate_count": 1},
        "rewrite_candidates": rows,
    }


class MirTypeIsolationSecurityRunnerTests(unittest.TestCase):
    def test_clone_candidate_classification_accepts_expected_partition(self) -> None:
        evidence = runner.validate_clone_candidate_classification(
            ready_clone_classification_audit()
        )
        self.assertEqual(evidence["single_heap_scope_rows"], 1)
        self.assertEqual(evidence["non_heap_skipped_count"], 4)
        self.assertEqual(evidence["ambiguous_unsolved_count"], 1)

    def test_clone_candidate_classification_rejects_nonheap_as_unsolved(self) -> None:
        audit = ready_clone_classification_audit()
        row = next(
            row
            for row in audit["rewrite_candidates"]
            if row["mir_function"].endswith("::clone_nonheap_option")
        )
        row["lowering_kind"] = "semantic_scope_unsolved_heap_object_candidate"
        row["rewrite_status"] = "semantic_scope_rewrite_skipped_unresolved_heap_object_type"
        audit["summary"]["semantic_scope_unsolved_candidate_count"] = 2
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
        audit["summary"]["semantic_scope_unsolved_candidate_count"] = 0
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
                with self.assertRaisesRegex(AssertionError, "drop/deallocation"):
                    runner.validate_allocation_side_recovery_requirement(
                        {"rewrite_candidates": [row]}, runtime
                    )

    def test_allocation_side_recovery_rejects_active_requested_identity(self) -> None:
        with self.assertRaisesRegex(AssertionError, "was not required"):
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
