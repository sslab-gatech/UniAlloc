#!/usr/bin/env python3
"""Unit tests for the cross-allocator large-page chart renderer."""

from __future__ import annotations

import csv
import json
import pathlib
import subprocess
import sys
import tempfile
import unittest
import xml.etree.ElementTree as ET


ROOT = pathlib.Path(__file__).resolve().parents[2]
SCRIPT = (
    ROOT / "evaluation" / "scripts" / "render_cross_allocator_large_page_charts.py"
)
SVG_NAMESPACE = "{http://www.w3.org/2000/svg}"


def valid_summary() -> dict[str, object]:
    return {
        "schema_version": 1,
        "measured_repeats": 7,
        "case_summaries": {
            "unialloc_lifetime_thp_on": {"samples": 7},
            "unialloc_lifetime_thp_off": {"samples": 7},
            "google_tcmalloc_temeraire": {"samples": 7},
            "snmalloc_default": {"samples": 7},
        },
        "google_tcmalloc_temeraire_gate": {
            "identity_claim_ready": True,
            "hugepage_mechanism_claim_ready": False,
            "hpaa_performance_causality_claim_ready": False,
            "measured_sample_count": 7,
            "backed_sample_count": 5,
            "excluded_blocks": [3, 6],
            "sample_evidence": [
                {
                    "block": block,
                    "physical_hugepage_backing_realized": block not in {3, 6},
                }
                for block in range(7)
            ],
        },
        "evidence_contract": {
            "cross_family_raw_timing_ranking_allowed": False,
            "google_tcmalloc_identity": (
                "fixed-revision Bazel link-time Temeraire/HPAA"
            ),
            "google_tcmalloc_hpaa_performance_causality_claim_ready": False,
            "gperftools_legacy_claim_role": "historical-only",
        },
        "slide_data": {
            "toggle_effects": [
                {
                    "label": "UniAlloc lifetime THP",
                    "mechanism": "selective lifetime-guided THP",
                    "speedup_pct": 12.4,
                    "speedup_ci_low_pct": 9.1,
                    "speedup_ci_high_pct": 15.2,
                    "max_resident_change_pct": -8.3,
                    "max_resident_ci_low_pct": -10.0,
                    "max_resident_ci_high_pct": -6.2,
                },
                {
                    "label": "mimalloc THP",
                    "mechanism": "process-wide THP",
                    "speedup_pct": 3.5,
                    "speedup_ci_low_pct": 1.0,
                    "speedup_ci_high_pct": 5.8,
                    "max_resident_change_pct": 28.0,
                    "max_resident_ci_low_pct": 22.0,
                    "max_resident_ci_high_pct": 34.0,
                },
            ],
            "endpoints": [
                {
                    "label": "UniAlloc THP on",
                    "mechanism": "selective lifetime-guided THP",
                    "ns_per_touch": 4.2,
                    "max_effective_resident_mib": 128.0,
                    "large_page_backing_mib": 96.0,
                    "pair": "unialloc",
                    "mode": "on",
                },
                {
                    "label": "UniAlloc THP off",
                    "mechanism": "selective lifetime-guided THP",
                    "ns_per_touch": 4.8,
                    "max_effective_resident_mib": 140.0,
                    "large_page_backing_mib": 0.0,
                    "pair": "unialloc",
                    "mode": "off",
                },
                {
                    "label": "Google TCMalloc / Temeraire",
                    "mechanism": "temeraire-hpaa",
                    "ns_per_touch": 4.6,
                    "max_effective_resident_mib": 132.0,
                    "large_page_backing_mib": 80.0,
                    "pair": "google-tcmalloc",
                    "mode": "default",
                },
                {
                    "label": "snmalloc default",
                    "mechanism": "allocator default",
                    "ns_per_touch": 5.1,
                    "max_effective_resident_mib": 121.5,
                    "large_page_backing_mib": 0.0,
                    "pair": "snmalloc",
                    "mode": "default",
                },
            ],
            "backing": [
                {
                    "label": "UniAlloc THP on",
                    "anon_thp_mib": 96.0,
                    "hugetlb_mib": 0.0,
                },
                {
                    "label": "Google TCMalloc / Temeraire",
                    "anon_thp_mib": 80.0,
                    "hugetlb_mib": 0.0,
                },
                {
                    "label": "Explicit HugeTLB",
                    "anon_thp_mib": 0.0,
                    "hugetlb_mib": 64.0,
                },
            ],
        },
    }


def run_renderer(summary: pathlib.Path, output_dir: pathlib.Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [
            sys.executable,
            str(SCRIPT),
            "--summary",
            str(summary),
            "--output-dir",
            str(output_dir),
            "--no-png",
        ],
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )


class CrossAllocatorLargePageChartTests(unittest.TestCase):
    def test_cli_renders_editable_16_by_9_svg_and_numeric_labels(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = pathlib.Path(temporary)
            summary_path = root / "summary.json"
            summary_path.write_text(json.dumps(valid_summary()), encoding="utf-8")
            output_dir = root / "charts"

            result = run_renderer(summary_path, output_dir)
            self.assertEqual(result.returncode, 0, result.stderr)
            for stem in ("incremental-effect", "endpoint-frontier", "actual-backing"):
                svg_path = output_dir / f"{stem}.svg"
                csv_path = output_dir / f"{stem}.csv"
                self.assertTrue(svg_path.is_file())
                self.assertTrue(csv_path.is_file())
                xml_root = ET.parse(svg_path).getroot()
                self.assertEqual(xml_root.tag, SVG_NAMESPACE + "svg")
                self.assertEqual(xml_root.attrib["width"], "1600")
                self.assertEqual(xml_root.attrib["height"], "900")
                self.assertEqual(xml_root.attrib["viewBox"], "0 0 1600 900")
                backgrounds = [
                    element
                    for element in xml_root.findall(SVG_NAMESPACE + "rect")
                    if element.attrib.get("id") == "background"
                ]
                self.assertEqual(len(backgrounds), 1)
                self.assertEqual(backgrounds[0].attrib.get("fill"), "#FFFFFF")
                self.assertEqual(xml_root.findall(".//" + SVG_NAMESPACE + "image"), [])
                self.assertGreater(
                    len(xml_root.findall(".//" + SVG_NAMESPACE + "text")), 5
                )

            incremental = (output_dir / "incremental-effect.svg").read_text(
                encoding="utf-8"
            )
            self.assertIn("+12.4%", incremental)
            self.assertIn("−8.3%", incremental)
            self.assertIn("dependent-pointer touch execution", incremental)
            self.assertIn("semantic Rust probe", incremental)
            self.assertIn("resident delta excludes unused pool capacity", incremental)
            self.assertIn("7 measured blocks per arm", incremental)
            self.assertNotIn("20 paired blocks", incremental)
            self.assertNotIn("gperftools", incremental.casefold())
            endpoint = (output_dir / "endpoint-frontier.svg").read_text(
                encoding="utf-8"
            )
            self.assertIn("4.20 ns/touch · 128.0 MiB", endpoint)
            self.assertIn("96.0 MiB large-page backing", endpoint)
            self.assertIn("0.0 MiB large-page backing", endpoint)
            self.assertNotIn('id="pareto-frontier"', endpoint)
            self.assertIn("Cross-family raw timing ranking withheld", endpoint)
            self.assertIn("Google TCMalloc / Temeraire HPAA", endpoint)
            self.assertIn("physical backing 5/7 measured samples", endpoint)
            self.assertIn("matched HPAA-off control", endpoint)
            backing = (output_dir / "actual-backing.svg").read_text(
                encoding="utf-8"
            )
            self.assertIn("96.0 MiB", backing)
            self.assertIn("THP 96.0 · HugeTLB 0.0", backing)
            self.assertIn(
                "anonymous THP from smaps; explicit HugeTLB from status", backing
            )
            self.assertIn("cross-family comparison is backing-only", backing)
            self.assertIn("Google TCMalloc / Temeraire HPAA", backing)
            self.assertIn("physical backing 5/7 measured samples", backing)
            self.assertIn("matched HPAA-off control", backing)

    def test_pareto_frontier_requires_cross_family_ranking_permission(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = pathlib.Path(temporary)
            document = valid_summary()
            summary_path = root / "summary.json"
            summary_path.write_text(json.dumps(document), encoding="utf-8")
            output_dir = root / "charts"

            result = run_renderer(summary_path, output_dir)
            self.assertEqual(result.returncode, 0, result.stderr)
            endpoint = (output_dir / "endpoint-frontier.svg").read_text(
                encoding="utf-8"
            )
            self.assertNotIn('id="pareto-frontier"', endpoint)
            self.assertNotIn("<title>Pareto frontier</title>", endpoint)
            self.assertIn("Cross-family raw timing ranking withheld", endpoint)

            document["evidence_contract"][  # type: ignore[index]
                "cross_family_raw_timing_ranking_allowed"
            ] = True
            summary_path.write_text(json.dumps(document), encoding="utf-8")
            result = run_renderer(summary_path, output_dir)
            self.assertEqual(result.returncode, 0, result.stderr)
            endpoint = (output_dir / "endpoint-frontier.svg").read_text(
                encoding="utf-8"
            )
            self.assertIn('id="pareto-frontier"', endpoint)

    def test_gperftools_legacy_is_visible_only_as_historical_evidence(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = pathlib.Path(temporary)
            document = valid_summary()
            document["case_summaries"]["gperftools_legacy_default"] = {  # type: ignore[index]
                "samples": 7,
                "label": "gperftools 2.18.1 legacy default",
            }
            document["slide_data"]["endpoints"].append(  # type: ignore[index]
                {
                    "label": "gperftools-legacy anon",
                    "mechanism": "gperftools_legacy",
                    "ns_per_touch": 5.0,
                    "max_effective_resident_mib": 130.0,
                    "large_page_backing_mib": 0.0,
                    "pair": "gperftools-legacy",
                    "mode": "off",
                }
            )
            document["slide_data"]["backing"].append(  # type: ignore[index]
                {
                    "label": "gperftools-legacy anon",
                    "anon_thp_mib": 0.0,
                    "hugetlb_mib": 0.0,
                }
            )
            summary_path = root / "summary.json"
            summary_path.write_text(json.dumps(document), encoding="utf-8")
            output_dir = root / "charts"

            result = run_renderer(summary_path, output_dir)
            self.assertEqual(result.returncode, 0, result.stderr)
            for filename in ("endpoint-frontier.svg", "actual-backing.svg"):
                rendered = (output_dir / filename).read_text(encoding="utf-8")
                self.assertIn("gperftools-legacy anon [historical]", rendered)
                self.assertIn(
                    "Historical only: gperftools 2.18.1 legacy default", rendered
                )

    def test_repeat_count_must_match_per_case_evidence(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = pathlib.Path(temporary)
            document = valid_summary()
            document["case_summaries"]["snmalloc_default"]["samples"] = 6  # type: ignore[index]
            summary_path = root / "summary.json"
            summary_path.write_text(json.dumps(document), encoding="utf-8")
            output_dir = root / "charts"

            result = run_renderer(summary_path, output_dir)
            self.assertEqual(result.returncode, 2)
            self.assertIn("measured_repeats", result.stderr)
            self.assertFalse(output_dir.exists())

    def test_csv_preserves_source_fields_and_values(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = pathlib.Path(temporary)
            document = valid_summary()
            summary_path = root / "summary.json"
            summary_path.write_text(json.dumps(document), encoding="utf-8")
            output_dir = root / "charts"
            result = run_renderer(summary_path, output_dir)
            self.assertEqual(result.returncode, 0, result.stderr)

            expected = {
                "incremental-effect.csv": (
                    [
                        "label",
                        "mechanism",
                        "speedup_pct",
                        "speedup_ci_low_pct",
                        "speedup_ci_high_pct",
                        "max_resident_change_pct",
                        "max_resident_ci_low_pct",
                        "max_resident_ci_high_pct",
                    ],
                    document["slide_data"]["toggle_effects"],  # type: ignore[index]
                ),
                "endpoint-frontier.csv": (
                    [
                        "label",
                        "mechanism",
                        "ns_per_touch",
                        "max_effective_resident_mib",
                        "large_page_backing_mib",
                        "pair",
                        "mode",
                    ],
                    document["slide_data"]["endpoints"],  # type: ignore[index]
                ),
                "actual-backing.csv": (
                    ["label", "anon_thp_mib", "hugetlb_mib"],
                    document["slide_data"]["backing"],  # type: ignore[index]
                ),
            }
            for filename, (fields, source_rows) in expected.items():
                self.assertNotIn(b"\r\n", (output_dir / filename).read_bytes())
                with (output_dir / filename).open(encoding="utf-8", newline="") as handle:
                    reader = csv.DictReader(handle)
                    rows = list(reader)
                self.assertEqual(reader.fieldnames, fields)
                self.assertEqual(len(rows), len(source_rows))
                for rendered, source in zip(rows, source_rows, strict=True):
                    self.assertEqual(rendered["label"], source["label"])
                    for field in fields[1:]:
                        if isinstance(source[field], (int, float)):
                            self.assertAlmostEqual(float(rendered[field]), source[field])
                        else:
                            self.assertEqual(rendered[field], source[field])

    def test_invalid_schema_fails_closed_without_output_directory(self) -> None:
        invalid_documents = []
        missing_field = valid_summary()
        del missing_field["slide_data"]["endpoints"][0]["ns_per_touch"]  # type: ignore[index]
        invalid_documents.append(missing_field)

        inverted_interval = valid_summary()
        inverted_interval["slide_data"]["toggle_effects"][0][  # type: ignore[index]
            "speedup_ci_low_pct"
        ] = 13.0
        invalid_documents.append(inverted_interval)

        nonfinite = valid_summary()
        nonfinite["slide_data"]["backing"][0]["anon_thp_mib"] = float("nan")  # type: ignore[index]
        invalid_documents.append(nonfinite)

        negative_endpoint = valid_summary()
        negative_endpoint["slide_data"]["endpoints"][0][  # type: ignore[index]
            "max_effective_resident_mib"
        ] = -1.0
        invalid_documents.append(negative_endpoint)

        missing_ranking_boundary = valid_summary()
        del missing_ranking_boundary["evidence_contract"][  # type: ignore[index]
            "cross_family_raw_timing_ranking_allowed"
        ]
        invalid_documents.append(missing_ranking_boundary)

        for index, document in enumerate(invalid_documents):
            with self.subTest(index=index), tempfile.TemporaryDirectory() as temporary:
                root = pathlib.Path(temporary)
                summary_path = root / "summary.json"
                summary_path.write_text(json.dumps(document), encoding="utf-8")
                output_dir = root / "charts"
                result = run_renderer(summary_path, output_dir)
                self.assertEqual(result.returncode, 2)
                self.assertIn("error:", result.stderr)
                self.assertFalse(output_dir.exists())


if __name__ == "__main__":
    unittest.main()
