#!/usr/bin/env python3
"""Unit tests for the Rsedis THP runtime matrix."""

from __future__ import annotations

import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path

from evaluation.scripts import rsedis_thp_matrix as matrix


class EnvironmentTests(unittest.TestCase):
    def test_clean_allocator_environment_removes_inherited_controls(self) -> None:
        cleaned, removed = matrix.clean_allocator_environment(
            {
                "PATH": "/bin",
                "KEEP_ME": "yes",
                "LD_PRELOAD": "/tmp/foreign.so",
                "GLIBC_TUNABLES": "glibc.pthread.rseq=0",
                "MIMALLOC_ALLOW_THP": "9",
                "MIMALLOC_VERBOSE": "1",
                "TCMALLOC_MEMFS_MALLOC_PATH": "/mnt/huge",
                "TCMALLOC_SAMPLE_PARAMETER": "1",
                "MALLOC_CONF": "dirty_decay_ms:0",
            }
        )
        self.assertEqual(cleaned, {"PATH": "/bin", "KEEP_ME": "yes"})
        self.assertEqual(
            set(removed),
            {
                "LD_PRELOAD",
                "GLIBC_TUNABLES",
                "MIMALLOC_ALLOW_THP",
                "MIMALLOC_VERBOSE",
                "TCMALLOC_MEMFS_MALLOC_PATH",
                "TCMALLOC_SAMPLE_PARAMETER",
                "MALLOC_CONF",
            },
        )

    def test_runtime_cells_have_only_intended_allocator_settings(self) -> None:
        base = {
            "PATH": "/bin",
            "MIMALLOC_ALLOW_THP": "inherited",
            "MIMALLOC_PAGE_RESET": "inherited",
            "TCMALLOC_MEMFS_MALLOC_PATH": "/inherited",
            "TCMALLOC_SAMPLE_PARAMETER": "inherited",
            "LD_PRELOAD": "/inherited.so",
        }
        paths = {
            "mimalloc_binary": Path("/tmp/rsedis-mimalloc"),
            "system_binary": Path("/tmp/rsedis-system"),
            "tcmalloc_library": Path("/tmp/libtcmalloc.so"),
        }
        default = matrix.make_runtime_cell(
            "mimalloc_thp_default", source_environment=base, **paths
        )
        off = matrix.make_runtime_cell("mimalloc_thp_off", source_environment=base, **paths)
        tcmalloc = matrix.make_runtime_cell(
            "tcmalloc_default", source_environment=base, **paths
        )
        self.assertEqual(default.environment["MIMALLOC_ALLOW_THP"], "1")
        self.assertEqual(off.environment["MIMALLOC_ALLOW_THP"], "0")
        for cell in (default, off):
            self.assertEqual(cell.environment["MIMALLOC_ALLOW_LARGE_OS_PAGES"], "0")
            self.assertEqual(cell.environment["MIMALLOC_RESERVE_HUGE_OS_PAGES"], "0")
            self.assertNotIn("LD_PRELOAD", cell.environment)
            self.assertFalse(any(key.startswith("TCMALLOC_") for key in cell.environment))
        self.assertEqual(tcmalloc.environment["LD_PRELOAD"], "/tmp/libtcmalloc.so")
        self.assertEqual(tcmalloc.environment["TCMALLOC_MEMFS_MALLOC_PATH"], "")
        self.assertFalse(any(key.startswith("MIMALLOC_") for key in tcmalloc.environment))
        self.assertNotIn("TCMALLOC_SAMPLE_PARAMETER", tcmalloc.environment)


class ParsingTests(unittest.TestCase):
    def test_parse_redis_csv(self) -> None:
        parsed = matrix.parse_redis_csv(b'"SET","61234.50"\n"GET","71,111.25"\n')
        self.assertEqual(parsed, {"SET": 61234.5, "GET": 71111.25})

    def test_parse_memory_evidence(self) -> None:
        status = matrix.parse_status_text(
            "Name:\trsedis\nVmRSS:\t1234 kB\nVmHWM:\t2345 kB\nTHP_enabled:\t1\n"
        )
        smaps = matrix.parse_smaps_rollup_text(
            "Rss: 1234 kB\nAnonHugePages: 2048 kB\n"
            "Shared_Hugetlb: 1024 kB\nPrivate_Hugetlb: 2048 kB\n"
        )
        snapshot = {"status": status, "smaps_rollup": smaps}
        self.assertEqual(status["THP_enabled"], 1)
        self.assertEqual(smaps["AnonHugePages_kib"], 2048)
        self.assertEqual(smaps["Hugetlb_kib"], 3072)
        self.assertTrue(matrix.required_memory_evidence_present(snapshot))

    def test_rotation_moves_each_cell_to_each_order_position(self) -> None:
        orders = [matrix.rotated_order(index) for index in range(3)]
        for position in range(3):
            self.assertEqual({order[position] for order in orders}, set(matrix.VARIANTS))

    def test_persisted_command_shape_is_json_serializable(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            raw_dir = Path(directory)
            result = matrix.run_command(
                [Path("/bin/true")],
                cwd=raw_dir,
                env={"PATH": "/bin"},
                timeout=5,
            )
            persisted = matrix.persist_command(raw_dir, raw_dir / "command", result)
            self.assertEqual(persisted["command"], ["/bin/true"])
            self.assertEqual(json.loads(json.dumps(persisted)), persisted)

    def test_exact_mapped_path_accepts_normalized_requested_library(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            actual = root / "real" / "libtcmalloc.so"
            actual.parent.mkdir()
            actual.write_bytes(b"library")
            requested = root / "requested" / "libtcmalloc.so"
            requested.parent.mkdir()
            requested.symlink_to(actual)
            maps = f"7f000000-7f001000 r-xp 00000000 08:01 123 {actual}\n"
            self.assertEqual(
                matrix.exact_mapped_path_matches(maps, requested),
                [str(actual.resolve())],
            )

    def test_exact_mapped_path_rejects_same_basename_from_wrong_directory(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            requested = root / "expected" / "libtcmalloc.so"
            wrong = root / "wrong" / "libtcmalloc.so"
            requested.parent.mkdir()
            wrong.parent.mkdir()
            requested.write_bytes(b"expected")
            wrong.write_bytes(b"wrong")
            maps = f"7f000000-7f001000 r-xp 00000000 08:01 123 {wrong}\n"
            self.assertEqual(matrix.exact_mapped_path_matches(maps, requested), [])


def successful_record(
    variant: str,
    round_index: int,
    *,
    rps: float,
    elapsed: float,
    peak: int,
    anon: int,
) -> dict[str, object]:
    return {
        "variant": variant,
        "round_index": round_index,
        "phase": "measured",
        "success": True,
        "benchmark": {"elapsed_seconds": elapsed},
        "combined_effective_requests_per_second": rps,
        "throughput_requests_per_second": {"SET": rps * 0.9, "GET": rps * 1.1},
        "idle": {
            "status": {"VmRSS_kib": peak // 2},
            "smaps_rollup": {"AnonHugePages_kib": 0},
        },
        "post": {
            "status": {"VmRSS_kib": peak - 100, "VmHWM_kib": peak, "THP_enabled": 1},
            "smaps_rollup": {"AnonHugePages_kib": anon, "Hugetlb_kib": 0},
        },
    }


class SummaryTests(unittest.TestCase):
    def test_summary_uses_only_successful_measured_records(self) -> None:
        records = [
            successful_record("mimalloc_thp_default", 1, rps=100, elapsed=2, peak=2000, anon=1024),
            successful_record("mimalloc_thp_default", 2, rps=120, elapsed=1.8, peak=2200, anon=2048),
            {
                **successful_record("mimalloc_thp_default", 3, rps=999, elapsed=1, peak=999, anon=999),
                "success": False,
            },
        ]
        summary = matrix.summarize_variant(
            "mimalloc_thp_default", records, repetitions=3
        )
        self.assertEqual(summary["successful_repetitions"], 2)
        self.assertFalse(summary["all_successful"])
        self.assertEqual(summary["combined_effective_rps_median"], 110)
        self.assertEqual(summary["peak_rss_kib_median"], 2100)
        self.assertEqual(summary["post_anon_huge_pages_kib_median"], 1536)

    def test_paired_comparisons_are_round_matched(self) -> None:
        records: list[dict[str, object]] = []
        for round_index in (1, 2):
            records.extend(
                [
                    successful_record("mimalloc_thp_default", round_index, rps=100, elapsed=2, peak=2000, anon=2048),
                    successful_record("mimalloc_thp_off", round_index, rps=105, elapsed=1.9, peak=1800, anon=0),
                    successful_record("tcmalloc_default", round_index, rps=95, elapsed=2.1, peak=2200, anon=0),
                ]
            )
        comparisons = matrix.paired_comparisons(records, warmups=1, repetitions=2)
        off, tcmalloc = comparisons
        self.assertEqual(off["paired_rounds"], 2)
        self.assertAlmostEqual(off["median_throughput_ratio"], 1.05)
        self.assertAlmostEqual(off["median_peak_rss_ratio"], 0.9)
        self.assertEqual(off["median_post_anon_huge_pages_delta_kib"], -2048)
        self.assertAlmostEqual(tcmalloc["median_throughput_ratio"], 0.95)


class CliTests(unittest.TestCase):
    def test_nonempty_raw_directory_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            executable = root / "executable"
            executable.write_text("#!/bin/sh\n", encoding="utf-8")
            executable.chmod(0o755)
            library = root / "lib.so"
            library.write_bytes(b"library")
            raw = root / "raw"
            raw.mkdir()
            (raw / "existing").write_text("evidence", encoding="utf-8")
            with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
                matrix.parse_args(
                    [
                        "--mimalloc-rsedis-binary",
                        str(executable),
                        "--system-rsedis-binary",
                        str(executable),
                        "--gperftools-library",
                        str(library),
                        "--raw-dir",
                        str(raw),
                    ]
                )


if __name__ == "__main__":
    unittest.main()
