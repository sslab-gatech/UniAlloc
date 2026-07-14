import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import lifetime_hugepage_experiment as experiment


def row(
    *,
    peak_huge: int,
    peak_ordinary: int,
    reclaimed_huge: int,
    reclaimed_ordinary: int,
    retained_huge: int,
    retained_ordinary: int,
    fragmentation: float,
    ns_per_touch: float,
    fallback: int = 0,
) -> dict:
    retained_extents = retained_huge + retained_ordinary
    retained_bytes = retained_extents * 2 * 1024 * 1024
    stranded_bytes = int(retained_bytes * fragmentation)
    return {
        "fallback_extents": fallback,
        "nohugepage_advice_failures": 0,
        "hugetlb_pool_restored": True,
        "own_mappings_released": True,
        "allocation_ns_per_object": 10.0,
        "release_ns_per_object": 5.0,
        "ns_per_touch": ns_per_touch,
        "touch_ns": ns_per_touch * 1000,
        "peak_hugetlb_extents": peak_huge,
        "peak_ordinary_extents": peak_ordinary,
        "reclaimed_hugetlb_extents": reclaimed_huge,
        "reclaimed_ordinary_extents": reclaimed_ordinary,
        "teardown_hugetlb_extents": retained_huge,
        "teardown_ordinary_extents": retained_ordinary,
        "retained_hugetlb_extents": retained_huge,
        "retained_ordinary_extents": retained_ordinary,
        "stranded_huge_slots": 1 if fragmentation else 0,
        "stranded_slots": 1 if fragmentation else 0,
        "retained_bytes": retained_bytes,
        "live_bytes": retained_bytes - stranded_bytes,
        "stranded_bytes": stranded_bytes,
        "huge_fragmentation_ratio": fragmentation if retained_huge else 0.0,
        "total_fragmentation_ratio": fragmentation,
    }


class LifetimeHugepageExperimentTests(unittest.TestCase):
    def test_summary_accepts_structural_and_tlb_go(self) -> None:
        rows = {
            "ordinary-mixed": [
                row(
                    peak_huge=0,
                    peak_ordinary=8,
                    reclaimed_huge=0,
                    reclaimed_ordinary=0,
                    retained_huge=0,
                    retained_ordinary=8,
                    fragmentation=0.5,
                    ns_per_touch=150.0,
                )
            ],
            "ordinary-segregated": [
                row(
                    peak_huge=0,
                    peak_ordinary=8,
                    reclaimed_huge=0,
                    reclaimed_ordinary=4,
                    retained_huge=0,
                    retained_ordinary=4,
                    fragmentation=0.0,
                    ns_per_touch=140.0,
                )
            ],
            "huge-mixed": [
                row(
                    peak_huge=8,
                    peak_ordinary=0,
                    reclaimed_huge=0,
                    reclaimed_ordinary=0,
                    retained_huge=8,
                    retained_ordinary=0,
                    fragmentation=0.5,
                    ns_per_touch=115.0,
                )
            ],
            "huge-segregated": [
                row(
                    peak_huge=8,
                    peak_ordinary=0,
                    reclaimed_huge=4,
                    reclaimed_ordinary=0,
                    retained_huge=4,
                    retained_ordinary=0,
                    fragmentation=0.0,
                    ns_per_touch=112.0,
                )
            ],
            "tiered": [
                row(
                    peak_huge=4,
                    peak_ordinary=4,
                    reclaimed_huge=0,
                    reclaimed_ordinary=4,
                    retained_huge=4,
                    retained_ordinary=0,
                    fragmentation=0.0,
                    ns_per_touch=110.0,
                )
            ],
        }

        summary = experiment.summarize(rows)

        self.assertTrue(summary["structural_go"])
        self.assertEqual(summary["verdict"], "go-fragmentation-and-tlb")
        self.assertEqual(
            summary["comparisons"]["tiered_retained_hugetlb_reduction_vs_huge_mixed"],
            0.5,
        )

    def test_missing_hugetlb_capacity_is_a_clean_no_go(self) -> None:
        empty = row(
            peak_huge=0,
            peak_ordinary=8,
            reclaimed_huge=0,
            reclaimed_ordinary=0,
            retained_huge=0,
            retained_ordinary=8,
            fragmentation=0.0,
            ns_per_touch=100.0,
            fallback=8,
        )
        rows = {policy: [dict(empty)] for policy in experiment.POLICIES}

        summary = experiment.summarize(rows)

        self.assertFalse(summary["structural_go"])
        self.assertEqual(summary["verdict"], "no-go-current-prototype")


if __name__ == "__main__":
    unittest.main()
