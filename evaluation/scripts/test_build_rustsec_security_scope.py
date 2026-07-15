#!/usr/bin/env python3
"""Unit tests for the complete RustSec security-scope ledger."""

from __future__ import annotations

import importlib.util
import pathlib
import unittest


ROOT = pathlib.Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "evaluation" / "scripts" / "build_rustsec_security_scope.py"
spec = importlib.util.spec_from_file_location("build_rustsec_security_scope", SCRIPT)
assert spec is not None and spec.loader is not None
scope = importlib.util.module_from_spec(spec)
spec.loader.exec_module(scope)


def one_case_inputs(*, tracked_reclaim: bool = False) -> dict[str, object]:
    advisory_id = "RUSTSEC-2020-0001"
    return {
        "review": {
            "strong_candidates": {
                "cross_type_reuse": [advisory_id],
                "tracked_reclaim_or_invalid_free": (
                    [advisory_id] if tracked_reclaim else []
                ),
                "supplemental_rudra_source_evidence": [],
            },
            "conditional_candidates": [],
            "excluded": {},
        },
        "inventory": {
            "candidates": [
                {
                    "advisory_id": advisory_id,
                    "package": "example",
                    "title": "example",
                    "classification_signals": ["use_after_free"],
                }
            ]
        },
        "corpus": {
            "cases": [
                {
                    "case_id": "RSH-001",
                    "advisory_id": advisory_id,
                    "taxonomy": {"primary_primitive": "use_after_free"},
                }
            ]
        },
        "catalogs": [
            {
                "cases": [
                    {
                        "case_id": "RSH-001",
                        "advisory_id": advisory_id,
                        "scenarios": [{"scenario_id": "RSH-001-upstream"}],
                    }
                ]
            }
        ],
    }


class RustSecSecurityScopeTests(unittest.TestCase):
    def test_union_and_mechanism_attribution_remain_distinct(self) -> None:
        review = {
            "rustsec_commit": "a" * 40,
            "strong_candidates": {
                "cross_type_reuse": ["RUSTSEC-2020-0001", "RUSTSEC-2020-0003"],
                "tracked_reclaim_or_invalid_free": [
                    "RUSTSEC-2020-0001",
                    "RUSTSEC-2020-0002",
                    "RUSTSEC-2020-0003",
                ],
                "supplemental_rudra_source_evidence": [],
            },
            "conditional_candidates": [],
            "excluded": {},
        }
        inventory = {
            "candidates": [
                {
                    "advisory_id": f"RUSTSEC-2020-000{index}",
                    "package": f"crate-{index}",
                    "title": f"case {index}",
                    "classification_signals": [
                        "use_after_free" if index != 2 else "double_free"
                    ],
                    "source_path": f"crates/case-{index}.md",
                    "source_sha256": str(index) * 64,
                }
                for index in range(1, 4)
            ]
        }
        corpus = {
            "cases": [
                {
                    "case_id": "RSH-001",
                    "advisory_id": "RUSTSEC-2020-0001",
                    "assessment_profile": "reuse_dependent_uaf",
                    "taxonomy": {"primary_primitive": "use_after_free"},
                }
            ]
        }
        catalogs = [
            {
                "cases": [
                    {
                        "case_id": "RSH-001",
                        "advisory_id": "RUSTSEC-2020-0001",
                        "scenarios": [{"scenario_id": "RSH-001-upstream"}],
                    }
                ]
            }
        ]
        statuses = [
            {
                "advisory_id": "RUSTSEC-2020-0002",
                "case_id": "RSH-002",
                "integration_status": "blocked",
                "blocker_reason": "upstream witness unavailable",
                "evidence": ["pinned advisory contains no reproducer"],
            }
        ]
        results = {
            "results": [
                {
                    "advisory_id": "RUSTSEC-2020-0001",
                    "case_id": "RSH-001",
                    "scenario_id": "RSH-001-upstream",
                    "mechanism": "type_isolation",
                    "outcome": "mitigated",
                    "evidence_path": "evidence/a.json",
                },
                {
                    "advisory_id": "RUSTSEC-2020-0001",
                    "case_id": "RSH-001",
                    "scenario_id": "RSH-001-upstream",
                    "mechanism": "reclaim_checks",
                    "outcome": "detected",
                    "evidence_path": "evidence/a.json",
                },
            ]
        }

        built = scope.build_scope(
            inventory=inventory,
            review=review,
            corpus=corpus,
            catalogs=catalogs,
            statuses=statuses,
            mechanism_results=results,
        )

        self.assertEqual([row["advisory_id"] for row in built["cases"]], [
            "RUSTSEC-2020-0001",
            "RUSTSEC-2020-0003",
        ])
        by_id = {row["advisory_id"]: row for row in built["cases"]}
        self.assertEqual(
            by_id["RUSTSEC-2020-0001"]["final_attribution"],
            "multiple_allocator_mechanisms",
        )
        self.assertEqual(
            [row["advisory_id"] for row in built["excluded_cases"]],
            ["RUSTSEC-2020-0002"],
        )
        self.assertEqual(
            built["excluded_cases"][0]["integration"]["status"], "blocked"
        )
        self.assertEqual(
            by_id["RUSTSEC-2020-0003"]["integration"]["status"],
            "pending_integration",
        )
        self.assertEqual(
            by_id["RUSTSEC-2020-0003"]["candidate_mechanisms"],
            ["type_isolation", "reclaim_checks"],
        )
        self.assertEqual(built["counts"]["reviewed_strong_candidate_count"], 3)
        self.assertEqual(built["counts"]["strong_candidate_count"], 2)
        self.assertEqual(built["counts"]["excluded_non_evaluable_count"], 1)
        self.assertEqual(built["counts"]["type_isolation_observed_count"], 1)
        self.assertEqual(built["counts"]["other_feature_observed_count"], 1)
        self.assertEqual(built["counts"]["mechanism_overlap_observed_count"], 1)

    def test_terminal_gate_rejects_pending_or_source_only_cases(self) -> None:
        with self.assertRaisesRegex(scope.ScopeError, "non-terminal integration"):
            scope.require_terminal_integration(
                [
                    {
                        "advisory_id": "RUSTSEC-2020-0001",
                        "integration": {"status": "source_evidence_only"},
                    }
                ]
            )

    def test_batch_status_schema_keeps_blockers_visible(self) -> None:
        rows = scope.status_rows(
            {
                "baseline_results": [
                    {
                        "advisory_id": "RUSTSEC-2020-0001",
                        "case_id": "RSH-001",
                        "scenario_id": "RSH-001-upstream",
                        "experiment": {"path": "run.json", "sha256": "a" * 64},
                    }
                ],
                "blocked_cases": [
                    {
                        "advisory_id": "RUSTSEC-2020-0002",
                        "case_id": "RSH-002",
                        "blocker_code": "external_runtime_required",
                        "reason": "requires a foreign host",
                        "repository_artifacts": {"BLOCKER.md": "b" * 64},
                    }
                ],
            }
        )

        self.assertEqual(rows[0]["integration_status"], "executable")
        self.assertEqual(rows[1]["integration_status"], "blocked")
        self.assertEqual(rows[1]["blocker_reason"], "requires a foreign host")

    def test_direct_status_schema_normalizes_aliases_and_keeps_blockers(self) -> None:
        rows = scope.status_rows(
            {
                "cases": [
                    {
                        "advisory_id": "RUSTSEC-2020-0001",
                        "case_id": "RSH-001",
                        "status": "executable_and_validated",
                    }
                ],
                "blocked_cases": [
                    {
                        "advisory_id": "RUSTSEC-2020-0002",
                        "case_id": "RSH-002",
                        "blocker_code": "no_reproducer",
                        "reason": "published material has no witness",
                    }
                ],
            }
        )

        self.assertEqual(rows[0]["integration_status"], "executable")
        self.assertEqual(rows[1]["integration_status"], "blocked")
        self.assertEqual(rows[1]["blocker_reason"], "published material has no witness")

    def test_direct_status_schema_accepts_terminal_status(self) -> None:
        rows = scope.status_rows(
            {
                "cases": [
                    {
                        "advisory_id": "RUSTSEC-2020-0001",
                        "case_id": "RSH-001",
                        "terminal_status": "executable_and_validated",
                    }
                ]
            }
        )

        self.assertEqual(rows[0]["integration_status"], "executable")

    def test_duplicate_status_rows_fail_closed(self) -> None:
        with self.assertRaisesRegex(scope.ScopeError, "duplicate integration status"):
            scope._status_index(
                [
                    {
                        "advisory_id": "RUSTSEC-2020-0001",
                        "integration_status": "executable",
                    },
                    {
                        "advisory_id": "RUSTSEC-2020-0001",
                        "integration_status": "blocked",
                    },
                ]
            )

    def test_same_case_catalog_rows_union_scenario_ids(self) -> None:
        indexed = scope._catalog_index(
            [
                {
                    "cases": [
                        {
                            "case_id": "RSH-001",
                            "advisory_id": "RUSTSEC-2020-0001",
                            "scenarios": [{"scenario_id": "RSH-001-a"}],
                        }
                    ]
                },
                {
                    "cases": [
                        {
                            "case_id": "RSH-001",
                            "advisory_id": "RUSTSEC-2020-0001",
                            "scenarios": [{"scenario_id": "RSH-001-b"}],
                        }
                    ]
                },
            ],
            {},
        )
        self.assertEqual(
            indexed["RUSTSEC-2020-0001"]["scenario_ids"],
            ["RSH-001-a", "RSH-001-b"],
        )

    def test_reviewed_primitive_override_preserves_reason_and_evidence(self) -> None:
        review = {
            "strong_candidates": {
                "cross_type_reuse": ["RUSTSEC-2020-0001"],
                "tracked_reclaim_or_invalid_free": [],
                "supplemental_rudra_source_evidence": [],
            },
            "conditional_candidates": [],
            "excluded": {},
        }
        inventory = {
            "candidates": [
                {
                    "advisory_id": "RUSTSEC-2020-0001",
                    "package": "example",
                    "title": "example",
                    "classification_signals": ["double_free"],
                }
            ]
        }
        override = {
            "overrides": [
                {
                    "advisory_id": "RUSTSEC-2020-0001",
                    "case_id": "RSH-001",
                    "primary_primitive": "use_after_free",
                    "replaces": "double_free",
                    "reason": "Miri reports a dangling-reference access.",
                    "evidence_paths": ["evidence/run.json"],
                }
            ]
        }

        built = scope.build_scope(
            inventory=inventory,
            review=review,
            corpus={"cases": []},
            catalogs=[
                {
                    "cases": [
                        {
                            "case_id": "RSH-001",
                            "advisory_id": "RUSTSEC-2020-0001",
                            "scenarios": [],
                        }
                    ]
                }
            ],
            primitive_overrides=[override],
        )

        row = built["cases"][0]
        self.assertEqual(row["primary_primitive"], "use_after_free")
        self.assertEqual(
            row["primary_primitive_review"],
            {
                "source": "reviewed_override",
                "replaces": "double_free",
                "reason": "Miri reports a dangling-reference access.",
                "evidence_paths": ["evidence/run.json"],
            },
        )
        self.assertEqual(
            built["counts"]["primary_primitive"], {"use_after_free": 1}
        )

    def test_repeatable_primitive_overrides_reject_unknown_advisory(self) -> None:
        with self.assertRaisesRegex(scope.ScopeError, "unknown advisory"):
            scope._primitive_override_index(
                [
                    {
                        "overrides": [
                            {
                                "advisory_id": "RUSTSEC-2020-9999",
                                "primary_primitive": "use_after_free",
                                "replaces": "double_free",
                                "reason": "validated witness",
                                "evidence_paths": ["evidence/run.json"],
                            }
                        ]
                    }
                ],
                {"RUSTSEC-2020-0001"},
            )

    def test_primitive_override_cli_is_repeatable(self) -> None:
        args = scope.build_parser().parse_args(
            [
                "--primitive-overrides",
                "first.json",
                "--primitive-overrides",
                "second.json",
            ]
        )

        self.assertEqual(
            args.primitive_overrides,
            [pathlib.Path("first.json"), pathlib.Path("second.json")],
        )

    def test_repeatable_primitive_overrides_reject_invalid_primitive(self) -> None:
        with self.assertRaisesRegex(scope.ScopeError, "invalid primitive override"):
            scope._primitive_override_index(
                [
                    {
                        "overrides": [
                            {
                                "advisory_id": "RUSTSEC-2020-0001",
                                "primary_primitive": "heap_problem",
                                "replaces": "double_free",
                                "reason": "validated witness",
                                "evidence_paths": ["evidence/run.json"],
                            }
                        ]
                    }
                ],
                {"RUSTSEC-2020-0001"},
            )

    def test_primitive_override_fails_closed_when_base_label_drifts(self) -> None:
        review = {
            "strong_candidates": {
                "cross_type_reuse": ["RUSTSEC-2020-0001"],
                "tracked_reclaim_or_invalid_free": [],
                "supplemental_rudra_source_evidence": [],
            }
        }
        with self.assertRaisesRegex(scope.ScopeError, "stale primitive override"):
            scope.build_scope(
                inventory={
                    "candidates": [
                        {
                            "advisory_id": "RUSTSEC-2020-0001",
                            "classification_signals": ["use_after_free"],
                        }
                    ]
                },
                review=review,
                corpus={"cases": []},
                catalogs=[],
                primitive_overrides=[
                    {
                        "overrides": [
                            {
                                "advisory_id": "RUSTSEC-2020-0001",
                                "primary_primitive": "use_after_free",
                                "replaces": "double_free",
                                "reason": "validated witness",
                                "evidence_paths": ["evidence/run.json"],
                            }
                        ]
                    }
                ],
            )

    def test_mechanism_result_case_and_scenario_must_match_integration(self) -> None:
        values = one_case_inputs(tracked_reclaim=True)
        base_result = {
            "advisory_id": "RUSTSEC-2020-0001",
            "case_id": "RSH-999",
            "scenario_id": "RSH-001-upstream",
            "mechanism": "reclaim_checks",
            "outcome": "detected",
        }
        with self.assertRaisesRegex(scope.ScopeError, "case_id mismatch"):
            scope.build_scope(
                **values, mechanism_results={"results": [base_result]}
            )

        base_result["case_id"] = "RSH-001"
        base_result["scenario_id"] = "RSH-999-wrong"
        with self.assertRaisesRegex(scope.ScopeError, "outside the integrated case"):
            scope.build_scope(
                **values, mechanism_results={"results": [base_result]}
            )

    def test_non_candidate_mechanism_requires_reviewed_amendment(self) -> None:
        values = one_case_inputs()
        result = {
            "advisory_id": "RUSTSEC-2020-0001",
            "case_id": "RSH-001",
            "scenario_id": "RSH-001-upstream",
            "mechanism": "reclaim_checks",
            "outcome": "detected",
        }
        with self.assertRaisesRegex(scope.ScopeError, "lacks candidate registration"):
            scope.build_scope(
                **values, mechanism_results={"results": [result]}
            )

        built = scope.build_scope(
            **values,
            mechanism_results={"results": [result]},
            mechanism_amendments=[
                {
                    "amendments": [
                        {
                            "advisory_id": "RUSTSEC-2020-0001",
                            "case_id": "RSH-001",
                            "scenario_id": "RSH-001-upstream",
                            "mechanism": "reclaim_checks",
                            "allowed_outcomes": ["detected", "inconclusive"],
                            "reason": "terminal witness exposed duplicate reclaim",
                            "evidence_paths": ["evidence/run.json"],
                        }
                    ]
                }
            ],
        )
        registration = built["cases"][0]["mechanism_results"][0][
            "evaluation_registration"
        ]
        self.assertEqual(
            registration["source"], "reviewed_post_integration_amendment"
        )

    def test_blocked_status_is_excluded_from_evaluable_scope(self) -> None:
        values = one_case_inputs()
        values["catalogs"][0]["cases"][0]["scenarios"] = []
        built = scope.build_scope(
            **values,
            statuses=[
                {
                    "advisory_id": "RUSTSEC-2020-0001",
                    "case_id": "RSH-001",
                    "integration_status": "blocked",
                    "blocker_reason": "external runtime required",
                    "evidence": ["BLOCKER.md"],
                }
            ],
        )
        self.assertEqual(built["cases"], [])
        self.assertEqual(len(built["excluded_cases"]), 1)
        self.assertEqual(
            built["excluded_cases"][0]["integration"]["status"], "blocked"
        )
        self.assertEqual(
            built["excluded_cases"][0]["scope_exclusion"]["code"],
            "no_executable_reproduction",
        )
        self.assertEqual(built["counts"]["strong_candidate_count"], 0)
        self.assertEqual(built["counts"]["reviewed_strong_candidate_count"], 1)

    def test_blocked_status_conflicts_with_executable_catalog_scenario(self) -> None:
        values = one_case_inputs()
        with self.assertRaisesRegex(scope.ScopeError, "blocked status conflicts"):
            scope.build_scope(
                **values,
                statuses=[
                    {
                        "advisory_id": "RUSTSEC-2020-0001",
                        "case_id": "RSH-001",
                        "integration_status": "blocked",
                        "blocker_reason": "contradictory blocker",
                        "evidence": ["BLOCKER.md"],
                    }
                ],
            )

    def test_excluded_blocker_requires_reason_and_evidence(self) -> None:
        values = one_case_inputs()
        values["catalogs"][0]["cases"][0]["scenarios"] = []
        with self.assertRaisesRegex(
            scope.ScopeError, "blocked scope exclusion requires a reason and evidence"
        ):
            scope.build_scope(
                **values,
                statuses=[
                    {
                        "advisory_id": "RUSTSEC-2020-0001",
                        "case_id": "RSH-001",
                        "integration_status": "blocked",
                        "blocker_reason": "external runtime required",
                        "evidence": [],
                    }
                ],
            )

    def test_results_outside_strong_scope_fail_closed(self) -> None:
        values = one_case_inputs()
        with self.assertRaisesRegex(scope.ScopeError, "non-scope advisories"):
            scope.build_scope(
                **values,
                mechanism_results={
                    "results": [
                        {
                            "advisory_id": "RUSTSEC-2020-9999",
                            "case_id": "RSH-999",
                            "scenario_id": "RSH-999-upstream",
                            "mechanism": "type_isolation",
                            "outcome": "inconclusive",
                        }
                    ]
                },
            )

    def test_mechanism_result_requires_explicit_scenario_binding(self) -> None:
        values = one_case_inputs(tracked_reclaim=True)
        with self.assertRaisesRegex(scope.ScopeError, "requires .*scenario_id"):
            scope.build_scope(
                **values,
                mechanism_results={
                    "results": [
                        {
                            "advisory_id": "RUSTSEC-2020-0001",
                            "case_id": "RSH-001",
                            "mechanism": "reclaim_checks",
                            "outcome": "detected",
                        }
                    ]
                },
            )

    def test_mechanism_amendment_cli_is_repeatable(self) -> None:
        args = scope.build_parser().parse_args(
            [
                "--mechanism-amendments",
                "first.json",
                "--mechanism-amendments",
                "second.json",
            ]
        )
        self.assertEqual(
            args.mechanism_amendments,
            [pathlib.Path("first.json"), pathlib.Path("second.json")],
        )


if __name__ == "__main__":
    unittest.main()
