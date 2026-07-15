#!/usr/bin/env python3
"""Unit and smoke tests for the cross-allocator large-page experiment."""

from __future__ import annotations

import json
import argparse
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock
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
    def test_custom_build_root_derives_all_google_artifact_paths(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "google"
            args = experiment.parse_args(
                [
                    "--output-dir",
                    str(Path(temporary) / "run"),
                    "--google-tcmalloc-build-root",
                    str(root),
                ]
            )
            self.assertEqual(args.google_tcmalloc_provenance, root / "provenance.json")
            self.assertEqual(
                args.google_tcmalloc_binary,
                root / "bin/cross_allocator_large_page_workload_google_tcmalloc",
            )
            self.assertEqual(
                args.google_tcmalloc_control_binary,
                root / "bin/cross_allocator_large_page_workload_system",
            )

    def test_blocked_google_arm_writes_output_local_status(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary) / "blocked-run"
            with (
                mock.patch.object(
                    experiment,
                    "google_tcmalloc_provenance",
                    side_effect=RuntimeError("fixed-revision Bazel proof missing"),
                ),
                mock.patch.object(experiment, "host_snapshot", return_value="host\n"),
            ):
                returncode = experiment.main(
                    [
                        "--output-dir",
                        str(output),
                        "--skip-build",
                    ]
                )
            self.assertEqual(returncode, 2)
            status = json.loads(
                (output / "runner-manifest.json").read_text(encoding="utf-8")
            )
            self.assertEqual(status["status"], "BLOCKED")
            self.assertFalse(status["claim_grade"])
            self.assertFalse(status["fallback_attempted"])
            self.assertTrue((output / "manifest.json").is_file())

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
        cases = {case.name: case for case in experiment.neutral_cases(True)}
        self.assertEqual(
            cases["google_tcmalloc_temeraire"].mechanism, "temeraire-hpaa"
        )
        self.assertEqual(cases["mimalloc_thp_on"].expected_backing, "thp")
        self.assertEqual(cases["jemalloc_thp_off"].expected_backing, "no-thp")
        self.assertEqual(cases["jemalloc_thp_on"].mechanism, "allocator-wide-thp")
        self.assertEqual(
            cases["gperftools_legacy_hugetlb"].mechanism,
            "legacy-explicit-hugetlb",
        )
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
        cases = {case.name: case for case in experiment.neutral_cases(True)}
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
                hugetlb, cases["gperftools_legacy_hugetlb"], 0.25
            ),
            [],
        )

    def test_gperftools_is_legacy_opt_in_and_never_replaces_google_tcmalloc(self) -> None:
        default_names = {case.name for case in experiment.neutral_cases()}
        legacy_names = {case.name for case in experiment.neutral_cases(True)}
        self.assertIn("google_tcmalloc_temeraire", default_names)
        self.assertNotIn("gperftools_legacy_default", default_names)
        self.assertIn("gperftools_legacy_default", legacy_names)
        self.assertIn("gperftools_legacy_hugetlb", legacy_names)

    def test_google_tcmalloc_provenance_fails_closed_on_wrong_identity(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            binary = root / "probe"
            binary.write_bytes(b"gperftools legacy payload")
            provenance_path = root / "provenance.json"
            provenance_path.write_text(
                json.dumps(
                    {
                        "status": "VERIFIED",
                        "claim_eligible": True,
                        "fallback_allowed": False,
                        "repository": "https://github.com/gperftools/gperftools.git",
                        "commit": experiment.google_tcmalloc.GOOGLE_TCMALLOC_COMMIT,
                        "malloc_target": experiment.google_tcmalloc.MALLOC_TARGET,
                        "hugepage_allocator_identity": "Temeraire / HugePageAwareAllocator (HPAA)",
                        "gperftools_legacy_eligible_as_google_tcmalloc": False,
                        "verification": {
                            "binary_sha256": experiment.sha256_file(binary)
                        },
                    }
                ),
                encoding="utf-8",
            )
            args = argparse.Namespace(
                skip_build=True,
                google_tcmalloc_provenance=provenance_path,
                google_tcmalloc_build_root=root,
                google_tcmalloc_binary=binary,
                google_tcmalloc_control_binary=root / "control",
                bazel="bazel",
            )
            with self.assertRaisesRegex(RuntimeError, "BLOCKED"):
                experiment.google_tcmalloc_provenance(args)

    def test_google_tcmalloc_provenance_rejects_non_object_root(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            provenance_path = root / "provenance.json"
            provenance_path.write_text("[]\n", encoding="utf-8")
            args = argparse.Namespace(
                skip_build=True,
                google_tcmalloc_provenance=provenance_path,
                google_tcmalloc_build_root=root,
                google_tcmalloc_binary=root / "probe",
                google_tcmalloc_control_binary=root / "control",
                bazel="bazel",
            )
            with self.assertRaisesRegex(RuntimeError, "root must be an object"):
                experiment.google_tcmalloc_provenance(args)

    def test_neutral_commands_share_the_bazel_codegen_control(self) -> None:
        cases = {case.name: case for case in experiment.neutral_cases()}
        args = argparse.Namespace(
            cpu=3,
            numa_node=0,
            objects=1024,
            slot_bytes=4096,
            passes=1,
            warmup_passes=1,
            waves=1,
            settle_ms=0,
        )
        google = Path("/tmp/google-tcmalloc-probe")
        control = Path("/tmp/bazel-system-control")
        google_command = experiment.neutral_command(
            cases["google_tcmalloc_temeraire"], args, 7, google, control
        )
        glibc_command = experiment.neutral_command(
            cases["glibc_default"], args, 7, google, control
        )
        mimalloc_command = experiment.neutral_command(
            cases["mimalloc_thp_on"], args, 7, google, control
        )
        self.assertIn(str(google), google_command)
        self.assertIn(str(control), glibc_command)
        self.assertIn(str(control), mimalloc_command)

    def test_google_hpaa_claim_gate_excludes_each_unbacked_sample(self) -> None:
        rows = [
            {
                "block": 0,
                "anon_huge_delta_kib": 2048,
                "hugetlb_delta_kib": 0,
                "peak_thp_coverage": 0.5,
            },
            {
                "block": 1,
                "anon_huge_delta_kib": 0,
                "hugetlb_delta_kib": 0,
                "peak_thp_coverage": 0.0,
            },
        ]
        gate = experiment.google_tcmalloc_hugepage_claim_gate(rows, 0.25)
        self.assertTrue(gate["identity_claim_ready"])
        self.assertFalse(gate["hugepage_mechanism_claim_ready"])
        self.assertFalse(gate["hpaa_performance_causality_claim_ready"])
        self.assertEqual(gate["backed_sample_count"], 1)
        self.assertEqual(gate["excluded_blocks"], [1])

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
