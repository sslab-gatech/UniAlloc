#!/usr/bin/env python3
"""Tests for RustSec mechanism-result ledger merging."""

from __future__ import annotations

import importlib.util
import pathlib
import unittest


ROOT = pathlib.Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "evaluation/scripts/merge_rustsec_mechanism_results.py"
spec = importlib.util.spec_from_file_location("merge_mechanisms", SCRIPT)
assert spec is not None and spec.loader is not None
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


def payload(advisory: str, outcome: str) -> dict[str, object]:
    positive = outcome in {"detected", "mitigated"}
    case_id = "RSH-001" if advisory.endswith("1") else "RSH-002"
    return {
        "boundary": "bounded evidence",
        "results": [
            {
                "advisory_id": advisory,
                "case_id": case_id,
                "scenario_id": f"{case_id}-matched",
                "mechanism": "reclaim_checks",
                "outcome": outcome,
                "true_positive": positive,
                "result_semantics": (
                    "exact_diagnostic_true_positive"
                    if positive
                    else "matched_negative_without_exact_reclaim_diagnostic"
                ),
                "positive_scope": (
                    "duplicate_reclaim_event_in_integrated_witness"
                    if positive
                    else None
                ),
                "negative_boundary": (
                    None if positive else "integrated scenario only"
                ),
            }
        ],
    }


def recovery_payload(advisory: str = "RUSTSEC-TEST-0001") -> dict[str, object]:
    value = payload(advisory, "detected")
    row = value["results"][0]
    row.update(
        {
            "scenario_id": "RSH-001-derived-layout-validation",
            "mechanism": "recovery_layout_validation",
            "positive_scope": "allocation_deallocation_layout_mismatch_edge",
        }
    )
    return value


class MergeMechanismResultsTests(unittest.TestCase):
    def test_accepts_recovery_layout_exact_diagnostic(self) -> None:
        result = module.merge([(recovery_payload(), {"path": "layout"})])

        self.assertEqual(result["counts"], {"detected": 1})
        self.assertEqual(
            result["results"][0]["mechanism"], "recovery_layout_validation"
        )

    def test_accepts_fail_closed_recovery_layout_evidence_gap(self) -> None:
        value = recovery_payload()
        row = value["results"][0]
        row.update(
            {
                "outcome": "inconclusive",
                "true_positive": False,
                "result_semantics": "evidence_gap",
                "positive_scope": None,
            }
        )

        result = module.merge([(value, {"path": "layout-gap"})])

        self.assertEqual(result["counts"], {"inconclusive": 1})

    def test_keeps_distinct_positive_mechanisms_for_one_case(self) -> None:
        reclaim = payload("RUSTSEC-TEST-0001", "detected")
        recovery = recovery_payload()

        result = module.merge(
            [(reclaim, {"path": "reclaim"}), (recovery, {"path": "layout"})]
        )

        self.assertEqual(result["counts"], {"detected": 2})
        self.assertEqual(
            {row["mechanism"] for row in result["results"]},
            {"reclaim_checks", "recovery_layout_validation"},
        )

    def test_merges_and_recounts(self) -> None:
        result = module.merge(
            [
                (payload("RUSTSEC-TEST-0001", "detected"), {"path": "a"}),
                (payload("RUSTSEC-TEST-0002", "no_signal"), {"path": "b"}),
            ]
        )
        self.assertEqual(result["counts"], {"detected": 1, "no_signal": 1})
        self.assertEqual(len(result["results"]), 2)

    def test_rejects_conflicting_duplicate(self) -> None:
        with self.assertRaisesRegex(module.MergeError, "conflicting duplicate"):
            module.merge(
                [
                    (payload("RUSTSEC-TEST-0001", "detected"), {"path": "a"}),
                    (payload("RUSTSEC-TEST-0001", "no_signal"), {"path": "b"}),
                ]
            )

    def test_rejects_implicit_or_inconsistent_true_positive(self) -> None:
        implicit = payload("RUSTSEC-TEST-0001", "detected")
        del implicit["results"][0]["true_positive"]
        with self.assertRaisesRegex(module.MergeError, "true_positive"):
            module.merge([(implicit, {"path": "a"})])

        mislabeled = payload("RUSTSEC-TEST-0002", "no_signal")
        mislabeled["results"][0]["true_positive"] = True
        with self.assertRaisesRegex(module.MergeError, "true_positive"):
            module.merge([(mislabeled, {"path": "b"})])

    def test_rejects_semantically_spoofed_positive_tuple(self) -> None:
        spoofed = payload("RUSTSEC-TEST-0001", "detected")
        row = spoofed["results"][0]
        row["outcome"] = "mitigated"
        row["result_semantics"] = "matched_negative_without_exact_reclaim_diagnostic"

        with self.assertRaisesRegex(module.MergeError, "unsupported positive"):
            module.merge([(spoofed, {"path": "a"})])

    def test_rejects_no_signal_without_a_negative_boundary(self) -> None:
        negative = payload("RUSTSEC-TEST-0002", "no_signal")
        negative["results"][0]["negative_boundary"] = None

        with self.assertRaisesRegex(module.MergeError, "negative_boundary"):
            module.merge([(negative, {"path": "b"})])

    def test_rejects_scenario_not_bound_to_case(self) -> None:
        mismatched = payload("RUSTSEC-TEST-0001", "detected")
        mismatched["results"][0]["scenario_id"] = "RSH-002-matched"

        with self.assertRaisesRegex(module.MergeError, "explicitly bound"):
            module.merge([(mismatched, {"path": "a"})])


if __name__ == "__main__":
    unittest.main()
