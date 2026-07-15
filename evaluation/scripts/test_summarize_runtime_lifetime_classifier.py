#!/usr/bin/env python3
"""Tests for runtime lifetime classifier evidence summarization."""

from __future__ import annotations

import importlib.util
import pathlib
import sys
import unittest


ROOT = pathlib.Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "evaluation" / "scripts" / "summarize_runtime_lifetime_classifier.py"
spec = importlib.util.spec_from_file_location("summarize_runtime_lifetime_classifier", SCRIPT)
assert spec is not None and spec.loader is not None
summary = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = summary
spec.loader.exec_module(summary)


class RuntimeLifetimeSummaryTests(unittest.TestCase):
    def test_bootstrap_constant_samples_are_exact(self) -> None:
        low, high = summary.median_ratio_interval([1.0] * 5, [2.0] * 5, iterations=100)
        self.assertEqual((low, high), (2.0, 2.0))

    def test_classification_separates_coverage_accuracy_and_abstention(self) -> None:
        stats = {
            "adaptive_site_count": 4,
            "adaptive_cold_sites": 2,
            "adaptive_short_sites": 1,
            "adaptive_long_sites": 1,
            "adaptive_eligible_allocations": 20,
            "adaptive_short_bypassed_allocations": 8,
            "adaptive_short_routed_allocations": 2,
            "adaptive_long_routed_allocations": 2,
            "adaptive_short_observations": 5,
            "adaptive_long_observations": 3,
            "adaptive_censored_observations": 1,
            "predictor_tp": 2,
            "predictor_tn": 2,
            "predictor_fp": 1,
            "predictor_fn": 0,
            "static_hint_tp": 0,
            "static_hint_tn": 0,
            "static_hint_fp": 0,
            "static_hint_fn": 0,
            "static_hint_abstained": 8,
            "phase_advances": 0,
            "thp_extent_mappings": 2,
            "thp_advice_attempts": 2,
            "adaptive_missing_identity_bypasses": 0,
            "adaptive_site_table_bypasses": 0,
            "adaptive_trailer_corruptions": 0,
        }
        result = {"measurements": [{"app": "fixture", "runtime_lifetime_stats": stats}]}

        totals = summary.summarize_classification(result)["totals"]

        self.assertEqual(totals["site_classification_coverage_percent"], 50.0)
        self.assertEqual(totals["learned_allocation_decision_coverage_percent"], 60.0)
        self.assertEqual(totals["conditional_learned_prediction_accuracy_percent"], 80.0)
        self.assertEqual(totals["static_hint_coverage_percent"], 0.0)

    def test_performance_rejects_mismatched_outputs(self) -> None:
        def fixture(output: str) -> dict[str, object]:
            return {
                "apps": ["fixture"],
                "measurements": [
                    {
                        "app": "fixture",
                        "warmup": False,
                        "wall_seconds": 1.0,
                        "peak_rss_kib": 100,
                        "output_sha256": output,
                    }
                ],
                "builds": [
                    {
                        "app": "fixture",
                        "force_load": {"features": ["type_isolation", "lifetime_hugepage"]},
                    }
                ],
            }

        with self.assertRaises(summary.SummaryError):
            summary.summarize_performance(fixture("a"), fixture("b"))


if __name__ == "__main__":
    unittest.main()
