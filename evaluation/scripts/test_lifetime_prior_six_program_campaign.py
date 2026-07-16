#!/usr/bin/env python3
"""Contract tests for the six-program Rust lifetime-prior campaign."""

from __future__ import annotations

import hashlib
import importlib.util
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import unittest
from unittest import mock


ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "evaluation" / "scripts" / "lifetime_prior_six_program_campaign.py"
spec = importlib.util.spec_from_file_location("lifetime_prior_six_program_campaign", SCRIPT)
assert spec is not None and spec.loader is not None
campaign = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = campaign
spec.loader.exec_module(campaign)


def write_audit(
    root: Path,
    *,
    enabled: bool,
    basis: str,
    hint: int,
    confidence: int = 0,
    crate_name: str | None = None,
    rewrite_status: str = "actual_semantic_scope_enter_exit_rewrite_applied",
    body_clone_returned: bool = True,
    actual_semantic_rewrite: bool = True,
    runtime_key: dict[str, int] | None = None,
    rewrite_candidates: list[dict[str, object]] | None = None,
) -> None:
    root.mkdir(parents=True, exist_ok=True)
    candidate = {
        "lifetime_hint_basis": basis,
        "lifetime_hint": hint,
        "lifetime_hint_confidence": confidence,
        "rewrite_status": rewrite_status,
        "lowering_kind": "semantic_scope_enter_exit_rewrite",
    }
    if runtime_key is not None:
        candidate.update(
            {
                field: runtime_key[field]
                for field in ("callsite", "type_id", "module_id")
            }
        )
        candidate["lifetime_analysis_features"] = {
            "runtime_join_key_complete": True,
            "runtime_join_key": runtime_key,
            "requested_layout_basis": "exact_box_new_payload_layout",
        }
    if rewrite_status == campaign.GENERIC_REWRITE_STATUS:
        candidate.update(
            {
                "type_id_basis": campaign.GENERIC_RUNTIME_TYPE_ID_BASIS,
                "replacement_symbol": (
                    "__unialloc_semantic_scope_push_for_rust_type"
                ),
                "replacement_resolution_status": (
                    "resolved_unialloc_semantic_scope_push_for_rust_type_pop"
                ),
                "metadata_pairing_contract": (
                    "semantic_scope_monomorphized_runtime_type_metadata"
                ),
            }
        )
    (root / "audit.json").write_text(
        json.dumps(
            {
                "rustc_args": (
                    ["rustc", "--crate-name", crate_name]
                    if crate_name is not None
                    else []
                ),
                "compiler_pass": {
                    "automatic_rust_lifetime_prior_enabled": enabled,
                    "actual_semantic_scope_rewrite_requested": True,
                    "actual_semantic_scope_rewrite": actual_semantic_rewrite,
                    "body_clone_returned_to_rustc": body_clone_returned,
                    "continue_compilation": True,
                },
                "rewrite_candidates": (
                    rewrite_candidates
                    if rewrite_candidates is not None
                    else [candidate]
                ),
            }
        ),
        encoding="utf-8",
    )


def runtime_stats(arm) -> dict[str, object]:
    stats: dict[str, object] = {
        field: 0 for field in campaign.runtime_lifetime.RUNTIME_REQUIRED_FIELDS
    }
    for field in (
        "all_mappings_released",
        "adaptive_observation_recording",
        "adaptive_force_track_all",
    ):
        stats[field] = False
    stats.update(
        {
            "policy": arm.expected_policy,
            "backend": 1 if arm.expected_policy in {5, 8} else 0,
            "adaptive_observation_recording": True,
            "adaptive_force_track_all": arm.force_track,
        }
    )
    return stats


def generic_compiler_row(
    *,
    callsite: int = 11,
    module_id: int = 13,
    requested_size: int | None = 4096,
    align: int | None = 8,
    hint: int = 2,
) -> dict[str, object]:
    audit_key = {
        "callsite": callsite,
        "type_id": 0,
        "module_id": module_id,
        "requested_size": requested_size,
        "align": align,
    }
    resolution_key = {
        field: audit_key[field] for field in campaign.GENERIC_RESOLUTION_KEY_FIELDS
    }
    return {
        **audit_key,
        "identity_mode": "generic_runtime_type",
        "compiler_audit_key": audit_key,
        "audit_type_id_sentinel": 0,
        "runtime_join_key_complete": False,
        "numeric_exact_key_complete": False,
        "generic_resolution_key_complete": (
            isinstance(requested_size, int)
            and requested_size > 0
            and isinstance(align, int)
            and align > 0
            and align & (align - 1) == 0
        ),
        "generic_resolution_key": resolution_key,
        "rewrite_status": campaign.GENERIC_REWRITE_STATUS,
        "type_id_basis": campaign.GENERIC_RUNTIME_TYPE_ID_BASIS,
        "replacement_symbol": "__unialloc_semantic_scope_push_for_rust_type_hints",
        "replacement_resolution_status": (
            "resolved_unialloc_semantic_scope_push_for_rust_type_hints_pop"
        ),
        "metadata_pairing_contract": (
            "semantic_scope_monomorphized_runtime_type_metadata"
        ),
        "lifetime_hint": hint,
        "lifetime_hint_basis": (
            "automatic_rust_lifetime_prior_return_long"
            if hint == 2
            else (
                "automatic_rust_lifetime_prior_all_path_local_release_short"
                if hint == 1
                else "default_unknown"
            )
        ),
    }


def generic_runtime_row(
    *,
    type_id: int = 17,
    callsite: int = 11,
    module_id: int = 13,
    requested_size: int = 4096,
    align: int = 8,
    allocation_count: int = 3,
    predictor_key_ambiguous: bool = False,
    latest_static_prior: int = 2,
) -> dict[str, object]:
    return {
        "callsite": callsite,
        "type_id": type_id,
        "module_id": module_id,
        "requested_size": requested_size,
        "align": align,
        "allocation_count": allocation_count,
        "allocation_requested_bytes": allocation_count * requested_size,
        "long_outcomes": allocation_count,
        "short_outcomes": 0,
        "censored_outcomes": 0,
        "long_requested_bytes": allocation_count * requested_size,
        "short_requested_bytes": 0,
        "censored_requested_bytes": 0,
        "live_survival_observations": 0,
        "live_survival_requested_bytes": 0,
        "live_survival_payload_bytes": 0,
        "current_live_survival_inflight": 0,
        "current_live_survival_inflight_requested_bytes": 0,
        "current_live_survival_inflight_payload_bytes": 0,
        "predictor_key_ambiguous": predictor_key_ambiguous,
        "latest_static_prior": latest_static_prior,
    }


def dynamic_compiler_row(
    *, callsite: int = 41, type_id: int = 43, module_id: int = 47
) -> dict[str, object]:
    audit_key = {
        "callsite": callsite,
        "type_id": type_id,
        "module_id": module_id,
        "requested_size": None,
        "align": None,
    }
    features = {
        "runtime_join_key_complete": False,
        "borrowed_vec_reserve_prior_eligible": True,
        "observation_candidate_rule_applied": True,
        "runtime_layout_captured_by_semantic_scope": True,
        "runtime_layout_subcohort_contract": (
            campaign.BORROWED_VEC_RESERVE_SUBCOHORT_CONTRACT
        ),
        "runtime_observation_min_requested_bytes": (
            campaign.BORROWED_VEC_RESERVE_MIN_REQUESTED_BYTES
        ),
        "runtime_observation_max_requested_bytes": (
            campaign.BORROWED_VEC_RESERVE_MAX_REQUESTED_BYTES
        ),
    }
    return {
        **audit_key,
        "identity_mode": "numeric_dynamic_layout",
        "compiler_audit_key": audit_key,
        "dynamic_layout_identity_key": {
            field: audit_key[field]
            for field in campaign.DYNAMIC_LAYOUT_IDENTITY_KEY_FIELDS
        },
        "runtime_join_key_complete": False,
        "numeric_exact_key_complete": False,
        "dynamic_layout_key_complete": True,
        "rewrite_status": "actual_semantic_scope_enter_exit_rewrite_applied",
        "lifetime_hint": campaign.BORROWED_VEC_RESERVE_OBSERVE_HINT,
        "lifetime_hint_basis": campaign.BORROWED_VEC_RESERVE_OBSERVE_BASIS,
        "lifetime_analysis_features": features,
    }


def smaps_text(*, rss: int, anon_hugepages: int) -> str:
    values = {
        "Rss": rss,
        "Pss": max(0, rss - 1),
        "Anonymous": max(0, rss - 2),
        "Private_Clean": 1,
        "Private_Dirty": max(0, rss - 3),
        "Shared_Clean": 2,
        "Shared_Dirty": 0,
        "AnonHugePages": anon_hugepages,
        "ShmemPmdMapped": 0,
        "FilePmdMapped": 0,
        "Shared_Hugetlb": 0,
        "Private_Hugetlb": 0,
    }
    return "\n".join(f"{field}: {value} kB" for field, value in values.items())


class LifetimePriorSixProgramCampaignTests(unittest.TestCase):
    def test_arm_matrix_keeps_prior_opt_in_and_diagnostic_separate(self) -> None:
        campaign.validate_arm_contract()
        self.assertEqual(5, len(campaign.PERFORMANCE_ARMS))
        self.assertEqual(2, len(campaign.SCREENING_ARMS))
        self.assertEqual(
            ["all-unknown", "compiler-prior"],
            [arm.build_group for arm in campaign.SCREENING_ARMS],
        )
        self.assertTrue(campaign.SCREENING_ARM.force_track)
        self.assertFalse(campaign.SCREENING_ARM.performance_arm)
        runtime_only_thp = campaign.ARM_BY_NAME[
            "adaptive-selective-thp-all-unknown"
        ]
        self.assertEqual(
            "adaptive-selective-thp-compiler-prior",
            campaign.runtime_arm_selector(runtime_only_thp),
        )
        for arm in campaign.ARMS:
            with self.subTest(arm=arm.name):
                environment = campaign.arm_build_environment(arm)
                self.assertEqual(
                    arm.automatic_rust_lifetime_prior,
                    campaign.AUTO_PRIOR_ENV in environment,
                )
                if arm.automatic_rust_lifetime_prior:
                    self.assertEqual("1", environment[campaign.AUTO_PRIOR_ENV])

    def test_six_target_manifest_reuses_exact_runner_contracts(self) -> None:
        campaign.validate_target_contracts()
        manifest = campaign.build_campaign_manifest()
        self.assertEqual(set(campaign.TARGET_ORDER), set(manifest["targets"]))
        self.assertEqual(
            campaign.collections_oxipng.TARGETS["oxipng"].commit,
            manifest["targets"]["oxipng"]["source_commit"],
        )
        self.assertEqual(
            list(campaign.psr.TARGET_SPECS["polars"].target_crates),
            manifest["targets"]["polars"]["target_crates"],
        )
        self.assertEqual(
            list(campaign.redb_actix.TARGETS["actix_web"].target_crates),
            manifest["targets"]["actix_web"]["target_crates"],
        )
        self.assertTrue(
            all(row["screening_ready"] for row in manifest["targets"].values())
        )
        self.assertEqual({"oxipng", "redb"}, {
            target_id
            for target_id, row in manifest["targets"].items()
            if row["performance_ready"]
        })
        self.assertEqual(
            {"polars", "swc", "rustpython", "actix_web"},
            set(manifest["performance_blockers"]),
        )

    def test_rustpython_routed_scope_excludes_uninstrumentable_git_dependency(
        self,
    ) -> None:
        routed = campaign.stage_a_target_crates("rustpython")
        self.assertIn("rustpython_vm", routed)
        self.assertIn("rustpython_codegen", routed)
        self.assertNotIn("rustpython_ruff_python_parser", routed)
        gaps = campaign.TARGET_CRATE_COVERAGE_GAPS["rustpython"]
        self.assertEqual("rustpython_ruff_python_parser", gaps[0]["crate"])
        self.assertIn("first-party", gaps[0]["claim_boundary"])

    def test_actix_uses_service_workload_with_metadata_bearing_crates(self) -> None:
        target = campaign.TARGETS["actix_web"]
        self.assertEqual("async_web_service_direct", target.harness_id)
        self.assertEqual(
            ("service", "actix_web", "actix_http", "actix_router"),
            campaign.stage_a_target_crates("actix_web"),
        )
        self.assertEqual("service", campaign._artifact_name("actix_web"))

    def test_oxipng_calibration_retains_measured_identity_and_estimates(self) -> None:
        record = campaign.calibration_record(campaign.TARGETS["oxipng"])
        assert record is not None
        self.assertEqual(8, record["work_units"])
        self.assertEqual(10.87, record["elapsed_seconds"])
        self.assertEqual(42_276, record["peak_rss_kib"])
        self.assertFalse(record["claim_grade"])
        self.assertEqual(campaign.OXIPNG_INPUT_SHA256, record["input_sha256"])
        self.assertAlmostEqual(
            86.96, record["estimated_seconds_by_work_units"]["64"], places=2
        )
        self.assertAlmostEqual(
            173.92, record["estimated_seconds_by_work_units"]["128"], places=2
        )

    def test_work_derivation_rounds_up_and_enforces_hard_cap(self) -> None:
        calibration = campaign.Calibration("test", 8, 10.0, None, False, "test")
        plan = campaign.derive_work_units(
            calibration, target_seconds=40.0, granularity=8
        )
        self.assertEqual(32, plan["work_units"])
        self.assertEqual(40.0, plan["estimated_seconds"])
        with self.assertRaises(campaign.CampaignContractError):
            campaign.derive_work_units(
                calibration,
                target_seconds=601.0,
                granularity=8,
            )

    def test_stage_duration_contract_is_fail_closed(self) -> None:
        campaign.validate_stage_duration("screening", 20.0)
        campaign.validate_stage_duration("screening", 60.0)
        campaign.validate_stage_duration("performance", 120.0)
        campaign.validate_stage_duration("performance", 300.0)
        for stage, value in (("screening", 19.9), ("performance", 301.0)):
            with self.subTest(stage=stage, value=value):
                with self.assertRaises(campaign.CampaignContractError):
                    campaign.validate_stage_duration(stage, value)

    def test_criterion_command_records_explicit_warmup_boundary(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            binary = Path(directory) / "criterion-bench"
            binary.write_bytes(b"bench")
            command = campaign.criterion_screening_command(
                binary=binary,
                selector="real/program",
                measurement_seconds=30.0,
                warm_up_seconds=5.0,
            )
            warmup_index = command.index("--warm-up-time")
            self.assertEqual("5.000", command[warmup_index + 1])
            with self.assertRaises(campaign.CampaignContractError):
                campaign.criterion_screening_command(
                    binary=binary,
                    selector="real/program",
                    measurement_seconds=30.0,
                    warm_up_seconds=0.0,
                )

    def test_screening_summary_and_long_opportunity_gate(self) -> None:
        rows = [
            {
                "requested_size": 4096,
                "long_requested_bytes": 2 * campaign.MIB,
                "short_requested_bytes": campaign.MIB,
                "censored_requested_bytes": 0,
                "allocation_requested_bytes": 3 * campaign.MIB,
                "live_survival_observations": 0,
                "current_live_survival_inflight": 0,
                "current_live_survival_inflight_requested_bytes": 0,
                "latest_prediction": 2,
            },
            {
                "requested_size": 64,
                "long_requested_bytes": 0,
                "short_requested_bytes": campaign.MIB,
                "censored_requested_bytes": 64,
                "allocation_requested_bytes": campaign.MIB + 64,
                "live_survival_observations": 0,
                "current_live_survival_inflight": 0,
                "current_live_survival_inflight_requested_bytes": 0,
                "latest_prediction": 1,
            },
        ]
        summary = campaign.summarize_screening_sites(rows)
        self.assertEqual(2 * campaign.MIB, summary["long_requested_bytes"])
        self.assertEqual(campaign.MIB, summary["confirmed_short_requested_bytes"])
        self.assertAlmostEqual(0.5, summary["candidate_long_byte_density"])
        gate = campaign.opportunity_gate(summary)
        self.assertTrue(gate["passed"])
        self.assertTrue(gate["long_cohort_passed"])
        self.assertFalse(gate["confirmed_short_passed"])

    def test_confirmed_short_bytes_can_admit_stage_b(self) -> None:
        gate = campaign.opportunity_gate(
            {
                "long_requested_bytes": 0,
                "confirmed_short_requested_bytes": 8 * campaign.MIB,
            }
        )
        self.assertTrue(gate["passed"])
        self.assertTrue(gate["confirmed_short_passed"])

    def test_screening_counts_current_survivors_once_and_keeps_history_as_provenance(self) -> None:
        row = {
            "requested_size": 4096,
            "long_requested_bytes": 4096,
            "short_requested_bytes": 0,
            "censored_requested_bytes": 0,
            "allocation_requested_bytes": 4 * 4096,
            "live_survival_observations": 3,
            "current_live_survival_inflight": 2,
            "current_live_survival_inflight_requested_bytes": 2 * 4096,
            "latest_prediction": 2,
        }
        summary = campaign.summarize_screening_sites([row])

        self.assertEqual(3 * 4096, summary["long_requested_bytes"])
        self.assertEqual(4096, summary["completed_long_requested_bytes"])
        self.assertEqual(3, summary["live_survival_observations"])
        self.assertEqual(2, summary["current_live_survival_inflight"])

    def test_screening_rejects_malformed_current_survivor_bytes(self) -> None:
        row = {
            "requested_size": 4096,
            "long_requested_bytes": 0,
            "short_requested_bytes": 0,
            "censored_requested_bytes": 0,
            "allocation_requested_bytes": 4096,
            "live_survival_observations": 1,
            "current_live_survival_inflight": 1,
            "current_live_survival_inflight_requested_bytes": 1,
            "latest_prediction": 2,
        }
        with self.assertRaises(campaign.CampaignContractError):
            campaign.summarize_screening_sites([row])

    def test_explicit_sample_arm_must_match_compiler_build_group(self) -> None:
        with tempfile.TemporaryDirectory() as directory, self.assertRaises(
            campaign.CampaignContractError
        ):
            campaign.run_stage_a_sample(
                "oxipng",
                "all-unknown",
                build={},
                raw_dir=Path(directory),
                input_path=None,
                work_units=1,
                measurement_seconds=30.0,
                timeout=60.0,
                sample_interval=0.5,
                arm_name="adaptive-ordinary-compiler-prior",
            )

    def test_compiler_audit_digest_and_prior_mode_are_bound(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            write_audit(
                root,
                enabled=True,
                basis="automatic_rust_lifetime_prior_return_long",
                hint=2,
                confidence=70,
            )
            summary = campaign.summarize_compiler_prior_audits(root)
            self.assertTrue(summary["automatic_rust_lifetime_prior_enabled"])
            self.assertEqual(1, summary["classified_candidate_count"])
            self.assertEqual(64, len(summary["audit_digest"]))
            campaign.validate_build_audit_for_arm(
                campaign.ARM_BY_NAME["adaptive-ordinary-compiler-prior"], summary
            )
            with self.assertRaises(campaign.CampaignContractError):
                campaign.validate_build_audit_for_arm(
                    campaign.ARM_BY_NAME["adaptive-ordinary-all-unknown"], summary
                )

    def test_generic_and_numeric_scope_rewrites_share_applied_contract(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            write_audit(
                root / "generic",
                enabled=True,
                basis="automatic_rust_lifetime_prior_return_long",
                hint=2,
                confidence=70,
                crate_name="execution",
                rewrite_status="actual_semantic_scope_generic_type_rewrite_applied",
                runtime_key={
                    "callsite": 1,
                    "type_id": 0,
                    "module_id": 3,
                    "requested_size_bytes": 4096,
                    "requested_align_bytes": 8,
                },
            )
            write_audit(
                root / "numeric",
                enabled=True,
                basis=(
                    "automatic_rust_lifetime_prior_all_path_local_release_short"
                ),
                hint=1,
                confidence=85,
                crate_name="execution",
                rewrite_status="actual_semantic_scope_enter_exit_rewrite_applied",
                runtime_key={
                    "callsite": 11,
                    "type_id": 12,
                    "module_id": 13,
                    "requested_size_bytes": 8192,
                    "requested_align_bytes": 16,
                },
            )
            summary = campaign.summarize_compiler_prior_audits(root)
            exported = campaign.export_compiler_site_features(root)
            self.assertEqual(2, summary["classified_candidate_count"])
            self.assertEqual(2, summary["applied_allocation_candidate_count"])
            self.assertEqual(2, summary["applied_prior_hinted_candidate_count"])
            self.assertEqual(2, exported["site_count"])
            self.assertEqual(2, exported["applied_prior_hinted_count"])
            self.assertEqual(1, exported["numeric_site_count"])
            self.assertEqual(1, exported["generic_site_count"])
            self.assertEqual(1, exported["numeric_exact_key_complete_count"])
            self.assertEqual(1, exported["generic_resolution_key_complete_count"])
            generic = next(
                row
                for row in exported["rows"]
                if row["identity_mode"] == "generic_runtime_type"
            )
            self.assertFalse(generic["runtime_join_key_complete"])
            self.assertTrue(generic["audit_runtime_join_key_complete"])
            self.assertEqual(0, generic["audit_type_id_sentinel"])
            self.assertEqual(
                {
                    "actual_semantic_scope_enter_exit_rewrite_applied",
                    "actual_semantic_scope_generic_type_rewrite_applied",
                },
                {row["rewrite_status"] for row in exported["rows"]},
            )
            campaign.validate_build_audit_for_arm(
                campaign.SCREENING_ARM, summary, ("execution",)
            )

    def test_concrete_generic_scope_rewrite_exports_and_joins_numeric_key5(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            runtime_key = {
                "callsite": 101,
                "type_id": 102,
                "module_id": 103,
                "requested_size_bytes": 24_576,
                "requested_align_bytes": 8,
            }
            write_audit(
                root,
                enabled=True,
                basis="automatic_rust_lifetime_prior_return_long",
                hint=2,
                confidence=70,
                crate_name="execution",
                rewrite_status=campaign.GENERIC_REWRITE_STATUS,
                runtime_key=runtime_key,
            )
            exported = campaign.export_compiler_site_features(root)
            self.assertEqual(1, exported["numeric_site_count"])
            self.assertEqual(0, exported["generic_site_count"])
            self.assertEqual(1, exported["numeric_exact_key_complete_count"])
            compiler = exported["rows"][0]
            self.assertEqual("numeric_exact", compiler["identity_mode"])
            self.assertIsNone(compiler["audit_type_id_sentinel"])

            runtime = generic_runtime_row(
                callsite=runtime_key["callsite"],
                type_id=runtime_key["type_id"],
                module_id=runtime_key["module_id"],
                requested_size=runtime_key["requested_size_bytes"],
                align=runtime_key["requested_align_bytes"],
            )
            joined = campaign.join_compiler_runtime_sites(exported, [runtime])
            self.assertEqual(1, joined["numeric_matched_site_count"])
            self.assertEqual(0, joined["generic_compiler_site_count"])
            self.assertEqual(
                "exact_numeric_match", joined["rows"][0]["resolution_status"]
            )

    def test_borrowed_vec_candidate_exports_authenticated_dynamic_key3(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            runtime_key = {
                "callsite": 41,
                "type_id": 43,
                "module_id": 47,
                "requested_size_bytes": None,
                "requested_align_bytes": None,
            }
            write_audit(
                root,
                enabled=True,
                basis=campaign.BORROWED_VEC_RESERVE_OBSERVE_BASIS,
                hint=campaign.BORROWED_VEC_RESERVE_OBSERVE_HINT,
                confidence=70,
                runtime_key=runtime_key,
            )
            path = root / "audit.json"
            document = json.loads(path.read_text(encoding="utf-8"))
            features = document["rewrite_candidates"][0][
                "lifetime_analysis_features"
            ]
            features.update(
                dynamic_compiler_row()["lifetime_analysis_features"]
            )
            path.write_text(json.dumps(document), encoding="utf-8")

            summary = campaign.summarize_compiler_prior_audits(root)
            exported = campaign.export_compiler_site_features(root)
            self.assertEqual(0, summary["classified_candidate_count"])
            self.assertEqual(0, summary["applied_prior_hinted_candidate_count"])
            self.assertEqual(1, summary["applied_observation_candidate_count"])
            self.assertEqual(0, exported["numeric_site_count"])
            self.assertEqual(1, exported["dynamic_layout_site_count"])
            self.assertEqual(1, exported["dynamic_layout_key_complete_count"])
            self.assertEqual(1, exported["applied_observation_candidate_count"])
            candidate = exported["rows"][0]
            self.assertEqual("numeric_dynamic_layout", candidate["identity_mode"])
            self.assertTrue(candidate["dynamic_layout_key_complete"])
            self.assertFalse(candidate["runtime_join_key_complete"])

    def test_reuse_refreshes_evaluator_derived_compiler_site_export(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            audit_root = root / "audits"
            write_audit(
                audit_root,
                enabled=True,
                basis="automatic_rust_lifetime_prior_return_long",
                hint=2,
                confidence=70,
                crate_name="execution",
                rewrite_status=campaign.GENERIC_REWRITE_STATUS,
                runtime_key={
                    "callsite": 101,
                    "type_id": 0,
                    "module_id": 103,
                    "requested_size_bytes": 4096,
                    "requested_align_bytes": 8,
                },
            )
            compiler_audit = campaign.summarize_compiler_prior_audits(audit_root)
            build_dir = root / "build"
            build_dir.mkdir()
            compiler_sites_path = build_dir / "compiler-sites.json"
            compiler_sites_path.write_text(
                json.dumps(
                    {"source": "unialloc-compiler-runtime-exact-site-export-v1"}
                ),
                encoding="utf-8",
            )
            record_path = build_dir / "build.json"
            cached = {
                "audit_dir": str(audit_root),
                "compiler_sites_path": str(compiler_sites_path),
                "compiler_sites_sha256": campaign.sha256_file(
                    compiler_sites_path
                ),
                "compiler_sites": {"source": "stale-v1"},
                "compiler_audit": compiler_audit,
            }
            campaign.write_json(record_path, cached)

            self.assertTrue(
                campaign.refresh_cached_compiler_site_export(
                    cached, record_path=record_path
                )
            )
            exported = json.loads(compiler_sites_path.read_text(encoding="utf-8"))
            persisted = json.loads(record_path.read_text(encoding="utf-8"))
            self.assertEqual(
                campaign.COMPILER_SITE_EXPORT_SOURCE, exported["source"]
            )
            self.assertEqual(1, exported["generic_site_count"])
            self.assertEqual(1, exported["generic_resolution_key_complete_count"])
            self.assertEqual(
                campaign.COMPILER_SITE_EXPORT_SOURCE,
                persisted["compiler_sites"]["source"],
            )
            self.assertEqual(
                campaign.sha256_file(compiler_sites_path),
                persisted["compiler_sites_sha256"],
            )
            self.assertTrue(
                persisted["compiler_sites_derivation"][
                    "regenerated_during_reuse"
                ]
            )
            self.assertFalse(
                persisted["compiler_sites_derivation"]["binary_rebuilt"]
            )

    def test_compiler_audit_rejects_missing_or_mixed_prior_provenance(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            write_audit(root / "on", enabled=True, basis="unknown", hint=0)
            write_audit(root / "off", enabled=False, basis="unknown", hint=0)
            with self.assertRaises(campaign.CampaignContractError):
                campaign.summarize_compiler_prior_audits(root)

    def test_compiler_audit_requires_every_requested_target_crate(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            write_audit(
                root,
                enabled=True,
                basis="automatic_rust_lifetime_prior_return_long",
                hint=2,
                confidence=70,
                crate_name="execution",
            )
            summary = campaign.summarize_compiler_prior_audits(root)
            campaign.validate_build_audit_for_arm(
                campaign.SCREENING_ARM, summary, ("execution",)
            )
            with self.assertRaises(campaign.CampaignContractError):
                campaign.validate_build_audit_for_arm(
                    campaign.SCREENING_ARM,
                    summary,
                    ("execution", "rustpython_vm"),
                )

    def test_compiler_audit_requires_applied_rewrite_and_body_clone(self) -> None:
        for body_clone, semantic_rewrite, status in (
            (False, True, "actual_semantic_scope_enter_exit_rewrite_applied"),
            (True, False, "actual_semantic_scope_enter_exit_rewrite_applied"),
            (True, True, "semantic_scope_enter_exit_rewrite_planned"),
        ):
            with self.subTest(
                body_clone=body_clone,
                semantic_rewrite=semantic_rewrite,
                status=status,
            ), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                write_audit(
                    root,
                    enabled=True,
                    basis="automatic_rust_lifetime_prior_return_long",
                    hint=2,
                    confidence=70,
                    crate_name="execution",
                    rewrite_status=status,
                    body_clone_returned=body_clone,
                    actual_semantic_rewrite=semantic_rewrite,
                )
                summary = campaign.summarize_compiler_prior_audits(root)
                with self.assertRaises(campaign.CampaignContractError):
                    campaign.validate_build_audit_for_arm(
                        campaign.SCREENING_ARM, summary, ("execution",)
                    )

    def test_compiler_prior_requires_complete_hint_transport(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            write_audit(
                root,
                enabled=True,
                basis="automatic_rust_lifetime_prior_return_long",
                hint=2,
                confidence=70,
                crate_name="execution",
            )
            summary = campaign.summarize_compiler_prior_audits(root)
            summary["classified_candidate_count"] = 2
            with self.assertRaises(campaign.CampaignContractError):
                campaign.validate_build_audit_for_arm(
                    campaign.SCREENING_ARM, summary, ("execution",)
                )

    def test_polars_shaped_audits_allow_zero_and_drop_only_crates(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            write_audit(
                root / "runner",
                enabled=True,
                basis="automatic_rust_lifetime_prior_return_long",
                hint=2,
                confidence=70,
                crate_name="polars_unialloc_primary",
            )
            write_audit(
                root / "zero",
                enabled=True,
                basis="default_unknown",
                hint=0,
                crate_name="polars",
                actual_semantic_rewrite=False,
                rewrite_candidates=[],
            )
            write_audit(
                root / "drop",
                enabled=True,
                basis="default_unknown",
                hint=0,
                crate_name="polars_core",
                actual_semantic_rewrite=False,
                rewrite_candidates=[
                    {
                        "lifetime_hint_basis": "default_unknown",
                        "lifetime_hint": 0,
                        "lifetime_hint_confidence": 0,
                        "rewrite_status": (
                            "actual_semantic_scope_drop_rewrite_applied"
                        ),
                    }
                ],
            )
            summary = campaign.summarize_compiler_prior_audits(root)
            self.assertTrue(summary["actual_rewrite_provenance_valid"])
            self.assertEqual(1, summary["applied_allocation_candidate_count"])
            self.assertEqual(1, summary["classified_candidate_count"])
            self.assertEqual(1, summary["applied_prior_hinted_candidate_count"])
            zero = summary["crate_provenance"]["polars"]
            drop = summary["crate_provenance"]["polars_core"]
            runner = summary["crate_provenance"]["polars_unialloc_primary"]
            self.assertEqual(1, zero["base_valid_file_count"])
            self.assertEqual(0, zero["applied_allocation_file_count"])
            self.assertEqual(1, drop["base_valid_file_count"])
            self.assertEqual(0, drop["applied_allocation_file_count"])
            self.assertEqual(1, runner["applied_allocation_file_count"])
            self.assertEqual(1, runner["valid_applied_allocation_file_count"])
            campaign.validate_build_audit_for_arm(
                campaign.SCREENING_ARM,
                summary,
                ("polars_unialloc_primary", "polars", "polars_core"),
            )

    def test_exact_join_rejects_duplicate_tuples_and_marks_zero_coverage(self) -> None:
        runtime = {
            field: index + 1
            for index, field in enumerate(campaign.runtime_lifetime.RUNTIME_SITE_KEY_FIELDS)
        }
        runtime.update(
            {
                "align": 8,
                "allocation_count": 1,
                "predictor_key_ambiguous": False,
                "latest_static_prior": 0,
            }
        )
        with self.assertRaises(campaign.CampaignContractError):
            campaign.join_compiler_runtime_sites(
                {"rows": []}, [runtime, dict(runtime)]
            )
        compiler = {
            **runtime,
            "runtime_join_key_complete": True,
            "lifetime_hint": 2,
            "lifetime_hint_basis": "automatic_rust_lifetime_prior_return_long",
        }
        with self.assertRaises(campaign.CampaignContractError):
            campaign.join_compiler_runtime_sites(
                {"rows": [compiler, dict(compiler)]}, [runtime]
            )
        unmatched = dict(runtime)
        unmatched["callsite"] += 1000
        result = campaign.join_compiler_runtime_sites(
            {"rows": [compiler]}, [unmatched]
        )
        self.assertEqual("no-static-coverage", result["status"])
        self.assertEqual(0.0, result["match_coverage"])
        self.assertFalse(result["runtime_join_rewrite_success"])

    def test_generic_join_resolves_one_authenticated_runtime_type(self) -> None:
        compiler = generic_compiler_row()
        runtime = generic_runtime_row()
        result = campaign.join_compiler_runtime_sites(
            {"rows": [compiler]}, [runtime]
        )
        self.assertEqual("complete-static-coverage", result["status"])
        self.assertEqual(1, result["generic_compiler_site_count"])
        self.assertEqual(1, result["generic_resolved_site_count"])
        self.assertEqual(0, result["numeric_matched_site_count"])
        self.assertTrue(result["generic_runtime_join_rewrite_success"])
        joined = result["rows"][0]
        self.assertEqual(
            "generic_unique_runtime_type_match", joined["resolution_status"]
        )
        self.assertEqual(0, joined["audit_type_id_sentinel"])
        self.assertEqual(17, joined["resolved_runtime_exact_key"]["type_id"])
        self.assertTrue(result["claim_scope"]["adaptive_site_key_unchanged"])
        outcomes = result["matched_applied_prior_outcomes"]
        self.assertTrue(outcomes["evidence_complete"])
        self.assertEqual(1, outcomes["site_count"])
        self.assertEqual(3, outcomes["allocation_count"])
        self.assertEqual(3, outcomes["long_outcomes"])

    def test_dynamic_key3_joins_multiple_runtime_layout_subcohorts(self) -> None:
        compiler = dynamic_compiler_row()
        runtime_8k = generic_runtime_row(
            callsite=41,
            type_id=43,
            module_id=47,
            requested_size=8192,
            align=8,
            allocation_count=3,
            latest_static_prior=0,
        )
        runtime_15k = generic_runtime_row(
            callsite=41,
            type_id=43,
            module_id=47,
            requested_size=15_360,
            align=16,
            allocation_count=5,
            latest_static_prior=0,
        )
        result = campaign.join_compiler_runtime_sites(
            {"rows": [compiler]}, [runtime_8k, runtime_15k]
        )
        joined = result["rows"][0]
        self.assertEqual(
            "dynamic_key3_runtime_layout_match", joined["resolution_status"]
        )
        self.assertTrue(joined["matched"])
        self.assertIsNone(joined["resolved_runtime_exact_key"])
        self.assertEqual(2, len(joined["resolved_runtime_exact_keys"]))
        self.assertEqual(2, len(joined["runtime_subcohorts"]))
        self.assertEqual(1, result["dynamic_layout_resolved_site_count"])
        self.assertEqual(2, result["dynamic_layout_resolved_subcohort_count"])
        self.assertTrue(result["observation_candidate_join_success"])
        outcomes = result["matched_observation_candidate_outcomes"]
        self.assertEqual(2, outcomes["site_count"])
        self.assertEqual(8, outcomes["allocation_count"])
        self.assertEqual(
            3 * 8192 + 5 * 15_360, outcomes["allocation_requested_bytes"]
        )
        self.assertEqual("no-static-coverage", result["status"])

    def test_dynamic_key3_rejects_out_of_band_and_static_prior_transport(self) -> None:
        compiler = dynamic_compiler_row()
        out_of_band = generic_runtime_row(
            callsite=41,
            type_id=43,
            module_id=47,
            requested_size=1024,
            latest_static_prior=0,
        )
        result = campaign.join_compiler_runtime_sites(
            {"rows": [compiler]}, [out_of_band]
        )
        self.assertEqual(
            "rejected_runtime_layout_outside_candidate_band",
            result["rows"][0]["resolution_status"],
        )

        wrong_transport = generic_runtime_row(
            callsite=41,
            type_id=43,
            module_id=47,
            requested_size=8192,
            latest_static_prior=2,
        )
        result = campaign.join_compiler_runtime_sites(
            {"rows": [compiler]}, [wrong_transport]
        )
        self.assertEqual(
            "rejected_observation_transport_mismatch",
            result["rows"][0]["resolution_status"],
        )

    def test_join_separates_matched_from_all_executed_prior_outcomes(self) -> None:
        compiler = generic_compiler_row()
        matched = generic_runtime_row()
        executed_only = generic_runtime_row(
            type_id=29,
            callsite=31,
            requested_size=8,
            allocation_count=3973,
            latest_static_prior=1,
        )
        executed_only.update(
            {
                "long_outcomes": 0,
                "short_outcomes": 3973,
                "long_requested_bytes": 0,
                "short_requested_bytes": 3973 * 8,
            }
        )
        result = campaign.join_compiler_runtime_sites(
            {"rows": [compiler]}, [matched, executed_only]
        )
        matched_outcomes = result["matched_applied_prior_outcomes"]
        executed_outcomes = result["executed_static_prior_outcomes"]
        self.assertEqual(1, matched_outcomes["site_count"])
        self.assertEqual(3, matched_outcomes["allocation_count"])
        self.assertEqual(3, matched_outcomes["long_outcomes"])
        self.assertEqual(2, executed_outcomes["site_count"])
        self.assertEqual(3976, executed_outcomes["allocation_count"])
        self.assertEqual(3, executed_outcomes["long_outcomes"])
        self.assertEqual(3973, executed_outcomes["short_outcomes"])

    def test_numeric_join_never_falls_back_to_generic_key4(self) -> None:
        runtime = generic_runtime_row(type_id=17)
        compiler = {
            **{
                field: runtime[field]
                for field in campaign.runtime_lifetime.RUNTIME_SITE_KEY_FIELDS
            },
            "type_id": 19,
            "runtime_join_key_complete": True,
            "lifetime_hint": 2,
            "lifetime_hint_basis": "automatic_rust_lifetime_prior_return_long",
        }
        result = campaign.join_compiler_runtime_sites(
            {"rows": [compiler]}, [runtime]
        )
        self.assertEqual("unobserved", result["rows"][0]["resolution_status"])
        self.assertEqual(0, result["matched_site_count"])
        self.assertEqual(0, result["generic_compiler_site_count"])

    def test_numeric_join_authenticates_static_prior_transport(self) -> None:
        runtime = generic_runtime_row(type_id=17, latest_static_prior=1)
        compiler = {
            **{
                field: runtime[field]
                for field in campaign.runtime_lifetime.RUNTIME_SITE_KEY_FIELDS
            },
            "runtime_join_key_complete": True,
            "lifetime_hint": 2,
            "lifetime_hint_basis": "automatic_rust_lifetime_prior_return_long",
        }
        result = campaign.join_compiler_runtime_sites(
            {"rows": [compiler]}, [runtime]
        )
        joined = result["rows"][0]
        self.assertFalse(joined["matched"])
        self.assertEqual(
            "rejected_static_prior_transport_mismatch",
            joined["resolution_status"],
        )
        self.assertEqual(
            "runtime_latest_static_prior_mismatch",
            joined["rejection_reason"],
        )

    def test_generic_join_rejects_multi_type_zst_and_ambiguous_rows(self) -> None:
        cases = (
            (
                generic_compiler_row(),
                [generic_runtime_row(type_id=17), generic_runtime_row(type_id=19)],
                "rejected_multi_runtime_type_id",
            ),
            (generic_compiler_row(requested_size=0), [], "ineligible_zst"),
            (
                generic_compiler_row(),
                [generic_runtime_row(predictor_key_ambiguous=True)],
                "rejected_predictor_key_ambiguous",
            ),
            (
                generic_compiler_row(),
                [generic_runtime_row(allocation_count=0)],
                "rejected_zero_runtime_allocations",
            ),
        )
        for compiler, runtime, expected in cases:
            with self.subTest(expected=expected):
                result = campaign.join_compiler_runtime_sites(
                    {"rows": [compiler]}, runtime
                )
                self.assertEqual(expected, result["rows"][0]["resolution_status"])
                self.assertFalse(result["rows"][0]["matched"])

    def test_generic_join_rejects_bad_basis_symbol_and_prior_transport(self) -> None:
        cases: list[tuple[dict[str, object], dict[str, object], str, str]] = []
        bad_basis = generic_compiler_row()
        bad_basis["type_id_basis"] = "compiler_type_name_hash"
        cases.append(
            (
                bad_basis,
                generic_runtime_row(),
                "rejected_generic_contract",
                "bad_type_id_basis",
            )
        )
        bad_symbol = generic_compiler_row()
        bad_symbol["replacement_symbol"] = "__unauthenticated_generic_push"
        cases.append(
            (
                bad_symbol,
                generic_runtime_row(),
                "rejected_generic_contract",
                "unauthenticated_replacement_symbol",
            )
        )
        cases.append(
            (
                generic_compiler_row(),
                generic_runtime_row(latest_static_prior=1),
                "rejected_static_prior_transport_mismatch",
                "runtime_latest_static_prior_mismatch",
            )
        )
        for compiler, runtime, status, reason in cases:
            with self.subTest(reason=reason):
                result = campaign.join_compiler_runtime_sites(
                    {"rows": [compiler]}, [runtime]
                )
                joined = result["rows"][0]
                self.assertEqual(status, joined["resolution_status"])
                self.assertEqual(reason, joined["rejection_reason"])

    def test_generic_join_rejects_duplicate_key4_and_double_claim(self) -> None:
        generic = generic_compiler_row()
        runtime = generic_runtime_row()
        with self.assertRaises(campaign.CampaignContractError):
            campaign.join_compiler_runtime_sites(
                {"rows": [generic, dict(generic)]}, [runtime]
            )
        numeric = {
            **{
                field: runtime[field]
                for field in campaign.runtime_lifetime.RUNTIME_SITE_KEY_FIELDS
            },
            "runtime_join_key_complete": True,
            "lifetime_hint": 0,
            "lifetime_hint_basis": "default_unknown",
        }
        with self.assertRaises(campaign.CampaignContractError):
            campaign.join_compiler_runtime_sites(
                {"rows": [numeric, generic]}, [runtime]
            )

    def test_compiler_export_rejects_raw_and_top_level_identity_mismatch(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            write_audit(
                root,
                enabled=True,
                basis="automatic_rust_lifetime_prior_return_long",
                hint=2,
                confidence=70,
                rewrite_status=campaign.GENERIC_REWRITE_STATUS,
                runtime_key={
                    "callsite": 11,
                    "type_id": 0,
                    "module_id": 13,
                    "requested_size_bytes": 4096,
                    "requested_align_bytes": 8,
                },
            )
            path = root / "audit.json"
            document = json.loads(path.read_text(encoding="utf-8"))
            document["rewrite_candidates"][0]["callsite"] = 12
            path.write_text(json.dumps(document), encoding="utf-8")
            with self.assertRaises(campaign.CampaignContractError):
                campaign.export_compiler_site_features(root)

    def test_unknown_rewrite_match_cannot_substitute_for_prior_coverage(self) -> None:
        runtime = {
            field: index + 1
            for index, field in enumerate(campaign.runtime_lifetime.RUNTIME_SITE_KEY_FIELDS)
        }
        runtime.update(
            {
                "align": 8,
                "allocation_count": 1,
                "predictor_key_ambiguous": False,
                "latest_static_prior": 2,
            }
        )
        unknown = {
            **runtime,
            "runtime_join_key_complete": True,
            "lifetime_hint": 0,
            "lifetime_hint_basis": "default_unknown",
        }
        long_unmatched = {
            **runtime,
            "callsite": runtime["callsite"] + 100,
            "runtime_join_key_complete": True,
            "lifetime_hint": 2,
            "lifetime_hint_basis": "automatic_rust_lifetime_prior_return_long",
        }
        short_incomplete = {
            **runtime,
            "runtime_join_key_complete": False,
            "lifetime_hint": 1,
            "lifetime_hint_basis": (
                "automatic_rust_lifetime_prior_all_path_local_release_short"
            ),
        }
        result = campaign.join_compiler_runtime_sites(
            {"rows": [unknown, long_unmatched, short_incomplete]}, [runtime]
        )
        self.assertFalse(result["generic_runtime_join_rewrite_success"])
        self.assertTrue(result["runtime_join_rewrite_success"])
        self.assertEqual(2, result["applied_prior_classified_count"])
        self.assertEqual(0, result["matched_applied_prior_site_count"])
        self.assertEqual("no-static-coverage", result["status"])
        self.assertFalse(result["static_coverage_claim_eligible"])

    def test_prior_eligibility_survives_run_projection_into_target_state(self) -> None:
        runtime = {
            field: index + 1
            for index, field in enumerate(campaign.runtime_lifetime.RUNTIME_SITE_KEY_FIELDS)
        }
        runtime.update(
            {
                "align": 8,
                "allocation_count": 1,
                "predictor_key_ambiguous": False,
                "latest_static_prior": 2,
            }
        )
        applied_long = {
            **runtime,
            "runtime_join_key_complete": True,
            "lifetime_hint": 2,
            "lifetime_hint_basis": "automatic_rust_lifetime_prior_return_long",
        }
        long_join = campaign.join_compiler_runtime_sites(
            {"rows": [applied_long]}, [runtime]
        )
        long_projection = campaign.compact_compiler_runtime_exact_join(long_join)
        self.assertNotIn("rows", long_projection)
        self.assertTrue(long_projection["static_coverage_claim_eligible"])
        self.assertEqual(1, long_projection["matched_applied_prior_site_count"])
        long_state = campaign.target_static_coverage_state(
            {"compiler-prior": {"compiler_runtime_exact_join": long_projection}}
        )
        self.assertEqual("complete", long_state["status"])
        self.assertTrue(long_state["claim_eligible"])

        unknown = {
            **runtime,
            "runtime_join_key_complete": True,
            "lifetime_hint": 0,
            "lifetime_hint_basis": "default_unknown",
        }
        unknown_projection = campaign.compact_compiler_runtime_exact_join(
            campaign.join_compiler_runtime_sites({"rows": [unknown]}, [runtime])
        )
        unknown_state = campaign.target_static_coverage_state(
            {"compiler-prior": {"compiler_runtime_exact_join": unknown_projection}}
        )
        self.assertEqual("complete-no-static-coverage", unknown_state["status"])
        self.assertFalse(unknown_state["claim_eligible"])

    def test_all_unknown_only_marks_static_coverage_not_measured(self) -> None:
        state = campaign.target_static_coverage_state(
            {
                "all-unknown": {
                    "compiler_runtime_exact_join": {
                        "static_coverage_claim_eligible": True
                    }
                }
            }
        )
        self.assertEqual("not-measured", state["measurement_status"])
        self.assertEqual("complete-static-coverage-not-measured", state["status"])
        self.assertFalse(state["claim_eligible"])

    def test_runtime_hook_smoke_requires_an_admitted_exact_site(self) -> None:
        stats = {
            "adaptive_force_track_all_admitted_allocations": 1,
            "adaptive_force_track_all_admitted_requested_bytes": 4096,
        }
        long_site = {
            "allocation_count": 1,
            "long_outcomes": 1,
            "long_requested_bytes": 4096,
            "maximum_completed_age_bytes": campaign.RUNTIME_LONG_AGE_BYTES,
        }
        admission = campaign.validate_runtime_hook_smoke_admission(
            stats, [long_site]
        )
        self.assertEqual(1, admission["admitted_allocations"])
        self.assertEqual(4096, admission["admitted_requested_bytes"])
        self.assertEqual(1, admission["runtime_exact_site_count"])
        self.assertEqual(1, admission["decisive_long_outcome_count"])
        for rejected_stats, rejected_sites in (
            (
                {
                    "adaptive_force_track_all_admitted_allocations": 0,
                    "adaptive_force_track_all_admitted_requested_bytes": 0,
                },
                [],
            ),
            (stats, []),
            (
                stats,
                [{**long_site, "allocation_count": 0}],
            ),
            (
                stats,
                [{**long_site, "long_outcomes": 0, "long_requested_bytes": 0}],
            ),
            (
                stats,
                [
                    {
                        **long_site,
                        "maximum_completed_age_bytes": (
                            campaign.RUNTIME_LONG_AGE_BYTES - 1
                        ),
                    }
                ],
            ),
        ):
            with self.subTest(stats=rejected_stats, sites=rejected_sites):
                with self.assertRaises(campaign.BuildBlocked):
                    campaign.validate_runtime_hook_smoke_admission(
                        rejected_stats, rejected_sites
                    )

    def test_fragmentation_parser_requires_complete_accounting(self) -> None:
        row = {field: 0 for field in campaign.REQUIRED_FRAGMENTATION_FIELDS}
        row.update(
            {
                "extent_bytes": 2 * campaign.MIB,
                "current_extents": 1,
                "peak_extents": 2,
                "current_ordinary_extents": 1,
                "peak_ordinary_extents": 2,
                "live_objects": 2,
                "live_slot_bytes": 4096,
                "retained_bytes": 2 * campaign.MIB,
                "retained_slack_bytes": 2 * campaign.MIB - 4096,
                "stranded_bytes": 2 * campaign.MIB - 4096,
            }
        )
        stderr = campaign.FRAGMENTATION_PREFIX + json.dumps(row)
        self.assertEqual(row, campaign.parse_fragmentation(stderr))
        row["stranded_bytes"] -= 1
        with self.assertRaises(campaign.CampaignContractError):
            campaign.parse_fragmentation(
                campaign.FRAGMENTATION_PREFIX + json.dumps(row)
            )

    def test_smaps_parser_and_pair_gate_validate_each_sample(self) -> None:
        ordinary = [
            campaign.parse_smaps_rollup(
                smaps_text(rss=1000, anon_hugepages=0), elapsed_seconds=1.0
            )
        ]
        thp = [
            campaign.parse_smaps_rollup(
                smaps_text(rss=2000, anon_hugepages=2048), elapsed_seconds=1.0
            )
        ]
        gate = campaign.validate_thp_pair_backing(ordinary, thp)
        self.assertTrue(gate["passed"])
        failed = campaign.validate_thp_pair_backing(ordinary, ordinary)
        self.assertFalse(failed["passed"])
        self.assertIn("no resident AnonHugePages", failed["reasons"][0])

    def test_thp_pair_gate_never_uses_max_to_mask_an_unbacked_sample(self) -> None:
        ordinary = [
            campaign.parse_smaps_rollup(
                smaps_text(rss=1000, anon_hugepages=0), elapsed_seconds=float(index)
            )
            for index in range(2)
        ]
        thp = [
            campaign.parse_smaps_rollup(
                smaps_text(rss=2000, anon_hugepages=value),
                elapsed_seconds=float(index),
            )
            for index, value in enumerate((2048, 0))
        ]
        gate = campaign.validate_thp_pair_backing(ordinary, thp)
        self.assertFalse(gate["passed"])
        self.assertEqual(1, gate["eligible_pair_count"])
        self.assertEqual(1, gate["excluded_pair_count"])
        self.assertTrue(gate["pair_eligibility"][0]["eligible"])
        self.assertFalse(gate["pair_eligibility"][1]["eligible"])

    def test_runtime_evidence_distinguishes_policy_and_backing(self) -> None:
        diagnostic = campaign.SCREENING_ARM
        stats = runtime_stats(diagnostic)
        campaign.validate_runtime_evidence(diagnostic, stats, [])
        stats["policy"] = 5
        with self.assertRaises(campaign.CampaignContractError):
            campaign.validate_runtime_evidence(diagnostic, stats, [])
        stats = runtime_stats(diagnostic)
        stats["thp_extent_mappings"] = 1
        with self.assertRaises(campaign.CampaignContractError):
            campaign.validate_runtime_evidence(diagnostic, stats, [])

    def test_redb_command_has_an_explicit_fixed_repeat_count(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            binary = root / "redb-driver"
            binary.write_text("binary", encoding="utf-8")
            command = campaign.redb_command(
                binary=binary, work_dir=root / "work", work_units=128
            )
        self.assertEqual("delete_reinsert", command[-3])
        self.assertEqual("128", command[-1])

    def test_generated_drivers_retain_state_and_accept_fixed_work(self) -> None:
        redb = campaign.amplified_redb_source()
        self.assertIn("for _ in 0..work_units", redb)
        self.assertIn("let output_digest = table_digest(&database);", redb)
        self.assertLess(redb.index("let database = new_database"), redb.index("for _ in"))
        polars = campaign.amplified_polars_source()
        self.assertIn("retained_frame: Option<&DataFrame>", polars)
        self.assertIn("for work_index in 0..work_units", polars)
        self.assertLess(polars.index("Some(input_frame()?)"), polars.index("for work_index"))

    def test_runtime_hook_smoke_exercises_policy8_force_track_hooks(self) -> None:
        source = campaign.runtime_hook_smoke_source()
        self.assertIn("AdaptiveRuntimeOrdinary", source)
        self.assertIn("lifetime_hugepage_adaptive_site_force_track_all_enable", source)
        self.assertIn(campaign.RUNTIME_ARM_ENV, source)
        self.assertIn("UNIALLOC_LIFETIME_SMOKE=ok", source)

    def test_stage_a_cli_supports_space_saving_measured_subset(self) -> None:
        args = campaign.parse_args(
            [
                "--stage-a",
                "--dry-run",
                "--targets",
                "polars,actix_web",
                "--build-groups",
                "compiler-prior",
            ]
        )
        self.assertEqual(("polars", "actix_web"), args.targets)
        self.assertEqual(("compiler-prior",), args.build_groups)

    def test_manifest_marks_stage_a_as_cross_clock_opportunity_only(self) -> None:
        screening = campaign.build_campaign_manifest()["stages"]["screening"]
        self.assertEqual(
            "process_wide_requested_generation_bytes",
            screening["stage_a_pressure_basis"],
        )
        self.assertEqual(
            "eligible_exact_site_payload_capacity",
            screening["production_pressure_basis"],
        )
        self.assertFalse(screening["cross_clock_accuracy_claim"])

    def test_stage_a_classifier_summary_refuses_production_accuracy(self) -> None:
        stats = runtime_stats(campaign.SCREENING_ARM)
        stats.update(
            {
                "static_hint_tp": 7,
                "static_hint_tn": 2,
                "static_hint_fp": 1,
                "static_hint_fn": 0,
                "static_hint_abstained": 3,
                "adaptive_promotions": 1,
                "adaptive_demotions": 0,
                "adaptive_prior_corrections": 1,
                "predictor_tp": 6,
                "predictor_tn": 3,
                "predictor_fp": 1,
                "predictor_fn": 0,
            }
        )
        summary = campaign.runtime_classification_summary(stats)
        self.assertEqual(0.9, summary["stage_a_pressure_clock_accuracy"])
        self.assertIsNone(summary["production_accuracy"])
        self.assertFalse(summary["production_accuracy_claim_eligible"])
        self.assertFalse(summary["cross_clock_accuracy_claim"])
        self.assertEqual(0.9, summary["runtime_predictor_accuracy"])

        production = campaign.runtime_classification_summary(
            stats, evidence_stage="production"
        )
        self.assertIsNone(production["stage_a_pressure_clock_accuracy"])
        self.assertEqual(0.9, production["production_accuracy"])
        self.assertEqual("production", production["accuracy_evidence_stage"])
        self.assertFalse(production["production_accuracy_claim_eligible"])

    def test_rustpython_runtime_context_uses_retained_assets_and_libpython(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            runtime_cwd = Path(directory)
            build = {
                "runtime_cwd": str(runtime_cwd),
                "runtime_dependency": {"library_directory": "/verified/libpython"},
            }
            cwd, environment = campaign.stage_a_runtime_context(
                "rustpython", build, campaign.SCREENING_ARM
            )
            self.assertEqual(runtime_cwd, cwd)
            self.assertEqual(
                "/verified/libpython",
                environment["LD_LIBRARY_PATH"].split(":", 1)[0],
            )
        with self.assertRaises(campaign.CampaignContractError):
            campaign.stage_a_runtime_context(
                "rustpython", {}, campaign.SCREENING_ARM
            )

    def test_swc_build_preserves_verified_compatibility_flags(self) -> None:
        flags = campaign.stage_a_rustflags("swc", "compiler-prior")
        self.assertIn("-Zshare-generics=y", flags)
        self.assertIn("-C target-feature=+sse2", flags)
        self.assertIn("-Wl,-z,nodelete", flags)
        self.assertNotIn(
            "share-generics", campaign.stage_a_rustflags("polars", "compiler-prior")
        )

    def test_compiler_build_contract_retains_release_symbols(self) -> None:
        with (
            tempfile.TemporaryDirectory() as directory,
            mock.patch.object(
                campaign.matrix, "rustc_sysroot", return_value=Path("/sysroot")
            ),
            mock.patch.object(
                campaign.matrix,
                "typeiso_environment",
                side_effect=lambda environment, **_kwargs: dict(environment),
            ),
        ):
            root = Path(directory)
            environment = campaign._compiler_build_environment(
                "compiler-prior",
                wrapper=root / "wrapper",
                audit_dir=root / "audits",
                pass_log_dir=root / "logs",
                target_dir=root / "target",
                target_crates=("oxipng",),
                temporary_dir=root / "tmp",
            )
            environment["RUSTFLAGS"] = campaign.stage_a_rustflags(
                "oxipng", "compiler-prior"
            )
            provenance = campaign.compiler_build_environment_provenance(environment)
        self.assertEqual("false", environment[campaign.RELEASE_STRIP_ENV])
        self.assertEqual("false", provenance["cargo_profile_release_strip"])
        self.assertTrue(provenance["release_symbols_retained"])
        self.assertEqual("1", provenance["automatic_rust_lifetime_prior"])
        with self.assertRaises(campaign.CampaignContractError):
            campaign.compiler_build_environment_provenance(
                {campaign.RELEASE_STRIP_ENV: "true"}
            )

    def test_target_snapshot_dependency_rewrites_are_isolated_and_provenanced(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            baseline_root = root / "frozen-unialloc"
            (baseline_root / "unialloc").mkdir(parents=True)
            pass_source = (
                baseline_root
                / "tools"
                / "unialloc-rustc-pass"
                / "unialloc-rustc-mir-rewrite-dry-run.rs"
            )
            pass_source.parent.mkdir(parents=True)
            pass_source.write_text("fn main() {}\n", encoding="utf-8")
            (baseline_root / "Cargo.toml").write_text(
                '[workspace]\nmembers=["unialloc"]\n', encoding="utf-8"
            )
            baseline_lock = baseline_root / "Cargo.lock"
            baseline_lock.write_text(
                (
                    "version = 4\n\n"
                    '[[package]]\nname = "libc"\nversion = "0.2.183"\n\n'
                    '[[package]]\nname = "num_cpus"\nversion = "1.13.0"\n'
                ),
                encoding="utf-8",
            )
            baseline_manifest = baseline_root / "unialloc" / "Cargo.toml"
            baseline_manifest.write_text(
                (
                    '[package]\nname="unialloc"\nversion="0.0.0"\n'
                    '\n[dependencies]\nlibc = { version = "=0.2.183", '
                    "default-features = false }\n"
                    '\n[build-dependencies]\nnum_cpus = "=1.13.0"\n'
                    'libc = "=0.2.183"\n'
                ),
                encoding="utf-8",
            )
            checkout = root / "swc"
            checkout.mkdir()
            (checkout / "Cargo.lock").write_text(
                (
                    "version = 4\n\n"
                    '[[package]]\nname = "num_cpus"\nversion = "1.16.0"\n'
                ),
                encoding="utf-8",
            )
            baseline = {
                "path": str(baseline_root),
                "pass_source": str(pass_source),
                "campaign_snapshot_sha256": "a" * 64,
                "unialloc_implementation_sha256": "b" * 64,
            }
            production_paths = (
                campaign.ROOT / "Cargo.toml",
                campaign.ROOT / "Cargo.lock",
                campaign.ROOT / "unialloc" / "Cargo.toml",
            )
            production_hashes = {
                path: campaign.sha256_file(path) for path in production_paths
            }
            baseline_manifest_sha = campaign.sha256_file(baseline_manifest)
            baseline_lock_sha = campaign.sha256_file(baseline_lock)

            def execute(command, *, cwd, env, timeout):
                command = list(map(str, command))
                if "update" in command:
                    package = command[command.index("-p") + 1]
                    version = command[command.index("--precise") + 1]
                    lock = Path(cwd) / "Cargo.lock"
                    text_value = lock.read_text(encoding="utf-8")
                    pattern = (
                        rf'(name = "{re.escape(package)}"\nversion = ")'
                        r'[^\"]+(\")'
                    )
                    text_value, count = re.subn(
                        pattern, rf"\g<1>{version}\g<2>", text_value
                    )
                    self.assertEqual(1, count)
                    lock.write_text(text_value, encoding="utf-8")
                return {
                    "command": command,
                    "exit_code": 0,
                    "timed_out": False,
                    "stdout": b"",
                    "stderr": b"",
                }

            with mock.patch.object(campaign.matrix, "execute", side_effect=execute):
                compatible = campaign.prepare_target_compatible_allocator_snapshot(
                    "swc",
                    checkout=checkout,
                    raw_dir=root / "raw",
                    baseline=baseline,
                    timeout=60,
                )
            compatible_root = Path(compatible["path"])
            provenance = compatible["dependency_compatibility"]
            self.assertNotEqual(baseline_root, compatible_root)
            self.assertTrue(compatible_root.is_relative_to(root / "raw"))
            self.assertIn(
                'num_cpus = "=1.16.0"',
                (compatible_root / "unialloc" / "Cargo.toml").read_text(
                    encoding="utf-8"
                ),
            )
            self.assertEqual(
                "1.16.0",
                campaign._locked_package_version(
                    compatible_root / "Cargo.lock", "num_cpus"
                ),
            )
            self.assertEqual("rewritten-target-snapshot", provenance["status"])
            self.assertEqual(1, len(provenance["rewrites"]))
            self.assertEqual(
                "=1.16.0", provenance["rewrites"][0]["new_requirement"]
            )
            self.assertEqual(
                campaign.sha256_file(compatible_root / "unialloc" / "Cargo.toml"),
                provenance["post_rewrite_manifest_sha256"],
            )
            self.assertEqual(
                campaign.sha256_file(compatible_root / "Cargo.lock"),
                provenance["post_rewrite_lock_sha256"],
            )
            self.assertEqual(baseline_manifest_sha, campaign.sha256_file(baseline_manifest))
            self.assertEqual(baseline_lock_sha, campaign.sha256_file(baseline_lock))
            self.assertEqual(
                production_hashes,
                {path: campaign.sha256_file(path) for path in production_paths},
            )
            with mock.patch.object(
                campaign.matrix,
                "execute",
                side_effect=AssertionError("cached snapshot executed Cargo"),
            ):
                cached = campaign.prepare_target_compatible_allocator_snapshot(
                    "swc",
                    checkout=checkout,
                    raw_dir=root / "raw",
                    baseline=baseline,
                    timeout=60,
                )
            self.assertEqual(
                provenance["provenance_digest"],
                cached["dependency_compatibility"]["provenance_digest"],
            )

    def test_rustpython_snapshot_rewrites_target_locked_libc_and_num_cpus(
        self,
    ) -> None:
        rules = campaign.TARGET_SNAPSHOT_DEPENDENCY_REWRITES["rustpython"]
        manifest = (
            '[dependencies]\nlibc = { version = "=0.2.183", '
            "default-features = false }\n"
            '\n[build-dependencies]\nlibc = "=0.2.183"\n'
            'num_cpus = "=1.13.0"\n'
        )
        rewritten, records = campaign._rewrite_snapshot_dependency_requirements(
            manifest, rules, {"libc": "0.2.186", "num_cpus": "1.17.0"}
        )
        self.assertEqual(3, len(records))
        self.assertEqual(2, rewritten.count('"=0.2.186"'))
        self.assertEqual(1, rewritten.count('"=1.17.0"'))
        self.assertNotIn('"=0.2.183"', rewritten)
        self.assertNotIn('"=1.13.0"', rewritten)
        with self.assertRaises(campaign.CampaignContractError):
            campaign._rewrite_snapshot_dependency_requirements(
                '[dependencies]\nlibc = "^0.2"\n',
                rules,
                {"libc": "0.2.186", "num_cpus": "1.17.0"},
            )

    def test_runtime_instrumentation_preserves_inner_docs_and_attributes(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "router.rs"
            original = (
                "//! Actix router benchmark.\n"
                "#![allow(clippy::needless_borrow)]\n\n"
                "fn benchmark_router() {}\n"
            )
            source.write_text(original, encoding="utf-8")
            campaign._append_instrumentation(source)
            rewritten = source.read_text(encoding="utf-8")
            self.assertTrue(rewritten.startswith(original.rstrip() + "\n\n"))
            self.assertLess(
                rewritten.index("#![allow(clippy::needless_borrow)]"),
                rewritten.index("UNIALLOC_LIFETIME_EXPERIMENT_INITIALIZER"),
            )
            self.assertLess(
                rewritten.index("fn benchmark_router()"),
                rewritten.index("UNIALLOC_LIFETIME_EXPERIMENT_INITIALIZER"),
            )
            with self.assertRaises(campaign.CampaignContractError):
                campaign._append_instrumentation(source)

    def test_post_injection_manifests_and_lock_are_retained_by_hash(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            worktree = root / "work"
            manifest = worktree / "Cargo.toml"
            child = worktree / "crate" / "Cargo.toml"
            child.parent.mkdir(parents=True)
            manifest.write_text("[workspace]\n", encoding="utf-8")
            child.write_text("[package]\nname='x'\n", encoding="utf-8")
            (worktree / "Cargo.lock").write_text("version = 4\n", encoding="utf-8")
            record = campaign.retain_post_injection_build_inputs(
                {
                    "manifest": "Cargo.toml",
                    "patched_manifests": ["crate/Cargo.toml"],
                },
                worktree=worktree,
                build_dir=root / "build",
            )
            self.assertEqual(2, record["manifest_count"])
            self.assertEqual(
                ["Cargo.toml", "crate/Cargo.toml"],
                [row["relative_path"] for row in record["manifests"]],
            )
            self.assertTrue(Path(record["retained_cargo_lock"]).is_file())
            self.assertEqual(
                record["cargo_lock_sha256"], record["retained_cargo_lock_sha256"]
            )
            self.assertTrue(
                campaign.validate_retained_post_injection_build_inputs(record)
            )
            Path(record["retained_cargo_lock"]).write_text(
                "version = 3\n", encoding="utf-8"
            )
            self.assertFalse(
                campaign.validate_retained_post_injection_build_inputs(record)
            )

    def test_nested_injection_root_normalizes_relative_manifest_paths(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            worktree = Path(directory) / "build-work" / "redb"
            nested = worktree / "redb-source"
            nested.mkdir(parents=True)
            (nested / "Cargo.toml").write_text("[package]\n", encoding="utf-8")
            (worktree / "Cargo.toml").write_text("[workspace]\n", encoding="utf-8")
            (worktree / "Cargo.lock").write_text("version = 4\n", encoding="utf-8")
            with mock.patch.object(
                campaign.matrix,
                "inject_unialloc_workspace_crates",
                return_value=[Path("Cargo.toml")],
            ):
                patched = campaign._inject_workspace_dependencies(
                    nested, ("redb",), "unialloc = {}"
                )
            self.assertEqual([str((nested / "Cargo.toml").resolve())], patched)
            record = campaign.retain_post_injection_build_inputs(
                {
                    "manifest": "Cargo.toml",
                    "patched_manifests": patched,
                },
                worktree=worktree,
                build_dir=Path(directory) / "build",
            )
            self.assertEqual(
                ["Cargo.toml", "redb-source/Cargo.toml"],
                [row["relative_path"] for row in record["manifests"]],
            )

    def test_generated_oxipng_and_redb_trees_pass_real_cargo_metadata(self) -> None:
        evaluation_root = campaign.ROOT / "evaluation"
        with tempfile.TemporaryDirectory(
            dir=evaluation_root, prefix=".stage-a-metadata-"
        ) as directory:
            root = Path(directory)
            oxipng_checkout = root / "checkout-oxipng"
            (oxipng_checkout / "src").mkdir(parents=True)
            (oxipng_checkout / "Cargo.toml").write_text(
                '[package]\nname="oxipng"\nversion="0.0.0"\nedition="2021"\n',
                encoding="utf-8",
            )
            (oxipng_checkout / "src" / "main.rs").write_text(
                "fn main() {}\n", encoding="utf-8"
            )
            redb_checkout = root / "checkout-redb"
            (redb_checkout / "src").mkdir(parents=True)
            (redb_checkout / "Cargo.toml").write_text(
                (
                    '[package]\nname="redb"\nversion="0.0.0"\nedition="2021"\n'
                    '\n[workspace]\nmembers=["."]\n'
                ),
                encoding="utf-8",
            )
            (redb_checkout / "src" / "lib.rs").write_text(
                "pub fn marker() {}\n", encoding="utf-8"
            )
            with (
                mock.patch.object(
                    campaign, "_inject_workspace_dependencies", return_value=[]
                ),
                mock.patch.object(
                    campaign, "_stage_a_dependency", return_value="unialloc = {}"
                ),
                mock.patch.object(
                    campaign.matrix, "find_cached_spin", return_value=root / "spin"
                ),
                mock.patch.object(campaign.matrix, "add_spin_patch"),
            ):
                oxipng = campaign.prepare_stage_a_source(
                    "oxipng",
                    "compiler-prior",
                    checkout=oxipng_checkout,
                    raw_dir=root / "raw",
                    snapshot={"path": str(root / "snapshot")},
                )
                redb = campaign.prepare_stage_a_source(
                    "redb",
                    "compiler-prior",
                    checkout=redb_checkout,
                    raw_dir=root / "raw",
                    snapshot={"path": str(root / "snapshot")},
                )
            for prepared in (oxipng, redb):
                metadata = subprocess.run(
                    [
                        "cargo",
                        f"+{campaign.TOOLCHAIN}",
                        "metadata",
                        "--format-version",
                        "1",
                        "--no-deps",
                        "--manifest-path",
                        prepared["manifest"],
                    ],
                    cwd=Path(prepared["worktree"]),
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True,
                    check=False,
                )
                self.assertEqual(0, metadata.returncode, metadata.stderr)
            oxipng_manifest = Path(oxipng["manifest"]).read_text(encoding="utf-8")
            self.assertIn("[workspace]", oxipng_manifest)
            redb_metadata = json.loads(
                subprocess.run(
                    [
                        "cargo",
                        f"+{campaign.TOOLCHAIN}",
                        "metadata",
                        "--format-version",
                        "1",
                        "--no-deps",
                        "--manifest-path",
                        redb["manifest"],
                    ],
                    cwd=Path(redb["worktree"]),
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True,
                    check=True,
                ).stdout
            )
            target_names = {
                target["name"]
                for package in redb_metadata["packages"]
                for target in package["targets"]
            }
            self.assertIn("unialloc-redb-actix-runner", target_names)
            self.assertFalse((Path(redb["worktree"]) / "redb-source").exists())

    def test_polars_generated_manifest_is_in_post_injection_manifest_set(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            checkout = root / "checkout"
            checkout.mkdir()
            (checkout / "Cargo.toml").write_text("[workspace]\n", encoding="utf-8")
            with (
                mock.patch.object(
                    campaign, "_inject_workspace_dependencies", return_value=[]
                ),
                mock.patch.object(
                    campaign, "_stage_a_dependency", return_value="unialloc = {}"
                ),
                mock.patch.object(
                    campaign.matrix, "find_cached_spin", return_value=root / "spin"
                ),
                mock.patch.object(campaign.matrix, "add_spin_patch"),
            ):
                prepared = campaign.prepare_stage_a_source(
                    "polars",
                    "compiler-prior",
                    checkout=checkout,
                    raw_dir=root / "raw",
                    snapshot={"path": str(root / "snapshot")},
                )
            generated = (
                root
                / "raw"
                / "build-work"
                / "polars"
                / "compiler-prior"
                / "crates"
                / "polars-unialloc-primary"
                / "Cargo.toml"
            ).resolve()
            self.assertIn(str(generated), prepared["patched_manifests"])

    def test_evaluator_provenance_hashes_runner_and_helpers(self) -> None:
        provenance = campaign.campaign_evaluator_provenance()
        self.assertEqual(64, len(provenance["digest"]))
        self.assertEqual(
            campaign.sha256_file(Path(campaign.__file__)),
            provenance["files"]["stage_a_runner"]["sha256"],
        )
        self.assertIn("realworld_matrix_helper", provenance["files"])

    def test_single_process_guard_observes_descendants(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            result = campaign.execute_monitored_process(
                [
                    sys.executable,
                    "-c",
                    (
                        "import subprocess,sys,time; "
                        "child=subprocess.Popen([sys.executable,'-c','import time;time.sleep(.25)']); "
                        "time.sleep(.3); child.wait()"
                    ),
                ],
                cwd=root,
                env=dict(os.environ),
                artifact_dir=root / "run",
                timeout=2,
                sample_interval=0.01,
            )
            self.assertFalse(result["single_process_guard"]["passed"])
            self.assertGreaterEqual(
                result["single_process_guard"]["observed_descendant_count"], 1
            )

    def test_preflight_retains_other_sources_when_one_checkout_fails(self) -> None:
        def source(target_id, _root):
            if target_id == "redb":
                raise campaign.CampaignContractError("redb unavailable")
            return {
                "target_id": target_id,
                "checkout": "/tmp/polars",
                "source_commit": campaign.TARGETS[target_id].source_commit,
            }

        with tempfile.TemporaryDirectory() as directory, mock.patch.object(
            campaign, "ensure_pinned_checkout", side_effect=source
        ):
            plan = campaign.stage_a_preflight(
                targets=("redb", "polars"),
                raw_dir=Path(directory),
                checkout_root=Path(directory) / "checkouts",
                work_units=campaign.STAGE_A_FIXED_WORK_UNITS,
                measurement_seconds=30.0,
                build_groups=("compiler-prior",),
                jobs=1,
            )
        self.assertFalse(plan["success"])
        self.assertIn("redb", plan["source_failures"])
        self.assertIn("polars", plan["sources"])
        self.assertEqual(
            {campaign.RELEASE_STRIP_ENV: "false"},
            plan["release_symbol_retention_environment"],
        )
        self.assertTrue(plan["activation_proof_requires_retained_symbols"])
        self.assertFalse(plan["run_commands"]["redb"]["commands_generated"])
        self.assertIn("compiler-prior", plan["run_commands"]["polars"])

    def test_preflight_does_not_plan_oxipng_run_after_source_failure(self) -> None:
        def source(target_id, _root):
            if target_id == "oxipng":
                raise campaign.CampaignContractError("oxipng unavailable")
            return {
                "target_id": target_id,
                "checkout": "/tmp/polars",
                "source_commit": campaign.TARGETS[target_id].source_commit,
            }

        with tempfile.TemporaryDirectory() as directory, mock.patch.object(
            campaign, "ensure_pinned_checkout", side_effect=source
        ):
            plan = campaign.stage_a_preflight(
                targets=("oxipng", "polars"),
                raw_dir=Path(directory),
                checkout_root=Path(directory) / "checkouts",
                work_units=campaign.STAGE_A_FIXED_WORK_UNITS,
                measurement_seconds=30.0,
                build_groups=("compiler-prior",),
                jobs=1,
            )
        self.assertFalse(plan["success"])
        self.assertFalse(
            plan["run_commands"]["oxipng"]["commands_generated"]
        )
        self.assertIn("compiler-prior", plan["run_commands"]["polars"])

    def test_campaign_continues_after_build_failure_and_retries_duration_once(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            raw = Path(directory)
            preflight = {
                "sources": {
                    target: {
                        "target_id": target,
                        "checkout": f"/tmp/{target}",
                        "source_commit": campaign.TARGETS[target].source_commit,
                    }
                    for target in ("redb", "polars")
                },
                "source_failures": {},
                "oxipng_input": None,
            }

            def build(target_id, *_args, **_kwargs):
                if target_id == "redb":
                    raise campaign.BuildBlocked("synthetic redb failure")
                return {"target_id": target_id}

            run_count = 0

            def run(target_id, build_group, **kwargs):
                nonlocal run_count
                self.assertEqual("polars", target_id)
                self.assertEqual("compiler-prior", build_group)
                run_count += 1
                return {
                    "wall_seconds": 5.0 if run_count == 1 else 30.0,
                    "work_units": kwargs["work_units"],
                    "procfs": {"peak_rss_kib": 1},
                    "runtime_site_summary": {
                        "long_requested_bytes": 0,
                        "confirmed_short_requested_bytes": 0,
                    },
                    "compiler_runtime_exact_join": {
                        "runtime_join_rewrite_success": True,
                        "static_coverage_claim_eligible": True,
                        "status": "complete-static-coverage",
                    },
                }

            snapshot = {
                "unialloc_implementation_sha256": "0" * 64,
                "allocator_revision": None,
            }
            with (
                mock.patch.object(
                    campaign, "stage_a_preflight", return_value=preflight
                ),
                mock.patch.object(
                    campaign.psr, "snapshot_allocator", return_value=snapshot
                ),
                mock.patch.object(
                    campaign.psr,
                    "build_mir_driver",
                    return_value=raw / "wrapper",
                ),
                mock.patch.object(
                    campaign,
                    "compile_run_runtime_hook_smoke",
                    return_value={"success": True},
                ),
                mock.patch.object(
                    campaign, "build_stage_a_binary", side_effect=build
                ),
                mock.patch.object(campaign, "run_stage_a_sample", side_effect=run),
            ):
                result = campaign.run_stage_a_campaign(
                    targets=("redb", "polars"),
                    raw_dir=raw,
                    checkout_root=raw / "checkouts",
                    work_units={"redb": 128, "polars": 88},
                    measurement_seconds=30.0,
                    jobs=1,
                    build_timeout=60,
                    run_timeout=60,
                    sample_interval=0.01,
                    reuse=False,
                    allocator_revision=None,
                    build_groups=("compiler-prior",),
                    calibrate_fixed_work=False,
                )
            self.assertEqual(
                "blocked_by_real_build_failure", result["targets"]["redb"]["status"]
            )
            self.assertEqual("complete", result["targets"]["polars"]["status"])
            self.assertEqual(2, run_count)
            retry = result["targets"]["polars"]["fixed_work_duration_retry"]
            self.assertTrue(retry["performed"])
            self.assertEqual(88, retry["first_work_units"])
            self.assertEqual(704, retry["retry_work_units"])

    def test_all_six_have_executable_stage_a_command_shapes(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            binary = root / "adapter"
            binary.write_text("binary", encoding="utf-8")
            for target_id in ("swc", "rustpython", "actix_web"):
                with self.subTest(target=target_id):
                    command = campaign.screening_command(
                        target_id,
                        binary=binary,
                        work_dir=root / target_id,
                        measurement_seconds=30.0,
                    )
                    self.assertIn("--measurement-time", command)
                    self.assertEqual(
                        "30.000", command[command.index("--measurement-time") + 1]
                    )
            polars = campaign.screening_command(
                "polars", binary=binary, work_dir=root / "polars", work_units=88
            )
            self.assertEqual("filter_retain", polars[-2])
            self.assertEqual("88", polars[-1])

    def test_oxipng_command_materializes_exact_unique_inputs(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            binary = root / "oxipng"
            binary.write_text("binary", encoding="utf-8")
            source = root / "issue-141.png"
            source.write_bytes(b"pinned-test-input")
            original = campaign.OXIPNG_INPUT_SHA256
            campaign.OXIPNG_INPUT_SHA256 = hashlib.sha256(source.read_bytes()).hexdigest()
            try:
                command = campaign.planned_fixed_work_command(
                    "oxipng",
                    binary=binary,
                    work_dir=root / "run",
                    work_units=8,
                    input_path=source,
                )
            finally:
                campaign.OXIPNG_INPUT_SHA256 = original
            inputs = sorted((root / "run" / "inputs").glob("*.png"))
            self.assertEqual(8, len(inputs))
            self.assertEqual(8, len(set(path.name for path in inputs)))
            self.assertEqual("--dir", command[-10])

    def test_blocked_adapters_fail_closed_and_retain_planned_cli(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            binary = root / "adapter"
            binary.write_text("binary", encoding="utf-8")
            with self.assertRaises(campaign.AdapterBlocked):
                campaign.planned_fixed_work_command(
                    "swc", binary=binary, work_dir=root, work_units=10
                )
            command = campaign.planned_fixed_work_command(
                "swc",
                binary=binary,
                work_dir=root,
                work_units=10,
                allow_blocked_adapter=True,
            )
            self.assertIn("--unialloc-fixed-work-units", command)
            self.assertIn("large_fixer", command)


if __name__ == "__main__":
    unittest.main()
