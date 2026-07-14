#!/usr/bin/env python3

from __future__ import annotations

import argparse
import importlib.util
import json
import unittest
from pathlib import Path
from unittest import mock


SCRIPT = Path(__file__).with_name("lifetime_epoch_cohort_experiment.py")
SPEC = importlib.util.spec_from_file_location("lifetime_epoch_cohort_experiment", SCRIPT)
assert SPEC is not None and SPEC.loader is not None
EXPERIMENT = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(EXPERIMENT)


def valid_row(policy: str, repeat: int) -> dict[str, object]:
    baseline = policy == "long-huge"
    target_retained = 100 if baseline else 60
    reset_retained = 100 if baseline else 90
    all_retained = target_retained + reset_retained
    overlap_retained = 100
    full_retained = overlap_retained + all_retained
    total = 1248
    row: dict[str, object] = {
        "source": "lifetime_epoch_cohort_probe",
        "policy": policy,
        "repeat": repeat,
        "trace_digest": f"trace-{repeat}",
        "passed": True,
        "geometry_gate_passed": True,
        "accounting_consistent": True,
        "release_gate_passed": True,
        "runtime_validation_gate_passed": True,
        "hugetlb_gate_passed": True,
        "hugetlb_required": True,
        "own_mappings_released": True,
        "pool_restored": True,
        "process_returncode": 0,
        "warmup_cycles": 0,
        "measured_cycles": 1,
        "seed": 7 + repeat,
        "slot_bytes": 4096,
        "slots_per_region": 16,
        "regions_per_extent": 32,
        "large_objects": 496,
        "large_regions": 31,
        "bridge_objects": 256,
        "bridge_regions": 16,
        "total_allocations": total,
        "measured_allocation_objects": 752,
        "all_boundary_definition": "target_plus_reset",
        "full_cycle_definition": "overlap_plus_target_plus_reset",
        "hugetlb_fallback_extent_mappings": 0,
        "ordinary_extent_mappings": 0,
        "mapping_failures": 0,
        "allocation_fallbacks": 0,
        "extent_unmap_failures": 0,
        "unsupported_layout_bypasses": 0,
        "unknown_bypasses": 0,
        "nohugepage_advice_failures": 0,
        "runtime_validation_excluded_objects": 0,
        "runtime_validation_excluded_bytes": 0,
        "predictor_tn_objects": 0,
        "predictor_fp_objects": 0,
        "predictor_fn_objects": 0,
        "placement_tn_objects": 0,
        "placement_fp_objects": 0,
        "placement_fn_objects": 0,
        "final_current_extents": 0,
        "final_live_objects": 0,
        "routed_allocations": total,
        "routed_deallocations": total,
        "slot_bump_allocations": total,
        "slot_reuse_hits": 0,
        "runtime_validated_objects": total,
        "runtime_validated_bytes": total * 4096,
        "predictor_tp_objects": total,
        "predictor_tp_bytes": total * 4096,
        "placement_tp_objects": total,
        "placement_tp_bytes": total * 4096,
        "sentinel_touches": total * 4,
        "phase_advances": 4,
        "expected_phase_advances": 4,
        "measured_lifecycle_ns_per_object": 100.0 if baseline else 110.0,
        "measured_allocation_ns": 50_000 if baseline else 55_000,
        "measured_release_ns": 30_000 if baseline else 33_000,
        "measured_epoch_advance_ns": 1_000 if baseline else 2_000,
        "measured_hugetlb_extent_mappings": 1 if baseline else 2,
        "measured_epoch_cohort_extent_mappings": 0 if baseline else 2,
        "measured_extent_unmaps": 1 if baseline else 2,
        "measured_identity_region_assignments": 47,
        "measured_identity_region_releases": 47,
        "hugetlb_extent_mappings": 2 if baseline else 3,
        "extent_unmaps": 2 if baseline else 3,
    }
    retained = {
        "overlap": overlap_retained,
        "target": target_retained,
        "reset": reset_retained,
        "all_boundary": all_retained,
        "full_cycle": full_retained,
    }
    for prefix, value in retained.items():
        row[f"{prefix}_retained_byte_epochs"] = value
        row[f"{prefix}_hugetlb_byte_epochs"] = value
        row[f"{prefix}_pinned_byte_epochs"] = 0 if baseline else 1
        row[f"{prefix}_live_byte_epochs"] = 50
    return row


class LifetimeEpochCohortExperimentTests(unittest.TestCase):
    @mock.patch.object(EXPERIMENT.shutil, "which", return_value="/usr/bin/numactl")
    def test_probe_command_pins_and_requires_hugetlb(self, _which: mock.Mock) -> None:
        args = argparse.Namespace(
            warmup_cycles=3,
            cycles=9,
            numa_node=1,
            cpu=17,
        )
        command = EXPERIMENT.probe_command(
            Path("/tmp/lifetime_epoch_cohort_probe"),
            "epoch-cohort",
            args,
            991,
        )
        self.assertEqual(command[:3], ["numactl", "--physcpubind=17", "--membind=1"])
        self.assertIn("--require-hugetlb", command)
        self.assertEqual(command[command.index("--policy") + 1], "epoch-cohort")
        self.assertEqual(command[command.index("--warmup-cycles") + 1], "3")
        self.assertEqual(command[command.index("--measured-cycles") + 1], "9")
        self.assertEqual(command[command.index("--seed") + 1], "991")

    def test_parse_probe_requires_exactly_one_expected_row(self) -> None:
        row = valid_row("long-huge", 0)
        parsed = EXPERIMENT.parse_probe(json.dumps(row) + "\n")
        self.assertEqual(parsed["policy"], "long-huge")
        with self.assertRaises(RuntimeError):
            EXPERIMENT.parse_probe("")

    def test_summary_go_at_exact_effect_thresholds(self) -> None:
        rows = [
            valid_row(policy, repeat)
            for repeat in range(3)
            for policy in EXPERIMENT.POLICIES
        ]
        summary = EXPERIMENT.summarize(rows, bootstrap_samples=100)
        self.assertTrue(summary["go"])
        self.assertEqual(summary["recommendation"], "FRAGMENTATION-GO")
        self.assertFalse(summary["performance_evaluated"])
        self.assertEqual(summary["performance_recommendation"], "INCONCLUSIVE")
        self.assertAlmostEqual(summary["target_auc_reduction"]["reduction"], 0.40)
        self.assertAlmostEqual(
            summary["all_boundary_auc_reduction"]["reduction"], 0.25
        )
        self.assertAlmostEqual(summary["full_cycle_auc_reduction"]["reduction"], 1 / 6)
        self.assertTrue(summary["paired_evidence"]["passed"])
        self.assertTrue(
            summary["lifecycle_cost"]["measured_lifecycle_ns_per_object"][
                "available"
            ]
        )

    def test_digest_mismatch_is_no_go(self) -> None:
        rows = [valid_row(policy, 0) for policy in EXPERIMENT.POLICIES]
        rows[1]["trace_digest"] = "different"
        summary = EXPERIMENT.summarize(rows, bootstrap_samples=20)
        self.assertFalse(summary["go"])
        self.assertFalse(summary["paired_evidence"]["passed"])
        mismatch = summary["paired_evidence"]["mismatches"][0]
        self.assertIn("trace_digest", mismatch["fields"])

    def test_hugetlb_fallback_is_no_go(self) -> None:
        rows = [valid_row(policy, 0) for policy in EXPERIMENT.POLICIES]
        rows[1]["hugetlb_fallback_extent_mappings"] = 1
        summary = EXPERIMENT.summarize(rows, bootstrap_samples=20)
        self.assertFalse(summary["go"])
        self.assertFalse(summary["structural_go"])
        target_summary = summary["policy_summaries"]["epoch-cohort"]
        self.assertFalse(target_summary["hard_gates_passed"])
        self.assertIn(
            "hugetlb_fallback_extent_mappings",
            target_summary["hard_gate_failures"][0]["failures"],
        )

    def test_zero_lifecycle_timing_is_no_go_without_division_error(self) -> None:
        rows = [valid_row(policy, 0) for policy in EXPERIMENT.POLICIES]
        rows[0]["measured_lifecycle_ns_per_object"] = 0.0
        summary = EXPERIMENT.summarize(rows, bootstrap_samples=20)
        self.assertFalse(summary["go"])
        self.assertFalse(summary["structural_go"])
        self.assertFalse(
            summary["lifecycle_cost"]["measured_lifecycle_ns_per_object"][
                "available"
            ]
        )


if __name__ == "__main__":
    unittest.main()
