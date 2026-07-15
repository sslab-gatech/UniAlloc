#!/usr/bin/env python3
"""Tests for the deterministic RustSec security-scope figure."""

from __future__ import annotations

import csv
import importlib.util
import io
import pathlib
import tempfile
import unittest


ROOT = pathlib.Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "evaluation/scripts/plot_rustsec_security_scope.py"
spec = importlib.util.spec_from_file_location("plot_rustsec_security_scope", SCRIPT)
assert spec is not None and spec.loader is not None
plot = importlib.util.module_from_spec(spec)
spec.loader.exec_module(plot)


def typeiso_edge_mitigation(**updates: object) -> dict[str, object]:
    result: dict[str, object] = {
        "mechanism": "type_isolation",
        "outcome": "mitigated",
        "validated_mitigation": True,
        "result_semantics": "causal_compiler_bound_reuse_edge_mitigation",
        "positive_scope": "exploit_enabling_cross_identity_reuse_edge",
        "compiler_automatic_victim_coverage": False,
        "source_vulnerability_detection_validated": False,
        "vulnerability_specific_detection_signal": False,
        "full_source_vulnerability_detection": False,
    }
    result.update(updates)
    return result


class RustSecSecurityScopePlotTests(unittest.TestCase):
    def test_summary_keeps_mechanisms_and_evidence_gaps_separate(self) -> None:
        ledger = {
            "rustsec_commit": "a" * 40,
            "cases": [
                {
                    "advisory_id": "A",
                    "primary_primitive": "use_after_free",
                    "integration": {"status": "executable"},
                    "mechanism_results": [
                        typeiso_edge_mitigation(synthetic_reduction=True)
                    ],
                },
                {
                    "advisory_id": "B",
                    "primary_primitive": "double_free",
                    "integration": {"status": "executable"},
                    "mechanism_results": [
                        {"mechanism": "reclaim_checks", "outcome": "detected"}
                    ],
                },
                {
                    "advisory_id": "C",
                    "primary_primitive": "double_free",
                    "integration": {"status": "executable"},
                    "mechanism_results": [
                        typeiso_edge_mitigation(),
                        {"mechanism": "reclaim_checks", "outcome": "detected"},
                    ],
                },
                {
                    "advisory_id": "H",
                    "primary_primitive": "invalid_free",
                    "integration": {"status": "executable"},
                    "mechanism_results": [
                        {
                            "mechanism": "recovery_layout_validation",
                            "outcome": "detected",
                        }
                    ],
                },
                {
                    "advisory_id": "D",
                    "primary_primitive": "invalid_deallocation",
                    "integration": {"status": "executable"},
                    "mechanism_results": [
                        {"mechanism": "reclaim_checks", "outcome": "no_signal"}
                    ],
                },
                {
                    "advisory_id": "F",
                    "primary_primitive": "out_of_bounds_read",
                    "integration": {"status": "executable"},
                    "mechanism_results": [],
                },
                {
                    "advisory_id": "G",
                    "primary_primitive": "use_after_free",
                    "integration": {"status": "executable"},
                    "mechanism_results": [
                        {"mechanism": "reclaim_checks", "outcome": "inconclusive"}
                    ],
                },
            ],
            "excluded_cases": [
                {
                    "advisory_id": "E",
                    "primary_primitive": "use_after_free",
                    "integration": {
                        "status": "blocked",
                        "blocker_reason": "requires an unavailable native runtime",
                        "evidence": ["evidence/E/BLOCKER.md"],
                    },
                    "scope_exclusion": {
                        "code": "no_executable_reproduction",
                        "reason": "requires an unavailable native runtime",
                        "evidence": ["evidence/E/BLOCKER.md"],
                    },
                    "mechanism_results": [],
                }
            ],
        }

        summary = plot.figure_summary(ledger)

        self.assertEqual(summary["scope_count"], 7)
        self.assertEqual(summary["reviewed_candidate_count"], 8)
        self.assertEqual(summary["excluded_before_evaluation_count"], 1)
        self.assertEqual(
            summary["case_attribution_counts"],
            {
                "other_allocator_feature_only": 2,
                "type_isolation_only": 1,
                "multiple_allocator_mechanisms": 1,
                "no_allocator_signal_observed": 1,
                "not_yet_attributed": 2,
            },
        )
        self.assertEqual(
            summary["mechanism_coverage_counts"],
            {
                "type_isolation_edge_covered": 2,
                "reclaim_checks_exact_detection": 2,
                "recovery_layout_validation_exact_detection": 1,
            },
        )
        self.assertTrue(summary["mechanism_categories_are_nonexclusive"])
        self.assertEqual(summary["accounted_case_count"], 7)
        self.assertEqual(
            summary["type_isolation_claim_boundary"],
            {
                "manual_derived_reuse_edge_case_count": 2,
                "manual_derived_reuse_edge_case_ids": ["A", "C"],
                "compiler_automatic_victim_coverage_case_count": 0,
                "compiler_automatic_victim_coverage_case_ids": [],
                "synthetic_reduction_case_count": 1,
                "synthetic_reduction_case_ids": ["A"],
                "automatic_full_source_vulnerability_detection_case_count": 0,
                "causal_mitigated_derived_edge_case_count": 2,
                "vulnerability_specific_detection_case_count": 0,
                "full_source_vulnerability_detection_case_count": 0,
                "compiler_automatic_victim_coverage": False,
                "source_vulnerability_detection_validated": False,
                "claim_grade": False,
            },
        )
        self.assertEqual(summary["unresolved_integration_count"], 0)
        self.assertEqual(summary["primitive_counts"]["double_free"], 2)

    def test_unrun_and_inconclusive_cases_never_become_no_signal(self) -> None:
        ledger = {
            "cases": [
                {
                    "advisory_id": "UNRUN",
                    "primary_primitive": "use_after_free",
                    "integration": {"status": "executable"},
                    "mechanism_results": [],
                    # A stale aggregate attribution must not affect the plot.
                    "final_attribution": "no_allocator_signal_observed",
                },
                {
                    "advisory_id": "INCONCLUSIVE",
                    "primary_primitive": "use_after_free",
                    "integration": {"status": "executable"},
                    "mechanism_results": [
                        {"mechanism": "reclaim_checks", "outcome": "inconclusive"}
                    ],
                },
                {
                    "advisory_id": "MATCHED",
                    "primary_primitive": "use_after_free",
                    "integration": {"status": "executable"},
                    "mechanism_results": [
                        {"mechanism": "reclaim_checks", "outcome": "no_signal"}
                    ],
                },
                {
                    "advisory_id": "PENDING",
                    "primary_primitive": "use_after_free",
                    "integration": {"status": "pending_integration"},
                    "mechanism_results": [],
                },
            ]
        }

        summary = plot.figure_summary(ledger)

        self.assertEqual(
            summary["case_attribution_case_ids"]["not_yet_attributed"],
            ["UNRUN", "INCONCLUSIVE"],
        )
        self.assertEqual(
            summary["case_attribution_case_ids"][
                "no_allocator_signal_observed"
            ],
            ["MATCHED"],
        )
        self.assertEqual(summary["unresolved_integration_case_ids"], ["PENDING"])

    def test_excluded_case_requires_reason_and_evidence(self) -> None:
        ledger = {
            "cases": [],
            "excluded_cases": [
                {
                    "advisory_id": "BLOCKED",
                    "primary_primitive": "double_free",
                    "integration": {
                        "status": "blocked",
                        "blocker_reason": "external runtime unavailable",
                        "evidence": [],
                    },
                    "scope_exclusion": {
                        "code": "no_executable_reproduction",
                        "reason": "external runtime unavailable",
                        "evidence": [],
                    },
                    "mechanism_results": [],
                }
            ]
        }

        with self.assertRaisesRegex(ValueError, "lacks evidence"):
            plot.figure_summary(ledger)

    def test_unmodeled_positive_mechanism_requires_its_own_plot_category(self) -> None:
        ledger = {
            "cases": [
                {
                    "advisory_id": "FUTURE",
                    "primary_primitive": "use_after_free",
                    "integration": {"status": "executable"},
                    "mechanism_results": [
                        {"mechanism": "quarantine", "outcome": "detected"}
                    ],
                }
            ]
        }

        with self.assertRaisesRegex(ValueError, "separate plot categories"):
            plot.figure_summary(ledger)

    def test_observed_typeiso_requires_a_derived_edge_identity_contract(self) -> None:
        missing_automatic_boundary = typeiso_edge_mitigation()
        del missing_automatic_boundary["compiler_automatic_victim_coverage"]
        missing_source_boundary = typeiso_edge_mitigation()
        del missing_source_boundary["source_vulnerability_detection_validated"]
        invalid_results = (
            typeiso_edge_mitigation(outcome="detected"),
            typeiso_edge_mitigation(validated_mitigation=False),
            typeiso_edge_mitigation(true_positive=True),
            typeiso_edge_mitigation(positive_scope="source_vulnerability"),
            typeiso_edge_mitigation(
                compiler_automatic_victim_coverage=True,
                automatic_edge_identity_probe=False,
            ),
            typeiso_edge_mitigation(source_vulnerability_detection_validated=True),
            typeiso_edge_mitigation(vulnerability_specific_detection_signal=True),
            typeiso_edge_mitigation(full_source_vulnerability_detection=True),
            missing_automatic_boundary,
            missing_source_boundary,
        )
        for index, result in enumerate(invalid_results):
            with self.subTest(index=index, result=result):
                ledger = {
                    "cases": [
                        {
                            "case_id": f"INVALID-{index}",
                            "advisory_id": f"ADVISORY-{index}",
                            "primary_primitive": "use_after_free",
                            "integration": {"status": "executable"},
                            "mechanism_results": [result],
                        }
                    ]
                }
                with self.assertRaisesRegex(
                    ValueError, "violates the derived-edge claim contract"
                ):
                    plot.figure_summary(ledger)

    def test_typeiso_validation_does_not_short_circuit_after_a_valid_result(self) -> None:
        ledger = {
            "cases": [
                {
                    "case_id": "RSH-MIXED",
                    "primary_primitive": "use_after_free",
                    "integration": {"status": "source_evidence_only"},
                    "mechanism_results": [
                        typeiso_edge_mitigation(),
                        typeiso_edge_mitigation(validated_mitigation=False),
                    ],
                }
            ]
        }

        with self.assertRaisesRegex(
            ValueError, "violates the derived-edge claim contract"
        ):
            plot.figure_summary(ledger)

    def test_typeiso_contract_accepts_explicit_source_boundary_fields(self) -> None:
        ledger = {
            "cases": [
                {
                    "case_id": "RSH-EDGE",
                    "advisory_id": "RUSTSEC-EDGE",
                    "primary_primitive": "use_after_free",
                    "integration": {"status": "executable"},
                    "mechanism_results": [
                        typeiso_edge_mitigation(
                            compiler_automatic_victim_coverage=False,
                            source_vulnerability_detection_validated=False,
                            automatic_source_coverage=False,
                        )
                    ],
                }
            ]
        }

        summary = plot.figure_summary(ledger)

        self.assertEqual(
            summary["mechanism_coverage_case_ids"]["type_isolation_edge_covered"],
            ["RSH-EDGE"],
        )
        self.assertEqual(
            summary["case_attribution_case_ids"]["type_isolation_only"],
            ["RSH-EDGE"],
        )

    def test_typeiso_contract_accepts_compiler_automatic_derived_coverage(self) -> None:
        ledger = {
            "cases": [
                {
                    "case_id": "RSH-AUTO",
                    "primary_primitive": "use_after_free",
                    "integration": {"status": "executable"},
                    "mechanism_results": [
                        typeiso_edge_mitigation(
                            automatic_edge_identity_probe=True,
                            manual_victim_identity_annotation=False,
                            compiler_automatic_victim_coverage=True,
                            automatic_source_coverage=False,
                        )
                    ],
                }
            ]
        }

        summary = plot.figure_summary(ledger)

        boundary = summary["type_isolation_claim_boundary"]
        self.assertEqual(
            boundary["compiler_automatic_victim_coverage_case_ids"],
            ["RSH-AUTO"],
        )
        self.assertEqual(boundary["manual_derived_reuse_edge_case_count"], 0)
        self.assertTrue(boundary["compiler_automatic_victim_coverage"])
        self.assertFalse(boundary["source_vulnerability_detection_validated"])
        self.assertEqual(boundary["causal_mitigated_derived_edge_case_count"], 1)
        self.assertEqual(boundary["vulnerability_specific_detection_case_count"], 0)
        self.assertEqual(
            boundary["full_source_vulnerability_detection_case_count"], 0
        )

    def test_svg_and_csv_are_written_without_external_plot_dependencies(self) -> None:
        ledger = {
            "rustsec_commit": "b" * 40,
            "cases": [
                {
                    "advisory_id": "A",
                    "primary_primitive": "use_after_free",
                    "integration": {"status": "executable"},
                    "mechanism_results": [],
                }
            ],
        }
        with tempfile.TemporaryDirectory() as temporary:
            output = pathlib.Path(temporary)
            plot.write_figure_bundle(ledger, output)
            svg = (output / "rustsec-security-scope.svg").read_text(encoding="utf-8")
            csv_text = (output / "rustsec-security-scope.csv").read_text(
                encoding="utf-8"
            )

        self.assertIn("<svg", svg)
        self.assertIn("Exclusive case attribution", svg)
        self.assertIn("TypeIso-mitigated derived edge", svg)
        self.assertIn("reclaim_checks exact detection", svg)
        self.assertIn("Recovery-layout validation", svg)
        self.assertIn(
            "vulnerability-specific and full-source detections: 0", svg
        )
        self.assertIn("No exact allocator signal observed", svg)
        self.assertIn("Excluded before evaluation", svg)
        self.assertIn("Unresolved mechanism attribution", svg)
        self.assertIn("RUSTSEC scope", svg)
        csv_rows = list(csv.DictReader(io.StringIO(csv_text)))
        self.assertEqual(csv_rows[0].keys(), {"section", "key", "label", "count"})
        mechanism_rows = {
            row["key"]: row
            for row in csv_rows
            if row["section"] == "mechanism_coverage"
        }
        self.assertEqual(mechanism_rows["type_isolation_edge_covered"]["count"], "0")
        case_rows = {
            row["key"]: row for row in csv_rows if row["section"] == "case_attribution"
        }
        self.assertEqual(case_rows["not_yet_attributed"]["count"], "1")


if __name__ == "__main__":
    unittest.main()
