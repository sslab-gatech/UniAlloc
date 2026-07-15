#!/usr/bin/env python3

from __future__ import annotations

import csv
import hashlib
import json
import re
import shutil
import statistics
import struct
import subprocess
import tempfile
import unittest
from pathlib import Path


REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
SCRIPT_PATH = (
    REPOSITORY_ROOT / "evaluation" / "scripts" / "plot_allocator_feature_thp_slides.py"
)

EXPECTED_STEMS = (
    "01-ripgrep-configurations",
    "02-oxipng-configurations",
    "03-rsedis-configurations",
)
EXPECTED_TABLE_ROWS = {
    "oxipng-configurations.csv": 124,
    "oxipng-hugetlb-accounting.csv": 2,
    "ripgrep-configurations.csv": 49,
    "rsedis-configurations.csv": 40,
}
EXPECTED_CAMPAIGNS = {
    "ripgrep_realworld_single_thread": ("ripgrep", 7, 7),
    "oxipng_realworld_single_thread": ("Oxipng", 7, 7),
    "oxipng_feature_matrix_four_threads": ("Oxipng", 15, 5),
    "rsedis_allocator_matrix": ("Rsedis", 5, 5),
    "rsedis_thp_mechanism_matrix": ("Rsedis", 3, 5),
}
LEGACY_STEMS = (
    "00-type-isolation-cross-harness",
    "00-evaluation-map",
    "01-realworld-allocator-tradeoff",
    "02-type-isolation-realworld",
    "03-rsedis-allocator-comparison",
    "04-oxipng-feature-matrix",
    "05-rsedis-thp-mechanism",
    "06-hugetlb-accounting",
    "02-fd-configurations",
)
CJK_PATTERN = re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff]")


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def read_csv(path: Path) -> list[dict[str, str]]:
    with path.open(encoding="utf-8", newline="") as handle:
        return list(csv.DictReader(handle))


def first_row(
    rows: list[dict[str, str]], campaign: str, variant: str
) -> dict[str, str]:
    return next(
        row for row in rows if row["campaign"] == campaign and row["variant"] == variant
    )


class TargetCentricFigureExportTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        uv = shutil.which("uv")
        if uv is None:
            raise unittest.SkipTest("uv is required to run the PEP 723 figure script")

        cls.uv = uv
        cls.temporary_directory = tempfile.TemporaryDirectory()
        cls.output_dir = Path(cls.temporary_directory.name) / "figures"
        cls.output_dir.mkdir(parents=True)
        (cls.output_dir / "data").mkdir()

        # Prove that regeneration removes superseded visual and table assets.
        (cls.output_dir / f"{LEGACY_STEMS[0]}.svg").write_text(
            "obsolete", encoding="utf-8"
        )
        (cls.output_dir / "00-slide-contact-sheet.png").write_bytes(b"obsolete")
        (cls.output_dir / "legacy-slide-pack.pdf").write_bytes(b"obsolete")
        (cls.output_dir / "data" / "legacy-summary.csv").write_text(
            "obsolete\n", encoding="utf-8"
        )
        cls.generation = cls.run_generator(cls.output_dir)
        cls.manifest = json.loads(
            (cls.output_dir / "slide-data-manifest.json").read_text(encoding="utf-8")
        )

    @classmethod
    def tearDownClass(cls) -> None:
        cls.temporary_directory.cleanup()

    @classmethod
    def run_generator(cls, output_dir: Path) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [
                cls.uv,
                "run",
                str(SCRIPT_PATH),
                "--output-dir",
                str(output_dir),
            ],
            cwd=REPOSITORY_ROOT,
            check=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )

    def target_rows(self, name: str) -> list[dict[str, str]]:
        return read_csv(self.output_dir / "data" / name)

    def test_pack_contains_exactly_three_target_figures(self) -> None:
        self.assertEqual(
            set(EXPECTED_STEMS),
            {path.stem for path in self.output_dir.glob("*.svg")},
        )
        self.assertEqual(
            set(EXPECTED_STEMS),
            {path.stem for path in self.output_dir.glob("*.png")},
        )
        self.assertEqual(
            ["allocator-feature-thp-slide-pack.pdf"],
            sorted(path.name for path in self.output_dir.glob("*.pdf")),
        )
        self.assertEqual(
            list(EXPECTED_STEMS),
            [contract["stem"] for contract in self.manifest["chart_contracts"]],
        )
        self.assertEqual(
            ["ripgrep", "Oxipng", "Rsedis"],
            [contract["target"] for contract in self.manifest["chart_contracts"]],
        )

        generated_names = {
            path.name for path in self.output_dir.iterdir() if path.is_file()
        }
        for stem in LEGACY_STEMS:
            self.assertFalse(
                any(name.startswith(stem) for name in generated_names),
                f"obsolete figure survived regeneration: {stem}",
            )
        self.assertNotIn("00-slide-contact-sheet.png", generated_names)
        self.assertNotIn("legacy-slide-pack.pdf", generated_names)
        self.assertFalse((self.output_dir / "data" / "legacy-summary.csv").exists())
        self.assertFalse((self.output_dir / "data" / "fd-configurations.csv").exists())
        self.assertFalse(
            (self.output_dir / "data" / "type-isolation-cross-harness.csv").exists()
        )
        readme = (self.output_dir / "README.md").read_text(encoding="utf-8")
        self.assertIn(
            "**three** presentation-eligible 16:9 measured-target detail", readme
        )
        self.assertIn("three target tables and one HugeTLB accounting table", readme)
        self.assertIn("paired performance deltas and absolute peak RSS in MiB", readme)
        self.assertTrue(
            any(
                boundary == "Each figure represents one measured target."
                for boundary in self.manifest["boundaries"]
            )
        )
        durable_metadata = readme + json.dumps(self.manifest, sort_keys=True)
        self.assertNotIn("cross-harness", durable_metadata)
        self.assertNotIn("layout preview", durable_metadata)

    def test_long_form_tables_preserve_all_measured_rows(self) -> None:
        data_dir = self.output_dir / "data"
        self.assertEqual(
            set(EXPECTED_TABLE_ROWS),
            {path.name for path in data_dir.glob("*.csv")},
        )
        for name, expected_rows in EXPECTED_TABLE_ROWS.items():
            self.assertEqual(expected_rows, len(read_csv(data_dir / name)), name)

        manifest_tables = {
            Path(table["path"]).name: table for table in self.manifest["data_tables"]
        }
        self.assertEqual(set(EXPECTED_TABLE_ROWS), set(manifest_tables))
        for name, expected_rows in EXPECTED_TABLE_ROWS.items():
            path = data_dir / name
            self.assertEqual(expected_rows, manifest_tables[name]["rows"])
            self.assertEqual(sha256_file(path), manifest_tables[name]["sha256"])

        self.assertEqual(3, self.manifest["evaluation_scope"]["unique_target_count"])
        self.assertEqual(
            37, self.manifest["evaluation_scope"]["timed_configuration_cells"]
        )
        self.assertEqual(213, self.manifest["evaluation_scope"]["measured_timed_runs"])
        self.assertEqual(37, self.manifest["evaluation_scope"]["timed_warmups"])
        self.assertTrue(self.manifest["evaluation_scope"]["type_isolation_is_subset"])

    def test_oxipng_and_rsedis_campaigns_remain_separate(self) -> None:
        rows = (
            self.target_rows("ripgrep-configurations.csv")
            + self.target_rows("oxipng-configurations.csv")
            + self.target_rows("rsedis-configurations.csv")
        )
        self.assertEqual(set(EXPECTED_CAMPAIGNS), {row["campaign"] for row in rows})

        for campaign, (
            target,
            variant_count,
            repetitions,
        ) in EXPECTED_CAMPAIGNS.items():
            campaign_rows = [row for row in rows if row["campaign"] == campaign]
            variants = {row["variant"] for row in campaign_rows}
            self.assertEqual({target}, {row["target"] for row in campaign_rows})
            self.assertEqual(variant_count, len(variants), campaign)
            self.assertEqual(variant_count * repetitions, len(campaign_rows), campaign)
            for variant in variants:
                variant_rows = [
                    row for row in campaign_rows if row["variant"] == variant
                ]
                self.assertEqual(
                    set(range(1, repetitions + 1)),
                    {int(row["round"]) for row in variant_rows},
                    f"{campaign}:{variant}",
                )
                self.assertEqual(
                    {repetitions}, {int(row["repetitions"]) for row in variant_rows}
                )

        oxipng = self.target_rows("oxipng-configurations.csv")
        self.assertEqual(
            {"oxipng_realworld_single_thread", "oxipng_feature_matrix_four_threads"},
            {row["campaign"] for row in oxipng},
        )
        rsedis = self.target_rows("rsedis-configurations.csv")
        self.assertEqual(
            {"rsedis_allocator_matrix", "rsedis_thp_mechanism_matrix"},
            {row["campaign"] for row in rsedis},
        )
        self.assertNotIn(
            "unialloc",
            {
                row["variant"]
                for row in rsedis
                if row["campaign"] == "rsedis_thp_mechanism_matrix"
            },
        )

        manifest_campaign_ids = {
            campaign["id"] for campaign in self.manifest["campaigns"]
        }
        contract_campaign_ids = {
            campaign_id
            for contract in self.manifest["chart_contracts"]
            for campaign_id in contract["campaigns"]
        }
        self.assertEqual(manifest_campaign_ids, contract_campaign_ids)

    def test_medians_and_same_round_deltas_recompute_from_raw_rows(self) -> None:
        rows = (
            self.target_rows("ripgrep-configurations.csv")
            + self.target_rows("oxipng-configurations.csv")
            + self.target_rows("rsedis-configurations.csv")
        )
        index = {
            (row["campaign"], row["variant"], int(row["round"])): row for row in rows
        }

        for campaign in EXPECTED_CAMPAIGNS:
            campaign_rows = [row for row in rows if row["campaign"] == campaign]
            for variant in {row["variant"] for row in campaign_rows}:
                variant_rows = [
                    row for row in campaign_rows if row["variant"] == variant
                ]
                performance_median = statistics.median(
                    float(row["performance_value"]) for row in variant_rows
                )
                rss_median = statistics.median(
                    float(row["peak_rss_mib"]) for row in variant_rows
                )
                self.assertAlmostEqual(
                    performance_median,
                    float(variant_rows[0]["performance_value_median"]),
                    places=12,
                )
                self.assertAlmostEqual(
                    rss_median,
                    float(variant_rows[0]["peak_rss_mib_median"]),
                    places=12,
                )

                plot_performance_deltas: list[float] = []
                plot_rss_deltas: list[float] = []
                matched_performance_deltas: list[float] = []
                matched_rss_deltas: list[float] = []
                for row in variant_rows:
                    round_index = int(row["round"])
                    plot_reference = index[
                        (campaign, row["plot_reference"], round_index)
                    ]
                    matched_reference = index[
                        (campaign, row["matched_reference"], round_index)
                    ]
                    performance = float(row["performance_value"])
                    rss = float(row["peak_rss_mib"])
                    plot_performance_delta = 100.0 * (
                        performance / float(plot_reference["performance_value"]) - 1.0
                    )
                    plot_rss_delta = 100.0 * (
                        rss / float(plot_reference["peak_rss_mib"]) - 1.0
                    )
                    matched_performance_delta = 100.0 * (
                        performance / float(matched_reference["performance_value"])
                        - 1.0
                    )
                    matched_rss_delta = 100.0 * (
                        rss / float(matched_reference["peak_rss_mib"]) - 1.0
                    )
                    self.assertAlmostEqual(
                        plot_performance_delta,
                        float(row["plot_performance_delta_percent"]),
                        places=10,
                    )
                    self.assertAlmostEqual(
                        plot_rss_delta,
                        float(row["plot_peak_rss_delta_percent"]),
                        places=10,
                    )
                    self.assertAlmostEqual(
                        matched_performance_delta,
                        float(row["matched_performance_delta_percent"]),
                        places=10,
                    )
                    self.assertAlmostEqual(
                        matched_rss_delta,
                        float(row["matched_peak_rss_delta_percent"]),
                        places=10,
                    )
                    plot_performance_deltas.append(plot_performance_delta)
                    plot_rss_deltas.append(plot_rss_delta)
                    matched_performance_deltas.append(matched_performance_delta)
                    matched_rss_deltas.append(matched_rss_delta)

                expected_medians = {
                    "plot_performance_delta_percent_median": statistics.median(
                        plot_performance_deltas
                    ),
                    "plot_peak_rss_delta_percent_median": statistics.median(
                        plot_rss_deltas
                    ),
                    "matched_performance_delta_percent_median": statistics.median(
                        matched_performance_deltas
                    ),
                    "matched_peak_rss_delta_percent_median": statistics.median(
                        matched_rss_deltas
                    ),
                }
                for field, expected in expected_medians.items():
                    self.assertAlmostEqual(
                        expected, float(variant_rows[0][field]), places=10
                    )

    def test_reported_oxipng_and_rsedis_anchors(self) -> None:
        oxipng = self.target_rows("oxipng-configurations.csv")
        realworld_type_isolation = first_row(
            oxipng, "oxipng_realworld_single_thread", "typeiso_perf"
        )
        feature_type_isolation = first_row(
            oxipng, "oxipng_feature_matrix_four_threads", "typeiso_perf"
        )
        self.assertEqual("typed_plain", realworld_type_isolation["matched_reference"])
        self.assertAlmostEqual(
            -0.4408368329684609,
            float(realworld_type_isolation["matched_performance_delta_percent_median"]),
            places=10,
        )
        self.assertAlmostEqual(
            1.7280169101288712,
            float(feature_type_isolation["matched_performance_delta_percent_median"]),
            places=10,
        )

        rsedis = self.target_rows("rsedis-configurations.csv")
        unialloc = first_row(rsedis, "rsedis_allocator_matrix", "unialloc")
        thp_allowed = first_row(
            rsedis, "rsedis_thp_mechanism_matrix", "mimalloc_thp_default"
        )
        thp_disabled = first_row(
            rsedis, "rsedis_thp_mechanism_matrix", "mimalloc_thp_off"
        )
        self.assertAlmostEqual(
            2.6140566821845512,
            float(unialloc["plot_performance_delta_percent_median"]),
            places=10,
        )
        self.assertAlmostEqual(
            24.359375, float(unialloc["peak_rss_mib_median"]), places=10
        )
        self.assertAlmostEqual(
            68.390625, float(thp_allowed["peak_rss_mib_median"]), places=10
        )
        self.assertAlmostEqual(
            10.22265625, float(thp_disabled["peak_rss_mib_median"]), places=10
        )

    def test_svg_png_and_pdf_exports_have_slide_contracts(self) -> None:
        expected_headlines = {
            "01-ripgrep-configurations": "ripgrep: UniAlloc uses 6 MiB at +1.8% wall time",
            "02-oxipng-configurations": "Oxipng: UniAlloc stays below System RSS in both campaigns",
            "03-rsedis-configurations": "Rsedis: UniAlloc gains +2.6% throughput at 24.4 MiB RSS",
        }
        expected_target_labels = {
            "01-ripgrep-configurations": ("System", "UniAlloc", "Type Isolation"),
            "02-oxipng-configurations": ("System", "UniAlloc", "+ hugepage"),
            "03-rsedis-configurations": (
                "System (ptmalloc)",
                "UniAlloc",
                "mimalloc - THP disabled",
            ),
        }

        for stem in EXPECTED_STEMS:
            svg = (self.output_dir / f"{stem}.svg").read_text(encoding="utf-8")
            view_box_match = re.search(r'viewBox="0 0 ([0-9.]+) ([0-9.]+)"', svg)
            self.assertIsNotNone(view_box_match, stem)
            width, height = map(float, view_box_match.groups())
            self.assertAlmostEqual(16.0 / 9.0, width / height, places=10)
            self.assertIn("<text ", svg, stem)
            self.assertIn(expected_headlines[stem], svg, stem)
            for label in expected_target_labels[stem]:
                self.assertIn(f">{label}</text>", svg, f"{stem}:{label}")

            png = (self.output_dir / f"{stem}.png").read_bytes()
            self.assertEqual(b"\x89PNG\r\n\x1a\n", png[:8], stem)
            self.assertEqual((2400, 1350), struct.unpack(">II", png[16:24]), stem)

        pdf = (self.output_dir / "allocator-feature-thp-slide-pack.pdf").read_bytes()
        self.assertTrue(pdf.startswith(b"%PDF-"))
        self.assertEqual(3, len(re.findall(rb"/Type\s*/Page\b", pdf)))
        self.assertEqual(3, self.manifest["generator"]["pdf_pages"])
        self.assertEqual(
            {"width": 2400, "height": 1350},
            self.manifest["generator"]["png_dimensions"],
        )
        self.assertEqual(
            {"width": 16, "height": 9},
            self.manifest["generator"]["figure_size_inches"],
        )
        self.assertEqual(
            {"width": 1152, "height": 648},
            self.manifest["generator"]["svg_viewbox_points"],
        )

    def test_manifest_hashes_cover_every_visual_asset(self) -> None:
        manifest_assets = {
            asset["path"]: asset["sha256"] for asset in self.manifest["assets"]
        }
        expected_assets = {
            *(f"{stem}.svg" for stem in EXPECTED_STEMS),
            *(f"{stem}.png" for stem in EXPECTED_STEMS),
            "allocator-feature-thp-slide-pack.pdf",
        }
        self.assertEqual(expected_assets, set(manifest_assets))
        for relative_path, expected_hash in manifest_assets.items():
            self.assertEqual(
                expected_hash,
                sha256_file(self.output_dir / relative_path),
                relative_path,
            )

    def test_generated_text_is_english_only_and_omits_legacy_stems(self) -> None:
        text_paths = sorted(
            path
            for path in self.output_dir.rglob("*")
            if path.is_file() and path.suffix in {".csv", ".json", ".md", ".svg"}
        )
        self.assertTrue(text_paths)
        for path in text_paths:
            content = path.read_text(encoding="utf-8")
            self.assertIsNone(CJK_PATTERN.search(content), str(path))

        durable_metadata = "\n".join(
            (self.output_dir / name).read_text(encoding="utf-8")
            for name in ("README.md", "slide-data-manifest.json")
        )
        for stem in LEGACY_STEMS:
            self.assertNotIn(stem, durable_metadata)

    def test_complete_generation_is_byte_deterministic(self) -> None:
        second_output = Path(self.temporary_directory.name) / "second-generation"
        self.run_generator(second_output)

        first_files = {
            path.relative_to(self.output_dir)
            for path in self.output_dir.rglob("*")
            if path.is_file()
        }
        second_files = {
            path.relative_to(second_output)
            for path in second_output.rglob("*")
            if path.is_file()
        }
        self.assertEqual(first_files, second_files)
        for relative_path in sorted(first_files):
            self.assertEqual(
                sha256_file(self.output_dir / relative_path),
                sha256_file(second_output / relative_path),
                str(relative_path),
            )


if __name__ == "__main__":
    unittest.main()
