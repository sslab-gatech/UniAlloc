import sys
import unittest
from pathlib import Path

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


class LifetimeHugepageAllocatorExperimentTests(unittest.TestCase):
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
        self.assertEqual(summary["verdict"], "go-integrated-placement-and-tlb")
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
        self.assertEqual(summary["verdict"], "no-go-integrated-arena")


if __name__ == "__main__":
    unittest.main()
