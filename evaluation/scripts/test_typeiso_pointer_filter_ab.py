#!/usr/bin/env python3
"""Regression tests for the Type Isolation pointer-filter A/B runner."""

from __future__ import annotations

import importlib.util
import os
import pathlib
import tempfile
import unittest


ROOT = pathlib.Path(__file__).resolve().parents[2]
RUNNER_PATH = ROOT / "evaluation" / "scripts" / "run_typeiso_pointer_filter_ab.py"
SPEC = importlib.util.spec_from_file_location("typeiso_pointer_filter_ab", RUNNER_PATH)
assert SPEC is not None and SPEC.loader is not None
runner = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(runner)


class TypeIsoPointerFilterABTests(unittest.TestCase):
    def test_parse_cpu_list_requires_two_distinct_non_negative_cpus(self) -> None:
        self.assertEqual(runner.parse_cpu_list("22,23"), (22, 23))
        for invalid in ("22", "22,22", "-1,2", "a,b", "1,2,3"):
            with self.subTest(invalid=invalid), self.assertRaises((ValueError, TypeError)):
                runner.parse_cpu_list(invalid)

    def test_variant_order_counterbalances_adjacent_rounds(self) -> None:
        self.assertEqual(runner.variant_order(1), ("baseline", "candidate"))
        self.assertEqual(runner.variant_order(2), ("candidate", "baseline"))
        self.assertEqual(runner.variant_order(3), ("baseline", "candidate"))

    def test_clean_environment_removes_allocator_and_toolchain_overrides(self) -> None:
        names = {
            "RUSTFLAGS": "-C debuginfo=2",
            "RUSTUP_TOOLCHAIN": "stable",
            "LD_PRELOAD": "/tmp/liballocator.so",
            "MIMALLOC_VERBOSE": "1",
        }
        prior = {name: os.environ.get(name) for name in names}
        try:
            os.environ.update(names)
            clean = runner.clean_environment()
        finally:
            for name, value in prior.items():
                if value is None:
                    os.environ.pop(name, None)
                else:
                    os.environ[name] = value
        for name in names:
            self.assertNotIn(name, clean)
        self.assertIn("PATH", clean)

    def test_parse_probe_rejects_wrong_source_or_live_state(self) -> None:
        valid = (
            '{"source":"typeiso_pointer_filter_bench",'
            '"final_count":0,"query_hits":0}\n'
        )
        self.assertEqual(
            runner.parse_probe(valid, "typeiso_pointer_filter_bench")["final_count"],
            0,
        )
        with self.assertRaises(RuntimeError):
            runner.parse_probe(valid, "typeiso_allocator_hotpath")
        with self.assertRaises(RuntimeError):
            runner.parse_probe(
                '{"source":"typeiso_pointer_filter_bench","final_count":1}\n',
                "typeiso_pointer_filter_bench",
            )

    def test_panel_summary_reports_min_median_max_and_paired_wins(self) -> None:
        rows = []
        for round_number, (baseline, candidate) in enumerate(
            ((10.0, 8.0), (12.0, 9.0), (11.0, 10.0)), start=1
        ):
            rows.extend(
                [
                    {
                        "variant": "baseline",
                        "round": round_number,
                        "scenario": "mixed",
                        "ns": baseline,
                    },
                    {
                        "variant": "candidate",
                        "round": round_number,
                        "scenario": "mixed",
                        "ns": candidate,
                    },
                ]
            )
        summary = runner.summarize_panel(rows, "ns", ("mixed",))
        mixed = summary["scenarios"]["mixed"]
        self.assertEqual(mixed["baseline"]["min"], 10.0)
        self.assertEqual(mixed["baseline"]["median"], 11.0)
        self.assertEqual(mixed["baseline"]["max"], 12.0)
        self.assertEqual(mixed["candidate"]["median"], 9.0)
        self.assertEqual(mixed["paired_candidate_wins"], 3)
        self.assertAlmostEqual(mixed["runtime_delta_percent"], -18.18181818)

    def test_render_svg_contains_both_panels(self) -> None:
        report = {
            "protocol": {"rounds": 3},
            "pointer_filter": {
                "scenarios": {"mixed": {"runtime_delta_percent": -50.0}}
            },
            "allocator_hotpath": {
                "scenarios": {"raw": {"runtime_delta_percent": 0.25}}
            },
        }
        with tempfile.TemporaryDirectory() as temporary:
            output = pathlib.Path(temporary) / "result.svg"
            runner.render_svg(report, output)
            svg = output.read_text(encoding="utf-8")
        self.assertIn("filter / mixed", svg)
        self.assertIn("allocator / raw", svg)
        self.assertIn("-50.00%", svg)
        self.assertIn("+0.25%", svg)
        self.assertIn("3 counterbalanced process runs", svg)


if __name__ == "__main__":
    unittest.main()
