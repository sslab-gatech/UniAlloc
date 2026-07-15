#!/usr/bin/env python3
"""Tests for reclaim-check overhead summarization."""

from __future__ import annotations

import importlib.util
import pathlib
import unittest


ROOT = pathlib.Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "evaluation/scripts/summarize_reclaim_checks_overhead.py"
spec = importlib.util.spec_from_file_location("reclaim_overhead", SCRIPT)
assert spec is not None and spec.loader is not None
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


def matrix(app: str, *, output: str = "a" * 64) -> dict[str, object]:
    def row(variant: str, wall: float, rss: int) -> dict[str, object]:
        return {
            "app": app,
            "variant": variant,
            "sample_count": 7,
            "work_amount": 100,
            "work_unit": "bytes",
            "output_sha256": output,
            "median_wall_seconds": wall,
            "wall_mad_seconds": 0.01,
            "median_peak_rss_kib": rss,
            "rss_mad_kib": 0,
        }

    return {
        "success": True,
        "summaries": [
            row("unialloc_no_optional", 1.0, 1000),
            row("unialloc_reclaim_checks", 1.02, 1010),
        ],
    }


class ReclaimOverheadSummaryTests(unittest.TestCase):
    def test_computes_wall_and_rss_deltas(self) -> None:
        result = module.compare(matrix("ripgrep"), "ripgrep")
        self.assertAlmostEqual(result["delta"]["median_wall_percent"], 2.0)
        self.assertEqual(result["delta"]["median_peak_rss_kib"], 10.0)
        self.assertAlmostEqual(result["delta"]["median_peak_rss_percent"], 1.0)

    def test_rejects_different_outputs(self) -> None:
        value = matrix("oxipng")
        value["summaries"][1]["output_sha256"] = "b" * 64
        with self.assertRaisesRegex(module.OverheadError, "outputs differ"):
            module.compare(value, "oxipng")


if __name__ == "__main__":
    unittest.main()
