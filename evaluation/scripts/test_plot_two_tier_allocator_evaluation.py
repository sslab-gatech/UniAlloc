#!/usr/bin/env python3

from __future__ import annotations

import csv
import hashlib
import json
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "evaluation" / "scripts" / "plot_two_tier_allocator_evaluation.py"
ALLOCATORS = (
    ("unialloc", "bench_ourself", "UniAlloc"),
    ("ptmalloc", "bench_ptmalloc", "ptmalloc"),
    ("jemalloc", "bench_jemalloc", "jemalloc"),
    ("mimalloc", "bench_mimalloc", "mimalloc"),
    ("tcmalloc", "bench_tcmalloc", "TCMalloc"),
    ("snmalloc", "bench_snmalloc", "snmalloc"),
    ("scudo", "bench_scudo", "Scudo"),
)
TARGETS = ("collections", "oxipng", "redb", "polars", "swc", "rustpython", "actix_web")
FIXED = {"oxipng", "redb", "polars"}
COMPARISONS = {
    "compiler_route": ("unialloc", "typed_plain"),
    "end_to_end": ("unialloc", "typeiso_perf"),
    "policy_increment": ("typed_plain", "typeiso_perf"),
}
EXPECTED_FILES = {
    "artifact-manifest.json",
    "macrobenchmarks.csv",
    "macrobenchmarks.png",
    "macrobenchmarks.svg",
    "microbenchmarks.csv",
    "microbenchmarks.png",
    "microbenchmarks.svg",
    "presentation-data.json",
}


def digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def write_json(path: Path, value: object) -> None:
    path.write_text(
        json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )


def build_micro(root: Path) -> tuple[Path, Path]:
    families = (
        "binary_heap",
        "btree",
        "linked_list",
        "slice",
        "str",
        "string",
        "vec",
        "vec_deque",
    )
    benchmarks = sorted(
        ["binary_heap::a", "binary_heap::b", "binary_heap::fast"]
        + [f"{family}::a" for family in families[1:]]
    )
    records_path = root / "records.jsonl"
    record_rows: list[dict[str, object]] = []
    cells: list[dict[str, object]] = []
    robust: list[str] = []
    for benchmark in benchmarks:
        base = 50.0 if benchmark.endswith("::fast") else 200.0
        if base >= 100.0:
            robust.append(benchmark)
        for allocator_index, (allocator, feature, _) in enumerate(ALLOCATORS):
            ratio = 1.0
            if allocator == "ptmalloc":
                ratio = {
                    "binary_heap::a": 2.0,
                    "binary_heap::b": 8.0,
                    "binary_heap::fast": 16.0,
                }.get(benchmark, 1.0)
            elif allocator != "unialloc":
                ratio = 1.0 + allocator_index / 20.0
            rss_ratio = (
                16.0 if allocator == "scudo" and benchmark.endswith("::fast") else ratio
            )
            ns = base * ratio
            rss = 1000.0 * rss_ratio
            for phase, round_number in (
                ("warmup", 0),
                ("measured", 1),
                ("measured", 2),
                ("measured", 3),
            ):
                record_rows.append(
                    {
                        "schema_version": 2,
                        "allocator": allocator,
                        "feature": feature,
                        "benchmark": benchmark,
                        "phase": phase,
                        "round": round_number,
                        "valid": True,
                        "timed_out": False,
                        "ns_per_iter": ns,
                        "peak_rss_kib": rss,
                    }
                )
            cells.append(
                {
                    "allocator": allocator,
                    "feature": feature,
                    "benchmark": benchmark,
                    "family": benchmark.split("::", 1)[0],
                    "status": "complete",
                    "comparable": True,
                    "robust_selected": benchmark in robust,
                    "sample_ns_per_iter": [ns, ns, ns],
                    "median_ns_per_iter": ns,
                    "ratio_vs_unialloc": ratio,
                }
            )
    records_path.write_text(
        "".join(json.dumps(row, sort_keys=True) + "\n" for row in record_rows),
        encoding="utf-8",
    )
    counts = {
        family: sum(item.startswith(f"{family}::") for item in benchmarks)
        for family in families
    }
    result = {
        "schema_version": 2,
        "source": "rust-std-bench-full-allocator-feature-variant-distribution",
        "success": True,
        "claim_grade": False,
        "variants": [
            {"allocator": allocator, "feature": feature, "label": label}
            for allocator, feature, label in ALLOCATORS
        ],
        "inventory": {
            "benchmark_count": len(benchmarks),
            "benchmarks": benchmarks,
            "family_counts": counts,
        },
        "methodology": {
            "reference_allocator": "unialloc",
            "measured_fresh_processes_per_complete_cell": 3,
            "warmup_fresh_processes_per_cell": 1,
            "direction": "lower ratio is faster",
        },
        "input_artifacts": {
            "records": {
                "bytes": records_path.stat().st_size,
                "sha256": digest(records_path),
            }
        },
        "cells": cells,
        "selection": {
            "comparable_benchmarks": benchmarks,
            "comparable_count": len(benchmarks),
            "robust_benchmarks": robust,
            "robust_count": len(robust),
            "robust_threshold_ns_per_iter": 100.0,
        },
    }
    result_path = root / "micro.json"
    write_json(result_path, result)
    return result_path, records_path


def comparison_rows(
    performance: dict[str, float], rss: dict[str, float]
) -> dict[str, dict[str, object]]:
    rows: dict[str, dict[str, object]] = {}
    for family, (reference, subject) in COMPARISONS.items():
        rows[family] = {
            "reference": reference,
            "subject": subject,
            "execution_cost_ratio_median": performance[subject]
            / performance[reference],
            "peak_rss_ratio_median": rss[subject] / rss[reference],
        }
    return rows


def build_macro(root: Path) -> Path:
    policy_perf = {
        "collections": (1.0,),
        "oxipng": (4.0, 1.0),
        "redb": (1.0,),
        "polars": (1.0,),
        "swc": (16.0,),
        "rustpython": (1.0,),
        "actix_web": (1.0,),
    }
    policy_rss = {
        "collections": (1.0,),
        "oxipng": (4.0, 1.0),
        "redb": (1.0,),
        "polars": (1.0,),
        "swc": (16.0,),
        "rustpython": (2.0,),
        "actix_web": (3.0,),
    }
    targets = []
    for target_id in TARGETS:
        work_model = (
            "fixed_work"
            if target_id in FIXED
            else "workload_native_adaptive_iterations"
        )
        eligibility = "core" if target_id in FIXED else "diagnostic_only"
        harnesses = []
        for index, (perf_ratio, rss_ratio) in enumerate(
            zip(policy_perf[target_id], policy_rss[target_id], strict=True)
        ):
            direction = "higher_is_better" if target_id == "swc" else "lower_is_better"
            cost = {"unialloc": 1.0, "typed_plain": 1.0, "typeiso_perf": perf_ratio}
            rss = {"unialloc": 1.0, "typed_plain": 1.0, "typeiso_perf": rss_ratio}
            measurements = []
            for round_number in range(1, 6):
                for variant in ("unialloc", "typed_plain", "typeiso_perf"):
                    performance = 100.0 * cost[variant]
                    if direction == "higher_is_better":
                        performance = 100.0 / cost[variant]
                    measurements.append(
                        {
                            "round": round_number,
                            "variant": variant,
                            "performance": performance,
                            "peak_rss_mib": 100.0 * rss[variant],
                        }
                    )
            comparisons = comparison_rows(cost, rss)
            harnesses.append(
                {
                    "id": f"{target_id}-{index + 1}",
                    "metric_direction": direction,
                    "rss_work_model": work_model,
                    "rss_comparison_eligibility": eligibility,
                    "gates": {
                        "correctness": True,
                        "build_success": True,
                        "allocator_activation": True,
                        "actual_mir_provenance": True,
                        "stats_disabled": True,
                        "source_audit_retained": True,
                        "compiler_route_equivalent": True,
                    },
                    "compiler_route_attribution": {
                        "accepted_interval": [0.85, 1.15],
                        "classification": "within_predeclared_interval",
                        "median_cost_ratio": 1.0,
                    },
                    "comparison_families": comparisons,
                    "measurements": measurements,
                }
            )
        targets.append(
            {
                "id": target_id,
                "implementation_revision": "0" * 40,
                "implementation_sha256": "a" * 64,
                "rss_work_model": work_model,
                "rss_comparison_eligibility": eligibility,
                "harnesses": harnesses,
            }
        )
    result = {
        "schema_version": 1,
        "suite_id": "type-isolation-primary-v1",
        "status": "complete",
        "implementation_revision": "0" * 40,
        "implementation_sha256": "a" * 64,
        "suite_manifest_sha256": "b" * 64,
        "original_campaign_manifest_sha256": "c" * 64,
        "targets": targets,
    }
    path = root / "macro.json"
    write_json(path, result)
    return path


class TwoTierAllocatorEvaluationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.uv = shutil.which("uv")
        if cls.uv is None:
            raise unittest.SkipTest("uv is required")
        cls.temp = tempfile.TemporaryDirectory()
        cls.root = Path(cls.temp.name)
        cls.micro, cls.records = build_micro(cls.root)
        cls.macro = build_macro(cls.root)
        cls.output_a = cls.root / "output-a"
        cls.output_b = cls.root / "output-b"
        cls.run_generator(cls.output_a)
        cls.run_generator(cls.output_b)
        cls.data = json.loads(
            (cls.output_a / "presentation-data.json").read_text(encoding="utf-8")
        )

    @classmethod
    def tearDownClass(cls) -> None:
        cls.temp.cleanup()

    @classmethod
    def command(
        cls,
        output: Path,
        *,
        micro: Path | None = None,
        records: Path | None = None,
        macro: Path | None = None,
    ) -> list[str]:
        return [
            cls.uv,
            "run",
            str(SCRIPT),
            "--fixture-contract",
            "--micro-results",
            str(micro or cls.micro),
            "--micro-records",
            str(records or cls.records),
            "--type-isolation-results",
            str(macro or cls.macro),
            "--output-dir",
            str(output),
        ]

    @classmethod
    def run_generator(cls, output: Path) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            cls.command(output), cwd=ROOT, check=True, text=True, capture_output=True
        )

    def test_exact_bundle_and_manifest_hashes(self) -> None:
        self.assertEqual(
            EXPECTED_FILES, {path.name for path in self.output_a.iterdir()}
        )
        manifest = json.loads(
            (self.output_a / "artifact-manifest.json").read_text(encoding="utf-8")
        )
        self.assertEqual(
            EXPECTED_FILES - {"artifact-manifest.json"}, set(manifest["artifacts"])
        )
        for name, metadata in manifest["artifacts"].items():
            path = self.output_a / name
            self.assertEqual(path.stat().st_size, metadata["bytes"])
            self.assertEqual(digest(path), metadata["sha256"])

    def test_micro_hierarchy_timer_floor_and_separate_collections(self) -> None:
        micro = self.data["microbenchmarks"]
        ptmalloc = micro["rust_std_bench"]["variants"]["ptmalloc"]["performance"]
        self.assertEqual(9, ptmalloc["observation_count"])
        self.assertAlmostEqual(4.0 ** (1.0 / 8.0), ptmalloc["headline_ratio"])
        self.assertEqual(
            "separate", micro["collections_type_isolation_subset"]["cohort_pooling"]
        )
        self.assertNotIn("collections", self.data["macrobenchmarks"]["target_order"])

    def test_macro_target_hierarchy_policy_family_and_rss_split(self) -> None:
        macro = self.data["macrobenchmarks"]
        policy = macro["comparison_families"]["policy_increment"]
        self.assertEqual(
            ("typed_plain", "typeiso_perf"), (policy["reference"], policy["subject"])
        )
        self.assertAlmostEqual(
            32.0 ** (1.0 / 6.0),
            policy["performance"]["suite_equal_target_geomean_ratio"],
        )
        self.assertAlmostEqual(
            2.0 ** (1.0 / 3.0), policy["peak_rss"]["suite_equal_target_geomean_ratio"]
        )
        self.assertEqual(
            ["oxipng", "redb", "polars"], policy["peak_rss"]["included_targets"]
        )
        self.assertEqual(
            ["swc", "rustpython", "actix_web"],
            policy["peak_rss"]["excluded_diagnostic_targets"],
        )
        self.assertEqual(7, policy["performance"]["route_equivalent_harness_count"])

    def test_csv_keeps_uncapped_policy_and_raw_ratios(self) -> None:
        with (self.output_a / "macrobenchmarks.csv").open(
            encoding="utf-8", newline=""
        ) as handle:
            macro_rows = list(csv.DictReader(handle))
        self.assertTrue(
            any(row["comparison"] == "policy_increment" for row in macro_rows)
        )
        self.assertTrue(any(float(row["ratio"]) == 16.0 for row in macro_rows))
        with (self.output_a / "microbenchmarks.csv").open(
            encoding="utf-8", newline=""
        ) as handle:
            micro_rows = list(csv.DictReader(handle))
        self.assertTrue(any(float(row["ratio"]) == 16.0 for row in micro_rows))
        self.assertFalse(self.data["display"]["raw_csv_ratios_capped"])

    def test_figures_are_title_free_two_panel_exports(self) -> None:
        for name in ("microbenchmarks.svg", "macrobenchmarks.svg"):
            svg = (self.output_a / name).read_text(encoding="utf-8")
            self.assertEqual(2, svg.count('id="axes_'))
            self.assertNotIn("<title", svg)
        macro_svg = (self.output_a / "macrobenchmarks.svg").read_text(encoding="utf-8")
        self.assertIn("Type Isolation / typed control execution cost", macro_svg)
        self.assertIn("target-policy-summary-performance", macro_svg)
        self.assertNotIn("headline-macro-performance-typed_plain", macro_svg)

    def test_all_artifact_bytes_are_deterministic(self) -> None:
        for name in EXPECTED_FILES:
            self.assertEqual(
                (self.output_a / name).read_bytes(),
                (self.output_b / name).read_bytes(),
                name,
            )

    def test_schema_hash_and_comparison_failures_preserve_existing_bundle(self) -> None:
        invalid_records = self.root / "records-tampered.jsonl"
        invalid_records.write_bytes(self.records.read_bytes() + b"\n")
        invalid_macro = self.root / "macro-tampered.json"
        value = json.loads(self.macro.read_text(encoding="utf-8"))
        value["targets"][1]["harnesses"][0]["comparison_families"]["policy_increment"][
            "execution_cost_ratio_median"
        ] = 9.0
        write_json(invalid_macro, value)
        for index, overrides in enumerate(
            ({"records": invalid_records}, {"macro": invalid_macro})
        ):
            output = self.root / f"protected-{index}"
            output.mkdir()
            sentinel = output / "sentinel.txt"
            sentinel.write_text("keep\n", encoding="utf-8")
            result = subprocess.run(
                self.command(output, **overrides),
                cwd=ROOT,
                text=True,
                capture_output=True,
            )
            self.assertNotEqual(0, result.returncode)
            self.assertEqual({"sentinel.txt"}, {path.name for path in output.iterdir()})
            self.assertEqual("keep\n", sentinel.read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
