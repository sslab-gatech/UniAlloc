#!/usr/bin/env python3
"""Tests for bounded Type Isolation source-sweep result export."""

from __future__ import annotations

import importlib.util
import pathlib
import unittest


ROOT = pathlib.Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "evaluation/scripts/export_rustsec_typeiso_source_results.py"
spec = importlib.util.spec_from_file_location("typeiso_source_results", SCRIPT)
assert spec is not None and spec.loader is not None
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


def record(
    status: str,
    *,
    repetitions: int = 3,
    findings: int = 0,
    signals: int = 0,
    rewrites: int = 4,
    coverage: bool = False,
) -> dict[str, object]:
    return {
        "execution_status": "completed",
        "executed_repetitions": repetitions,
        "tool_finding_count": findings,
        "native_diagnostic_signal_count": signals,
        "compiler_rewrites": rewrites,
        "critical_site_coverage_validated": coverage,
        "oracle_status": status,
    }


def scenario() -> dict[str, object]:
    return {
        "case_id": "RSH-001",
        "scenario_id": "RSH-001-upstream",
        "vulnerable": {
            "system": record("tool_finding_observed", findings=3, rewrites=0),
            "typed_plain": record("native_clean_runs_inconclusive"),
            "typeiso": record("native_clean_runs_inconclusive"),
        },
        "patched": {
            "system": record("clean_tool_runs_observed", rewrites=0),
            "typed_plain": record("native_clean_runs_inconclusive"),
            "typeiso": record("native_clean_runs_inconclusive"),
        },
    }


class TypeIsoSourceResultTests(unittest.TestCase):
    def test_executed_source_witness_remains_inconclusive_without_coverage(self) -> None:
        result = module.evaluate_scenario(
            scenario(), advisory_id="RUSTSEC-TEST-0001", minimum_repetitions=3
        )

        self.assertEqual(result["outcome"], "inconclusive")
        self.assertFalse(result["true_positive"])
        self.assertEqual(result["result_semantics"], "evidence_gap")
        self.assertIsNone(result["positive_scope"])
        self.assertEqual(
            result["reason"], "compiler_critical_site_coverage_not_validated"
        )
        self.assertTrue(result["checks"]["vulnerable_system_oracle_reproduced"])
        self.assertTrue(result["checks"]["patched_system_control_valid"])
        self.assertFalse(
            result["checks"]["compiler_critical_site_coverage_validated"]
        )

    def test_compile_rejected_patch_is_a_valid_control(self) -> None:
        value = scenario()
        value["patched"]["system"] = record(
            "patched_control_compile_rejection_observed",
            repetitions=0,
            rewrites=0,
        )
        value["patched"]["typeiso"] = record(
            "native_diagnostic_compile_rejection_observed", repetitions=0
        )

        result = module.evaluate_scenario(
            value, advisory_id="RUSTSEC-TEST-0001", minimum_repetitions=3
        )

        self.assertTrue(result["checks"]["patched_system_control_valid"])
        self.assertTrue(result["checks"]["patched_typeiso_arm_completed"])

    def test_export_rejects_unknown_or_duplicate_scenarios(self) -> None:
        summary = {
            "schema_version": 2,
            "source": "unialloc-rustsec-heap-full-sweep-summary",
            "scenarios": [scenario()],
        }
        corpus = {
            "cases": [
                {"case_id": "RSH-001", "advisory_id": "RUSTSEC-TEST-0001"}
            ]
        }
        artifact = {"path": "evidence.json", "bytes": 1, "sha256": "00"}

        with self.assertRaises(module.ResultError):
            module.export(
                summary,
                corpus,
                scenario_ids=["RSH-999-missing"],
                minimum_repetitions=3,
                summary_artifact=artifact,
                corpus_artifact=artifact,
            )
        with self.assertRaises(module.ResultError):
            module.export(
                summary,
                corpus,
                scenario_ids=["RSH-001-upstream", "RSH-001-upstream"],
                minimum_repetitions=3,
                summary_artifact=artifact,
                corpus_artifact=artifact,
            )


if __name__ == "__main__":
    unittest.main()
