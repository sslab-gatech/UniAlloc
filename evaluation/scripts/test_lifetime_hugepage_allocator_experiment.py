import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
import lifetime_hugepage_allocator_experiment as experiment


def row(
    *,
    peak_huge: int,
    peak_ordinary: int,
    touch: float,
    stranded: int = 0,
    steady_rss_kib: int = 800,
) -> dict:
    return {
        "passed": True,
        "accounting_consistent": True,
        "own_mappings_released": True,
        "allocation_ns_per_object": 100.0,
        "ns_per_touch": touch,
        "peak_rss_kib": 1000,
        "steady_rss_kib": steady_rss_kib,
        "peak_hugetlb_kib": peak_huge * 2048,
        "steady_hugetlb_kib": peak_huge * 1024,
        "peak_hugetlb_extents": peak_huge,
        "peak_ordinary_extents": peak_ordinary,
        "peak_identity_regions": (peak_huge + peak_ordinary) * 32,
        "peak_retained_bytes": (peak_huge + peak_ordinary) * 2 * 1024 * 1024,
        "peak_reusable_unassigned_region_bytes": 0,
        "peak_assigned_region_slack_bytes": stranded,
        "peak_retained_slack_bytes": stranded,
        "peak_stranded_bytes": stranded,
        "steady_hugetlb_extents": max(0, peak_huge // 2),
        "steady_ordinary_extents": 0,
        "steady_identity_regions": max(0, peak_huge // 2) * 32,
        "steady_retained_bytes": max(0, peak_huge // 2) * 2 * 1024 * 1024,
        "steady_reusable_unassigned_region_bytes": 0,
        "steady_assigned_region_slack_bytes": stranded,
        "steady_retained_slack_bytes": stranded,
        "steady_stranded_bytes": stranded,
        "slot_reuse_hits": 1,
        "identity_region_assignments": 1,
        "identity_region_releases": 1,
        "routed_allocations": 2,
        "routed_deallocations": 2,
        "slot_bump_allocations": 1,
        "final_identity_regions": 0,
        "final_live_objects": 0,
        "final_live_slot_bytes": 0,
        "allocation_fallbacks": 0,
        "hugetlb_fallback_extent_mappings": 0,
        "mapping_failures": 0,
        "nohugepage_advice_failures": 0,
        "extent_unmap_failures": 0,
        "unsupported_layout_bypasses": 0,
    }


def classified_row(
    *,
    peak_huge: int = 4,
    peak_ordinary: int = 4,
    touch: float = 108.0,
    trace_digest: str | None = None,
) -> dict:
    result = row(
        peak_huge=peak_huge,
        peak_ordinary=peak_ordinary,
        touch=touch,
    )
    slot_bytes = 4096
    predictor = {"tp": 40, "tn": 45, "fp": 5, "fn": 0}
    placement = {"tp": 38, "tn": 47, "fp": 3, "fn": 2}
    effective_placement = {"tp": 38, "tn": 52, "fp": 3, "fn": 7}
    result.update(
        {
            "policy": "epoch-cohort",
            "total_allocations": 100,
            "slot_bytes": slot_bytes,
            "static_classified": 90,
            "static_unknown": 10,
            "runtime_validated_objects": 90,
            "runtime_validated_bytes": 90 * slot_bytes,
            "runtime_validation_excluded_objects": 0,
            "runtime_validation_excluded_bytes": 0,
            "routed_allocations": 90,
            "routed_deallocations": 90,
            "unknown_bypasses": 10,
            "slot_bump_allocations": 89,
            "slot_reuse_hits": 1,
            "classification_coverage": 0.9,
            "classification_success_rate": 85 / 90,
            "classification_failure_rate": 5 / 90,
            "classification_precision": 40 / 45,
            "classification_recall": 1.0,
            "placement_success_rate": 0.9,
            "placement_failure_rate": 0.1,
            "placement_precision": 38 / 41,
            "placement_recall": 38 / 45,
            "current_epoch": 2,
            "phase_advances": 1,
            "epoch_cohort_extent_mappings": 4,
            "retained_byte_epochs": 64 * 1024 * 1024,
            "hugetlb_byte_epochs": 32 * 1024 * 1024,
            "ordinary_byte_epochs": 32 * 1024 * 1024,
        }
    )
    for prefix, counts in (
        ("predictor", predictor),
        ("placement", placement),
        ("effective_placement", effective_placement),
    ):
        for cell, objects in counts.items():
            result[f"{prefix}_{cell}_objects"] = objects
            result[f"{prefix}_{cell}_bytes"] = objects * slot_bytes
    for cell, objects in predictor.items():
        result[f"static_{cell}_objects"] = objects
        result[f"static_{cell}_bytes"] = objects * slot_bytes
    if trace_digest is not None:
        result["prediction_trace_digest"] = trace_digest
    return result


class LifetimeHugepageAllocatorExperimentTests(unittest.TestCase):
    def test_matrix_pairs_binary_and_confidence_epoch_on_the_same_trace(self) -> None:
        matrix = experiment.cases(
            (0.01,),
            include_shuffled=True,
            include_lifetime_only_baseline=False,
            confidence_threshold=80,
            correct_confidence=91,
            error_confidence=61,
            confidence_overlap_rate=0.2,
            unknown_rate=0.03,
        )
        by_name = {case.name: case for case in matrix}
        self.assertTrue(
            {
                "raw-default",
                "policy-off",
                "ordinary-segregated",
                "all-huge-segregated",
                "long-huge-binary",
                "confidence-only",
                "confidence-epoch",
                "long-huge-oracle",
            }.issubset(by_name)
        )

        for binary_name, confidence_name in (
            ("long-huge-binary", "confidence-only"),
            ("long-huge-binary", "confidence-epoch"),
            ("long-huge-binary-error-0p01", "confidence-only-error-0p01"),
            ("long-huge-binary-error-0p01", "confidence-epoch-error-0p01"),
            ("long-huge-binary-shuffled", "confidence-only-shuffled"),
            ("long-huge-binary-shuffled", "confidence-epoch-shuffled"),
        ):
            binary = by_name[binary_name]
            confidence = by_name[confidence_name]
            self.assertEqual(binary.false_long, confidence.false_long)
            self.assertEqual(binary.false_short, confidence.false_short)
            self.assertEqual(binary.correct_confidence, confidence.correct_confidence)
            self.assertEqual(binary.error_confidence, confidence.error_confidence)
            self.assertEqual(
                binary.confidence_overlap_rate, confidence.confidence_overlap_rate
            )
            self.assertEqual(binary.unknown_rate, confidence.unknown_rate)
            self.assertEqual(binary.confidence_threshold, 0)
            self.assertEqual(confidence.confidence_threshold, 80)

        perf_names = {case.name for case in experiment.perf_cases(matrix)}
        fp_heavy = by_name["confidence-epoch-fp-heavy-0p01"]
        self.assertEqual(fp_heavy.false_long, 0.01)
        self.assertEqual(fp_heavy.false_short, 0.0)
        self.assertTrue(
            {
                "ordinary-segregated",
                "all-huge-segregated",
                "long-huge-binary",
                "confidence-only",
                "confidence-epoch",
                "long-huge-binary-error-0p01",
                "confidence-only-error-0p01",
                "confidence-epoch-error-0p01",
                "long-huge-binary-fp-heavy-0p01",
                "confidence-only-fp-heavy-0p01",
                "confidence-epoch-fp-heavy-0p01",
            }.issubset(perf_names)
        )

    def test_probe_and_perf_commands_forward_confidence_controls(self) -> None:
        case = experiment.cases(
            (0.01,), False, False, confidence_overlap_rate=0.2
        )[-1]
        args = SimpleNamespace(
            types_per_truth=1,
            objects=100,
            slot_bytes=4096,
            long_fraction=0.5,
            ephemeral_waves=1,
            warmup_passes=2,
            passes=4,
            allow_hugetlb_fallback=False,
            numa_node=0,
            cpu=0,
        )
        with mock.patch.object(
            experiment,
            "pinned_command",
            side_effect=lambda command, _node, _cpu: command,
        ):
            command = experiment.probe_command(Path("probe"), case, args, 123)
            perf_command = experiment.perf_command(Path("probe"), case, args, 123)

        overlap_index = command.index("--confidence-overlap-rate")
        self.assertEqual(command[overlap_index + 1], "0.2")
        self.assertIn("--confidence-threshold", command)
        self.assertEqual(perf_command[:4], ["sudo", "-n", "perf", "stat"])
        self.assertIn("ls_l1_d_tlb_miss.all", " ".join(perf_command))

    def test_confusion_metrics_close_and_aggregate(self) -> None:
        sample = classified_row()
        sample["repeat"] = 0

        summary = experiment.median_metrics([sample])
        classification = summary["classification"]

        self.assertTrue(summary["all_confusion_closed"])
        self.assertEqual(classification["static_coverage"], 0.9)
        self.assertEqual(classification["predictor_objects"]["decided"], 90)
        self.assertAlmostEqual(
            classification["predictor_objects"]["precision"], 40 / 45
        )
        self.assertEqual(
            classification["effective_placement_bytes"]["fp"], 3 * 4096
        )
        self.assertEqual(classification["median_phase_advances"], 1)
        self.assertEqual(summary["median_hugetlb_byte_epochs"], 32 * 1024 * 1024)

    def test_runtime_slot_bytes_and_requested_bytes_keep_distinct_coverage(self) -> None:
        sample = classified_row()
        sample.update({"repeat": 0, "slot_bytes": 3000, "arena_slot_bytes": 3200})
        sample["runtime_validated_bytes"] = 90 * 3200
        for prefix, width in (
            ("static", 3000),
            ("effective_placement", 3000),
            ("predictor", 3200),
            ("placement", 3200),
        ):
            for cell in experiment.CONFUSION_CELLS:
                sample[f"{prefix}_{cell}_bytes"] = (
                    sample[f"{prefix}_{cell}_objects"] * width
                )

        summary = experiment.median_metrics([sample])
        classification = summary["classification"]

        self.assertTrue(summary["all_confusion_closed"])
        self.assertEqual(
            classification["runtime_byte_weighting"],
            "rounded_arena_slot_bytes",
        )
        self.assertEqual(
            classification["effective_placement_byte_weighting"],
            "requested_payload_bytes",
        )
        self.assertAlmostEqual(classification["predictor_bytes"]["coverage"], 0.9)
        self.assertEqual(
            classification["effective_placement_bytes"]["coverage"], 1.0
        )

    def test_policy_comparison_splits_byte_epoch_gain(self) -> None:
        binary = classified_row(touch=110.0)
        confidence = classified_row(touch=109.0)
        binary.update({"repeat": 0, "hugetlb_byte_epochs": 64 * 1024 * 1024})
        confidence.update({"repeat": 0, "hugetlb_byte_epochs": 32 * 1024 * 1024})
        rows = {"binary": [binary], "confidence": [confidence]}
        summaries = {
            name: experiment.median_metrics(values) for name, values in rows.items()
        }

        comparison = experiment.policy_comparison(
            rows, summaries, "binary", "confidence", seed=1
        )

        self.assertEqual(comparison["hugetlb_byte_epochs_reduction"], 0.5)
        self.assertIn("effective_placement_precision_delta", comparison["classification"])

    def test_malformed_confusion_is_a_hard_invariant_failure(self) -> None:
        sample = classified_row()
        sample["placement_fn_objects"] += 1
        sample["repeat"] = 0

        summary = experiment.median_metrics([sample])

        self.assertFalse(summary["all_confusion_closed"])
        self.assertIn(
            "placement_objects", summary["classification"]["closure_failures"]
        )

    def test_prediction_trace_digest_is_a_hard_paired_gate(self) -> None:
        binary = row(peak_huge=4, peak_ordinary=4, touch=110.0)
        confidence_only = row(peak_huge=4, peak_ordinary=4, touch=109.5)
        confidence = row(peak_huge=4, peak_ordinary=4, touch=109.0)
        binary.update({"repeat": 0, "prediction_trace_digest": "aaa"})
        confidence_only.update({"repeat": 0, "prediction_trace_digest": "aaa"})
        confidence.update({"repeat": 0, "prediction_trace_digest": "bbb"})
        rows = {
            "long-huge-binary-error-0p01": [binary],
            "confidence-only-error-0p01": [confidence_only],
            "confidence-epoch-error-0p01": [confidence],
        }

        evidence = experiment.paired_prediction_trace_evidence(rows)

        self.assertTrue(evidence["available"])
        self.assertFalse(evidence["all_matched"])
        self.assertEqual(evidence["mismatches"][0]["repeat"], 0)

    def test_integrated_summary_accepts_structural_and_tlb_go(self) -> None:
        rows = {
            "policy-off": [
                row(
                    peak_huge=0,
                    peak_ordinary=0,
                    touch=150.0,
                    steady_rss_kib=16_000,
                )
            ],
            "ordinary-segregated": [
                row(
                    peak_huge=0,
                    peak_ordinary=8,
                    touch=140.0,
                    steady_rss_kib=8_000,
                )
            ],
            "all-huge-segregated": [row(peak_huge=8, peak_ordinary=0, touch=112.0)],
            "long-huge-oracle": [row(peak_huge=4, peak_ordinary=4, touch=108.0)],
            "long-huge-error-0p01": [
                row(peak_huge=5, peak_ordinary=4, touch=112.0, stranded=2 * 1024 * 1024)
            ],
            "long-huge-lifetime-only-error-0p01": [
                row(
                    peak_huge=5,
                    peak_ordinary=8,
                    touch=116.0,
                    stranded=512 * 1024 * 1024,
                )
            ],
        }
        for values in rows.values():
            values[0]["repeat"] = 0

        summary = experiment.summarize(rows)

        self.assertTrue(summary["structural_go"])
        self.assertTrue(summary["binary_placement_go"])
        self.assertFalse(summary["confidence_abstention_evaluated"])
        self.assertFalse(summary["epoch_cohort_reclaim_evaluated"])
        self.assertEqual(summary["verdict"], "go-binary-lifetime-placement-and-tlb")
        self.assertEqual(
            summary["comparisons"]["long_huge_peak_hugetlb_reduction_vs_all_huge"],
            0.5,
        )
        self.assertGreaterEqual(
            summary["comparisons"][
                "long_huge_steady_resident_reduction_vs_policy_off"
            ],
            0.4,
        )
        identity = summary["comparisons"][
            "exact_identity_packing_vs_lifetime_only"
        ]["long-huge-error-0p01"]
        self.assertGreater(identity["retained_slack_reduction"], 0.99)

    def test_fallback_is_a_clean_no_go(self) -> None:
        rows = {
            name: [row(peak_huge=8 if "huge" in name else 0, peak_ordinary=4, touch=100.0)]
            for name, *_ in experiment.BASE_CASES
        }
        for values in rows.values():
            values[0]["repeat"] = 0
        rows["long-huge-oracle"][0]["hugetlb_fallback_extent_mappings"] = 1

        summary = experiment.summarize(rows)

        self.assertFalse(summary["structural_go"])
        self.assertEqual(summary["verdict"], "no-go-binary-lifetime-placement")


if __name__ == "__main__":
    unittest.main()
