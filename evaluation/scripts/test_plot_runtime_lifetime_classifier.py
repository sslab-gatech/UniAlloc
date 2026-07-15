#!/usr/bin/env python3
"""Tests for the runtime lifetime classifier slide figure."""

from __future__ import annotations

import importlib.util
import pathlib
import sys
import unittest


ROOT = pathlib.Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "evaluation" / "scripts" / "plot_runtime_lifetime_classifier.py"
spec = importlib.util.spec_from_file_location("plot_runtime_lifetime_classifier", SCRIPT)
assert spec is not None and spec.loader is not None
plot = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = plot
spec.loader.exec_module(plot)


class RuntimeLifetimePlotTests(unittest.TestCase):
    def test_svg_contains_performance_classification_and_boundary(self) -> None:
        record = {
            "performance": {
                "applications": [
                    {
                        "app": "fixture",
                        "wall_overhead_percent": 2.5,
                        "bootstrap_95_percent_wall_overhead": [1.0, 4.0],
                    }
                ]
            },
            "classification": {
                "totals": {
                    "learned_allocation_decision_coverage_percent": 75.0,
                    "site_classification_coverage_percent": 50.0,
                    "conditional_learned_prediction_accuracy_percent": 90.0,
                    "static_hint_coverage_percent": 25.0,
                    "short_runtime_observations": 12,
                    "long_runtime_observations": 3,
                }
            },
        }

        svg = plot.render(record)

        self.assertTrue(svg.startswith("<svg"))
        self.assertIn("fixture", svg)
        self.assertIn("2.50%", svg)
        self.assertIn("75.0%", svg)
        self.assertIn("12 Short outcomes", svg)
        self.assertTrue(svg.endswith("</svg>\n"))


if __name__ == "__main__":
    unittest.main()
