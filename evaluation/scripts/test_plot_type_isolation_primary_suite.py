#!/usr/bin/env python3

from __future__ import annotations

import csv
import hashlib
import json
import os
import shutil
import statistics
import struct
import subprocess
import tempfile
import unittest
from pathlib import Path


REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
SCRIPT_PATH = (
    REPOSITORY_ROOT / "evaluation" / "scripts" / "plot_type_isolation_primary_suite.py"
)
SUITE_PATH = (
    REPOSITORY_ROOT / "evaluation" / "config" / "type_isolation_primary_suite.json"
)
EXPECTED_TARGETS = (
    "Collections",
    "Oxipng",
    "redb",
    "Polars",
    "SWC",
    "RustPython",
    "Actix Web",
)
EXPECTED_TARGET_IDS = (
    "collections",
    "oxipng",
    "redb",
    "polars",
    "swc",
    "rustpython",
    "actix_web",
)
REQUIRED_GATES = (
    "correctness",
    "build_success",
    "allocator_activation",
    "actual_mir_provenance",
    "stats_disabled",
    "compiler_route_equivalent",
    "source_audit_retained",
)
TOTAL_HARNESSES = 34
IMPLEMENTATION_REVISION = "f5d0c19c1cc5b56fdac3282d69333dd8c85d4cf2"
IMPLEMENTATION_SHA256 = (
    "cab1e580c08e2b16308bae75501716049ba428b040fb04bf305269c9ba9eaf01"
)


def sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def build_complete_results(
    suite: dict[str, object], evidence_root: Path
) -> dict[str, object]:
    targets: list[dict[str, object]] = []
    variants = tuple(suite["variant_order"])
    for target_index, target in enumerate(suite["targets"]):
        harnesses: list[dict[str, object]] = []
        for harness_index, harness in enumerate(target["harnesses"]):
            measurements: list[dict[str, object]] = []
            for round_number in range(1, 6):
                base_performance = 100.0 + 3.0 * target_index + harness_index
                base_rss = 80.0 + 4.0 * target_index + harness_index
                round_scale = 1.0 + 0.002 * (round_number - 3)
                factors = {
                    "unialloc": (1.0, 1.0),
                    "typed_plain": (
                        1.012 + 0.001 * harness_index,
                        1.018 + 0.001 * target_index,
                    ),
                    "typeiso_perf": (
                        3.2
                        if target_index == 0 and harness_index == 0
                        else 1.021 + 0.001 * target_index,
                        1.027 + 0.001 * harness_index,
                    ),
                }
                for variant in variants:
                    performance_factor, rss_factor = factors[variant]
                    measurements.append(
                        {
                            "round": round_number,
                            "variant": variant,
                            "performance": base_performance
                            * performance_factor
                            * round_scale,
                            "peak_rss_mib": base_rss * rss_factor * round_scale,
                        }
                    )
            harnesses.append(
                {
                    "id": harness["id"],
                    "metric_direction": harness["metric_direction"],
                    "rss_work_model": target["rss_work_model"],
                    "rss_comparison_eligibility": (
                        "core"
                        if target["rss_work_model"] == "fixed_work"
                        else "diagnostic_only"
                    ),
                    "performance_unit": harness["performance_unit"],
                    "performance_source": harness["performance_source"],
                    "gates": {gate: True for gate in REQUIRED_GATES},
                    "warmup_attestations": {
                        variant: write_warmup_record(
                            evidence_root,
                            str(target["id"]),
                            str(harness["id"]),
                            str(variant),
                        )
                        for variant in variants
                    },
                    "compiler_route_attribution": {
                        "accepted_interval": [0.85, 1.15],
                        "classification": "within_predeclared_interval",
                        "median_cost_ratio": statistics.median(
                            row["performance"]
                            / next(
                                reference["performance"]
                                for reference in measurements
                                if reference["round"] == row["round"]
                                and reference["variant"] == "unialloc"
                            )
                            for row in measurements
                            if row["variant"] == "typed_plain"
                        ),
                    },
                    "measurements": measurements,
                }
            )
        targets.append(
            {
                "id": target["id"],
                "rss_work_model": target["rss_work_model"],
                "rss_comparison_eligibility": (
                    "core"
                    if target["rss_work_model"] == "fixed_work"
                    else "diagnostic_only"
                ),
                "source_commit": target["source"]["commit"],
                "implementation_revision": IMPLEMENTATION_REVISION,
                "implementation_sha256": IMPLEMENTATION_SHA256,
                "harnesses": harnesses,
            }
        )
    return {
        "schema_version": 1,
        "suite_id": suite["suite_id"],
        "status": "complete",
        "suite_manifest_sha256": sha256_file(SUITE_PATH),
        "implementation_revision": IMPLEMENTATION_REVISION,
        "implementation_sha256": IMPLEMENTATION_SHA256,
        "analysis_amendments": suite["analysis_amendments"],
        "original_campaign_manifest_sha256": suite["analysis_amendments"][0][
            "original_campaign_manifest_sha256"
        ],
        "compiler_route_attribution": {
            "accepted_interval": [0.85, 1.15],
            "classification": "all_routes_equivalent",
            "fail_count": 0,
            "metric": "paired_execution_cost_ratio",
            "pass_count": TOTAL_HARNESSES,
            "total_count": TOTAL_HARNESSES,
        },
        "targets": targets,
    }


def write_warmup_record(
    evidence_root: Path, target_id: str, harness_id: str, variant: str
) -> dict[str, object]:
    path = evidence_root / target_id / harness_id / f"{variant}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "target_id": target_id,
                "harness_id": harness_id,
                "variant": variant,
                "phase": "warmup",
                "round": 0,
                "performance": 1.0,
                "peak_rss_mib": 2.0,
            },
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )
    return {
        "target_id": target_id,
        "harness_id": harness_id,
        "variant": variant,
        "phase": "warmup",
        "round": 0,
        "performance": 1.0,
        "peak_rss_mib": 2.0,
        "record_sha256": sha256_file(path),
    }


class TypeIsolationPrimarySuiteFigureTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        uv = shutil.which("uv")
        if uv is None:
            raise unittest.SkipTest("uv is required for the Matplotlib exporter")
        cls.uv = uv
        cls.suite = json.loads(SUITE_PATH.read_text(encoding="utf-8"))

    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary_directory.name)
        self.results_path = self.root / "results.json"
        self.output_dir = self.root / "figures"
        self.results = build_complete_results(self.suite, self.root / "warmups")
        self.write_results(self.results)

    def tearDown(self) -> None:
        self.temporary_directory.cleanup()

    def write_results(self, results: dict[str, object]) -> None:
        self.results_path.write_text(
            json.dumps(results, indent=2) + "\n", encoding="utf-8"
        )

    def run_generator(
        self,
        *,
        check: bool = True,
        output_dir: Path | None = None,
        extra_env: dict[str, str] | None = None,
    ) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [
                self.uv,
                "run",
                str(SCRIPT_PATH),
                "--suite",
                str(SUITE_PATH),
                "--results",
                str(self.results_path),
                "--output-dir",
                str(output_dir or self.output_dir),
            ],
            cwd=REPOSITORY_ROOT,
            check=check,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            env={**os.environ, **(extra_env or {})},
        )

    def test_final_figure_contains_all_seven_targets_and_selected_harnesses(
        self,
    ) -> None:
        self.run_generator()
        svg = (self.output_dir / "type-isolation-primary-suite.svg").read_text(
            encoding="utf-8"
        )
        for target, target_id in zip(
            EXPECTED_TARGETS, EXPECTED_TARGET_IDS, strict=True
        ):
            self.assertIn(target, svg)
            self.assertEqual(1, svg.count(f'id="target-label-{target_id}"'))
        selected_labels = [
            harness["overview_label"]
            for target in self.suite["targets"]
            for harness in target["harnesses"]
            if "overview_rank" in harness
        ]
        self.assertEqual(14, len(selected_labels))
        for label in selected_labels:
            self.assertIn(label, svg)

        manifest = json.loads(
            (self.output_dir / "type-isolation-primary-suite-manifest.json").read_text(
                encoding="utf-8"
            )
        )
        self.assertEqual(
            [target["id"] for target in self.suite["targets"]],
            manifest["target_order"],
        )
        self.assertEqual(14, manifest["selected_harness_count"])
        self.assertTrue(manifest["presentation_eligible"])
        self.assertFalse(manifest["attribution_limited"])
        self.assertEqual(
            {
                "git_revision": IMPLEMENTATION_REVISION,
                "canonical_sha256": IMPLEMENTATION_SHA256,
            },
            manifest["implementation"],
        )
        self.assertEqual(
            {
                "classification": "all_routes_equivalent",
                "fail_count": 0,
                "pass_count": TOTAL_HARNESSES,
                "total_count": TOTAL_HARNESSES,
            },
            {
                key: manifest["compiler_route_attribution"][key]
                for key in (
                    "classification",
                    "fail_count",
                    "pass_count",
                    "total_count",
                )
            },
        )

    def test_final_figure_uses_title_free_horizontal_median_bars(self) -> None:
        self.run_generator()
        svg = (self.output_dir / "type-isolation-primary-suite.svg").read_text(
            encoding="utf-8"
        )
        self.assertTrue(all(line == line.rstrip() for line in svg.splitlines()))
        self.assertEqual(56, svg.count('<g id="bar-'))
        for forbidden in (
            "Actual-MIR Type Isolation",
            "Layout preview",
            "Data:",
            "Source:",
            "A  Execution",
            "B  Peak",
        ):
            self.assertNotIn(forbidden, svg)
        for required in (
            "Execution-cost ratio to UniAlloc | lower is better",
            "Peak-RSS ratio to UniAlloc | lower is better",
            "Typed path, policy off",
            "Type Isolation",
            "0.5x",
            "1x",
            "2x",
            "4x",
            "+1.2%",
        ):
            self.assertIn(required, svg)
        for legend_gid in ("legend-typed_plain", "legend-typeiso_perf"):
            self.assertIn(legend_gid, svg)
        self.assertIn('id="legend-adaptive-rss"', svg)
        self.assertIn(
            "‡ adaptive iterations; RSS is process-volume, not equal-work", svg
        )

        manifest = json.loads(
            (self.output_dir / "type-isolation-primary-suite-manifest.json").read_text(
                encoding="utf-8"
            )
        )
        self.assertEqual(
            {
                "title": False,
                "subtitle": False,
                "panel_title": False,
                "footer": False,
            },
            manifest["canvas_text"],
        )
        self.assertEqual(
            "grouped horizontal median bars on centered log2 ratio axes",
            manifest["encoding"],
        )
        self.assertEqual(
            {
                "annotation": "paired-median percent change",
                "center_ratio": 1.0,
                "tick_unit": "multiplicative ratio",
                "transform": "log2",
            },
            manifest["axis_encoding"],
        )
        self.assertEqual(
            {"performance": 2.0, "peak_rss_mib": 1.0},
            manifest["panel_log2_bounds"],
        )
        self.assertEqual(1, svg.count(">0.25x<"))

        png = (self.output_dir / "type-isolation-primary-suite.png").read_bytes()
        self.assertEqual(b"\x89PNG\r\n\x1a\n", png[:8])
        self.assertEqual((2400, 1350), struct.unpack(">II", png[16:24]))

    def test_route_failure_remains_in_the_figure_with_attribution_limits(self) -> None:
        first_harness = self.results["targets"][0]["harnesses"][0]
        first_harness["gates"]["compiler_route_equivalent"] = False
        for row in first_harness["measurements"]:
            if row["variant"] == "typed_plain":
                row["performance"] = 2.0 * next(
                    reference["performance"]
                    for reference in first_harness["measurements"]
                    if reference["round"] == row["round"]
                    and reference["variant"] == "unialloc"
                )
        first_harness["compiler_route_attribution"] = {
            "accepted_interval": [0.85, 1.15],
            "classification": "outside_predeclared_interval",
            "median_cost_ratio": 2.0,
        }
        self.results["status"] = "complete_with_attribution_limits"
        self.results["compiler_route_attribution"] = {
            "accepted_interval": [0.85, 1.15],
            "classification": "attribution_limits_present",
            "fail_count": 1,
            "metric": "paired_execution_cost_ratio",
            "pass_count": TOTAL_HARNESSES - 1,
            "total_count": TOTAL_HARNESSES,
        }
        self.write_results(self.results)

        self.run_generator()

        svg = (self.output_dir / "type-isolation-primary-suite.svg").read_text(
            encoding="utf-8"
        )
        self.assertEqual(56, svg.count('<g id="bar-'))
        self.assertIn("Binary heap push †‡", svg)
        self.assertIn("† typed route outside 0.85–1.15x", svg)
        self.assertIn('id="legend-route-attribution-limit"', svg)
        manifest = json.loads(
            (self.output_dir / "type-isolation-primary-suite-manifest.json").read_text(
                encoding="utf-8"
            )
        )
        self.assertTrue(manifest["presentation_eligible"])
        self.assertTrue(manifest["attribution_limited"])
        self.assertEqual(1, manifest["compiler_route_attribution"]["fail_count"])
        self.assertEqual(
            TOTAL_HARNESSES - 1,
            manifest["compiler_route_attribution"]["pass_count"],
        )

    def test_extreme_route_outlier_is_clipped_visually_and_retained_in_data(
        self,
    ) -> None:
        first_harness = self.results["targets"][0]["harnesses"][0]
        first_harness["gates"]["compiler_route_equivalent"] = False
        for row in first_harness["measurements"]:
            if row["variant"] == "typed_plain":
                row["performance"] = 53.9 * next(
                    reference["performance"]
                    for reference in first_harness["measurements"]
                    if reference["round"] == row["round"]
                    and reference["variant"] == "unialloc"
                )
        first_harness["compiler_route_attribution"] = {
            "accepted_interval": [0.85, 1.15],
            "classification": "outside_predeclared_interval",
            "median_cost_ratio": 53.9,
        }
        self.results["status"] = "complete_with_attribution_limits"
        self.results["compiler_route_attribution"] = {
            "accepted_interval": [0.85, 1.15],
            "classification": "attribution_limits_present",
            "fail_count": 1,
            "metric": "paired_execution_cost_ratio",
            "pass_count": TOTAL_HARNESSES - 1,
            "total_count": TOTAL_HARNESSES,
        }
        self.write_results(self.results)

        self.run_generator()

        svg = (self.output_dir / "type-isolation-primary-suite.svg").read_text(
            encoding="utf-8"
        )
        self.assertEqual(56, svg.count('<g id="bar-'))
        self.assertIn(
            'id="clip-performance-collections-binary_heap_push-typed_plain"',
            svg,
        )
        self.assertIn("53.9x (+5290.0%)", svg)
        self.assertIn("Binary heap push †‡", svg)

        manifest = json.loads(
            (self.output_dir / "type-isolation-primary-suite-manifest.json").read_text(
                encoding="utf-8"
            )
        )
        clipping = manifest["clipping"]
        self.assertEqual(2.0, clipping["display_log2_cap"])
        self.assertGreater(clipping["clipped_summary_count"], 0)
        self.assertGreater(clipping["clipped_observation_count"], 0)
        performance = clipping["panels"]["performance"]
        self.assertEqual([0.25, 4.0], performance["display_ratio_interval"])
        self.assertGreaterEqual(performance["raw_ratio_max"], 53.9)

        with (self.output_dir / "type-isolation-primary-suite-data.csv").open(
            encoding="utf-8", newline=""
        ) as handle:
            rows = list(csv.DictReader(handle))
        outlier_rows = [
            row
            for row in rows
            if row["target"] == "collections"
            and row["harness"] == "binary_heap_push"
            and row["variant"] == "typed_plain"
        ]
        self.assertEqual(5, len(outlier_rows))
        self.assertTrue(
            all(float(row["performance_cost_ratio"]) == 53.9 for row in outlier_rows)
        )
        self.assertTrue(
            all(row["performance_display_clipped"] == "true" for row in outlier_rows)
        )

    def test_adaptive_rss_rows_are_marked_and_machine_readable(self) -> None:
        self.run_generator()
        svg = (self.output_dir / "type-isolation-primary-suite.svg").read_text(
            encoding="utf-8"
        )
        for label in (
            "Binary heap push ‡",
            "Parse large input ‡",
            "Parse Mandelbrot ‡",
            "Direct async service ‡",
        ):
            self.assertIn(label, svg)
        self.assertNotIn("CLI image, 1 thread ‡", svg)
        self.assertNotIn("Bulk small values ‡", svg)
        self.assertIn('id="legend-adaptive-rss"', svg)

        manifest = json.loads(
            (self.output_dir / "type-isolation-primary-suite-manifest.json").read_text(
                encoding="utf-8"
            )
        )
        interpretation = manifest["rss_interpretation"]
        self.assertEqual(3, interpretation["fixed_work_target_count"])
        self.assertEqual(4, interpretation["adaptive_iterations_target_count"])
        self.assertEqual(6, interpretation["fixed_work_selected_harness_count"])
        self.assertEqual(8, interpretation["adaptive_selected_harness_count"])
        self.assertTrue(interpretation["uncapped_observations_preserved"])

        with (self.output_dir / "type-isolation-primary-suite-data.csv").open(
            encoding="utf-8", newline=""
        ) as handle:
            rows = list(csv.DictReader(handle))
        collections_rows = [row for row in rows if row["target"] == "collections"]
        oxipng_rows = [row for row in rows if row["target"] == "oxipng"]
        self.assertTrue(
            all(
                row["rss_work_model"] == "workload_native_adaptive_iterations"
                and row["rss_comparison_eligibility"] == "diagnostic_only"
                and row["rss_interpretation"] == "process_volume"
                and row["fixed_work_rss_aggregate_eligible"] == "false"
                for row in collections_rows
            )
        )
        self.assertTrue(
            all(
                row["rss_work_model"] == "fixed_work"
                and row["rss_comparison_eligibility"] == "core"
                and row["rss_interpretation"] == "equal_work"
                and row["fixed_work_rss_aggregate_eligible"] == "true"
                for row in oxipng_rows
            )
        )

    def test_stored_route_classification_is_recomputed(self) -> None:
        first_harness = self.results["targets"][0]["harnesses"][0]
        for row in first_harness["measurements"]:
            if row["variant"] == "typed_plain":
                row["performance"] *= 2.0
        self.write_results(self.results)
        completed = self.run_generator(check=False)
        self.assertNotEqual(0, completed.returncode)
        self.assertIn("stored compiler route classification", completed.stderr)
        self.assertFalse(self.output_dir.exists())

    def test_each_data_eligibility_gate_fails_closed(self) -> None:
        for gate in (
            "correctness",
            "build_success",
            "allocator_activation",
            "actual_mir_provenance",
            "stats_disabled",
            "source_audit_retained",
        ):
            with self.subTest(gate=gate):
                self.results = build_complete_results(
                    self.suite, self.root / f"warmups-{gate}"
                )
                self.results["targets"][0]["harnesses"][0]["gates"][gate] = False
                self.write_results(self.results)
                output_dir = self.root / f"failed-{gate}"
                completed = self.run_generator(
                    check=False,
                    output_dir=output_dir,
                )
                self.assertNotEqual(0, completed.returncode)
                self.assertIn(gate, completed.stderr)
                self.assertFalse(output_dir.exists())

    def test_warmup_evidence_failure_preserves_existing_bundle(self) -> None:
        self.run_generator()
        before = {path.name: sha256_file(path) for path in self.output_dir.iterdir()}
        del self.results["targets"][0]["harnesses"][0]["warmup_attestations"][
            "typed_plain"
        ]
        self.write_results(self.results)
        completed = self.run_generator(check=False)
        self.assertNotEqual(0, completed.returncode)
        self.assertIn("warmup", completed.stderr)
        self.assertEqual(
            before,
            {path.name: sha256_file(path) for path in self.output_dir.iterdir()},
        )

    def test_publish_failure_rolls_back_the_complete_bundle(self) -> None:
        self.run_generator()
        before = {path.name: sha256_file(path) for path in self.output_dir.iterdir()}
        completed = self.run_generator(
            check=False,
            extra_env={"UNIALLOC_TYPEISO_PLOT_TEST_FAIL_PUBLISH": "after_backup"},
        )
        self.assertNotEqual(0, completed.returncode)
        self.assertIn("injected bundle publication failure", completed.stderr)
        self.assertEqual(
            before,
            {path.name: sha256_file(path) for path in self.output_dir.iterdir()},
        )
        leftovers = [
            path.name
            for path in self.output_dir.parent.iterdir()
            if path.name.startswith(f".{self.output_dir.name}.")
        ]
        self.assertEqual([], leftovers)

    def test_final_generation_fails_closed_and_preserves_existing_output(self) -> None:
        self.output_dir.mkdir()
        sentinel = self.output_dir / "type-isolation-primary-suite.svg"
        sentinel.write_text("preserve-me", encoding="utf-8")
        before = sha256_file(sentinel)
        self.results["targets"] = self.results["targets"][:-1]
        self.write_results(self.results)
        completed = self.run_generator(check=False)
        self.assertNotEqual(0, completed.returncode)
        self.assertIn("actix_web", completed.stderr)
        self.assertEqual(before, sha256_file(sentinel))

    def test_final_generation_rejects_incomplete_variant_rounds(self) -> None:
        first_target = self.results["targets"][0]
        first_harness = first_target["harnesses"][0]
        first_harness["measurements"] = [
            row
            for row in first_harness["measurements"]
            if not (row["round"] == 5 and row["variant"] == "typeiso_perf")
        ]
        self.write_results(self.results)
        completed = self.run_generator(check=False)
        self.assertNotEqual(0, completed.returncode)
        self.assertIn("binary_heap_push", completed.stderr)
        self.assertIn("exact measured rounds 1 through 5", completed.stderr)
        self.assertFalse(self.output_dir.exists())

    def test_final_generation_rejects_extra_measured_rounds(self) -> None:
        harness = self.results["targets"][0]["harnesses"][0]
        for variant in ("unialloc", "typed_plain", "typeiso_perf"):
            template = next(
                row for row in harness["measurements"] if row["variant"] == variant
            )
            harness["measurements"].append({**template, "round": 6})
        self.write_results(self.results)
        completed = self.run_generator(check=False)
        self.assertNotEqual(0, completed.returncode)
        self.assertIn("exact measured rounds 1 through 5", completed.stderr)
        self.assertFalse(self.output_dir.exists())

    def test_final_generation_rejects_source_pin_mismatch(self) -> None:
        self.results["targets"][0]["source_commit"] = "0" * 40
        self.write_results(self.results)
        completed = self.run_generator(check=False)
        self.assertNotEqual(0, completed.returncode)
        self.assertIn("source commit mismatch", completed.stderr)
        self.assertIn("collections", completed.stderr)

    def test_final_generation_rejects_mixed_implementation_and_preserves_output(
        self,
    ) -> None:
        self.output_dir.mkdir()
        sentinel = self.output_dir / "type-isolation-primary-suite.svg"
        sentinel.write_text("preserve-me", encoding="utf-8")
        before = sha256_file(sentinel)
        self.results["targets"][0]["implementation_sha256"] = "0" * 64
        self.write_results(self.results)
        completed = self.run_generator(check=False)
        self.assertNotEqual(0, completed.returncode)
        self.assertIn("implementation_sha256", completed.stderr)
        self.assertEqual(before, sha256_file(sentinel))

    def test_generation_is_byte_deterministic(self) -> None:
        first = self.root / "first"
        second = self.root / "second"
        self.run_generator(output_dir=first)
        self.run_generator(output_dir=second)
        self.assertEqual(
            {path.name for path in first.iterdir()},
            {path.name for path in second.iterdir()},
        )
        for path in first.iterdir():
            self.assertEqual(path.read_bytes(), (second / path.name).read_bytes())


if __name__ == "__main__":
    unittest.main()
