#!/usr/bin/env python3
"""Contract tests for matched lifetime-prior Stage-B evaluation."""

from __future__ import annotations

import contextlib
import importlib.util
import io
from pathlib import Path
import sys
import unittest


ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "evaluation" / "scripts" / "lifetime_prior_stage_b_campaign.py"
SCRIPT_DIR = SCRIPT.parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))
spec = importlib.util.spec_from_file_location("lifetime_prior_stage_b_campaign", SCRIPT)
assert spec is not None and spec.loader is not None
campaign = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = campaign
spec.loader.exec_module(campaign)


def smaps(anon_hugepages_kib: int) -> dict[str, object]:
    return {
        "elapsed_seconds": 1.0,
        "rss_kib": 4096,
        "pss_kib": 4096,
        "anonymous_kib": 4096,
        "anon_hugepages_kib": anon_hugepages_kib,
    }


class LifetimePriorStageBCampaignTests(unittest.TestCase):
    def test_criterion_estimate_is_normalized_to_seconds(self) -> None:
        parsed = campaign.parse_criterion_estimate(
            "es/large/base/fixer time:   [12.780 ms 12.951 ms 13.138 ms]"
        )
        self.assertAlmostEqual(
            0.012951, parsed["estimate_seconds_per_iteration"]
        )
        self.assertTrue(parsed["diagnostic_only"])

    def test_criterion_estimate_requires_one_benchmark(self) -> None:
        with self.assertRaises(campaign.stage_a.CampaignContractError):
            campaign.parse_criterion_estimate("no benchmark estimate")

    def test_criterion_estimate_accepts_ansi_and_scientific_notation(self) -> None:
        parsed = campaign.parse_criterion_estimate(
            "\x1b[32mtime: [1.0e1 us 1.1e1 us 1.2e1 us]\x1b[0m"
        )
        self.assertAlmostEqual(11e-6, parsed["estimate_seconds_per_iteration"])
        with self.assertRaises(campaign.stage_a.CampaignContractError):
            campaign.parse_criterion_estimate("time: [12 us 11 us 13 us]")

    def test_exact_prior_coverage_compares_outcome_aggregates(self) -> None:
        aggregate = {
            "site_count": 2,
            "exact_site_key_digest": "a" * 64,
            "allocation_count": 10,
            "allocation_requested_bytes": 800,
            "long_outcomes": 7,
            "short_outcomes": 2,
            "censored_outcomes": 1,
            "long_requested_bytes": 560,
            "short_requested_bytes": 160,
            "censored_requested_bytes": 80,
            "long_prior_site_count": 2,
            "short_prior_site_count": 0,
            "missing_or_invalid_field_site_count": 0,
            "deduplicated_by": list(
                campaign.stage_a.runtime_lifetime.RUNTIME_SITE_KEY_FIELDS
            ),
            "evidence_complete": True,
        }
        join = {
            "source": "unialloc-compiler-runtime-exact-site-join-v2",
            "static_prior_join_success": True,
            "applied_prior_classified_count": 2,
            "applied_prior_incomplete_key_count": 0,
            "matched_applied_prior_site_count": 2,
            "resolution_status_counts": {},
            "matched_applied_prior_outcomes": dict(aggregate),
            "executed_static_prior_outcomes": dict(aggregate),
        }
        self.assertTrue(
            campaign.exact_prior_coverage_complete(join)
        )
        bypassed = dict(aggregate)
        bypassed["allocation_count"] = 11
        bypassed["allocation_requested_bytes"] = 900
        bypassed_join = {
            **join,
            "matched_applied_prior_outcomes": dict(bypassed),
            "executed_static_prior_outcomes": dict(bypassed),
        }
        self.assertTrue(
            campaign.exact_prior_coverage_complete(bypassed_join)
        )
        incomplete = dict(aggregate)
        incomplete["allocation_count"] = 11
        self.assertFalse(
            campaign.exact_prior_coverage_complete(
                {
                    **join,
                    "matched_applied_prior_outcomes": dict(aggregate),
                    "executed_static_prior_outcomes": incomplete,
                }
            )
        )

    def test_exact_prior_effect_requires_observed_allocations(self) -> None:
        empty = {
            "site_count": 0,
            "exact_site_key_digest": "b" * 64,
            "allocation_count": 0,
            "allocation_requested_bytes": 0,
            "long_outcomes": 0,
            "short_outcomes": 0,
            "censored_outcomes": 0,
            "long_requested_bytes": 0,
            "short_requested_bytes": 0,
            "censored_requested_bytes": 0,
            "long_prior_site_count": 0,
            "short_prior_site_count": 0,
            "missing_or_invalid_field_site_count": 0,
            "deduplicated_by": list(
                campaign.stage_a.runtime_lifetime.RUNTIME_SITE_KEY_FIELDS
            ),
            "evidence_complete": True,
        }
        empty_join = {
            "source": "unialloc-compiler-runtime-exact-site-join-v2",
            "static_prior_join_success": True,
            "applied_prior_classified_count": 1,
            "matched_applied_prior_site_count": 0,
            "resolution_status_counts": {},
            "matched_applied_prior_outcomes": dict(empty),
            "executed_static_prior_outcomes": dict(empty),
        }
        self.assertFalse(campaign.exact_prior_coverage_complete(empty_join))
        self.assertFalse(
            campaign.exact_prior_effect_observed(empty_join)
        )

    def test_exact_prior_coverage_rejects_missing_digest_and_fields(self) -> None:
        aggregate = {
            "site_count": 1,
            "allocation_count": 1,
            "allocation_requested_bytes": 8,
            "evidence_complete": True,
        }
        malformed = {
            "source": "unialloc-compiler-runtime-exact-site-join-v2",
            "static_prior_join_success": True,
            "applied_prior_classified_count": 1,
            "applied_prior_incomplete_key_count": 0,
            "matched_applied_prior_site_count": 1,
            "resolution_status_counts": {},
            "matched_applied_prior_outcomes": dict(aggregate),
            "executed_static_prior_outcomes": dict(aggregate),
        }
        self.assertFalse(campaign.exact_prior_coverage_complete(malformed))

    def test_all_unknown_control_rejects_static_prior_contamination(self) -> None:
        executed = {
            "site_count": 0,
            "allocation_count": 0,
            "allocation_requested_bytes": 0,
            "long_outcomes": 0,
            "short_outcomes": 0,
            "censored_outcomes": 0,
            "long_requested_bytes": 0,
            "short_requested_bytes": 0,
            "censored_requested_bytes": 0,
            "long_prior_site_count": 0,
            "short_prior_site_count": 0,
            "missing_or_invalid_field_site_count": 0,
            "evidence_complete": True,
            "deduplicated_by": list(
                campaign.stage_a.runtime_lifetime.RUNTIME_SITE_KEY_FIELDS
            ),
        }
        join = {
            "source": "unialloc-compiler-runtime-exact-site-join-v2",
            "applied_prior_classified_count": 0,
            "executed_static_prior_outcomes": executed,
        }
        self.assertTrue(campaign.all_unknown_control_clean(join))
        executed["site_count"] = 1
        self.assertFalse(campaign.all_unknown_control_clean(join))

    def test_zero_baseline_reduction_is_undefined(self) -> None:
        self.assertIsNone(campaign._optional_percent_reduction(0, 0))
        self.assertAlmostEqual(
            25.0, campaign._optional_percent_reduction(100, 75)
        )

    def test_thp_backing_is_checked_for_every_sample_pair(self) -> None:
        passed = campaign.strict_thp_pair_gate([smaps(0)], [smaps(2048)])
        self.assertTrue(passed["passed"])
        mixed = campaign.strict_thp_pair_gate(
            [smaps(0), smaps(0)], [smaps(2048), smaps(0)]
        )
        self.assertFalse(mixed["passed"])
        self.assertEqual(3, mixed["eligible_sample_count"])
        self.assertEqual(1, mixed["excluded_sample_count"])
        self.assertIn(
            "no resident AnonHugePages", " ".join(mixed["reasons"])
        )

    def test_thp_backing_allows_unmatched_sample_counts(self) -> None:
        result = campaign.strict_thp_pair_gate(
            [smaps(0), smaps(0)], [smaps(2048)]
        )
        self.assertTrue(result["passed"])
        self.assertEqual(
            "independent-per-sample-sustained-backing", result["pairing_mode"]
        )

    def test_measurement_phase_excludes_only_criterion_warmup(self) -> None:
        samples = [
            {**smaps(0), "elapsed_seconds": 0.0},
            {**smaps(0), "elapsed_seconds": 2.0},
            {**smaps(2048), "elapsed_seconds": 5.0},
            {**smaps(4096), "elapsed_seconds": 8.0},
        ]
        phase = campaign.measurement_phase_smaps(samples, start_seconds=5.0)
        selected = phase.pop("samples")
        self.assertEqual([2048, 4096], [row["anon_hugepages_kib"] for row in selected])
        self.assertEqual(2, phase["excluded_pre_measurement_sample_count"])
        self.assertTrue(campaign.strict_thp_pair_gate([smaps(0)], selected)["passed"])

    def test_measurement_phase_rejects_missing_or_nonmonotonic_samples(self) -> None:
        with self.assertRaises(campaign.stage_a.CampaignContractError):
            campaign.measurement_phase_smaps([smaps(0)], start_seconds=2.0)
        samples = [
            {**smaps(0), "elapsed_seconds": 2.0},
            {**smaps(0), "elapsed_seconds": 1.0},
        ]
        with self.assertRaises(campaign.stage_a.CampaignContractError):
            campaign.measurement_phase_smaps(samples, start_seconds=0.0)

    def test_thp_activity_requires_routing_mapping_and_successful_advice(self) -> None:
        stats = {
            "adaptive_long_routed_allocations": 4,
            "thp_extent_mappings": 2,
            "thp_advice_attempts": 2,
            "thp_advice_successes": 2,
            "thp_advice_errors": 0,
            "mapping_failures": 0,
        }
        self.assertTrue(campaign.selective_thp_runtime_gate(stats)["passed"])
        stats["adaptive_long_routed_allocations"] = 0
        self.assertFalse(campaign.selective_thp_runtime_gate(stats)["passed"])

    def test_arm_order_reverses_to_bound_order_bias(self) -> None:
        self.assertEqual(campaign.ARM_NAMES, campaign._arm_order(0))
        self.assertEqual(tuple(reversed(campaign.ARM_NAMES)), campaign._arm_order(1))

    def test_cli_requires_even_repeat_count(self) -> None:
        with contextlib.redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit):
                campaign.parse_args(
                    [
                        "--stage-a-results",
                        "stage-a.json",
                        "--target",
                        "swc",
                        "--raw-dir",
                        "raw",
                        "--repeats",
                        "3",
                    ]
                )


if __name__ == "__main__":
    unittest.main()
