#!/usr/bin/env python3

from __future__ import annotations

import csv
import hashlib
import json
import shutil
import statistics
import struct
import subprocess
import tempfile
import unittest
from pathlib import Path


REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
SCRIPT_PATH = (
    REPOSITORY_ROOT / "evaluation" / "scripts" / "plot_std_bench_allocator_variants.py"
)

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
FACTORS = {
    "unialloc": 1.0,
    "ptmalloc": 1.10,
    "jemalloc": 0.90,
    "mimalloc": 0.95,
    "tcmalloc": 1.05,
    "snmalloc": 0.85,
    "scudo": 1.25,
}
BENCHMARKS = (
    "linked_list::bench_push_back_pop_back",
    "slice::random_inserts",
    "vec_deque::bench_grow_1025",
)
BASE_MEDIANS = {
    BENCHMARKS[0]: 80.0,
    BENCHMARKS[1]: 200.0,
    BENCHMARKS[2]: 400.0,
}


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def read_csv(path: Path) -> list[dict[str, str]]:
    with path.open(encoding="utf-8", newline="") as handle:
        return list(csv.DictReader(handle))


def png_dimensions(path: Path) -> tuple[int, int]:
    payload = path.read_bytes()
    if payload[:8] != b"\x89PNG\r\n\x1a\n":
        raise AssertionError("missing PNG signature")
    return struct.unpack(">II", payload[16:24])


class StdBenchAllocatorVariantPlotTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        uv = shutil.which("uv")
        if uv is None:
            raise unittest.SkipTest("uv is required to run the PEP 723 plotter")
        cls.uv = uv

    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary_directory.name)
        self.summary_path = self.root / "raw" / "summary.json"
        self.records_path = self.root / "raw" / "records.jsonl"
        self.result_path = self.root / "tracked" / "result.json"
        self.csv_path = self.root / "figures" / "data.csv"
        self.svg_path = self.root / "figures" / "figure.svg"
        self.png_path = self.root / "figures" / "figure.png"
        self.manifest_path = self.root / "figures" / "manifest.json"
        self.write_fixture()

    def tearDown(self) -> None:
        self.temporary_directory.cleanup()

    def fixture_payloads(self) -> tuple[dict[str, object], list[dict[str, object]]]:
        records: list[dict[str, object]] = []
        cells: list[dict[str, object]] = []
        for benchmark in BENCHMARKS:
            base = BASE_MEDIANS[benchmark]
            for allocator in ALLOCATORS:
                target_median = base * FACTORS[allocator]
                measured_values = (
                    target_median * 0.9,
                    target_median,
                    target_median * 1.2,
                )
                records.append(
                    {
                        "allocator": allocator,
                        "benchmark": benchmark,
                        "feature": FEATURES[allocator],
                        "phase": "warmup",
                        "round": 0,
                        "valid": True,
                        "ns_per_iter": target_median * 1.5,
                        "stdout_path": f"warmup/{allocator}/{benchmark}.stdout",
                    }
                )
                for measured_round, value in enumerate(measured_values, start=1):
                    records.append(
                        {
                            "allocator": allocator,
                            "benchmark": benchmark,
                            "feature": FEATURES[allocator],
                            "phase": "measured",
                            "round": measured_round,
                            "valid": True,
                            "ns_per_iter": value,
                            "stdout_path": (
                                f"measured/{measured_round}/{allocator}/"
                                f"{benchmark}.stdout"
                            ),
                        }
                    )
                cells.append(
                    {
                        "allocator": allocator,
                        "benchmark": benchmark,
                        "feature": FEATURES[allocator],
                        "samples": 3,
                        "median_ns_per_iter": statistics.median(measured_values),
                    }
                )

        selected = [BENCHMARKS[1], BENCHMARKS[2]]
        summary: dict[str, object] = {
            "schema_version": 1,
            "generated_utc": "2026-07-14T18:30:00Z",
            "diagnostic_label": "synthetic current-toolchain diagnostic",
            "claim_grade": False,
            "methodology": {
                "allocators": list(ALLOCATORS),
                "benchmarks": list(BENCHMARKS),
                "warmup_fresh_processes_per_cell": 1,
                "measured_fresh_processes_per_cell": 3,
            },
            "cells": cells,
            "aggregates": [],
            "robustness_selection": {
                "rule": "retain a benchmark only when every allocator's median is >= 100 ns/iter",
                "threshold_ns_per_iter": 100.0,
                "selected_benchmarks": selected,
                "selected_count": len(selected),
            },
        }
        return summary, records

    def write_fixture(
        self,
        *,
        summary_transform=None,
        records_transform=None,
    ) -> None:
        summary, records = self.fixture_payloads()
        if summary_transform is not None:
            summary_transform(summary)
        if records_transform is not None:
            records_transform(records)
        self.summary_path.parent.mkdir(parents=True, exist_ok=True)
        self.summary_path.write_text(
            json.dumps(summary, indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )
        self.records_path.write_text(
            "".join(json.dumps(record, sort_keys=True) + "\n" for record in records),
            encoding="utf-8",
        )

    def command(self) -> list[str]:
        return [
            self.uv,
            "run",
            str(SCRIPT_PATH),
            "--summary",
            str(self.summary_path),
            "--records",
            str(self.records_path),
            "--result-json",
            str(self.result_path),
            "--long-csv",
            str(self.csv_path),
            "--figure-svg",
            str(self.svg_path),
            "--figure-png",
            str(self.png_path),
            "--manifest",
            str(self.manifest_path),
        ]

    def run_generator(self, *, check: bool = True) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            self.command(),
            cwd=REPOSITORY_ROOT,
            check=check,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )

    def test_exports_medians_dispersion_raw_points_and_two_panel_figure(self) -> None:
        completed = self.run_generator()
        self.assertEqual(
            [
                str(self.result_path),
                str(self.csv_path),
                str(self.svg_path),
                str(self.png_path),
                str(self.manifest_path),
            ],
            completed.stdout.splitlines(),
        )
        for path in (
            self.result_path,
            self.csv_path,
            self.svg_path,
            self.png_path,
            self.manifest_path,
        ):
            self.assertTrue(path.is_file(), path)

        result = json.loads(self.result_path.read_text(encoding="utf-8"))
        self.assertTrue(result["success"])
        self.assertEqual(3, result["methodology"]["measured_fresh_processes_per_cell"])
        self.assertEqual(7, len(result["variants"]))
        self.assertEqual(21, len(result["cells"]))
        self.assertEqual(
            [FEATURES[allocator] for allocator in ALLOCATORS],
            [variant["id"] for variant in result["variants"]],
        )

        target_cell = next(
            cell
            for cell in result["cells"]
            if cell["allocator"] == "jemalloc" and cell["benchmark"] == BENCHMARKS[0]
        )
        self.assertEqual([1, 2, 3], target_cell["measured_rounds"])
        self.assertEqual(3, len(target_cell["sample_ns_per_iter"]))
        self.assertAlmostEqual(72.0, target_cell["median_ns_per_iter"])
        self.assertAlmostEqual(7.2, target_cell["mad_ns_per_iter"])
        self.assertAlmostEqual(64.8, target_cell["min_ns_per_iter"])
        self.assertAlmostEqual(86.4, target_cell["max_ns_per_iter"])
        self.assertAlmostEqual(0.9, target_cell["median_ratio_vs_unialloc"])

        for aggregate in result["aggregates"]:
            expected = FACTORS[aggregate["allocator"]]
            self.assertAlmostEqual(
                expected, aggregate["geomean_median_ratio_vs_unialloc"]
            )
            self.assertAlmostEqual(
                expected, aggregate["robust_geomean_median_ratio_vs_unialloc"]
            )

        rows = read_csv(self.csv_path)
        self.assertEqual(7 * 3 * 3, len(rows))
        grouped: dict[tuple[str, str], list[dict[str, str]]] = {}
        for row in rows:
            grouped.setdefault((row["allocator"], row["benchmark"]), []).append(row)
        self.assertEqual(21, len(grouped))
        self.assertTrue(all(len(cell_rows) == 3 for cell_rows in grouped.values()))
        self.assertEqual(
            ["1", "2", "3"],
            [row["sample_index"] for row in grouped[("scudo", BENCHMARKS[2])]],
        )

        svg = self.svg_path.read_text(encoding="utf-8")
        self.assertIn("Rust std_bench allocator features", svg)
        self.assertIn("Aggregate across 3 leaves", svg)
        self.assertIn("Per-leaf cell medians", svg)
        self.assertFalse(any(line.endswith(" ") for line in svg.splitlines()))
        self.assertEqual((2700, 1500), png_dimensions(self.png_path))

    def test_manifest_hashes_sources_and_all_generated_artifacts(self) -> None:
        self.run_generator()
        manifest = json.loads(self.manifest_path.read_text(encoding="utf-8"))
        self.assertEqual(
            {
                "allocator_feature_variants": 7,
                "benchmarks": 3,
                "measured_fresh_processes_per_cell": 3,
                "measured_processes": 63,
            },
            manifest["matrix"],
        )
        entries = manifest["source_artifacts"] + manifest["generated_artifacts"]
        by_path = {entry["path"]: entry for entry in entries}
        for path in (
            self.summary_path,
            self.records_path,
            self.result_path,
            self.csv_path,
            self.svg_path,
            self.png_path,
        ):
            entry = by_path[str(path.resolve())]
            self.assertEqual(path.stat().st_size, entry["bytes"])
            self.assertEqual(sha256_file(path), entry["sha256"])

    def test_regeneration_is_byte_deterministic(self) -> None:
        self.run_generator()
        paths = (
            self.result_path,
            self.csv_path,
            self.svg_path,
            self.png_path,
            self.manifest_path,
        )
        first_hashes = {path: sha256_file(path) for path in paths}
        self.run_generator()
        self.assertEqual(first_hashes, {path: sha256_file(path) for path in paths})

    def test_fails_closed_when_a_cell_has_fewer_than_three_processes(self) -> None:
        def remove_one_record(records: list[dict[str, object]]) -> None:
            for index, record in enumerate(records):
                if (
                    record["phase"] == "measured"
                    and record["allocator"] == "scudo"
                    and record["benchmark"] == BENCHMARKS[2]
                    and record["round"] == 3
                ):
                    records.pop(index)
                    return
            raise AssertionError("fixture record not found")

        self.write_fixture(records_transform=remove_one_record)
        completed = self.run_generator(check=False)
        self.assertNotEqual(0, completed.returncode)
        self.assertIn(
            "must contain exactly 3 measured fresh-process records", completed.stderr
        )
        self.assertFalse(self.result_path.exists())

    def test_fails_closed_when_summary_claims_another_repetition_count(self) -> None:
        def change_repetitions(summary: dict[str, object]) -> None:
            methodology = summary["methodology"]
            assert isinstance(methodology, dict)
            methodology["measured_fresh_processes_per_cell"] = 5

        self.write_fixture(summary_transform=change_repetitions)
        completed = self.run_generator(check=False)
        self.assertNotEqual(0, completed.returncode)
        self.assertIn(
            "measured_fresh_processes_per_cell must equal 3", completed.stderr
        )
        self.assertFalse(self.result_path.exists())


if __name__ == "__main__":
    unittest.main()
