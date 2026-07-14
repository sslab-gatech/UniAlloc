#!/usr/bin/env python3
"""Unit and smoke tests for the cross-allocator large-page experiment."""

from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent))
import cross_allocator_large_page_experiment as experiment


def neutral_row(**updates: object) -> dict[str, object]:
    row: dict[str, object] = {
        "source": "cross_allocator_large_page_workload",
        "passed": True,
        "host_thp_mode": "madvise",
        "memory_evidence_complete": True,
        "preload_token_observed": True,
        "anon_huge_delta_kib": 0,
        "hugetlb_delta_kib": 0,
        "peak_thp_coverage": 0.0,
        "process_thp_disabled": 0,
    }
    row.update(updates)
    return row


def paired_rows(values: list[tuple[float, float]]) -> dict[str, list[dict[str, object]]]:
    return {
        "on": [
            {"block": block, "metric": target}
            for block, (target, _baseline) in enumerate(values)
        ],
        "off": [
            {"block": block, "metric": baseline}
            for block, (_target, baseline) in enumerate(values)
        ],
    }


class CrossAllocatorLargePageExperimentTests(unittest.TestCase):
    def test_semantic_summary_normalizes_historical_mechanism_metadata(self) -> None:
        row = {
            "case": "unialloc_default",
            "label": "UniAlloc default",
            "mechanism": "selective-lifetime-thp",
            "ns_per_touch": 1.0,
            "max_effective_resident_kib": 2.0,
            "allocator_lifecycle_ns_per_allocation": 3.0,
            "observed_anon_huge_delta_kib": 0.0,
            "peak_hugetlb_kib": 0.0,
            "classification_failure_rate": 0.05,
            "policy_intent_placement_failure_rate": 1 / 3,
        }
        summary = experiment.median_summary([row], "unialloc-semantic")
        self.assertEqual(summary["mechanism"], "allocator-default")
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "summary.csv"
            experiment.write_summary_csv(
                {"case_summaries": {"unialloc_default": summary}}, path
            )
            self.assertNotIn(b"\r\n", path.read_bytes())

    def test_matrix_separates_true_allocator_controls_from_os_sensitivity(self) -> None:
        semantic = {case.name: case for case in experiment.SEMANTIC_CASES}
        self.assertEqual(
            semantic["unialloc_lifetime_thp_off"].mechanism,
            "lifetime-layout-thp-disabled",
        )
        self.assertEqual(
            semantic["unialloc_lifetime_thp_on"].mechanism,
            "selective-lifetime-thp",
        )
        cases = {case.name: case for case in experiment.neutral_cases()}
        self.assertEqual(cases["mimalloc_thp_on"].expected_backing, "thp")
        self.assertEqual(cases["jemalloc_thp_off"].expected_backing, "no-thp")
        self.assertEqual(cases["jemalloc_thp_on"].mechanism, "allocator-wide-thp")
        self.assertEqual(cases["gperftools_hugetlb"].mechanism, "explicit-hugetlb")
        self.assertEqual(cases["snmalloc_default"].mechanism, "os-eligibility")
        self.assertTrue(cases["snmalloc_os_thp_off"].disable_process_thp)

    def test_mimalloc_pair_holds_large_pages_and_purge_granularity_constant(self) -> None:
        cases = {case.name: case for case in experiment.neutral_cases()}
        off = dict(cases["mimalloc_thp_off"].environment)
        on = dict(cases["mimalloc_thp_on"].environment)
        self.assertEqual(off["MIMALLOC_ALLOW_THP"], "0")
        self.assertEqual(on["MIMALLOC_ALLOW_THP"], "1")
        for key in (
            "MIMALLOC_ALLOW_LARGE_OS_PAGES",
            "MIMALLOC_RESERVE_HUGE_OS_PAGES",
            "MIMALLOC_MINIMAL_PURGE_SIZE",
        ):
            self.assertEqual(on[key], off[key])

    def test_backing_validation_uses_actual_process_evidence(self) -> None:
        cases = {case.name: case for case in experiment.neutral_cases()}
        claimed_only = neutral_row(anon_huge_delta_kib=0, peak_thp_coverage=1.0)
        self.assertIn(
            "missing_anon_thp",
            experiment.validate_neutral_row(
                claimed_only, cases["jemalloc_thp_on"], 0.25
            ),
        )
        backed = neutral_row(anon_huge_delta_kib=262144, peak_thp_coverage=0.95)
        self.assertEqual(
            experiment.validate_neutral_row(backed, cases["jemalloc_thp_on"], 0.25),
            [],
        )
        hugetlb = neutral_row(hugetlb_delta_kib=262144)
        self.assertEqual(
            experiment.validate_neutral_row(
                hugetlb, cases["gperftools_hugetlb"], 0.25
            ),
            [],
        )

    def test_forced_off_semantic_pair_requires_identical_topology(self) -> None:
        common = {
            "prediction_trace_digest": "same",
            "routed_allocations": 10,
            "routed_deallocations": 10,
            "thp_extent_mappings": 4,
            "ordinary_extent_mappings": 4,
            "identity_region_assignments": 2,
            "slot_bump_allocations": 8,
            "slot_reuse_hits": 2,
        }
        rows = {
            "unialloc_lifetime_thp_on": dict(common),
            "unialloc_lifetime_thp_off": {
                **common,
                "process_thp_disable_requested": True,
                "process_thp_disabled": True,
                "observed_anon_huge_delta_kib": 0,
            },
        }
        self.assertEqual(experiment.semantic_topology_failures(rows), [])
        rows["unialloc_lifetime_thp_off"]["thp_extent_mappings"] = 3
        self.assertIn(
            "same_policy_topology:thp_extent_mappings",
            experiment.semantic_topology_failures(rows),
        )

    def test_paired_effect_reports_positive_lower_is_better_speedup(self) -> None:
        effect = experiment.paired_effect(
            paired_rows([(80.0, 100.0), (40.0, 50.0), (160.0, 200.0)]),
            target="on",
            baseline="off",
            field="metric",
            seed=7,
            resamples=500,
            equivalence_pct=3.0,
        )
        self.assertAlmostEqual(effect["paired_geomean_ratio"], 0.8)
        self.assertAlmostEqual(effect["improvement_pct"], 20.0)
        self.assertEqual(effect["conclusion"], "improved")

    def test_neutral_probe_smoke_is_deterministic_and_parseable(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            binary = Path(temporary) / "probe"
            subprocess.run(
                [
                    "cc",
                    "-std=c11",
                    "-O2",
                    "-Wall",
                    "-Wextra",
                    "-Werror",
                    "-fno-builtin-malloc",
                    str(experiment.NEUTRAL_SOURCE),
                    "-o",
                    str(binary),
                ],
                check=True,
            )
            command = [
                str(binary),
                "--objects",
                "1024",
                "--passes",
                "1",
                "--warmup-passes",
                "1",
                "--waves",
                "1",
                "--settle-ms",
                "0",
                "--seed",
                "19",
            ]
            rows = []
            for _ in range(2):
                completed = subprocess.run(
                    command,
                    text=True,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    check=True,
                )
                rows.append(json.loads(completed.stdout))
            self.assertTrue(rows[0]["passed"])
            self.assertEqual(rows[0]["checksum"], rows[1]["checksum"])
            self.assertEqual(rows[0]["source"], "cross_allocator_large_page_workload")


if __name__ == "__main__":
    unittest.main()
