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


def typeiso_payload(*, automatic: bool) -> dict[str, object]:
    return {
        "boundary": "derived edge only",
        "results": [
            {
                "advisory_id": "RUSTSEC-TEST-0001",
                "case_id": "RSH-001",
                "scenario_id": "RSH-001-derived-reuse",
                "mechanism": "type_isolation",
                "outcome": "mitigated",
                "validated_mitigation": True,
                "result_semantics": (
                    "causal_compiler_bound_reuse_edge_mitigation"
                ),
                "positive_scope": "exploit_enabling_cross_identity_reuse_edge",
                "negative_boundary": None,
                "automatic_edge_identity_probe": automatic,
                "manual_victim_identity_annotation": not automatic,
                "compiler_automatic_victim_coverage": automatic,
                "source_vulnerability_detection_validated": False,
                "vulnerability_specific_detection_signal": False,
                "full_source_vulnerability_detection": False,
                "automatic_source_coverage": False,
                "synthetic_reduction": False,
                "reduction_fidelity": (
                    "compiler_automatic_derived_reduction"
                    if automatic
                    else "manual_derived_reduction"
                ),
            }
        ],
    }


def legacy_typeiso_mitigation_row() -> dict[str, object]:
    row = dict(typeiso_payload(automatic=False)["results"][0])
    row.pop("validated_mitigation")
    row["true_positive"] = True
    row["result_semantics"] = "causal_mitigation_true_positive"
    return row


def legacy_typeiso_source_row() -> dict[str, object]:
    row = dict(typeiso_payload(automatic=False)["results"][0])
    row.update(
        {
            "scenario_id": "RSH-001-upstream",
            "outcome": "inconclusive",
            "true_positive": False,
            "result_semantics": "evidence_gap",
            "positive_scope": None,
        }
    )
    row.pop("validated_mitigation")
    return row


class MergeMechanismResultsTests(unittest.TestCase):
    def test_normalizes_legacy_source_rows_and_replaces_old_mitigations(self) -> None:
        current = typeiso_payload(automatic=True)
        current_row = current["results"][0]
        legacy = {
            "boundary": "legacy mixed evidence",
            "results": [
                payload("RUSTSEC-TEST-0002", "no_signal")["results"][0],
                legacy_typeiso_mitigation_row(),
                legacy_typeiso_source_row(),
            ],
        }

        normalized = module.normalize_legacy_payload(
            legacy,
            superseding_typeiso_keys=frozenset(
                {module.result_key(current_row)}
            ),
        )
        result = module.merge(
            [(normalized, {"path": "legacy"}), (current, {"path": "current"})]
        )

        self.assertEqual(
            result["counts"],
            {"inconclusive": 1, "mitigated": 1, "no_signal": 1},
        )
        typeiso_rows = [
            row for row in result["results"] if row["mechanism"] == "type_isolation"
        ]
        self.assertEqual(len(typeiso_rows), 2)
        source = next(row for row in typeiso_rows if row["outcome"] == "inconclusive")
        self.assertIs(source["validated_mitigation"], False)
        self.assertNotIn("true_positive", source)
        self.assertIs(source["vulnerability_specific_detection_signal"], False)
        self.assertIs(source["full_source_vulnerability_detection"], False)
        self.assertEqual(
            normalized["legacy_normalization"],
            {
                "normalized_typeiso_inconclusive_count": 1,
                "superseded_typeiso_mitigation_count": 1,
            },
        )

    def test_legacy_mitigation_requires_a_strict_current_replacement(self) -> None:
        legacy = {
            "results": [legacy_typeiso_mitigation_row()],
        }

        with self.assertRaisesRegex(module.MergeError, "lacks current replacement"):
            module.normalize_legacy_payload(
                legacy,
                superseding_typeiso_keys=frozenset(),
            )

    def test_legacy_source_normalization_rejects_positive_or_ambiguous_rows(self) -> None:
        mutations = (
            {"true_positive": True},
            {"result_semantics": "causal_mitigation_true_positive"},
            {"positive_scope": "source_vulnerability"},
        )
        for mutation in mutations:
            with self.subTest(mutation=mutation):
                row = legacy_typeiso_source_row()
                row.update(mutation)
                with self.assertRaisesRegex(
                    module.MergeError, "unsupported legacy Type Isolation source"
                ):
                    module.normalize_legacy_payload(
                        {"results": [row]},
                        superseding_typeiso_keys=frozenset(),
                    )

    def test_accepts_manual_and_automatic_typeiso_provenance(self) -> None:
        for automatic in (False, True):
            with self.subTest(automatic=automatic):
                result = module.merge(
                    [(typeiso_payload(automatic=automatic), {"path": "typeiso"})]
                )
                row = result["results"][0]
                self.assertIs(
                    row["compiler_automatic_victim_coverage"], automatic
                )

    def test_rejects_inconsistent_automatic_typeiso_provenance(self) -> None:
        invalid = typeiso_payload(automatic=True)
        invalid["results"][0]["manual_victim_identity_annotation"] = True

        with self.assertRaisesRegex(module.MergeError, "internally inconsistent"):
            module.merge([(invalid, {"path": "typeiso"})])

    def test_rejects_typeiso_detection_claim_in_a_mitigation_row(self) -> None:
        invalid = typeiso_payload(automatic=True)
        invalid["results"][0]["vulnerability_specific_detection_signal"] = True

        with self.assertRaisesRegex(module.MergeError, "vulnerability-specific"):
            module.merge([(invalid, {"path": "typeiso"})])

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

    def test_rejects_implicit_or_inconsistent_typeiso_mitigation(self) -> None:
        implicit = typeiso_payload(automatic=True)
        del implicit["results"][0]["validated_mitigation"]
        with self.assertRaisesRegex(module.MergeError, "validated_mitigation"):
            module.merge([(implicit, {"path": "typeiso"})])

        mislabeled = typeiso_payload(automatic=True)
        mislabeled["results"][0]["validated_mitigation"] = False
        with self.assertRaisesRegex(module.MergeError, "validated_mitigation"):
            module.merge([(mislabeled, {"path": "typeiso"})])

    def test_rejects_cross_mechanism_semantic_fields(self) -> None:
        typeiso = typeiso_payload(automatic=True)
        typeiso["results"][0]["true_positive"] = True
        with self.assertRaisesRegex(module.MergeError, "exclusively"):
            module.merge([(typeiso, {"path": "typeiso"})])

        detector = payload("RUSTSEC-TEST-0001", "detected")
        detector["results"][0]["validated_mitigation"] = True
        with self.assertRaisesRegex(module.MergeError, "exclusively"):
            module.merge([(detector, {"path": "detector"})])

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
