#!/usr/bin/env python3
"""Focused tests for the heap-lifetime ground-truth chart renderer."""

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
    ROOT
    / "evaluation"
    / "scripts"
    / "render_compiler_heap_lifetime_ground_truth_charts.py"
)
SVG_NAMESPACE = "{http://www.w3.org/2000/svg}"


def _site(semantics: str) -> dict[str, object]:
    return {
        "static_identity_joined": True,
        "compiler": {"hint_semantics": semantics},
    }


def _application(
    name: str,
    *,
    short: int,
    long: int,
    indeterminate: int,
    sites: list[dict[str, object]],
) -> dict[str, object]:
    completed = short + long + indeterminate
    return {
        "app": name,
        "runtime_exact_site_count": len(sites),
        "runtime_sites": sites,
        "outcomes": {
            "allocation_count": completed,
            "allocation_requested_bytes": completed * 10,
            "tracked_allocations": completed,
            "bypassed_unobserved_allocations": 0,
            "decisive_short_outcomes": short,
            "decisive_long_outcomes": long,
            "completed_indeterminate_outcomes": indeterminate,
            "live_right_censored_objects": 0,
            "decisive_short_requested_bytes": short * 10,
            "decisive_long_requested_bytes": long * 10,
            "completed_indeterminate_requested_bytes": indeterminate * 10,
            "live_inflight_age_lower_bound_bytes": 0,
        },
    }


def valid_summary() -> dict[str, object]:
    alpha = _application(
        "alpha",
        short=8,
        long=2,
        indeterminate=2,
        sites=[_site("unknown"), _site("proven_scoped_no_duration_claim")],
    )
    beta = _application(
        "beta",
        short=10,
        long=0,
        indeterminate=0,
        sites=[_site("unknown")],
    )
    # Use a byte-heavy second application so the first feature's 10-byte
    # denominator exercises the renderer's campaign-relative tiny-support mark.
    beta["outcomes"]["allocation_requested_bytes"] = 2_000  # type: ignore[index]
    beta["outcomes"]["decisive_short_requested_bytes"] = 2_000  # type: ignore[index]
    return {
        "schema_version": 1,
        "source": "compiler-heap-lifetime-ground-truth-join",
        "claim_boundary": (
            "Completed runtime outcomes define pressure-relative truth; compiler "
            "features remain observational."
        ),
        "parameters": {
            "minimum_decisive_outcomes": 3,
            "minimum_feature_sites": 2,
            "minimum_long_requested_byte_share": 0.8,
            "required_apps": ["alpha", "beta"],
        },
        "applications": [alpha, beta],
        "aggregate": {
            "allocation_count": 22,
            "allocation_requested_bytes": 2_120,
            "decisive_short_outcomes": 18,
            "decisive_long_outcomes": 2,
            "completed_indeterminate_outcomes": 2,
            "live_right_censored_objects": 0,
            "bypassed_unobserved_allocations": 0,
            "runtime_exact_site_count": 3,
            "matched_runtime_exact_site_count": 3,
            "exact_five_field_match_count": 1,
            "label_collection": {
                "force_track_all_verified": True,
                "selection_conditioned": False,
                "tracked_allocation_coverage_percent": 100.0,
                "unbiased_prevalence_claim_eligible": True,
            },
            "feature_correlations": [
                {
                    "feature": "reachable_cleanup_blocks_count=1",
                    "present_runtime_sites": 2,
                    "present_decisive_requested_bytes": 10,
                    "present_long_requested_bytes": 8,
                    "present_long_requested_byte_share": 0.8,
                    "absent_long_requested_byte_share": 0.1,
                    "selection_conditioned": False,
                    "interpretation": (
                        "observational association over fully tracked allocations"
                    ),
                },
                {
                    "feature": "allocation_in_natural_loop",
                    "present_runtime_sites": 2,
                    "present_decisive_requested_bytes": 100,
                    "present_long_requested_bytes": 20,
                    "present_long_requested_byte_share": 0.2,
                    "absent_long_requested_byte_share": None,
                    "selection_conditioned": False,
                    "interpretation": (
                        "observational association over fully tracked allocations"
                    ),
                },
            ],
            "long_dominant_candidates": [],
        },
    }


def run_renderer(
    summary_path: pathlib.Path, output_dir: pathlib.Path
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [
            sys.executable,
            str(SCRIPT),
            "--summary",
            str(summary_path),
            "--output-dir",
            str(output_dir),
            "--no-png",
        ],
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )


class CompilerHeapLifetimeGroundTruthChartTests(unittest.TestCase):
    def test_cli_renders_one_editable_overview_and_long_form_csv(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = pathlib.Path(temporary)
            summary_path = root / "summary.json"
            summary_path.write_text(json.dumps(valid_summary()), encoding="utf-8")
            output_dir = root / "charts"

            result = run_renderer(summary_path, output_dir)
            self.assertEqual(result.returncode, 0, result.stderr)
            svg_path = output_dir / "ground-truth-overview.svg"
            csv_path = output_dir / "ground-truth-overview.csv"
            self.assertTrue(svg_path.is_file())
            self.assertTrue(csv_path.is_file())

            xml_root = ET.parse(svg_path).getroot()
            self.assertEqual(xml_root.tag, SVG_NAMESPACE + "svg")
            self.assertEqual(xml_root.attrib["width"], "1600")
            self.assertEqual(xml_root.attrib["height"], "900")
            self.assertEqual(xml_root.attrib["viewBox"], "0 0 1600 900")
            self.assertEqual(xml_root.findall(".//" + SVG_NAMESPACE + "image"), [])

            rendered = svg_path.read_text(encoding="utf-8")
            self.assertIn("Short 66.67% (8)", rendered)
            self.assertIn("Long 16.67% (2)", rendered)
            self.assertIn("Indeterminate 16.67% (2)", rendered)
            self.assertIn("80.00%", rendered)
            self.assertIn("n=10 decisive B · 2 sites †", rendered)
            self.assertIn("Actionable 0.00% (0)", rendered)
            self.assertIn("Abstain 100.00% (3)", rendered)
            self.assertIn("0</text>", rendered)
            self.assertIn("eligible Long sites", rendered)
            self.assertIn("≥3 decisive outcomes", rendered)
            self.assertIn("≥80.00% Long byte share", rendered)
            self.assertIn("overlapping observational associations", rendered)
            self.assertIn("Abstained sites remain unclassified", rendered)
            self.assertNotIn("false negative", rendered.casefold())
            self.assertNotIn("misclassified", rendered.casefold())

            with csv_path.open(encoding="utf-8", newline="") as handle:
                rows = list(csv.DictReader(handle))
            self.assertEqual(len(rows), 11)
            alpha_long = next(
                row
                for row in rows
                if row["panel"] == "outcomes"
                and row["group"] == "alpha"
                and row["category"] == "Long"
            )
            self.assertEqual(alpha_long["count"], "2")
            self.assertEqual(alpha_long["denominator_count"], "12")
            self.assertAlmostEqual(float(alpha_long["share"]), 2 / 12)
            gate = next(row for row in rows if row["panel"] == "safe_profile_gate")
            self.assertEqual(gate["count"], "0")
            self.assertEqual(gate["threshold_minimum_decisive_outcomes"], "3")
            self.assertEqual(
                gate["threshold_minimum_long_requested_byte_share"], "0.8"
            )

    def test_classifier_coverage_is_derived_from_hint_semantics(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = pathlib.Path(temporary)
            document = valid_summary()
            document["applications"][0]["runtime_sites"][0]["compiler"][  # type: ignore[index]
                "hint_semantics"
            ] = "bounded_process_long_oracle"
            summary_path = root / "summary.json"
            summary_path.write_text(json.dumps(document), encoding="utf-8")
            output_dir = root / "charts"

            result = run_renderer(summary_path, output_dir)
            self.assertEqual(result.returncode, 0, result.stderr)
            rendered = (output_dir / "ground-truth-overview.svg").read_text(
                encoding="utf-8"
            )
            self.assertIn("Actionable 33.33% (1)", rendered)
            self.assertIn("Abstain 66.67% (2)", rendered)

    def test_non_finite_feature_share_fails_before_output_creation(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = pathlib.Path(temporary)
            document = valid_summary()
            document["aggregate"]["feature_correlations"][0][  # type: ignore[index]
                "present_long_requested_byte_share"
            ] = float("nan")
            summary_path = root / "summary.json"
            summary_path.write_text(json.dumps(document), encoding="utf-8")
            output_dir = root / "charts"

            result = run_renderer(summary_path, output_dir)
            self.assertEqual(result.returncode, 2)
            self.assertIn("must be finite", result.stderr)
            self.assertFalse(output_dir.exists())

    def test_candidate_below_preregistered_gate_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = pathlib.Path(temporary)
            document = valid_summary()
            document["aggregate"]["long_dominant_candidates"] = [  # type: ignore[index]
                {
                    "derived": {
                        "decisive_outcomes": 2,
                        "long_requested_byte_share": 1.0,
                    }
                }
            ]
            summary_path = root / "summary.json"
            summary_path.write_text(json.dumps(document), encoding="utf-8")
            output_dir = root / "charts"

            result = run_renderer(summary_path, output_dir)
            self.assertEqual(result.returncode, 2)
            self.assertIn("does not satisfy the safe profile gate", result.stderr)
            self.assertFalse(output_dir.exists())

    def test_unknown_future_hint_semantics_fail_closed(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = pathlib.Path(temporary)
            document = valid_summary()
            document["applications"][1]["runtime_sites"][0]["compiler"][  # type: ignore[index]
                "hint_semantics"
            ] = "future_duration_guess"
            summary_path = root / "summary.json"
            summary_path.write_text(json.dumps(document), encoding="utf-8")
            output_dir = root / "charts"

            result = run_renderer(summary_path, output_dir)
            self.assertEqual(result.returncode, 2)
            self.assertIn("hint_semantics is unsupported", result.stderr)
            self.assertFalse(output_dir.exists())


if __name__ == "__main__":
    unittest.main()
