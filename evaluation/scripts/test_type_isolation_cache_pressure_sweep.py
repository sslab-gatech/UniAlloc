#!/usr/bin/env python3
"""Contract tests for the controlled Type Isolation cache-pressure sweep."""

from __future__ import annotations

import argparse
import hashlib
import importlib.util
import tempfile
from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "evaluation" / "scripts" / "type_isolation_cache_pressure_sweep.py"
SPEC = importlib.util.spec_from_file_location("typeiso_cache_pressure", SCRIPT)
assert SPEC is not None and SPEC.loader is not None
pressure = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(pressure)


class TypeIsolationCachePressureSweepTests(unittest.TestCase):
    def test_positive_csv_parser_rejects_empty_and_nonpositive_values(self) -> None:
        self.assertEqual(
            (24, 64, 65536),
            pressure.parse_positive_csv("24,64,65536", "sizes"),
        )
        for value in ("", "0", "24,-1", "abc"):
            with self.subTest(value=value), self.assertRaises(
                argparse.ArgumentTypeError
            ):
                pressure.parse_positive_csv(value, "sizes")

    def test_sha256_file_hashes_exact_bytes(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "evidence.bin"
            payload = b"type-isolation-pressure\x00evidence"
            path.write_bytes(payload)
            self.assertEqual(hashlib.sha256(payload).hexdigest(), pressure.sha256_file(path))

    def test_generated_crate_is_standalone_and_uses_pinned_path_dependency(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            crate = Path(temporary) / "probe"
            manifest = pressure.write_probe_crate(crate)
            text = manifest.read_text(encoding="utf-8")
            self.assertIn("[workspace]", text)
            self.assertIn(f'path = "{pressure.UNIALLOC_DIR}"', text)
            self.assertIn('features = ["stats", "type_isolation"]', text)
            self.assertIn(
                "MAX_PLAIN_TYPE_CACHE_RETAINED_ENTRIES", pressure.RUST_PROBE
            )
            self.assertIn("type_cache_admission_stats_snapshot", pressure.RUST_PROBE)
            self.assertIn("rejected_registry_pressure_events", pressure.RUST_PROBE)
            self.assertIn("depot_current_entries", pressure.RUST_PROBE)
            self.assertIn("per_thread_plain_entry_budget", pressure.RUST_PROBE)
            self.assertEqual(
                (pressure.REPO_ROOT / "Cargo.lock").read_bytes(),
                (crate / "Cargo.lock").read_bytes(),
            )

    def test_generated_crate_can_target_a_frozen_implementation(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            crate = root / "probe"
            frozen_unialloc = root / "frozen" / "unialloc"
            frozen_unialloc.mkdir(parents=True)
            frozen_lock = root / "frozen" / "Cargo.lock"
            frozen_lock.write_text("# frozen lock\n", encoding="utf-8")

            manifest = pressure.write_probe_crate(
                crate,
                unialloc_dir=frozen_unialloc,
                lockfile=frozen_lock,
            )

            self.assertIn(f'path = "{frozen_unialloc}"', manifest.read_text())
            self.assertEqual(frozen_lock.read_bytes(), (crate / "Cargo.lock").read_bytes())

    def test_summary_preserves_capacity_and_outside_fraction_ranges(self) -> None:
        rows = [
            {
                "requested_size": 16384,
                "mode": "same",
                "burst": 256,
                "retained_entries": 5,
                "retained_bytes": 81920,
                "first_non_retained_free": 6,
                "non_retained_free_fraction": 251 / 256,
            },
            {
                "requested_size": 16384,
                "mode": "same",
                "burst": 256,
                "retained_entries": 4,
                "retained_bytes": 65536,
                "first_non_retained_free": 5,
                "non_retained_free_fraction": 252 / 256,
            },
        ]
        summary = pressure.summarize(rows, 256)
        self.assertEqual(1, len(summary))
        self.assertEqual([5, 6], summary[0]["first_non_retained_free_range"])
        self.assertEqual(
            [4, 5], summary[0]["at_maximum_burst"]["retained_entries_range"]
        )
        self.assertEqual(
            [251 / 256, 252 / 256],
            summary[0]["at_maximum_burst"]["non_retained_fraction_range"],
        )

    def test_summary_counts_l1_and_depot_retention_together(self) -> None:
        rows = [
            {
                "requested_size": 1024,
                "mode": "same",
                "burst": 256,
                "retained_entries": 193,
                "retained_bytes": 193 * 1024,
                "l1_retained_entries": 129,
                "depot_retained_entries": 64,
                "first_non_retained_free": 194,
                "non_retained_free_fraction": 63 / 256,
            }
        ]
        summary = pressure.summarize(rows, 256)
        self.assertEqual(
            [193, 193], summary[0]["at_maximum_burst"]["retained_entries_range"]
        )
        self.assertEqual(193, summary[0]["max_observed_retained_entries"])
        self.assertEqual(129, summary[0]["max_observed_l1_retained_entries"])
        self.assertEqual(64, summary[0]["max_observed_depot_retained_entries"])


if __name__ == "__main__":
    unittest.main()
