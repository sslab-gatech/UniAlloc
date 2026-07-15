#!/usr/bin/env python3
"""Tests for the complete std_bench allocator campaign exporter."""

from __future__ import annotations

import csv
import hashlib
import json
import shutil
import struct
import subprocess
import tempfile
import unittest
from pathlib import Path


REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
SCRIPT = Path(__file__).with_name("plot_std_bench_allocator_full.py")

ALLOCATORS = (
    "unialloc",
    "ptmalloc",
    "jemalloc",
    "mimalloc",
    "tcmalloc",
    "snmalloc",
    "scudo",
)
FEATURES = {
    "unialloc": "bench_ourself",
    "ptmalloc": "bench_ptmalloc",
    "jemalloc": "bench_jemalloc",
    "mimalloc": "bench_mimalloc",
    "tcmalloc": "bench_tcmalloc",
    "snmalloc": "bench_snmalloc",
    "scudo": "bench_scudo",
}
FAMILY_COUNTS = {
    "binary_heap": 6,
    "btree": 100,
    "linked_list": 9,
    "slice": 74,
    "str": 129,
    "string": 17,
    "vec": 118,
    "vec_deque": 15,
}
FACTORS = {
    "unialloc": 1.0,
    "ptmalloc": 1.1,
    "jemalloc": 0.9,
    "mimalloc": 0.95,
    "tcmalloc": 1.05,
    "snmalloc": 0.8,
    "scudo": 1.25,
}


def sha256_file(file: Path) -> str:
    digest = hashlib.sha256()
    digest.update(file.read_bytes())
    return digest.hexdigest()


def png_dimensions(file: Path) -> tuple[int, int]:
    payload = file.read_bytes()
    if payload[:8] != b"\x89PNG\r\n\x1a\n":
        raise AssertionError("missing PNG signature")
    return struct.unpack(">II", payload[16:24])


class FullStdBenchPlotterTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        uv = shutil.which("uv")
        if uv is None:
            raise unittest.SkipTest("uv is required for the PEP 723 plotter")
        cls.uv = uv

    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary_directory.name)
        self.summary = self.root / "raw" / "summary.json"
        self.records = self.root / "raw" / "records.jsonl"
        self.result = self.root / "tracked" / "result.json"
        self.long_csv = self.root / "figures" / "all-cells.csv"
        self.distribution_svg = self.root / "figures" / "distribution.svg"
        self.distribution_png = self.root / "figures" / "distribution.png"
        self.heatmap_svg = self.root / "figures" / "heatmap.svg"
        self.heatmap_png = self.root / "figures" / "heatmap.png"
        self.manifest = self.root / "figures" / "manifest.json"
        self.write_fixture()

    def tearDown(self) -> None:
        self.temporary_directory.cleanup()

    @staticmethod
    def benchmarks() -> list[str]:
        return [
            f"{family}::bench_{index:03d}"
            for family, count in FAMILY_COUNTS.items()
            for index in range(count)
        ]

    def fixture(self) -> tuple[dict[str, object], list[dict[str, object]]]:
        benchmarks = self.benchmarks()
        records: list[dict[str, object]] = []
        complete_cells: list[dict[str, object]] = []
        censored_cells: list[dict[str, object]] = []
        censor_plan = {
            ("scudo", "btree::bench_099"): ("warmup", 0),
            ("ptmalloc", "str::bench_128"): ("measured", 2),
        }
        for benchmark_index, benchmark in enumerate(benchmarks):
            if benchmark == "string::bench_016":
                base = 0.0
            elif benchmark == "slice::bench_000":
                base = 50.0
            else:
                base = 200.0 + benchmark_index
            for allocator in ALLOCATORS:
                factor = FACTORS[allocator]
                if allocator == "ptmalloc" and benchmark == "binary_heap::bench_000":
                    factor = 0.5
                if allocator == "ptmalloc" and benchmark == "vec_deque::bench_014":
                    factor = 2.0
                timeout = censor_plan.get((allocator, benchmark))
                warmup: dict[str, object] = {
                    "schema_version": 2,
                    "allocator": allocator,
                    "feature": FEATURES[allocator],
                    "benchmark": benchmark,
                    "phase": "warmup",
                    "round": 0,
                    "valid": timeout != ("warmup", 0),
                    "timed_out": timeout == ("warmup", 0),
                    "status": (
                        "timeout_censored" if timeout == ("warmup", 0) else "valid"
                    ),
                    "timeout_seconds": 10,
                    "cpu": 20,
                    "numa_node": 0,
                    "binary_sha256": f"{allocator}-binary-sha256",
                    "glibc_tunables_present": False,
                    "scudo_identity_marker_count": 1 if allocator == "scudo" else 0,
                    "scudo_runtime_library": (
                        "/tmp/libscudo.so" if allocator == "scudo" else None
                    ),
                    "reported_benchmark": (
                        benchmark if timeout != ("warmup", 0) else None
                    ),
                    "exit_code": 0 if timeout != ("warmup", 0) else -15,
                    "time_exit_status": 0 if timeout != ("warmup", 0) else None,
                    "stdout_path": f"runs/warmup/{allocator}/{benchmark}.stdout",
                }
                if warmup["valid"]:
                    warmup["ns_per_iter"] = base * factor * 1.1
                records.append(warmup)
                values: list[float] = []
                if timeout != ("warmup", 0):
                    for measured_round, jitter in enumerate((0.9, 1.0, 1.2), start=1):
                        if timeout == ("measured", measured_round):
                            records.append(
                                {
                                    **warmup,
                                    "phase": "measured",
                                    "round": measured_round,
                                    "valid": False,
                                    "timed_out": True,
                                    "status": "timeout_censored",
                                    "ns_per_iter": None,
                                    "stdout_path": (
                                        f"runs/measured/{measured_round}/{allocator}/"
                                        f"{benchmark}.stdout"
                                    ),
                                }
                            )
                            break
                        value = base * factor * jitter
                        values.append(value)
                        records.append(
                            {
                                **warmup,
                                "phase": "measured",
                                "round": measured_round,
                                "valid": True,
                                "timed_out": False,
                                "status": "valid",
                                "ns_per_iter": value,
                                "stdout_path": (
                                    f"runs/measured/{measured_round}/{allocator}/"
                                    f"{benchmark}.stdout"
                                ),
                            }
                        )
                if timeout is None:
                    complete_cells.append(
                        {
                            "allocator": allocator,
                            "feature": FEATURES[allocator],
                            "benchmark": benchmark,
                            "status": "complete",
                            "samples": 3,
                            "median_ns_per_iter": values[1],
                        }
                    )
                else:
                    censored_summary = {
                        "allocator": allocator,
                        "feature": FEATURES[allocator],
                        "benchmark": benchmark,
                        "status": "censored",
                        "phase": timeout[0],
                        "round": timeout[1],
                    }
                    censored_cells.append(censored_summary)
                    complete_cells.append(
                        {
                            **censored_summary,
                            "ns_per_iter_samples": values,
                            "median_ns_per_iter": None,
                        }
                    )

        comparable = [
            benchmark
            for benchmark in benchmarks
            if benchmark
            not in {
                "btree::bench_099",
                "str::bench_128",
                "string::bench_016",
            }
        ]
        robust = [
            benchmark for benchmark in comparable if benchmark != "slice::bench_000"
        ]
        summary: dict[str, object] = {
            "schema_version": 2,
            "generated_utc": "2026-07-14T21:00:00Z",
            "diagnostic_label": "synthetic full std_bench diagnostic",
            "claim_grade": False,
            "methodology": {
                "allocators": list(ALLOCATORS),
                "benchmarks": benchmarks,
                "warmup_fresh_processes_per_cell": 1,
                "measured_fresh_processes_per_complete_cell": 3,
                "timeout_seconds_per_process": 10,
            },
            "cells": complete_cells,
            "censored_cells": censored_cells,
            "comparison_selection": {
                "complete_all_allocator_benchmarks": [
                    benchmark
                    for benchmark in benchmarks
                    if benchmark not in {"btree::bench_099", "str::bench_128"}
                ],
                "comparable_benchmarks": comparable,
                "comparable_count": len(comparable),
            },
            "robustness_selection": {
                "threshold_ns_per_iter": 100.0,
                "selected_benchmarks": robust,
                "selected_count": len(robust),
            },
        }
        return summary, records

    def write_fixture(self, *, summary_transform=None, records_transform=None) -> None:
        summary, records = self.fixture()
        if summary_transform is not None:
            summary_transform(summary)
        if records_transform is not None:
            records_transform(records)
        self.summary.parent.mkdir(parents=True, exist_ok=True)
        self.summary.write_text(
            json.dumps(summary, indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )
        self.records.write_text(
            "".join(json.dumps(record, sort_keys=True) + "\n" for record in records),
            encoding="utf-8",
        )

    def command(self) -> list[str]:
        return [
            self.uv,
            "run",
            str(SCRIPT),
            "--summary",
            str(self.summary),
            "--records",
            str(self.records),
            "--result-json",
            str(self.result),
            "--long-csv",
            str(self.long_csv),
            "--distribution-svg",
            str(self.distribution_svg),
            "--distribution-png",
            str(self.distribution_png),
            "--heatmap-svg",
            str(self.heatmap_svg),
            "--heatmap-png",
            str(self.heatmap_png),
            "--manifest",
            str(self.manifest),
        ]

    def run_plotter(self, *, check: bool = True) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            self.command(),
            cwd=REPOSITORY_ROOT,
            check=check,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )

    def test_exports_complete_matrix_extrema_distributions_and_figures(self) -> None:
        completed = self.run_plotter()
        expected = [
            self.result,
            self.long_csv,
            self.distribution_svg,
            self.distribution_png,
            self.heatmap_svg,
            self.heatmap_png,
            self.manifest,
        ]
        self.assertEqual([str(file) for file in expected], completed.stdout.splitlines())
        self.assertTrue(all(file.is_file() for file in expected))

        result = json.loads(self.result.read_text(encoding="utf-8"))
        self.assertTrue(result["success"])
        self.assertEqual(468, result["inventory"]["benchmark_count"])
        self.assertEqual(FAMILY_COUNTS, result["inventory"]["family_counts"])
        self.assertEqual(3274, result["matrix"]["complete_cells"])
        self.assertEqual(2, result["matrix"]["censored_cells"])
        self.assertEqual(465, result["selection"]["comparable_count"])
        self.assertEqual(464, result["selection"]["robust_count"])
        self.assertEqual(2, len(result["censored_cells"]))

        ptmalloc = next(
            row for row in result["distributions"] if row["allocator"] == "ptmalloc"
        )
        raw = ptmalloc["raw"]
        self.assertEqual("binary_heap::bench_000", raw["minimum"]["benchmark"])
        self.assertAlmostEqual(0.5, raw["minimum"]["ratio_vs_unialloc"])
        self.assertEqual("vec_deque::bench_014", raw["maximum"]["benchmark"])
        self.assertAlmostEqual(2.0, raw["maximum"]["ratio_vs_unialloc"])
        self.assertEqual({"p05", "p25", "p50", "p75", "p95"}, set(raw["quantiles"]))
        self.assertIn("family_balanced_geomean", raw)
        self.assertEqual(464, ptmalloc["robust"]["benchmark_count"])
        self.assertEqual(3, len(raw["minimum"]["allocator_samples_ns_per_iter"]))
        self.assertEqual(3, len(raw["minimum"]["unialloc_samples_ns_per_iter"]))

        with self.long_csv.open(encoding="utf-8", newline="") as handle:
            rows = list(csv.DictReader(handle))
        self.assertEqual(7 * 468, len({(r["allocator"], r["benchmark"]) for r in rows}))
        self.assertTrue(any(r["cell_status"] == "censored" for r in rows))
        self.assertTrue(any(r["robust_selected"] == "false" for r in rows))

        distribution_svg = self.distribution_svg.read_text(encoding="utf-8")
        self.assertIn("Full Rust std_bench distribution", distribution_svg)
        self.assertIn("p05–p95", distribution_svg)
        heatmap_svg = self.heatmap_svg.read_text(encoding="utf-8")
        self.assertIn("468-leaf robust ratio map", heatmap_svg)
        self.assertEqual((2400, 1350), png_dimensions(self.distribution_png))
        self.assertEqual((2100, 4800), png_dimensions(self.heatmap_png))

    def test_manifest_hashes_every_source_and_generated_artifact(self) -> None:
        self.run_plotter()
        manifest = json.loads(self.manifest.read_text(encoding="utf-8"))
        self.assertEqual(
            {
                "allocator_feature_variants": 7,
                "benchmarks": 468,
                "complete_cells": 3274,
                "censored_cells": 2,
                "comparable_benchmarks": 465,
                "robust_benchmarks": 464,
            },
            manifest["matrix"],
        )
        entries = manifest["source_artifacts"] + manifest["generated_artifacts"]
        by_file = {entry["path"]: entry for entry in entries}
        for file in (
            self.summary,
            self.records,
            self.result,
            self.long_csv,
            self.distribution_svg,
            self.distribution_png,
            self.heatmap_svg,
            self.heatmap_png,
        ):
            entry = by_file[str(file.resolve())]
            self.assertEqual(sha256_file(file), entry["sha256"])
            self.assertEqual(file.stat().st_size, entry["bytes"])

    def test_regeneration_is_byte_deterministic(self) -> None:
        self.run_plotter()
        files = (
            self.result,
            self.long_csv,
            self.distribution_svg,
            self.distribution_png,
            self.heatmap_svg,
            self.heatmap_png,
            self.manifest,
        )
        before = {file: sha256_file(file) for file in files}
        self.run_plotter()
        self.assertEqual(before, {file: sha256_file(file) for file in files})

    def test_fails_closed_on_noncanonical_inventory(self) -> None:
        def corrupt(summary: dict[str, object]) -> None:
            methodology = summary["methodology"]
            assert isinstance(methodology, dict)
            benchmarks = methodology["benchmarks"]
            assert isinstance(benchmarks, list)
            benchmarks.pop()

        self.write_fixture(summary_transform=corrupt)
        completed = self.run_plotter(check=False)
        self.assertNotEqual(0, completed.returncode)
        self.assertIn("canonical inventory must contain 468", completed.stderr)
        self.assertFalse(self.result.exists())

    def test_fails_closed_on_records_after_terminal_timeout(self) -> None:
        def corrupt(records: list[dict[str, object]]) -> None:
            template = next(
                record
                for record in records
                if record["allocator"] == "scudo"
                and record["benchmark"] == "btree::bench_099"
            )
            records.append(
                {
                    **template,
                    "phase": "measured",
                    "round": 1,
                    "valid": True,
                    "timed_out": False,
                    "status": "valid",
                    "ns_per_iter": 300.0,
                    "exit_code": 0,
                    "time_exit_status": 0,
                    "reported_benchmark": "btree::bench_099",
                    "stdout_path": "runs/measured/1/scudo/btree::bench_099.stdout",
                }
            )

        self.write_fixture(records_transform=corrupt)
        completed = self.run_plotter(check=False)
        self.assertNotEqual(0, completed.returncode)
        self.assertIn("after terminal timeout", completed.stderr)
        self.assertFalse(self.result.exists())

    def test_fails_closed_on_lost_scudo_runtime_identity(self) -> None:
        def corrupt(records: list[dict[str, object]]) -> None:
            record = next(
                item
                for item in records
                if item["allocator"] == "scudo" and item["valid"] is True
            )
            record["scudo_identity_marker_count"] = 0

        self.write_fixture(records_transform=corrupt)
        completed = self.run_plotter(check=False)
        self.assertNotEqual(0, completed.returncode)
        self.assertIn("Scudo marker", completed.stderr)
        self.assertFalse(self.result.exists())

    def test_fails_closed_on_summary_selection_drift(self) -> None:
        def corrupt(summary: dict[str, object]) -> None:
            selection = summary["robustness_selection"]
            assert isinstance(selection, dict)
            selected = selection["selected_benchmarks"]
            assert isinstance(selected, list)
            selected.pop()

        self.write_fixture(summary_transform=corrupt)
        completed = self.run_plotter(check=False)
        self.assertNotEqual(0, completed.returncode)
        self.assertIn("robustness_selection", completed.stderr)
        self.assertFalse(self.result.exists())


if __name__ == "__main__":
    unittest.main()
