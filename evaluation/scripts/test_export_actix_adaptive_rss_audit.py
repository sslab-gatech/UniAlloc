#!/usr/bin/env python3

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from evaluation.scripts import export_actix_adaptive_rss_audit as audit


class ActixAdaptiveRssAuditTests(unittest.TestCase):
    def test_linear_fit_recovers_iteration_scaled_rss(self) -> None:
        fit = audit.linear_fit([10.0, 20.0, 30.0], [2.0, 3.0, 4.0])
        self.assertAlmostEqual(1.0, fit["intercept_mib"])
        self.assertAlmostEqual(
            1024.0 * 1024.0 / 10.0, fit["slope_bytes_per_measured_iteration"]
        )
        self.assertAlmostEqual(1.0, fit["r_squared"])

    def test_export_retains_all_eighteen_process_observations(self) -> None:
        rows = []
        for round_number in range(6):
            for offset, variant in enumerate(audit.VARIANTS, start=1):
                rows.append(
                    {
                        "measurement_path": f"measurement-{round_number}-{variant}.json",
                        "measurement_sha256": "a" * 64,
                        "measured_iteration_count": 1000 * offset + round_number,
                        "peak_rss_mib": 10.0 + offset,
                        "performance_seconds": 0.001 * offset,
                        "phase": "warmup" if round_number == 0 else "measurement",
                        "round": round_number,
                        "sample_path": f"sample-{round_number}-{variant}.json",
                        "sample_sha256": "b" * 64,
                        "variant": variant,
                    }
                )
        with tempfile.TemporaryDirectory() as raw:
            output = Path(raw) / "audit.json"
            with mock.patch.object(audit, "collect_rows", return_value=rows):
                audit.export(Path(raw), output)
            result = json.loads(output.read_text(encoding="utf-8"))
        self.assertEqual(18, len(result["observations"]))
        self.assertFalse(result["interpretation"]["equal_work_rss_eligible"])
        self.assertEqual(set(audit.VARIANTS), set(result["variant_summaries"]))


if __name__ == "__main__":
    unittest.main()
