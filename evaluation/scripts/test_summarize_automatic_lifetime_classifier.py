#!/usr/bin/env python3
"""Tests for automatic lifetime-classifier audit aggregation."""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path
import sys
import tempfile
import unittest


SCRIPT = Path(__file__).with_name("summarize_automatic_lifetime_classifier.py")
SPEC = importlib.util.spec_from_file_location("summarize_automatic_lifetime_classifier", SCRIPT)
assert SPEC is not None and SPEC.loader is not None
MODULE = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = MODULE
SPEC.loader.exec_module(MODULE)


def row(
    *,
    callsite: int,
    function: str,
    place: str,
    kind: str,
    hint: int,
    basis: str,
) -> dict[str, object]:
    return {
        "callsite": callsite,
        "type_id": 22,
        "module_id": 33,
        "mir_function": function,
        "semantic_object_type": "std::vec::Vec<u64>",
        "destination_place": place,
        "lowering_kind": kind,
        "lifetime_hint": hint,
        "lifetime_hint_confidence": 100 if hint else 0,
        "lifetime_hint_basis": basis,
    }


class SummarizeAutomaticLifetimeClassifierTest(unittest.TestCase):
    def test_deduplicates_sites_and_checks_allocation_drop_pairing(self) -> None:
        with tempfile.TemporaryDirectory(prefix="unialloc-classifier-summary-") as raw:
            root = Path(raw)
            short_basis = "automatic_exact_local_drop_before_phase_boundary"
            unknown_basis = "automatic_alias_or_escape_unknown"
            audit = {
                "schema_version": 1,
                "source": "unialloc-rustc-driver-mir-rewrite-dry-run",
                "compiler_pass": {"automatic_lifetime_classifier_enabled": True},
                "rewrite_candidates": [
                    row(
                        callsite=1,
                        function="app::short",
                        place="_1",
                        kind="semantic_scope_enter_exit_rewrite",
                        hint=1,
                        basis=short_basis,
                    ),
                    row(
                        callsite=2,
                        function="app::short",
                        place="_1",
                        kind="semantic_scope_drop_rewrite",
                        hint=1,
                        basis=short_basis,
                    ),
                    row(
                        callsite=3,
                        function="app::escape",
                        place="_2",
                        kind="semantic_scope_enter_exit_rewrite",
                        hint=0,
                        basis=unknown_basis,
                    ),
                    row(
                        callsite=4,
                        function="app::receiver",
                        place="_3",
                        kind="semantic_scope_enter_exit_rewrite",
                        hint=0,
                        basis="automatic_unsupported_site_unknown",
                    ),
                ],
            }
            (root / "one.json").write_text(json.dumps(audit), encoding="utf-8")
            (root / "duplicate.json").write_text(json.dumps(audit), encoding="utf-8")

            summary = MODULE.summarize_audit_root("fixture", root)
            self.assertEqual(summary["audit_file_count"], 2)
            self.assertEqual(summary["semantic_candidate_allocation_site_count"], 3)
            self.assertEqual(summary["automatic_eligible_allocation_site_count"], 2)
            self.assertEqual(summary["classified_allocation_site_count"], 1)
            self.assertEqual(summary["ephemeral_allocation_site_count"], 1)
            self.assertEqual(summary["long_lived_allocation_site_count"], 0)
            self.assertEqual(summary["abstained_allocation_site_count"], 1)
            self.assertEqual(summary["unsupported_allocation_site_count"], 1)
            self.assertEqual(summary["unknown_allocation_site_count"], 2)
            self.assertEqual(summary["classification_coverage"], 0.5)
            self.assertEqual(summary["end_to_end_classification_coverage"], 1 / 3)
            self.assertEqual(summary["classified_pairing_checked_count"], 1)
            self.assertEqual(summary["classified_pairing_missing_drop_count"], 0)
            self.assertEqual(summary["classified_pairing_mismatch_count"], 0)
            self.assertEqual(
                summary["unknown_reason_counts"],
                {
                    unknown_basis: 1,
                    "automatic_unsupported_site_unknown": 1,
                },
            )

    def test_reports_missing_and_mismatched_drop_evidence(self) -> None:
        with tempfile.TemporaryDirectory(prefix="unialloc-classifier-summary-") as raw:
            root = Path(raw)
            long_basis = "automatic_exact_local_drop_after_phase_boundary"
            short_basis = "automatic_exact_local_drop_before_phase_boundary"
            audit = {
                "schema_version": 1,
                "source": "unialloc-rustc-driver-mir-rewrite-dry-run",
                "compiler_pass": {"automatic_lifetime_classifier_enabled": True},
                "rewrite_candidates": [
                    row(
                        callsite=10,
                        function="app::missing",
                        place="_1",
                        kind="semantic_scope_enter_exit_rewrite",
                        hint=2,
                        basis=long_basis,
                    ),
                    row(
                        callsite=20,
                        function="app::mismatch",
                        place="_2",
                        kind="semantic_scope_enter_exit_rewrite",
                        hint=2,
                        basis=long_basis,
                    ),
                    row(
                        callsite=21,
                        function="app::mismatch",
                        place="_2",
                        kind="semantic_scope_drop_rewrite",
                        hint=1,
                        basis=short_basis,
                    ),
                ],
            }
            (root / "audit.json").write_text(json.dumps(audit), encoding="utf-8")
            summary = MODULE.summarize_audit_root("fixture", root)
            self.assertEqual(summary["classified_pairing_checked_count"], 2)
            self.assertEqual(summary["classified_pairing_missing_drop_count"], 1)
            self.assertEqual(summary["classified_pairing_mismatch_count"], 1)
            self.assertFalse(summary["pairing_consistent"])

    def test_profile_hints_are_paired_without_entering_automatic_coverage(self) -> None:
        with tempfile.TemporaryDirectory(prefix="unialloc-classifier-summary-") as raw:
            root = Path(raw)
            audit = {
                "schema_version": 1,
                "source": "unialloc-rustc-driver-mir-rewrite-dry-run",
                "compiler_pass": {"automatic_lifetime_classifier_enabled": True},
                "rewrite_candidates": [
                    row(
                        callsite=30,
                        function="app::profile_missing_drop",
                        place="_1",
                        kind="semantic_scope_enter_exit_rewrite",
                        hint=2,
                        basis="profile_exact_match",
                    ),
                    row(
                        callsite=40,
                        function="app::profile_mismatched_drop",
                        place="_2",
                        kind="semantic_scope_enter_exit_rewrite",
                        hint=2,
                        basis="profile_exact_match",
                    ),
                    row(
                        callsite=41,
                        function="app::profile_mismatched_drop",
                        place="_2",
                        kind="semantic_scope_drop_rewrite",
                        hint=0,
                        basis="profile_missing_entry",
                    ),
                ],
            }
            (root / "audit.json").write_text(json.dumps(audit), encoding="utf-8")

            summary = MODULE.summarize_audit_root("fixture", root)
            self.assertEqual(summary["semantic_candidate_allocation_site_count"], 2)
            self.assertEqual(summary["automatic_eligible_allocation_site_count"], 0)
            self.assertEqual(summary["classified_allocation_site_count"], 0)
            self.assertEqual(summary["nonautomatic_allocation_site_count"], 2)
            self.assertEqual(summary["classification_coverage"], 0.0)
            self.assertEqual(summary["end_to_end_classification_coverage"], 0.0)
            self.assertEqual(summary["classified_pairing_checked_count"], 2)
            self.assertEqual(summary["classified_pairing_missing_drop_count"], 1)
            self.assertEqual(summary["classified_pairing_mismatch_count"], 1)
            self.assertFalse(summary["pairing_consistent"])

    def test_drop_only_profile_selection_reports_reverse_pair_mismatch(self) -> None:
        with tempfile.TemporaryDirectory(prefix="unialloc-classifier-summary-") as raw:
            root = Path(raw)
            audit = {
                "schema_version": 1,
                "source": "unialloc-rustc-driver-mir-rewrite-dry-run",
                "compiler_pass": {"automatic_lifetime_classifier_enabled": True},
                "rewrite_candidates": [
                    row(
                        callsite=50,
                        function="app::reverse_partial_profile",
                        place="_1",
                        kind="semantic_scope_enter_exit_rewrite",
                        hint=0,
                        basis="profile_missing_entry",
                    ),
                    row(
                        callsite=51,
                        function="app::reverse_partial_profile",
                        place="_1",
                        kind="semantic_scope_drop_rewrite",
                        hint=2,
                        basis="profile_exact_match",
                    ),
                ],
            }
            (root / "audit.json").write_text(json.dumps(audit), encoding="utf-8")

            summary = MODULE.summarize_audit_root("fixture", root)
            self.assertEqual(summary["semantic_candidate_allocation_site_count"], 1)
            self.assertEqual(summary["automatic_eligible_allocation_site_count"], 0)
            self.assertEqual(summary["classified_allocation_site_count"], 0)
            self.assertEqual(summary["nonautomatic_allocation_site_count"], 1)
            self.assertEqual(summary["classification_coverage"], 0.0)
            self.assertEqual(summary["end_to_end_classification_coverage"], 0.0)
            self.assertEqual(summary["classified_pairing_checked_count"], 1)
            self.assertEqual(summary["classified_pairing_missing_drop_count"], 0)
            self.assertEqual(summary["classified_pairing_mismatch_count"], 1)
            self.assertFalse(summary["pairing_consistent"])

    def test_rejects_non_audit_json(self) -> None:
        with tempfile.TemporaryDirectory(prefix="unialloc-classifier-summary-") as raw:
            root = Path(raw)
            (root / "bad.json").write_text("{}", encoding="utf-8")
            with self.assertRaises(MODULE.SummaryError):
                MODULE.summarize_audit_root("fixture", root)


if __name__ == "__main__":
    unittest.main()
