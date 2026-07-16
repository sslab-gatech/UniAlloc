#!/usr/bin/env python3

from __future__ import annotations

import json
import re
import subprocess
import tempfile
import unittest
from pathlib import Path
from xml.etree import ElementTree


REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
SCRIPT = REPOSITORY_ROOT / "evaluation/scripts/render_lifetime_mixed_fastpath_slide.py"
SUMMARY = (
    REPOSITORY_ROOT
    / "docs/evidence/lifetime-resident-index-20260715"
    / "mixed-filler-fastpath-swc-quick-summary.json"
)
CJK = re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff]")


class LifetimeMixedFastpathSlideTests(unittest.TestCase):
    def test_renderer_emits_source_bound_diagnostic_slide(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "slide.svg"
            subprocess.run(
                ["python3", str(SCRIPT), "--output", str(output)],
                cwd=REPOSITORY_ROOT,
                check=True,
                capture_output=True,
                text=True,
            )
            ElementTree.parse(output)
            svg = output.read_text(encoding="utf-8")

        for expected in (
            "11 → 3",
            "996,240 → 56,666",
            "940,170 → 1,376",
            "separate allocator binaries",
            "Peak RSS arm medians",
            "Paired median saving",
            "94.31%",
            "+1.485%",
            "Paired median saving +34.185%",
            "presentation_claim_eligible=false",
            "CURRENT DIAGNOSTIC · NOT CLAIM-GRADE",
        ):
            self.assertIn(expected, svg)
        self.assertIsNone(CJK.search(svg))

    def test_renderer_rejects_performance_claim_eligible_source(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            directory_path = Path(directory)
            summary_path = directory_path / "summary.json"
            summary = json.loads(SUMMARY.read_text(encoding="utf-8"))
            summary["performance_claim_eligible"] = True
            summary_path.write_text(json.dumps(summary), encoding="utf-8")
            completed = subprocess.run(
                [
                    "python3",
                    str(SCRIPT),
                    "--summary",
                    str(summary_path),
                    "--output",
                    str(directory_path / "slide.svg"),
                ],
                cwd=REPOSITORY_ROOT,
                capture_output=True,
                text=True,
            )
        self.assertNotEqual(0, completed.returncode)
        self.assertIn("performance_claim_eligible=false", completed.stderr)


if __name__ == "__main__":
    unittest.main()
