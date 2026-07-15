#!/usr/bin/env python3
"""Focused unit tests for the std_bench allocator-variant campaign."""

from __future__ import annotations

import importlib.util
import json
import sys
import tempfile
import unittest
from unittest import mock
from pathlib import Path


SCRIPT = Path(__file__).with_name("run_std_bench_allocator_variants.py")
SPEC = importlib.util.spec_from_file_location(
    "run_std_bench_allocator_variants", SCRIPT
)
assert SPEC is not None and SPEC.loader is not None
campaign = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = campaign
SPEC.loader.exec_module(campaign)


class CampaignHelpersTest(unittest.TestCase):
    def test_variants_are_exactly_one_allocator_selector(self) -> None:
        self.assertEqual(len(campaign.VARIANTS), 7)
        self.assertEqual(len({item.allocator for item in campaign.VARIANTS}), 7)
        self.assertEqual(
            {item.feature for item in campaign.VARIANTS}, campaign.ALLOCATOR_SELECTORS
        )
        self.assertIn("bench_gperftools_legacy", campaign.ALL_ALLOCATOR_SELECTORS)
        self.assertNotIn("bench_gperftools_legacy", campaign.ALLOCATOR_SELECTORS)

    def test_parse_cargo_executable_uses_bench_artifact(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            binary = Path(directory) / "std_bench-deadbeef"
            binary.write_text("#!/bin/sh\n")
            binary.chmod(0o755)
            rows = [
                {"reason": "compiler-message", "message": {}},
                {
                    "reason": "compiler-artifact",
                    "target": {"name": "std_bench", "kind": ["bench"]},
                    "executable": str(binary),
                },
            ]
            stdout = ("\n".join(json.dumps(row) for row in rows) + "\n").encode()
            self.assertEqual(campaign.parse_cargo_executable(stdout), binary.resolve())

    def test_scudo_marker_requires_exact_line(self) -> None:
        marker = campaign.SCUDO_MARKER.encode()
        self.assertEqual(campaign.scudo_marker_count(marker), 1)
        self.assertEqual(campaign.scudo_marker_count(b"prefix " + marker), 0)
        self.assertEqual(campaign.scudo_marker_count(marker + marker), 2)

    def test_google_tcmalloc_marker_requires_exact_line(self) -> None:
        marker = campaign.GOOGLE_TCMALLOC_MARKER.encode()
        self.assertEqual(campaign.google_tcmalloc_marker_count(marker), 1)
        self.assertEqual(
            campaign.google_tcmalloc_marker_count(b"prefix " + marker), 0
        )
        self.assertEqual(
            campaign.google_tcmalloc_marker_count(marker + marker), 2
        )

    def test_modern_tcmalloc_prefix_is_canonical_runner_input(self) -> None:
        with mock.patch.dict(
            "os.environ",
            {"UNIALLOC_GOOGLE_TCMALLOC_PREFIX": "/tmp/google-tcmalloc"},
            clear=True,
        ):
            self.assertEqual(
                campaign.default_tcmalloc_dir(),
                Path("/tmp/google-tcmalloc/lib"),
            )

    def test_tcmalloc_environment_records_prefix_and_exact_library(self) -> None:
        variant = next(item for item in campaign.VARIANTS if item.allocator == "tcmalloc")
        lib_dir = Path("/tmp/google-tcmalloc/lib")
        env = campaign.build_environment(variant, Path("/tmp/target"), lib_dir)
        self.assertEqual(env["UNIALLOC_GOOGLE_TCMALLOC_PREFIX"], "/tmp/google-tcmalloc")
        self.assertEqual(
            env["UNIALLOC_GOOGLE_TCMALLOC_LIBRARY"],
            f"/tmp/google-tcmalloc/lib/{campaign.GOOGLE_TCMALLOC_LIBRARY}",
        )

    def test_summary_uses_three_sample_cell_medians_and_ratio_geomean(self) -> None:
        records = []
        for benchmark_index, benchmark in enumerate(campaign.BENCHMARKS):
            baseline = 100.0 + benchmark_index
            for variant_index, variant in enumerate(campaign.VARIANTS):
                factor = 1.0 + variant_index / 10.0
                for round_index, jitter in enumerate((-10.0, 0.0, 30.0), start=1):
                    records.append(
                        {
                            "phase": "measured",
                            "round": round_index,
                            "allocator": variant.allocator,
                            "feature": variant.feature,
                            "benchmark": benchmark,
                            "valid": True,
                            "ns_per_iter": baseline * factor + jitter,
                            "peak_rss_kib": 1024 + variant_index,
                            "start_loadavg": "1.0 0.5 0.2 1/1 1",
                            "start_cpu_frequency_khz": 3000000,
                            "end_cpu_frequency_khz": 3000000,
                            "involuntary_context_switches": 0,
                            "started_utc": "2026-07-14T00:00:00+00:00",
                            "finished_utc": "2026-07-14T00:00:01+00:00",
                        }
                    )
        with tempfile.TemporaryDirectory() as directory:
            summary = campaign.summarize(
                records, measured_rounds=3, output_dir=Path(directory)
            )
        first = next(
            cell
            for cell in summary["cells"]
            if cell["allocator"] == "unialloc"
            and cell["benchmark"] == campaign.BENCHMARKS[0]
        )
        self.assertEqual(first["ns_per_iter_samples"], [90.0, 100.0, 130.0])
        self.assertEqual(first["median_ns_per_iter"], 100.0)
        self.assertEqual(first["mad_ns_per_iter"], 10.0)
        self.assertEqual(first["ns_ratio_vs_unialloc"], 1.0)
        self.assertEqual(summary["record_counts"]["measured"], 168)

    def test_summary_rejects_missing_round(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaisesRegex(RuntimeError, "measured rounds"):
                campaign.summarize([], measured_rounds=3, output_dir=Path(directory))

    def test_resume_rejects_failed_or_duplicate_records(self) -> None:
        record = {
            "phase": "measured",
            "round": 1,
            "allocator": "unialloc",
            "benchmark": campaign.BENCHMARKS[0],
            "valid": False,
        }
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory)
            records = output / "records.jsonl"
            records.write_text(json.dumps(record) + "\n")
            with self.assertRaisesRegex(RuntimeError, "failed record"):
                campaign.load_completed_records(output)

            record["valid"] = True
            records.write_text((json.dumps(record) + "\n") * 2)
            with self.assertRaisesRegex(RuntimeError, "duplicate process key"):
                campaign.load_completed_records(output)


if __name__ == "__main__":
    unittest.main()
