import json
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
import lifetime_thp_allocator_experiment as experiment


def evidence_row(*, delta_kib: int, fault_alloc: int = 1, collapse_alloc: int = 0) -> dict:
    policy = "long-thp" if delta_kib > 0 else "ordinary-segregated"
    return {
        "source": "lifetime_hugepage_allocator_probe",
        "passed": True,
        "policy": policy,
        "total_allocations": 10,
        "allocation_ns": 100,
        "ephemeral_release_ns": 20,
        "wave_ns": 30,
        "teardown_ns": 10,
        "baseline_anon_hugepages_kib": 2048,
        "peak_anon_hugepages_kib": 2048 + delta_kib,
        "post_epoch_anon_hugepages_kib": 2048 + delta_kib,
        "steady_anon_hugepages_kib": 2048 + delta_kib,
        "wave_peak_anon_hugepages_kib": 0,
        "wave_max_anon_hugepages_kib": 0,
        "thp_actual_backing_observed": delta_kib > 0,
        "thp_backing_gate_passed": True,
        "thp_enabled_mode": "madvise",
        "smaps_rollup_available": True,
        "smaps_rollup_parse_success": True,
        "peak_process_thp_delta_coverage": 0.5 if delta_kib > 0 else 0.0,
        "peak_rss_kib": 1000,
        "peak_anon_kib": 800,
        "peak_hugetlb_kib": 0,
        "post_epoch_rss_kib": 900,
        "post_epoch_anon_kib": 700,
        "post_epoch_hugetlb_kib": 0,
        "steady_rss_kib": 800,
        "steady_anon_kib": 600,
        "steady_hugetlb_kib": 0,
        "wave_peak_observed": False,
        "wave_peak_rss_kib": 0,
        "wave_peak_anon_kib": 0,
        "wave_peak_hugetlb_kib": 0,
        "wave_peak_effective_resident_kib": 0,
        "peak_effective_resident_kib": 1000,
        "post_epoch_effective_resident_kib": 900,
        "steady_effective_resident_kib": 800,
        "max_effective_resident_kib": 1000,
        "overall_max_effective_resident_kib": 1000,
        "vmstat_scope": "system-wide-delta",
        "vmstat_thp_fault_alloc_delta": fault_alloc,
        "vmstat_thp_collapse_alloc_delta": collapse_alloc,
        "thp_extent_mappings": 2 if delta_kib > 0 else 0,
        "thp_candidate_extent_mappings": 0,
        "thp_advice_attempts": 2 if delta_kib > 0 else 0,
        "thp_advice_successes": 2 if delta_kib > 0 else 0,
        "thp_advice_failures": 0,
        "thp_collapse_attempts": collapse_alloc,
        "thp_collapse_eligible_extents": collapse_alloc,
        "thp_collapse_successes": collapse_alloc,
        "thp_collapse_failures": 0,
        "thp_collapse_last_error_code": 0,
        "lifetime_peak_thp_extents": 2 if delta_kib > 0 else 0,
        "steady_thp_extents": 2 if delta_kib > 0 else 0,
        "steady_thp_collapse_confirmed_extents": 0,
        "retained_byte_epochs": 2 * 1024 * 1024,
        "hugetlb_byte_epochs": 0,
        "ordinary_byte_epochs": 0 if delta_kib > 0 else 2 * 1024 * 1024,
        "thp_backend_vma_byte_epochs": 2 * 1024 * 1024 if delta_kib > 0 else 0,
        "byte_epoch_accounting_consistent": True,
        "first_epoch_advance_ns": 0,
        "epoch_advance_ns": 0,
        "wave_evidence_sampling_ns": 0,
        "placement_metric_semantics": "policy-intent",
        "placement_success_rate": 0.9,
        "placement_failure_rate": 0.1,
        "placement_precision": 0.9,
        "placement_recall": 0.9,
        "policy_intent_placement_success_rate": 0.9,
        "policy_intent_placement_failure_rate": 0.1,
        "policy_intent_placement_precision": 0.9,
        "policy_intent_placement_recall": 0.9,
        "runtime_confirmed_placement_available": False,
        "runtime_confirmed_placement_success_rate": None,
        "runtime_confirmed_placement_failure_rate": None,
        "runtime_confirmed_placement_precision": None,
        "runtime_confirmed_placement_recall": None,
    }


class LifetimeThpAllocatorExperimentTests(unittest.TestCase):
    def test_matrix_keeps_paired_static_confidence_epoch_and_backing_controls(self) -> None:
        matrix = {case.name: case for case in experiment.cases(80)}
        self.assertEqual(
            set(matrix),
            {
                "system-default",
                "ordinary-no-thp",
                "all-thp",
                "static-long-thp",
                "confidence-thp",
                "static-epoch-thp",
                "confidence-epoch-thp",
                "explicit-hugetlb",
            },
        )
        self.assertEqual(matrix["static-long-thp"].confidence_threshold, 0)
        self.assertEqual(matrix["confidence-thp"].confidence_threshold, 80)
        self.assertEqual(matrix["static-epoch-thp"].policy, "epoch-cohort-thp")

    def test_commands_request_strict_backing_gates(self) -> None:
        args = SimpleNamespace(
            types_per_truth=1,
            objects=100,
            slot_bytes=4096,
            long_fraction=0.5,
            false_long_rate=0.05,
            false_short_rate=0.05,
            correct_confidence=90,
            error_confidence=60,
            confidence_overlap_rate=0.1,
            unknown_rate=0.0,
            ephemeral_waves=2,
            warmup_passes=1,
            passes=2,
            allow_thp_fallback=False,
            allow_hugetlb_fallback=False,
            numa_node=0,
            cpu=0,
        )
        matrix = {case.name: case for case in experiment.cases(80)}
        with mock.patch.object(
            experiment.hugepage,
            "pinned_command",
            side_effect=lambda command, _node, _cpu: command,
        ):
            thp = experiment.probe_command(
                Path("probe"), matrix["confidence-thp"], args, 1
            )
            ordinary = experiment.probe_command(
                Path("probe"), matrix["ordinary-no-thp"], args, 1
            )
            hugetlb = experiment.probe_command(
                Path("probe"), matrix["explicit-hugetlb"], args, 1
            )
        self.assertIn("--require-thp", thp)
        self.assertIn("--require-no-thp", ordinary)
        self.assertIn("--require-hugetlb", hugetlb)

    def test_actual_smaps_backing_plus_vmstat_passes(self) -> None:
        row = evidence_row(delta_kib=4096, fault_alloc=2)
        parsed = experiment.parse_probe(json.dumps(row), "thp")
        self.assertEqual(parsed["observed_anon_huge_delta_kib"], 4096)
        self.assertEqual(parsed["observed_vmstat_thp_alloc_delta"], 2)

    def test_advice_or_claim_without_anon_hugepages_cannot_pass(self) -> None:
        row = evidence_row(delta_kib=0, fault_alloc=3)
        row.update(
            {
                "thp_advice_attempts": 8,
                "thp_advice_failures": 0,
                "thp_actual_backing_observed": True,
            }
        )
        failures = experiment.validate_backing_evidence(row, "thp")
        self.assertIn("thp_observed_mismatch", failures)
        self.assertIn("anon_hugepages", failures)

    def test_smaps_without_vmstat_corroboration_is_rejected(self) -> None:
        row = evidence_row(delta_kib=2048, fault_alloc=0, collapse_alloc=0)
        failures = experiment.validate_backing_evidence(row, "thp")
        self.assertIn("vmstat_thp_allocation", failures)

    def test_coverage_and_partial_collapse_remain_fractional(self) -> None:
        row = evidence_row(delta_kib=2048, fault_alloc=0, collapse_alloc=2)
        row.update(
            {
                "thp_collapse_eligible_extents": 4,
                "thp_collapse_attempts": 4,
                "thp_collapse_successes": 2,
                "thp_collapse_failures": 2,
            }
        )
        self.assertEqual(experiment.allocator_thp_coverage(row), 0.5)
        self.assertEqual(experiment.collapse_success_rate(row), 0.5)
        failures = experiment.validate_backing_evidence(row, "thp")
        self.assertIn("steady_thp_coverage", failures)
        self.assertNotIn(
            "steady_thp_coverage",
            experiment.validate_backing_evidence(row, "thp-eligible"),
        )

    def test_formal_evidence_requires_madvise_and_parseable_smaps(self) -> None:
        row = evidence_row(delta_kib=0, fault_alloc=0)
        row.update(
            {
                "thp_enabled_mode": "always",
                "smaps_rollup_available": False,
                "smaps_rollup_parse_success": False,
                "thp_backing_gate_passed": False,
            }
        )
        failures = experiment.validate_backing_evidence(row, "ordinary-no-thp")
        self.assertIn("thp_enabled_mode", failures)
        self.assertIn("smaps_rollup_unavailable", failures)
        self.assertIn("smaps_rollup_parse", failures)
        self.assertIn("probe_no_thp_gate", failures)

    def test_epoch_lifecycle_counts_only_first_advance_outside_wave(self) -> None:
        row = evidence_row(delta_kib=4096)
        row.update(
            {
                "first_epoch_advance_ns": 10,
                "epoch_advance_ns": 50,
                "wave_evidence_sampling_ns": 7,
            }
        )
        experiment.add_thp_derived_metrics(row)
        self.assertEqual(row["first_epoch_advance_ns_per_allocation"], 1.0)
        # allocation + release + wave(including later advances) + teardown + first advance
        self.assertEqual(row["lifecycle_including_epoch_ns_per_allocation"], 17.0)

    def test_overall_effective_resident_includes_live_wave_peak(self) -> None:
        row = evidence_row(delta_kib=4096)
        row.update(
            {
                "wave_peak_observed": True,
                "wave_peak_rss_kib": 1500,
                "wave_peak_anon_kib": 1200,
                "wave_peak_hugetlb_kib": 500,
                "wave_peak_effective_resident_kib": 2000,
                "max_effective_resident_kib": 2000,
                "overall_max_effective_resident_kib": 2000,
            }
        )
        self.assertNotIn(
            "effective_resident_accounting",
            experiment.validate_backing_evidence(row, "thp"),
        )
        row["overall_max_effective_resident_kib"] = 1000
        self.assertIn(
            "effective_resident_accounting",
            experiment.validate_backing_evidence(row, "thp"),
        )

    def test_eager_advice_has_no_object_level_confirmed_placement_rate(self) -> None:
        eager = evidence_row(delta_kib=4096)
        self.assertFalse(eager["runtime_confirmed_placement_available"])
        self.assertIsNone(eager["runtime_confirmed_placement_failure_rate"])

        epoch = evidence_row(delta_kib=4096, collapse_alloc=2)
        epoch.update(
            {
                "policy": "epoch-cohort-thp",
                "thp_extent_mappings": 4,
                "lifetime_peak_thp_extents": 4,
                "steady_thp_extents": 4,
                "steady_thp_collapse_confirmed_extents": 2,
                "runtime_confirmed_placement_available": True,
                "runtime_confirmed_placement_success_rate": 0.95,
                "runtime_confirmed_placement_failure_rate": 0.05,
                "runtime_confirmed_placement_precision": 0.95,
                "runtime_confirmed_placement_recall": 0.95,
            }
        )
        self.assertEqual(experiment.steady_allocator_thp_coverage(epoch), 1.0)
        self.assertEqual(experiment.steady_thp_backend_vma_coverage(epoch), 0.5)
        self.assertEqual(experiment.validate_backing_evidence(epoch, "thp"), [])

    def test_epoch_collapse_accounting_includes_advice_failures(self) -> None:
        row = evidence_row(delta_kib=2048, fault_alloc=0, collapse_alloc=1)
        row.update(
            {
                "policy": "epoch-cohort-thp",
                "thp_advice_attempts": 2,
                "thp_advice_successes": 1,
                "thp_advice_failures": 1,
                "thp_collapse_eligible_extents": 2,
                "thp_collapse_attempts": 1,
                "thp_collapse_successes": 1,
                "thp_collapse_failures": 0,
                "steady_thp_collapse_confirmed_extents": 1,
                "runtime_confirmed_placement_available": True,
                "runtime_confirmed_placement_success_rate": 0.9,
                "runtime_confirmed_placement_failure_rate": 0.1,
                "runtime_confirmed_placement_precision": 0.9,
                "runtime_confirmed_placement_recall": 0.9,
            }
        )
        failures = experiment.validate_backing_evidence(row, "thp")
        self.assertNotIn("thp_advice_result_accounting", failures)
        self.assertNotIn("thp_collapse_eligible_attempt_accounting", failures)
        self.assertNotIn("thp_collapse_result_accounting", failures)

    def test_thp_backend_vma_byte_epoch_name_is_required(self) -> None:
        row = evidence_row(delta_kib=4096)
        row["thp_byte_epochs"] = row.pop("thp_backend_vma_byte_epochs")
        failures = experiment.validate_backing_evidence(row, "thp")
        self.assertIn("missing:thp_backend_vma_byte_epochs", failures)

    def test_no_thp_control_rejects_positive_anon_huge_delta(self) -> None:
        row = evidence_row(delta_kib=2048)
        failures = experiment.validate_backing_evidence(row, "ordinary-no-thp")
        self.assertIn("unexpected_anon_hugepages", failures)

    def test_hugetlb_is_distinct_from_anon_thp(self) -> None:
        row = evidence_row(delta_kib=0, fault_alloc=0)
        row.update(
            {
                "policy": "long-huge",
                "peak_hugetlb_kib": 4096,
                "peak_effective_resident_kib": 5096,
                "max_effective_resident_kib": 5096,
                "overall_max_effective_resident_kib": 5096,
                "runtime_confirmed_placement_available": True,
                "runtime_confirmed_placement_success_rate": 0.9,
                "runtime_confirmed_placement_failure_rate": 0.1,
                "runtime_confirmed_placement_precision": 0.9,
                "runtime_confirmed_placement_recall": 0.9,
            }
        )
        self.assertEqual(experiment.validate_backing_evidence(row, "hugetlb"), [])

    def test_duplicate_probe_json_keys_are_rejected(self) -> None:
        with self.assertRaisesRegex(RuntimeError, "duplicate probe JSON key: passed"):
            experiment.parse_probe(
                '{"source":"lifetime_hugepage_allocator_probe",'
                '"passed":true,"passed":false}',
                "system-default",
            )

    def test_summary_reports_paired_thp_effects_against_ordinary(self) -> None:
        ordinary = evidence_row(delta_kib=0, fault_alloc=0)
        system_default = evidence_row(delta_kib=0, fault_alloc=0)
        system_default["policy"] = "raw-default"
        thp = evidence_row(delta_kib=4096, fault_alloc=1)
        for sample, backing, touch, lifecycle, rss in (
            (system_default, "system-default", 25.0, 110.0, 1100),
            (ordinary, "ordinary-no-thp", 20.0, 100.0, 1000),
            (thp, "thp", 15.0, 120.0, 900),
        ):
            steady_rss = rss - 200
            sample.update(
                {
                    "expected_backing": backing,
                    "repeat": 0,
                    "ns_per_touch": touch,
                    "observed_lifecycle_ns_per_allocation": lifecycle,
                    "peak_rss_kib": rss,
                    "peak_effective_resident_kib": rss,
                    "max_effective_resident_kib": max(rss, 900, 800),
                    "overall_max_effective_resident_kib": max(rss, 900, 800),
                    "steady_rss_kib": steady_rss,
                    "steady_effective_resident_kib": steady_rss,
                    "classification_coverage": 1.0,
                    "classification_failure_rate": 0.1,
                    "placement_failure_rate": 0.1,
                    "policy_intent_placement_failure_rate": 0.1,
                    "steady_retained_bytes": 1024,
                    "steady_retained_slack_bytes": 128,
                    "vmstat_thp_fault_fallback_delta": 0,
                    "vmstat_thp_collapse_alloc_failed_delta": 0,
                }
            )
        summary = experiment.summarize(
            {
                "system-default": [system_default],
                "ordinary-no-thp": [ordinary],
                "static-long-thp": [thp],
            }
        )
        effect = summary["comparisons"][
            "static-long-thp_vs_ordinary-no-thp"
        ]["touch"]
        self.assertAlmostEqual(effect["geomean_ratio"], 0.75)
        self.assertAlmostEqual(effect["improvement"], 0.25)
        resident = summary["comparisons"][
            "static-long-thp_vs_ordinary-no-thp"
        ]["max_effective_resident"]
        self.assertAlmostEqual(resident["geomean_ratio"], 0.9)
        self.assertNotIn(
            "peak_rss",
            summary["comparisons"]["static-long-thp_vs_ordinary-no-thp"],
        )
        self.assertIn(
            "static-long-thp_vs_system-default", summary["comparisons"]
        )
        steady = summary["comparisons"][
            "static-long-thp_vs_system-default"
        ]["steady_effective_resident"]
        self.assertAlmostEqual(steady["geomean_ratio"], 700 / 900)


if __name__ == "__main__":
    unittest.main()
