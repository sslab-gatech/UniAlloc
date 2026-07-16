#!/usr/bin/env python3
"""Contract tests for matched lifetime-prior Stage-B evaluation."""

from __future__ import annotations

import contextlib
import copy
import importlib.util
import io
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock


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


def complete_outcome_fields(values: dict[str, object]) -> dict[str, object]:
    return {
        **{
            field: 0
            for field in campaign.stage_a.RUNTIME_OUTCOME_AGGREGATE_FIELDS
        },
        **values,
    }


def minimal_cell_protocol() -> dict[str, object]:
    return {
        "schema_version": campaign.STAGE_B_PROTOCOL_SCHEMA_VERSION,
        "protocol_sha256": "a" * 64,
        "compatibility": {
            "target_id": "oxipng",
            "target_source_commit": campaign.stage_a.TARGETS[
                "oxipng"
            ].source_commit,
            "stage_a_results_sha256": "b" * 64,
            "allocator_implementation_revision": "c" * 40,
            "allocator_implementation_sha256": "d" * 64,
            "allocator_manifest_sha256": "e" * 64,
            "repeats": 4,
            "work_units": 1,
            "pinned_cpu": 18,
            "criterion_warm_up_seconds": None,
            "arms": [
                {
                    "arm": arm_name,
                    "stage_a_arm": campaign._stage_a_arm_name(arm_name),
                    "build_group": campaign._stage_a_arm(arm_name).build_group,
                    "runtime_selector": campaign.stage_a.runtime_arm_selector(
                        campaign._stage_a_arm(arm_name)
                    ),
                }
                for arm_name in campaign.ARM_NAMES
            ],
        },
    }


def minimal_cell_sample(attempt: Path) -> dict[str, object]:
    paths: dict[str, str] = {}
    for name, field in campaign.STAGE_B_ARTIFACT_FIELDS.items():
        path = attempt / f"{name}.txt"
        path.write_text(name, encoding="utf-8")
        paths[field] = str(path.resolve())
    return {
        "target_id": "oxipng",
        "arm": campaign.TRANSPORT_POLICY_DISABLED_ARM,
        "stage_a_arm": "default",
        "build_group": "all-unknown",
        "repeat": 0,
        "primary_metric_value_seconds": 0.25,
        "procfs": {"peak_rss_kib": 4096},
        "stdout_sha256": campaign.stage_a.sha256_file(Path(paths["stdout_path"])),
        "stderr_sha256": campaign.stage_a.sha256_file(Path(paths["stderr_path"])),
        **paths,
    }


def fake_derive_cell(
    identity: dict[str, object], artifacts: dict[str, object]
) -> tuple[dict[str, object], dict[str, object]]:
    sample = {
        "target_id": identity["target_id"],
        "arm": identity["arm"],
        "stage_a_arm": identity["stage_a_arm"],
        "build_group": identity["build_group"],
        "repeat": identity["repeat"],
        "measurement_session_id": identity["measurement_session_id"],
        "pre_gate_eligible": True,
        **{
            path_field: artifacts[name]["path"]
            for name, path_field in campaign.STAGE_B_ARTIFACT_FIELDS.items()
        },
    }
    return sample, {
        "performance": 0.25,
        "performance_unit": "seconds_per_work_unit",
        "peak_rss_mib": 4.0,
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
        aggregate = complete_outcome_fields({
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
        })
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
        self.assertTrue(campaign.exact_prior_coverage_complete(join))
        bypassed = dict(aggregate)
        bypassed["allocation_count"] = 11
        bypassed["allocation_requested_bytes"] = 900
        bypassed_join = {
            **join,
            "matched_applied_prior_outcomes": dict(bypassed),
            "executed_static_prior_outcomes": dict(bypassed),
        }
        self.assertTrue(campaign.exact_prior_coverage_complete(bypassed_join))
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
        empty = complete_outcome_fields({
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
        })
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
        self.assertFalse(campaign.exact_prior_effect_observed(empty_join))

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

    def test_exact_prior_coverage_compares_current_outcome_schema(self) -> None:
        aggregate = {
            "site_count": 1,
            "exact_site_key_digest": "c" * 64,
            "long_prior_site_count": 1,
            "short_prior_site_count": 0,
            "missing_or_invalid_field_site_count": 0,
            "deduplicated_by": list(
                campaign.stage_a.runtime_lifetime.RUNTIME_SITE_KEY_FIELDS
            ),
            "evidence_complete": True,
            **{
                field: 0
                for field in campaign.stage_a.RUNTIME_OUTCOME_AGGREGATE_FIELDS
            },
        }
        aggregate.update(
            {
                "allocation_count": 1,
                "allocation_requested_bytes": 8,
                "long_outcomes": 1,
                "long_requested_bytes": 8,
                "live_survival_observations": 1,
                "live_survival_requested_bytes": 8,
                "live_survival_payload_bytes": 8,
            }
        )
        join = {
            "source": "unialloc-compiler-runtime-exact-site-join-v2",
            "static_prior_join_success": True,
            "applied_prior_classified_count": 1,
            "applied_prior_incomplete_key_count": 0,
            "matched_applied_prior_site_count": 1,
            "resolution_status_counts": {},
            "matched_applied_prior_outcomes": dict(aggregate),
            "executed_static_prior_outcomes": dict(aggregate),
        }
        self.assertTrue(campaign.exact_prior_coverage_complete(join))
        join["executed_static_prior_outcomes"] = {
            **aggregate,
            "current_live_survival_inflight": 1,
            "current_live_survival_inflight_requested_bytes": 8,
            "current_live_survival_inflight_payload_bytes": 8,
        }
        self.assertFalse(campaign.exact_prior_coverage_complete(join))

    def test_all_unknown_control_rejects_static_prior_contamination(self) -> None:
        executed = complete_outcome_fields({
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
        })
        join = {
            "source": "unialloc-compiler-runtime-exact-site-join-v2",
            "applied_prior_classified_count": 0,
            "executed_static_prior_outcomes": executed,
        }
        self.assertTrue(campaign.all_unknown_control_clean(join))
        executed["site_count"] = 1
        self.assertFalse(campaign.all_unknown_control_clean(join))

    def test_all_unknown_control_checks_current_outcome_schema(self) -> None:
        executed = {
            "site_count": 0,
            "long_prior_site_count": 0,
            "short_prior_site_count": 0,
            "missing_or_invalid_field_site_count": 0,
            "evidence_complete": True,
            "deduplicated_by": list(
                campaign.stage_a.runtime_lifetime.RUNTIME_SITE_KEY_FIELDS
            ),
            **{
                field: 0
                for field in campaign.stage_a.RUNTIME_OUTCOME_AGGREGATE_FIELDS
            },
        }
        join = {
            "source": "unialloc-compiler-runtime-exact-site-join-v2",
            "applied_prior_classified_count": 0,
            "executed_static_prior_outcomes": executed,
        }
        self.assertTrue(campaign.all_unknown_control_clean(join))
        executed["current_live_survival_inflight"] = 1
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
        self.assertIn("no resident AnonHugePages", " ".join(mixed["reasons"]))

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
        self.assertEqual(
            [2048, 4096], [row["anon_hugepages_kib"] for row in selected]
        )
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

    def test_cli_defaults_to_four_and_rejects_two_repeats(self) -> None:
        args = campaign.parse_args(
            [
                "--stage-a-results",
                "stage-a.json",
                "--target",
                "swc",
                "--raw-dir",
                "raw",
            ]
        )
        self.assertEqual(4, args.repeats)
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
                        "2",
                    ]
                )

    def test_public_disabled_arm_maps_to_stage_a_default(self) -> None:
        self.assertNotIn("default", campaign.ARM_NAMES)
        self.assertEqual(
            "default",
            campaign._stage_a_arm_name(
                campaign.TRANSPORT_POLICY_DISABLED_ARM
            ),
        )

    def test_stage_a_contract_validates_revision_and_manifest_digest(self) -> None:
        target_id = "swc"
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            binary = root / "binary"
            sites = root / "sites.json"
            binary.write_bytes(b"binary")
            sites.write_text("{}\n", encoding="utf-8")
            implementation = "1" * 64
            revision = "2" * 40
            manifest = "3" * 64
            builds: dict[str, object] = {}
            for group in campaign.stage_a.BUILD_GROUPS:
                builds[group] = {
                    "build_group": group,
                    "automatic_rust_lifetime_prior": group == "compiler-prior",
                    "success": True,
                    "target_id": target_id,
                    "source_commit": campaign.stage_a.TARGETS[
                        target_id
                    ].source_commit,
                    "toolchain": campaign.stage_a.TOOLCHAIN,
                    "implementation_revision": revision,
                    "implementation_sha256": implementation,
                    "campaign_snapshot_sha256": manifest,
                    "base_campaign_snapshot_sha256": manifest,
                    "runtime_instrumentation_sha256": "4" * 64,
                    "generated_source_sha256": "5" * 64,
                    "activation": {"success": True},
                    "rewrite_claim_success": True,
                    "requested_target_crates": list(
                        campaign.stage_a.stage_a_target_crates(target_id)
                    ),
                    "compiler_sites": {
                        "source": campaign.stage_a.COMPILER_SITE_EXPORT_SOURCE
                    },
                    "binary": str(binary),
                    "binary_sha256": campaign.stage_a.sha256_file(binary),
                    "compiler_sites_path": str(sites),
                    "compiler_sites_sha256": campaign.stage_a.sha256_file(sites),
                    "allocator_dependency_compatibility": {
                        "provenance_digest": "6" * 64
                    },
                }
            document = {
                "allocator_snapshot": {
                    "allocator_revision": revision,
                    "unialloc_implementation_sha256": implementation,
                    "campaign_snapshot_sha256": manifest,
                },
                "targets": {
                    target_id: {
                        "status": "complete",
                        "opportunity_gate": {"passed": True},
                        "builds": builds,
                    }
                },
            }
            campaign._stage_a_target_contract(document, target_id)
            wrong_revision = copy.deepcopy(document)
            wrong_revision["targets"][target_id]["builds"]["compiler-prior"][
                "implementation_revision"
            ] = "7" * 40
            with self.assertRaises(campaign.stage_a.CampaignContractError):
                campaign._stage_a_target_contract(wrong_revision, target_id)
            wrong_manifest = copy.deepcopy(document)
            wrong_manifest["targets"][target_id]["builds"]["all-unknown"][
                "campaign_snapshot_sha256"
            ] = "8" * 64
            with self.assertRaises(campaign.stage_a.CampaignContractError):
                campaign._stage_a_target_contract(wrong_manifest, target_id)

    def test_stage_b_protocol_is_idempotent_and_resource_bound(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            stage_a_results = root / "stage-a-results.json"
            stage_a_results.write_text("{}\n", encoding="utf-8")
            document = {
                "allocator_snapshot": {
                    "allocator_revision": "1" * 40,
                    "unialloc_implementation_sha256": "2" * 64,
                    "campaign_snapshot_sha256": "3" * 64,
                }
            }
            common = {
                "document": document,
                "stage_a_results": stage_a_results,
                "target_id": "swc",
                "repeats": 4,
                "cpu": 18,
                "measurement_seconds": 30.0,
                "timeout": 120.0,
                "sample_interval": 0.5,
                "lock_path": campaign.DEFAULT_MEASUREMENT_LOCK,
                "criterion_warm_up_seconds": 5.0,
                "work_units": None,
                "input_path": None,
            }
            protocol = campaign.build_stage_b_protocol(**common)
            first = campaign.ensure_stage_b_protocol(root / "raw", protocol)
            second = campaign.ensure_stage_b_protocol(root / "raw", protocol)
            self.assertEqual(first, second)
            changed = campaign.build_stage_b_protocol(
                **{**common, "timeout": 121.0}
            )
            with self.assertRaises(campaign.stage_a.CampaignContractError):
                campaign.ensure_stage_b_protocol(root / "raw", changed)

    def test_stage_b_session_reuse_requires_immutable_record(self) -> None:
        protocol = minimal_cell_protocol()
        with tempfile.TemporaryDirectory() as directory:
            raw = Path(directory).resolve()
            first = campaign.ensure_stage_b_measurement_session(
                raw,
                protocol=protocol,
                target_id="oxipng",
                repeat=0,
            )
            path = raw / "sessions/oxipng/repeat-00.json"
            self.assertEqual(
                first,
                campaign.ensure_stage_b_measurement_session(
                    raw,
                    protocol=protocol,
                    target_id="oxipng",
                    repeat=0,
                ),
            )
            path.chmod(0o600)
            with self.assertRaises(
                campaign.immutable_evidence.ImmutableEvidenceError
            ):
                campaign.ensure_stage_b_measurement_session(
                    raw,
                    protocol=protocol,
                    target_id="oxipng",
                    repeat=0,
                )

    @mock.patch.object(campaign, "derive_stage_b_cell", side_effect=fake_derive_cell)
    def test_stage_b_cell_reuses_commit_and_recovers_after_failure(
        self, _derive: object
    ) -> None:
        protocol = minimal_cell_protocol()
        with tempfile.TemporaryDirectory() as directory:
            raw = Path(directory).resolve()
            calls = 0

            def collect(attempt: Path) -> dict[str, object]:
                nonlocal calls
                calls += 1
                return minimal_cell_sample(attempt)

            first = campaign.run_or_reuse_stage_b_cell(
                raw_dir=raw,
                protocol=protocol,
                target_id="oxipng",
                repeat=0,
                arm_name=campaign.TRANSPORT_POLICY_DISABLED_ARM,
                measurement_session_id="11111111-1111-4111-8111-111111111111",
                collect_sample=collect,
            )
            second = campaign.run_or_reuse_stage_b_cell(
                raw_dir=raw,
                protocol=protocol,
                target_id="oxipng",
                repeat=0,
                arm_name=campaign.TRANSPORT_POLICY_DISABLED_ARM,
                measurement_session_id="11111111-1111-4111-8111-111111111111",
                collect_sample=collect,
            )
            self.assertEqual(1, calls)
            self.assertEqual(first["record_path"], second["record_path"])
            self.assertEqual(first["record_sha256"], second["record_sha256"])

            failed_raw = raw / "failed"
            failed_calls = 0

            def fail_once(attempt: Path) -> dict[str, object]:
                nonlocal failed_calls
                failed_calls += 1
                (attempt / "partial.txt").write_text("partial", encoding="utf-8")
                if failed_calls == 1:
                    raise RuntimeError("interrupted")
                return minimal_cell_sample(attempt)

            with self.assertRaisesRegex(RuntimeError, "interrupted"):
                campaign.run_or_reuse_stage_b_cell(
                    raw_dir=failed_raw,
                    protocol=protocol,
                    target_id="oxipng",
                    repeat=0,
                    arm_name=campaign.TRANSPORT_POLICY_DISABLED_ARM,
                    measurement_session_id="22222222-2222-4222-8222-222222222222",
                    collect_sample=fail_once,
                )
            recovered = campaign.run_or_reuse_stage_b_cell(
                raw_dir=failed_raw,
                protocol=protocol,
                target_id="oxipng",
                repeat=0,
                arm_name=campaign.TRANSPORT_POLICY_DISABLED_ARM,
                measurement_session_id="22222222-2222-4222-8222-222222222222",
                collect_sample=fail_once,
            )
            self.assertEqual(2, failed_calls)
            self.assertTrue((failed_raw / recovered["record_path"]).is_file())

    @mock.patch.object(campaign, "derive_stage_b_cell", side_effect=fake_derive_cell)
    def test_stage_b_cell_reuse_rejects_artifact_mutation(
        self, _derive: object
    ) -> None:
        protocol = minimal_cell_protocol()
        with tempfile.TemporaryDirectory() as directory:
            raw = Path(directory).resolve()
            result = campaign.run_or_reuse_stage_b_cell(
                raw_dir=raw,
                protocol=protocol,
                target_id="oxipng",
                repeat=0,
                arm_name=campaign.TRANSPORT_POLICY_DISABLED_ARM,
                measurement_session_id="33333333-3333-4333-8333-333333333333",
                collect_sample=minimal_cell_sample,
            )
            Path(result["stdout_path"]).write_text("corrupt", encoding="utf-8")
            with self.assertRaises(
                campaign.immutable_evidence.ImmutableEvidenceError
            ):
                campaign.run_or_reuse_stage_b_cell(
                    raw_dir=raw,
                    protocol=protocol,
                    target_id="oxipng",
                    repeat=0,
                    arm_name=campaign.TRANSPORT_POLICY_DISABLED_ARM,
                    measurement_session_id="33333333-3333-4333-8333-333333333333",
                    collect_sample=minimal_cell_sample,
                )

    def test_stage_b_runtime_is_clean_and_records_libc_default_rseq(self) -> None:
        with tempfile.TemporaryDirectory() as temporary, mock.patch.dict(
            os.environ,
            {
                "PATH": "/usr/bin",
                "HOME": "/home/test",
                "LD_PRELOAD": "/tmp/wrong.so",
                "MALLOC_CONF": "dirty_decay_ms:0",
                "GLIBC_TUNABLES": "glibc.pthread.rseq=0",
                "RUSTFLAGS": "-Ctarget-cpu=native",
                "UNIALLOC_TEST_LEAK": "1",
            },
            clear=True,
        ):
            environment = campaign.stage_b_runtime_environment(
                Path(temporary), "adaptive-ordinary-compiler-prior"
            )
            record = campaign.suite_contract.runtime_environment_record(environment)
        self.assertEqual("C", environment["LANG"])
        self.assertEqual("C", environment["LC_ALL"])
        self.assertEqual(
            "adaptive-ordinary-compiler-prior",
            environment[campaign.stage_a.RUNTIME_ARM_ENV],
        )
        self.assertNotIn("GLIBC_TUNABLES", environment)
        self.assertEqual("libc_default", record["rseq_policy"])
        self.assertEqual(
            campaign.suite_contract.HOST_PRIMARY_MEASUREMENT_LOCK,
            campaign.DEFAULT_MEASUREMENT_LOCK,
        )


if __name__ == "__main__":
    unittest.main()
