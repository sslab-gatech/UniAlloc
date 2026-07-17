#!/usr/bin/env python3
"""Unit tests for the Oxipng real-app UniAlloc repro smoke."""

from __future__ import annotations

import importlib.util
import json
import pathlib
import subprocess
import sys
import tempfile
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "evaluation" / "scripts" / "oxipng_realapp_repro_smoke.py"

spec = importlib.util.spec_from_file_location("oxipng_realapp_repro_smoke", SCRIPT)
assert spec is not None and spec.loader is not None
smoke = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = smoke
spec.loader.exec_module(smoke)


def actual_scope_row(
    *,
    type_id: int,
    callsite: int,
    semantic_object_type: str,
    mir_function: str | None = None,
) -> dict:
    return {
        "mir_function": mir_function or f"fixture::{semantic_object_type}",
        "semantic_object_type": semantic_object_type,
        "type_id": type_id,
        "module_id": 77,
        "callsite": callsite,
        "flags": smoke.TYPE_ISOLATED,
        "lowering_kind": "semantic_scope_enter_exit_rewrite",
        "rewrite_status": smoke.ACTUAL_SEMANTIC_SCOPE_STATUS,
        "source_span": f"fixture.rs:{callsite}:1",
    }


def actual_drop_row(
    *, type_id: int, callsite: int, semantic_object_type: str
) -> dict:
    return {
        "mir_function": smoke.ADDRESS_ORACLE_FUNCTION,
        "semantic_object_type": semantic_object_type,
        "type_id": type_id,
        "module_id": 77,
        "callsite": callsite,
        "flags": smoke.TYPE_ISOLATED,
        "lowering_kind": "semantic_scope_drop_rewrite",
        "rewrite_status": smoke.ACTUAL_SEMANTIC_DROP_STATUS,
        "source_span": f"fixture.rs:{callsite}:1",
    }


def ownership_transfer_row(*, applied: bool) -> dict:
    return {
        "mir_function": "fixture::box_into_vec",
        "callee": (
            "std::slice::<impl [u8]>::into_vec::<std::alloc::Global>"
        ),
        "destination_type": "std::vec::Vec<u8>",
        "argument_types": ["std::boxed::Box<[u8]>"],
        "semantic_object_type": "std::vec::Vec<u8>",
        "type_id": 505,
        "module_id": 77,
        "callsite": 7007 if applied else 7008,
        "flags": smoke.TYPE_ISOLATED,
        "lowering_kind": smoke.SEMANTIC_OWNERSHIP_TRANSFER_KIND,
        "rewrite_status": (
            smoke.ACTUAL_SEMANTIC_OWNERSHIP_TRANSFER_STATUS
            if applied
            else "semantic_ownership_transfer_rewrite_skipped_fixture"
        ),
        "replacement_symbol": "__unialloc_semantic_box_slice_into_vec",
        "replacement_resolution_status": (
            "resolved_unialloc_semantic_box_slice_into_vec"
            if applied
            else "fixture_not_resolved"
        ),
        "metadata_pairing_contract": "pointer_preserving_owner_identity_rebind",
        "source_span": "fixture.rs:70:1",
    }


def runtime_type_row(
    *,
    type_id: int,
    callsite: int,
    allocations: int = 3,
    deallocations: int = 2,
    cache_hits: int = 1,
    cache_inserts: int = 2,
) -> dict:
    return {
        "type_id": type_id,
        "module_id": 77,
        "callsite": callsite,
        "allocations": allocations,
        "deallocations": deallocations,
        "cache_hits": cache_hits,
        "cache_inserts": cache_inserts,
        "cache_bypasses": 0,
        "observed_alloc_size": 64,
        "observed_alloc_align": 8,
        "observed_dealloc_size": 64,
        "observed_dealloc_align": 8,
        "policy_flags_seen": smoke.TYPE_ISOLATED,
    }


def fail_closed_row() -> dict:
    return {
        "mir_function": "fixture::ambiguous_clone",
        "lowering_kind": "semantic_scope_unsolved_heap_object_candidate",
        "rewrite_status": "semantic_scope_rewrite_skipped_ambiguous_heap_object_type",
        "replacement_resolution_status": (
            "rustc_middle_multiple_heap_object_types_not_lowered"
        ),
        "destination_type": "Result<Vec<u8>, String>",
        "semantic_object_type": "multiple_heap_owners(Vec<u8>,String)",
        "source_span": "fixture.rs:30:1",
    }


def fail_closed_callback_capable_row() -> dict:
    return {
        "mir_function": "fixture::hashmap_reserve",
        "lowering_kind": "semantic_scope_callback_capable_receiver_skipped",
        "rewrite_status": "semantic_scope_rewrite_skipped_callback_capable_receiver",
        "replacement_resolution_status": (
            "exact_receiver_call_callback_capable_not_lowered"
        ),
        "destination_type": "()",
        "semantic_object_type": "std::collections::HashMap<Key,Value>",
        "source_span": "fixture.rs:30:2",
    }


def fail_closed_multi_owner_drop_row() -> dict:
    return {
        "mir_function": "fixture::multi_owner_drop",
        "lowering_kind": "semantic_scope_drop_multiple_heap_owners_skipped",
        "rewrite_status": "semantic_scope_drop_rewrite_skipped_multiple_heap_owners",
        "replacement_resolution_status": (
            "rustc_middle_drop_multiple_heap_owners_not_lowered"
        ),
        "destination_type": "(Vec<u8>, String)",
        "semantic_object_type": "multiple_heap_owners(Vec<u8>,String)",
        "source_span": "fixture.rs:31:1",
    }


def ownership_transfer_runtime(
    *,
    before: tuple[int, int, int] = (0, 0, 0),
    delta: tuple[int, int, int] = (0, 0, 0),
) -> dict:
    after = tuple(left + right for left, right in zip(before, delta))
    dynamic = delta[0] != 0
    return {
        "source": smoke.OWNERSHIP_TRANSFER_RUNTIME_SOURCE,
        "before": dict(zip(("attempted", "applied", "rejected"), before)),
        "after": dict(zip(("attempted", "applied", "rejected"), after)),
        "delta": dict(zip(("attempted", "applied", "rejected"), delta)),
        "accounting_complete": delta[0] == delta[1] + delta[2],
        "dynamic_execution_observed": dynamic,
        "execution_status": (
            "dynamic_execution_observed"
            if dynamic
            else "zero_dynamic_execution_observed"
        ),
    }


def valid_contract_stats() -> dict:
    rows = [
        runtime_type_row(
            type_id=101,
            callsite=1001,
            allocations=2,
            deallocations=2,
            cache_hits=1,
        ),
        runtime_type_row(
            type_id=202,
            callsite=2002,
            allocations=1,
            deallocations=1,
            cache_hits=0,
            cache_inserts=1,
        ),
        runtime_type_row(
            type_id=101,
            callsite=3003,
            allocations=0,
            deallocations=2,
            cache_hits=0,
            cache_inserts=2,
        ),
        runtime_type_row(
            type_id=202,
            callsite=4004,
            allocations=0,
            deallocations=1,
            cache_hits=0,
            cache_inserts=1,
        ),
        runtime_type_row(type_id=303, callsite=5005),
        runtime_type_row(type_id=404, callsite=6006),
    ]
    return {
        "typed_allocations": 9,
        "fallback_allocations": 0,
        "raw_alloc_no_metadata": 0,
        "raw_dealloc_no_metadata": 0,
        "raw_realloc_no_metadata": 0,
        "recovery_identity_matches": 0,
        "recovery_identity_mismatches": 0,
        "last_mismatch_requested_type_id": 0,
        "last_mismatch_recorded_type_id": 0,
        "last_mismatch_requested_module_id": 0,
        "last_mismatch_recorded_module_id": 0,
        "last_mismatch_requested_callsite": 0,
        "last_mismatch_recorded_callsite": 0,
        "type_isolation_corrupt_slots": 0,
        "type_stats_rows": len(rows),
        "type_stats_dropped_events": 0,
        "type_rows": rows,
        "semantic_ownership_transfer": ownership_transfer_runtime(),
        "address_oracle": {
            "source": smoke.ADDRESS_ORACLE_SOURCE,
            "producer_first_address": 0x1000,
            "wrong_type_address": 0x2000,
            "producer_recovery_address": 0x1000,
            "element_size": 32,
            "element_align": 8,
            "capacity": 2,
            "allocation_size": 64,
            "wrong_type_not_reused": True,
            "same_type_reused": True,
            "recovery_identity_mismatches_before": 0,
            "recovery_identity_mismatches_after": 0,
            "corrupt_slots_after": 0,
        },
    }


def valid_contract_audits() -> list[dict]:
    return [
        {
            "file": "applied.json",
            "replacement_resolution_status": (
                "resolved_unialloc_allocator_metadata_abi"
            ),
            **{key: True for key in smoke.REQUIRED_AUDIT_FLAGS},
            "actual_type_scope_rows": [
                actual_scope_row(
                    type_id=101,
                    callsite=1001,
                    semantic_object_type=(
                        "std::vec::Vec<fixture::UniAllocAddressOracleProducer>"
                    ),
                    mir_function=smoke.ADDRESS_ORACLE_PRODUCER_HELPER,
                ),
                actual_scope_row(
                    type_id=202,
                    callsite=2002,
                    semantic_object_type=(
                        "std::vec::Vec<fixture::UniAllocAddressOracleWrongType>"
                    ),
                    mir_function=smoke.ADDRESS_ORACLE_WRONG_HELPER,
                ),
                actual_scope_row(
                    type_id=303,
                    callsite=5005,
                    semantic_object_type="Vec<NaturalProducer>",
                ),
                actual_scope_row(
                    type_id=404,
                    callsite=6006,
                    semantic_object_type="Vec<NaturalConsumer>",
                ),
            ],
            "actual_drop_scope_rows": [
                actual_drop_row(
                    type_id=101,
                    callsite=3003,
                    semantic_object_type=(
                        "std::vec::Vec<fixture::UniAllocAddressOracleProducer>"
                    ),
                ),
                actual_drop_row(
                    type_id=202,
                    callsite=4004,
                    semantic_object_type=(
                        "std::vec::Vec<fixture::UniAllocAddressOracleWrongType>"
                    ),
                ),
                actual_drop_row(
                    type_id=101,
                    callsite=3999,
                    semantic_object_type=(
                        "std::vec::Vec<fixture::UniAllocAddressOracleProducer>"
                    ),
                ),
            ],
            "fail_closed_rows": [fail_closed_row()],
        }
    ]


def valid_contract_totals() -> dict:
    return {
        "direct_rewrite_applied_count": 1,
        "semantic_ownership_transfer_candidate_count": 0,
        "semantic_ownership_transfer_rewrite_applied_count": 0,
        "semantic_scope_rewrite_applied_count": 4,
        "semantic_scope_drop_rewrite_applied_count": 1,
        "semantic_scope_unsolved_candidate_count": 1,
        "semantic_scope_callback_capable_skipped_count": 0,
        "semantic_scope_drop_unsolved_candidate_count": 0,
        "actual_type_scope_row_count": 4,
        "actual_drop_scope_row_count": 3,
        "fail_closed_semantic_row_count": 1,
        "fail_closed_callback_capable_row_count": 0,
        "fail_closed_drop_row_count": 0,
        "fail_closed_multi_owner_drop_row_count": 0,
    }


class OxipngRealappReproSmokeTests(unittest.TestCase):
    def make_minimal_oxipng_checkout(self, root: pathlib.Path) -> pathlib.Path:
        oxipng = root / "oxipng"
        (oxipng / "src").mkdir(parents=True)
        (oxipng / "Cargo.toml").write_text(
            "[package]\nname = \"oxipng\"\nversion = \"0.0.0\"\n\n[dependencies]\nbit-vec = \"0.6\"\n",
            encoding="utf-8",
        )
        (oxipng / "src" / "lib.rs").write_text(
            "#![deny(warnings)]\n#[cfg(feature = \"parallel\")]\nextern crate rayon;\n",
            encoding="utf-8",
        )
        (oxipng / "src" / "main.rs").write_text(
            "use std::time::Duration;\nfn main() {\n    let success = true;\n    if !success {\n        exit(1);\n    }\n}\n",
            encoding="utf-8",
        )
        return oxipng

    def test_apply_instrumentation_uses_relative_unialloc_path(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            tmp = pathlib.Path(td)
            oxipng = self.make_minimal_oxipng_checkout(tmp)
            unialloc = tmp / "repo" / "unialloc"
            unialloc.mkdir(parents=True)

            smoke.apply_instrumentation(oxipng, unialloc)

            cargo = (oxipng / "Cargo.toml").read_text(encoding="utf-8")
            self.assertIn('unialloc = { path = "../repo/unialloc"', cargo)
            self.assertNotIn(str(unialloc), cargo)
            self.assertIn("[workspace]", cargo)
            main = (oxipng / "src" / "main.rs").read_text(encoding="utf-8")
            self.assertIn("#[global_allocator]", main)
            self.assertIn("type_rows_json", main)
            self.assertIn("SemanticTypeStatsSnapshot::empty(); 256", main)
            self.assertIn("fn unialloc_type_isolation_address_oracle()", main)
            self.assertIn("unialloc_address_oracle_producer_vec", main)
            self.assertIn("unialloc_address_oracle_wrong_type_vec", main)
            self.assertIn("semantic_ownership_transfer_snapshot", main)
            self.assertIn("semantic_fallback_attribution_snapshot", main)
            self.assertIn("unialloc_ownership_transfer_before", main)
            self.assertIn("unialloc_ownership_transfer_after", main)
            self.assertIn('\\"raw_alloc_no_metadata\\"', main)
            self.assertIn('\\"raw_dealloc_no_metadata\\"', main)
            self.assertIn('\\"raw_realloc_no_metadata\\"', main)
            self.assertIn('\\"last_mismatch_requested_type_id\\"', main)
            self.assertIn('\\"last_mismatch_recorded_type_id\\"', main)
            self.assertIn('\\"last_mismatch_requested_module_id\\"', main)
            self.assertIn('\\"last_mismatch_recorded_module_id\\"', main)
            self.assertIn('\\"last_mismatch_requested_callsite\\"', main)
            self.assertIn('\\"last_mismatch_recorded_callsite\\"', main)
            for field in (
                "typed_cache_wrong_identity_denials",
                "last_wrong_identity_requested_type_id",
                "last_wrong_identity_retained_type_id",
                "last_wrong_identity_requested_module_id",
                "last_wrong_identity_retained_module_id",
                "last_wrong_identity_requested_callsite",
                "last_wrong_identity_size",
                "last_wrong_identity_align",
            ):
                self.assertIn(f'\\"{field}\\"', main)
                self.assertIn(f"stats.{field}", main)
            self.assertIn('\\"address_oracle\\"', main)
            self.assertNotIn("0xC002", main)
            self.assertIn("extern crate unialloc;", (oxipng / "src" / "lib.rs").read_text(encoding="utf-8"))

    def test_parse_stats_requires_exactly_one_stats_line(self) -> None:
        stats = smoke.parse_stats('noise\nUNIALLOC_STATS_JSON={"typed_allocations":1,"fallback_allocations":0}\n')
        self.assertEqual(stats["typed_allocations"], 1)
        with self.assertRaises(smoke.SmokeError):
            smoke.parse_stats("missing\n")
        with self.assertRaises(smoke.SmokeError):
            smoke.parse_stats("UNIALLOC_STATS_JSON={}\nUNIALLOC_STATS_JSON={}\n")

    def test_ownership_transfer_runtime_distinguishes_zero_and_dynamic_execution(
        self,
    ) -> None:
        zero = smoke.validate_semantic_ownership_transfer_runtime(
            valid_contract_stats()
        )
        self.assertEqual(zero["attempted"], 0)
        self.assertFalse(zero["dynamic_execution_observed"])
        self.assertEqual(
            zero["execution_status"], "zero_dynamic_execution_observed"
        )

        dynamic_stats = valid_contract_stats()
        dynamic_stats["semantic_ownership_transfer"] = ownership_transfer_runtime(
            before=(4, 3, 1),
            delta=(2, 1, 1),
        )
        dynamic = smoke.validate_semantic_ownership_transfer_runtime(dynamic_stats)
        self.assertEqual(
            dynamic["delta"], {"attempted": 2, "applied": 1, "rejected": 1}
        )
        self.assertTrue(dynamic["dynamic_execution_observed"])
        self.assertEqual(dynamic["execution_status"], "dynamic_execution_observed")

        rejected_only_stats = valid_contract_stats()
        rejected_only_stats["semantic_ownership_transfer"] = (
            ownership_transfer_runtime(delta=(1, 0, 1))
        )
        with self.assertRaisesRegex(
            smoke.SmokeError, "must include at least one applied transfer"
        ):
            smoke.validate_semantic_ownership_transfer_runtime(rejected_only_stats)

    def test_current_ownership_transfer_runtime_fails_closed_when_missing(self) -> None:
        stats = valid_contract_stats()
        del stats["semantic_ownership_transfer"]
        with self.assertRaisesRegex(
            smoke.SmokeError, "explicitly report semantic_ownership_transfer"
        ):
            smoke.validate_semantic_ownership_transfer_runtime(stats)

    def test_ownership_transfer_runtime_rejects_incomplete_or_false_delta(self) -> None:
        incomplete = valid_contract_stats()
        incomplete["semantic_ownership_transfer"] = ownership_transfer_runtime(
            delta=(3, 1, 1)
        )
        with self.assertRaisesRegex(
            smoke.SmokeError, "attempted must equal applied plus rejected"
        ):
            smoke.validate_semantic_ownership_transfer_runtime(incomplete)

        false_delta = valid_contract_stats()
        false_delta["semantic_ownership_transfer"] = ownership_transfer_runtime(
            delta=(2, 1, 1)
        )
        false_delta["semantic_ownership_transfer"]["after"]["attempted"] += 1
        false_delta["semantic_ownership_transfer"]["after"]["applied"] += 1
        with self.assertRaisesRegex(smoke.SmokeError, "delta does not match snapshots"):
            smoke.validate_semantic_ownership_transfer_runtime(false_delta)

    def test_legacy_reference_summary_is_loaded_without_backfilled_runtime_fields(
        self,
    ) -> None:
        legacy = {"schema_version": 2, "source": "preserved-oxipng-evidence"}
        with tempfile.TemporaryDirectory() as td:
            path = pathlib.Path(td) / "instrumented-oxipng-summary.json"
            path.write_text(json.dumps(legacy), encoding="utf-8")
            loaded = smoke.load_reference_summary(path)

        self.assertEqual(loaded, legacy)
        self.assertNotIn("semantic_ownership_transfer_runtime", loaded)

    def test_collect_audits_sums_only_target_crate(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            audit_dir = pathlib.Path(td)
            (audit_dir / "oxipng.json").write_text(
                json.dumps(
                    {
                        "compiler_pass": {
                            "crate_name": "oxipng",
                            "actual_allocator_call_replacement": True,
                            "actual_semantic_scope_rewrite": True,
                            "body_clone_returned_to_rustc": True,
                            "direct_local_metadata_abi": True,
                            "direct_local_size_align_with_semantic_drop": True,
                            "rewrite_applied_count": 2,
                            "semantic_scope_rewrite_applied_count": 2,
                            "semantic_scope_drop_rewrite_applied_count": 4,
                            "semantic_scope_unsolved_candidate_count": 1,
                            "semantic_scope_callback_capable_skipped_count": 1,
                            "semantic_scope_drop_unsolved_candidate_count": 0,
                        },
                        "rewrite_candidates": [
                            actual_scope_row(
                                type_id=101,
                                callsite=1001,
                                semantic_object_type="Vec<Producer>",
                            ),
                            actual_scope_row(
                                type_id=202,
                                callsite=2002,
                                semantic_object_type="Vec<Consumer>",
                            ),
                            ownership_transfer_row(applied=True),
                            ownership_transfer_row(applied=False),
                            fail_closed_row(),
                            fail_closed_callback_capable_row(),
                            fail_closed_multi_owner_drop_row(),
                        ],
                    }
                ),
                encoding="utf-8",
            )
            (audit_dir / "dep.json").write_text(json.dumps({"compiler_pass": {"crate_name": "dep"}}), encoding="utf-8")

            audits, totals = smoke.collect_audits(audit_dir, "oxipng")

            self.assertEqual(len(audits), 1)
            self.assertEqual(totals["direct_rewrite_applied_count"], 2)
            self.assertEqual(
                totals["semantic_ownership_transfer_candidate_count"], 2
            )
            self.assertEqual(
                totals["semantic_ownership_transfer_rewrite_applied_count"], 1
            )
            self.assertEqual(totals["semantic_scope_rewrite_applied_count"], 2)
            self.assertEqual(totals["semantic_scope_drop_rewrite_applied_count"], 4)
            self.assertEqual(totals["semantic_scope_unsolved_candidate_count"], 1)
            self.assertEqual(
                totals["semantic_scope_callback_capable_skipped_count"], 1
            )
            self.assertEqual(totals["semantic_scope_drop_unsolved_candidate_count"], 0)
            self.assertEqual(totals["actual_type_scope_row_count"], 2)
            self.assertEqual(totals["actual_drop_scope_row_count"], 0)
            self.assertEqual(totals["fail_closed_semantic_row_count"], 1)
            self.assertEqual(totals["fail_closed_callback_capable_row_count"], 1)
            self.assertEqual(totals["fail_closed_drop_row_count"], 1)
            self.assertEqual(totals["fail_closed_multi_owner_drop_row_count"], 1)
            self.assertEqual(len(audits[0]["actual_type_scope_rows"]), 2)
            self.assertEqual(
                len(audits[0]["semantic_ownership_transfer_rows"]), 1
            )
            self.assertEqual(
                audits[0]["semantic_ownership_transfer_rows"][0][
                    "replacement_symbol"
                ],
                "__unialloc_semantic_box_slice_into_vec",
            )
            self.assertEqual(
                audits[0]["semantic_ownership_transfer_rows"][0]["lowering_kind"],
                smoke.SEMANTIC_OWNERSHIP_TRANSFER_KIND,
            )
            self.assertEqual(len(audits[0]["fail_closed_rows"]), 3)
            coverage = smoke.compiler_coverage_summary(totals)
            self.assertFalse(coverage["audited_candidates_resolved"])
            self.assertTrue(coverage["has_unresolved_audited_candidates"])
            self.assertTrue(coverage["has_fail_closed_audited_candidates"])
            self.assertFalse(coverage["whole_program_compiler_coverage"])
            self.assertEqual(coverage["unsolved_candidate_count"], 1)
            self.assertEqual(coverage["fail_closed_candidate_count"], 3)
            self.assertEqual(
                coverage["semantic_scope_callback_capable_skipped_count"], 1
            )
            self.assertEqual(coverage["multi_owner_drop_fail_closed_count"], 1)
            self.assertEqual(
                coverage["semantic_ownership_transfer_candidate_count"], 2
            )
            self.assertEqual(
                coverage[
                    "semantic_ownership_transfer_rewrite_applied_count"
                ],
                1,
            )
            self.assertNotIn("complete compiler coverage", coverage["claim_boundary"])

    def test_zero_unsolved_means_audited_candidates_resolved_not_complete_coverage(self) -> None:
        coverage = smoke.compiler_coverage_summary(
            {
                "direct_rewrite_applied_count": 6,
                "semantic_scope_rewrite_applied_count": 846,
                "semantic_scope_drop_rewrite_applied_count": 532,
                "semantic_scope_unsolved_candidate_count": 0,
                "semantic_scope_callback_capable_skipped_count": 0,
                "semantic_scope_drop_unsolved_candidate_count": 0,
            }
        )

        self.assertTrue(coverage["audited_candidates_resolved"])
        self.assertFalse(coverage["has_unresolved_audited_candidates"])
        self.assertFalse(coverage["has_fail_closed_audited_candidates"])
        self.assertFalse(coverage["whole_program_compiler_coverage"])
        self.assertEqual(coverage["fail_closed_candidate_count"], 0)
        self.assertEqual(
            coverage["coverage_scope"],
            "target_crate_audited_semantic_and_drop_candidates",
        )
        self.assertIn("audited target-crate MIR", coverage["claim_boundary"])
        self.assertIn("not whole-program or object coverage", coverage["claim_boundary"])
        self.assertNotIn("complete compiler coverage", coverage["claim_boundary"])

    def test_callback_capable_skips_are_fail_closed_but_not_unsolved(self) -> None:
        coverage = smoke.compiler_coverage_summary(
            {
                "semantic_scope_unsolved_candidate_count": 0,
                "semantic_scope_callback_capable_skipped_count": 1,
                "semantic_scope_drop_unsolved_candidate_count": 0,
                "fail_closed_callback_capable_row_count": 1,
            }
        )

        self.assertFalse(coverage["audited_candidates_resolved"])
        self.assertFalse(coverage["has_unresolved_audited_candidates"])
        self.assertTrue(coverage["has_fail_closed_audited_candidates"])
        self.assertEqual(coverage["unsolved_candidate_count"], 0)
        self.assertEqual(coverage["fail_closed_candidate_count"], 1)
        self.assertEqual(
            coverage["semantic_scope_callback_capable_skipped_count"], 1
        )
        self.assertIn("callback-capable", coverage["claim_boundary"])

    def test_callback_capable_row_is_never_hidden_by_aggregate_shape(self) -> None:
        cmd = smoke.CommandResult(["cmd"], 0, "", "")
        audits = valid_contract_audits()
        audits[0]["fail_closed_rows"].append(fail_closed_callback_capable_row())
        totals = valid_contract_totals()
        totals["semantic_scope_callback_capable_skipped_count"] = 1
        totals["fail_closed_callback_capable_row_count"] = 1

        evidence = smoke.assert_contract(
            build=cmd,
            run=cmd,
            stats=valid_contract_stats(),
            output_sha256="a",
            expected_output_sha256="a",
            audits=audits,
            audit_totals=totals,
            fallback_note="reported fallback_allocations=0",
        )

        self.assertEqual(evidence["fail_closed_candidate_count"], 2)

    def test_multi_owner_drop_rows_prevent_false_resolved_coverage(self) -> None:
        coverage = smoke.compiler_coverage_summary(
            {
                "semantic_scope_unsolved_candidate_count": 0,
                "semantic_scope_drop_unsolved_candidate_count": 0,
                "fail_closed_semantic_row_count": 0,
                "fail_closed_drop_row_count": 265,
                "fail_closed_multi_owner_drop_row_count": 265,
            }
        )

        self.assertFalse(coverage["audited_candidates_resolved"])
        self.assertTrue(coverage["has_fail_closed_audited_candidates"])
        self.assertEqual(coverage["unsolved_candidate_count"], 0)
        self.assertEqual(coverage["fail_closed_candidate_count"], 265)
        self.assertEqual(coverage["multi_owner_drop_fail_closed_count"], 265)
        self.assertIn("fail-closed", coverage["claim_boundary"])

    def test_multi_owner_drop_fail_closed_row_is_never_hidden_by_aggregate_shape(self) -> None:
        cmd = smoke.CommandResult(["cmd"], 0, "", "")
        for aggregate_includes_multi_owner in (False, True):
            audits = valid_contract_audits()
            audits[0]["fail_closed_rows"].append(fail_closed_multi_owner_drop_row())
            totals = valid_contract_totals()
            totals["fail_closed_drop_row_count"] = 1
            totals["fail_closed_multi_owner_drop_row_count"] = 1
            totals["semantic_scope_drop_unsolved_candidate_count"] = int(
                aggregate_includes_multi_owner
            )

            evidence = smoke.assert_contract(
                build=cmd,
                run=cmd,
                stats=valid_contract_stats(),
                output_sha256="a",
                expected_output_sha256="a",
                audits=audits,
                audit_totals=totals,
                fallback_note="reported fallback_allocations=0",
            )
            self.assertEqual(evidence["fail_closed_candidate_count"], 2)

    def test_contract_fails_closed_on_missing_direct_rewrite(self) -> None:
        cmd = smoke.CommandResult(["cmd"], 0, "", "")
        with self.assertRaisesRegex(smoke.SmokeError, "direct MIR"):
            smoke.assert_contract(
                build=cmd,
                run=cmd,
                stats={"typed_allocations": 1, "fallback_allocations": 0, "type_isolation_corrupt_slots": 0},
                output_sha256="a",
                expected_output_sha256="a",
                audits=[{"file": "audit.json", **{key: True for key in smoke.REQUIRED_AUDIT_FLAGS}}],
                audit_totals={
                    "direct_rewrite_applied_count": 0,
                    "semantic_scope_rewrite_applied_count": 1,
                    "semantic_scope_drop_rewrite_applied_count": 1,
                    "semantic_scope_unsolved_candidate_count": 0,
                    "semantic_scope_drop_unsolved_candidate_count": 0,
                },
                fallback_note="reported fallback_allocations=0",
            )


    def test_contract_fails_closed_on_false_audit_flags(self) -> None:
        cmd = smoke.CommandResult(["cmd"], 0, "", "")
        totals = {
            "direct_rewrite_applied_count": 1,
            "semantic_scope_rewrite_applied_count": 1,
            "semantic_scope_drop_rewrite_applied_count": 1,
            "semantic_scope_unsolved_candidate_count": 0,
            "semantic_scope_drop_unsolved_candidate_count": 0,
        }
        audit = {key: True for key in smoke.REQUIRED_AUDIT_FLAGS}
        audit.update({
            "file": "audit.json",
            "body_clone_returned_to_rustc": False,
        })
        with self.assertRaisesRegex(smoke.SmokeError, "body_clone_returned_to_rustc"):
            smoke.assert_contract(
                build=cmd,
                run=cmd,
                stats={"typed_allocations": 1, "fallback_allocations": 0, "type_isolation_corrupt_slots": 0},
                output_sha256="a",
                expected_output_sha256="a",
                audits=[audit],
                audit_totals=totals,
                fallback_note="reported fallback_allocations=0",
            )


    def test_contract_accepts_mixed_no_candidate_and_applied_audits(self) -> None:
        cmd = smoke.CommandResult(["cmd"], 0, "", "")
        no_candidates = {
            "file": "no-candidates.json",
            "replacement_resolution_status": smoke.NO_SUPPORTED_DIRECT_REWRITE_STATUS,
            "actual_semantic_scope_rewrite": True,
            "body_clone_returned_to_rustc": True,
            **{key: False for key in smoke.DIRECT_REWRITE_AUDIT_FLAGS},
        }
        applied = {
            **valid_contract_audits()[0],
        }

        evidence = smoke.assert_contract(
            build=cmd,
            run=cmd,
            stats=valid_contract_stats(),
            output_sha256="a",
            expected_output_sha256="a",
            audits=[no_candidates, applied],
            audit_totals=valid_contract_totals(),
            fallback_note="reported fallback_allocations=0",
        )
        self.assertTrue(evidence["validated"])

    def test_contract_binds_actual_rewrite_to_runtime_classes_and_fail_closed_rows(self) -> None:
        cmd = smoke.CommandResult(["cmd"], 0, "", "")
        evidence = smoke.assert_contract(
            build=cmd,
            run=cmd,
            stats=valid_contract_stats(),
            output_sha256="a",
            expected_output_sha256="a",
            audits=valid_contract_audits(),
            audit_totals=valid_contract_totals(),
            fallback_note="reported fallback_allocations=0",
        )

        pair = evidence["same_layout_distinct_type_pair"]
        self.assertEqual(len(pair), 2)
        self.assertNotEqual(pair[0]["type_id"], pair[1]["type_id"])
        self.assertNotEqual(
            pair[0]["semantic_object_type"], pair[1]["semantic_object_type"]
        )
        self.assertEqual(pair[0]["observed_size"], pair[1]["observed_size"])
        self.assertEqual(evidence["fail_closed_candidate_count"], 1)
        transfer = evidence["semantic_ownership_transfer_runtime"]
        self.assertTrue(transfer["validated"])
        self.assertEqual(transfer["attempted"], 0)
        self.assertEqual(
            transfer["execution_status"], "zero_dynamic_execution_observed"
        )
        address = evidence["address_level_functional_oracle"]
        self.assertTrue(address["validated"])
        self.assertTrue(address["wrong_type_not_reused"])
        self.assertTrue(address["same_type_reused"])
        self.assertEqual(address["producer_compiler_identity"]["type_id"], 101)
        self.assertEqual(address["wrong_type_compiler_identity"]["type_id"], 202)
        self.assertEqual(address["producer_drop_identity_count"], 1)
        self.assertEqual(address["wrong_type_drop_identity_count"], 1)
        self.assertIn("injected functional oracle", evidence["claim_boundary"])
        self.assertIn("not natural application coverage", evidence["claim_boundary"])

    def test_contract_preserves_raw_metadata_counters_and_distinguishes_missing_from_zero(
        self,
    ) -> None:
        cmd = smoke.CommandResult(["cmd"], 0, "", "")

        reported_stats = valid_contract_stats()
        reported_stats.update(
            {
                "raw_alloc_no_metadata": 7,
                "raw_dealloc_no_metadata": 11,
                "raw_realloc_no_metadata": 13,
            }
        )
        reported = smoke.assert_contract(
            build=cmd,
            run=cmd,
            stats=reported_stats,
            output_sha256="a",
            expected_output_sha256="a",
            audits=valid_contract_audits(),
            audit_totals=valid_contract_totals(),
            fallback_note="reported fallback_allocations=0",
        )["raw_metadata_runtime"]
        self.assertEqual(reported["reporting_status"], "reported")
        self.assertTrue(reported["complete"])
        self.assertFalse(reported["all_zero"])
        self.assertEqual(reported["raw_alloc_no_metadata"], 7)
        self.assertEqual(reported["raw_dealloc_no_metadata"], 11)
        self.assertEqual(reported["raw_realloc_no_metadata"], 13)
        self.assertEqual(reported["missing_fields"], [])

        zero = smoke.assert_contract(
            build=cmd,
            run=cmd,
            stats=valid_contract_stats(),
            output_sha256="a",
            expected_output_sha256="a",
            audits=valid_contract_audits(),
            audit_totals=valid_contract_totals(),
            fallback_note="reported fallback_allocations=0",
        )["raw_metadata_runtime"]
        self.assertEqual(zero["reporting_status"], "reported")
        self.assertTrue(zero["all_zero"])

        missing_stats = valid_contract_stats()
        for field in (
            "raw_alloc_no_metadata",
            "raw_dealloc_no_metadata",
            "raw_realloc_no_metadata",
        ):
            del missing_stats[field]
        missing = smoke.assert_contract(
            build=cmd,
            run=cmd,
            stats=missing_stats,
            output_sha256="a",
            expected_output_sha256="a",
            audits=valid_contract_audits(),
            audit_totals=valid_contract_totals(),
            fallback_note="reported fallback_allocations=0",
        )["raw_metadata_runtime"]
        self.assertEqual(missing["reporting_status"], "missing")
        self.assertFalse(missing["complete"])
        self.assertIsNone(missing["all_zero"])
        self.assertIsNone(missing["raw_alloc_no_metadata"])
        self.assertIsNone(missing["raw_dealloc_no_metadata"])
        self.assertIsNone(missing["raw_realloc_no_metadata"])
        self.assertEqual(
            missing["missing_fields"],
            [
                "raw_alloc_no_metadata",
                "raw_dealloc_no_metadata",
                "raw_realloc_no_metadata",
            ],
        )

    def test_address_oracle_fails_closed_without_compiler_derived_identity(self) -> None:
        cmd = smoke.CommandResult(["cmd"], 0, "", "")
        audits = valid_contract_audits()
        audits[0]["actual_type_scope_rows"] = audits[0]["actual_type_scope_rows"][1:]
        totals = valid_contract_totals()
        totals["actual_type_scope_row_count"] = 3

        with self.assertRaisesRegex(smoke.SmokeError, "compiler-derived producer"):
            smoke.assert_contract(
                build=cmd,
                run=cmd,
                stats=valid_contract_stats(),
                output_sha256="a",
                expected_output_sha256="a",
                audits=audits,
                audit_totals=totals,
                fallback_note="reported fallback_allocations=0",
            )

    def test_valid_oracle_does_not_require_a_natural_same_layout_pair(self) -> None:
        cmd = smoke.CommandResult(["cmd"], 0, "", "")
        stats = valid_contract_stats()
        stats["type_rows"] = stats["type_rows"][:4]
        stats["type_stats_rows"] = 4
        audits = valid_contract_audits()
        audits[0]["actual_type_scope_rows"] = audits[0]["actual_type_scope_rows"][:2]
        totals = valid_contract_totals()
        totals["actual_type_scope_row_count"] = 2
        totals["semantic_scope_rewrite_applied_count"] = 2

        evidence = smoke.assert_contract(
            build=cmd,
            run=cmd,
            stats=stats,
            output_sha256="a",
            expected_output_sha256="a",
            audits=audits,
            audit_totals=totals,
            fallback_note="reported fallback_allocations=0",
        )

        self.assertFalse(evidence["natural_same_layout_pair_observed"])
        self.assertIsNone(evidence["same_layout_distinct_type_pair"])
        self.assertTrue(evidence["address_level_functional_oracle"]["validated"])

    def test_address_oracle_rejects_cross_type_reuse_and_missing_same_type_recovery(self) -> None:
        cmd = smoke.CommandResult(["cmd"], 0, "", "")
        cross_type = valid_contract_stats()
        cross_type["address_oracle"]["wrong_type_address"] = 0x1000
        cross_type["address_oracle"]["wrong_type_not_reused"] = False
        with self.assertRaisesRegex(smoke.SmokeError, "wrong type reused"):
            smoke.assert_contract(
                build=cmd,
                run=cmd,
                stats=cross_type,
                output_sha256="a",
                expected_output_sha256="a",
                audits=valid_contract_audits(),
                audit_totals=valid_contract_totals(),
                fallback_note="reported fallback_allocations=0",
            )

        no_recovery = valid_contract_stats()
        no_recovery["address_oracle"]["producer_recovery_address"] = 0x3000
        no_recovery["address_oracle"]["same_type_reused"] = False
        with self.assertRaisesRegex(smoke.SmokeError, "same type did not recover"):
            smoke.assert_contract(
                build=cmd,
                run=cmd,
                stats=no_recovery,
                output_sha256="a",
                expected_output_sha256="a",
                audits=valid_contract_audits(),
                audit_totals=valid_contract_totals(),
                fallback_note="reported fallback_allocations=0",
            )

    def test_global_recovery_corrections_preserve_count_when_oracle_is_exact(
        self,
    ) -> None:
        cmd = smoke.CommandResult(["cmd"], 0, "", "")
        recovery_corrected = valid_contract_stats()
        recovery_corrected.update(
            {
                "recovery_identity_matches": 3,
                "recovery_identity_mismatches": 7,
                "last_mismatch_requested_type_id": 202,
                "last_mismatch_recorded_type_id": 101,
                "last_mismatch_requested_module_id": 77,
                "last_mismatch_recorded_module_id": 77,
                "last_mismatch_requested_callsite": 4004,
                "last_mismatch_recorded_callsite": 1001,
            }
        )
        evidence = smoke.assert_contract(
            build=cmd,
            run=cmd,
            stats=recovery_corrected,
            output_sha256="a",
            expected_output_sha256="a",
            audits=valid_contract_audits(),
            audit_totals=valid_contract_totals(),
            fallback_note="reported fallback_allocations=0",
        )
        self.assertEqual(evidence["whole_run_recovery_identity_mismatches"], 7)
        self.assertFalse(evidence["whole_run_exact_compiler_identity"])
        self.assertEqual(
            evidence["whole_run_compiler_identity_status"],
            "recovery_corrected_non_exact",
        )
        self.assertEqual(
            evidence["address_level_functional_oracle"]["recovery_identity_mismatches"],
            0,
        )
        recovery = evidence["recovery_identity_runtime"]
        self.assertEqual(recovery["recovery_identity_matches"], 3)
        self.assertFalse(recovery["last_mismatch"]["describes_all_mismatches"])
        self.assertEqual(
            recovery["last_mismatch"]["requested"]["compiler_rows"][0]["role"],
            "drop_scope",
        )
        self.assertEqual(
            recovery["last_mismatch"]["recorded"]["compiler_rows"][0]["role"],
            "allocation_scope",
        )
        self.assertTrue(
            recovery["last_mismatch"]["type_or_module_difference_observed"]
        )
        self.assertTrue(recovery["last_mismatch"]["compiler_row_mapping_complete"])

    def test_single_recovery_mismatch_is_fully_localized(self) -> None:
        stats = valid_contract_stats()
        stats.update(
            {
                "recovery_identity_mismatches": 1,
                "last_mismatch_requested_type_id": 202,
                "last_mismatch_recorded_type_id": 101,
                "last_mismatch_requested_module_id": 77,
                "last_mismatch_recorded_module_id": 77,
                "last_mismatch_requested_callsite": 4004,
                "last_mismatch_recorded_callsite": 1001,
            }
        )

        evidence = smoke.recovery_identity_runtime_evidence(
            stats, valid_contract_audits()
        )

        self.assertTrue(evidence["last_mismatch"]["describes_all_mismatches"])
        self.assertEqual(
            evidence["last_mismatch"]["requested"]["compiler_rows"][0]["role"],
            "drop_scope",
        )
        self.assertEqual(
            evidence["last_mismatch"]["recorded"]["compiler_rows"][0]["role"],
            "allocation_scope",
        )

    def test_recovery_mismatch_details_fail_closed(self) -> None:
        missing_details = valid_contract_stats()
        missing_details["recovery_identity_mismatches"] = 1
        with self.assertRaisesRegex(smoke.SmokeError, "require nonzero"):
            smoke.recovery_identity_runtime_evidence(
                missing_details, valid_contract_audits()
            )

        missing_field = valid_contract_stats()
        missing_field["recovery_identity_mismatches"] = 1
        del missing_field["last_mismatch_recorded_callsite"]
        with self.assertRaisesRegex(smoke.SmokeError, "explicit nonnegative"):
            smoke.recovery_identity_runtime_evidence(
                missing_field, valid_contract_audits()
            )

        stale_details = valid_contract_stats()
        stale_details["last_mismatch_requested_type_id"] = 202
        with self.assertRaisesRegex(smoke.SmokeError, "must not retain stale"):
            smoke.recovery_identity_runtime_evidence(
                stale_details, valid_contract_audits()
            )

        unserialized_difference = valid_contract_stats()
        unserialized_difference.update(
            {
                "recovery_identity_mismatches": 1,
                "last_mismatch_requested_type_id": 101,
                "last_mismatch_recorded_type_id": 101,
                "last_mismatch_requested_module_id": 77,
                "last_mismatch_recorded_module_id": 77,
                "last_mismatch_requested_callsite": 1001,
                "last_mismatch_recorded_callsite": 1001,
            }
        )
        evidence = smoke.recovery_identity_runtime_evidence(
            unserialized_difference, valid_contract_audits()
        )
        last = evidence["last_mismatch"]
        self.assertFalse(last["type_or_module_difference_observed"])
        self.assertFalse(last["callsite_difference_observed"])
        self.assertTrue(last["unserialized_policy_or_hint_difference_required"])

        module_only = valid_contract_stats()
        module_only.update(
            {
                "recovery_identity_mismatches": 1,
                "last_mismatch_requested_type_id": 101,
                "last_mismatch_recorded_type_id": 101,
                "last_mismatch_requested_module_id": 88,
                "last_mismatch_recorded_module_id": 77,
                "last_mismatch_requested_callsite": 1001,
                "last_mismatch_recorded_callsite": 1001,
            }
        )
        module_evidence = smoke.recovery_identity_runtime_evidence(
            module_only, valid_contract_audits()
        )["last_mismatch"]
        self.assertTrue(module_evidence["type_or_module_difference_observed"])
        self.assertFalse(
            module_evidence["unserialized_policy_or_hint_difference_required"]
        )

        zero_context = valid_contract_stats()
        zero_context.update(
            {
                "recovery_identity_mismatches": 1,
                "last_mismatch_requested_type_id": 202,
                "last_mismatch_recorded_type_id": 101,
                "last_mismatch_requested_module_id": 0,
                "last_mismatch_recorded_module_id": 0,
                "last_mismatch_requested_callsite": 0,
                "last_mismatch_recorded_callsite": 0,
            }
        )
        zero_context_evidence = smoke.recovery_identity_runtime_evidence(
            zero_context, valid_contract_audits()
        )["last_mismatch"]
        self.assertTrue(
            zero_context_evidence["type_or_module_difference_observed"]
        )
        self.assertFalse(zero_context_evidence["compiler_row_mapping_complete"])

    def test_address_oracle_rejects_nonzero_mismatch_before_or_after(self) -> None:
        cmd = smoke.CommandResult(["cmd"], 0, "", "")
        for field in (
            "recovery_identity_mismatches_before",
            "recovery_identity_mismatches_after",
        ):
            with self.subTest(field=field):
                stats = valid_contract_stats()
                stats["address_oracle"][field] = 1
                with self.assertRaisesRegex(
                    smoke.SmokeError,
                    "address oracle recovery identity mismatches must be zero",
                ):
                    smoke.assert_contract(
                        build=cmd,
                        run=cmd,
                        stats=stats,
                        output_sha256="a",
                        expected_output_sha256="a",
                        audits=valid_contract_audits(),
                        audit_totals=valid_contract_totals(),
                        fallback_note="reported fallback_allocations=0",
                    )

    def test_address_oracle_rejects_corrupt_slots(self) -> None:
        cmd = smoke.CommandResult(["cmd"], 0, "", "")
        stats = valid_contract_stats()
        stats["address_oracle"]["corrupt_slots_after"] = 1

        with self.assertRaisesRegex(
            smoke.SmokeError, "address oracle corrupt slots must be zero"
        ):
            smoke.assert_contract(
                build=cmd,
                run=cmd,
                stats=stats,
                output_sha256="a",
                expected_output_sha256="a",
                audits=valid_contract_audits(),
                audit_totals=valid_contract_totals(),
                fallback_note="reported fallback_allocations=0",
            )

    def test_whole_run_rejects_corrupt_slots(self) -> None:
        cmd = smoke.CommandResult(["cmd"], 0, "", "")
        stats = valid_contract_stats()
        stats["type_isolation_corrupt_slots"] = 1

        with self.assertRaisesRegex(
            smoke.SmokeError, "type isolation corrupt slots must be zero"
        ):
            smoke.assert_contract(
                build=cmd,
                run=cmd,
                stats=stats,
                output_sha256="a",
                expected_output_sha256="a",
                audits=valid_contract_audits(),
                audit_totals=valid_contract_totals(),
                fallback_note="reported fallback_allocations=0",
            )

    def test_fail_closed_candidates_require_row_level_evidence(self) -> None:
        cmd = smoke.CommandResult(["cmd"], 0, "", "")
        missing_fail_closed = valid_contract_audits()
        missing_fail_closed[0]["fail_closed_rows"] = []
        with self.assertRaisesRegex(smoke.SmokeError, "row-level evidence"):
            smoke.assert_contract(
                build=cmd,
                run=cmd,
                stats=valid_contract_stats(),
                output_sha256="a",
                expected_output_sha256="a",
                audits=missing_fail_closed,
                audit_totals=valid_contract_totals(),
                fallback_note="reported fallback_allocations=0",
            )


    def test_fresh_temp_dir_rejects_existing_files_without_deleting(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            temp_dir = pathlib.Path(td) / "temp"
            temp_dir.mkdir()
            stale = temp_dir / "stale-target"
            stale.write_text("do not delete\n", encoding="utf-8")

            with self.assertRaisesRegex(smoke.SmokeError, "fresh/empty"):
                smoke.require_fresh_temp_dir(temp_dir)

            self.assertEqual(stale.read_text(encoding="utf-8"), "do not delete\n")


    def test_input_must_be_contained_inside_detached_checkout(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = pathlib.Path(td)
            checkout = root / "checkout"
            checkout.mkdir()
            inside = checkout / "tests" / "in.png"
            inside.parent.mkdir()
            inside.write_bytes(b"png")
            outside = root / "outside.png"
            outside.write_bytes(b"png")

            smoke.require_contained_path(inside, checkout, label="input PNG")
            with self.assertRaisesRegex(smoke.SmokeError, "detached checkout"):
                smoke.require_contained_path(outside, checkout, label="input PNG")


    def test_fresh_output_dir_rejects_existing_files_without_deleting(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            output_dir = pathlib.Path(td) / "out"
            output_dir.mkdir()
            stale = output_dir / "stale-audit.json"
            stale.write_text("do not delete\n", encoding="utf-8")

            with self.assertRaisesRegex(smoke.SmokeError, "fresh/empty"):
                smoke.require_fresh_output_dir(output_dir)

            self.assertEqual(stale.read_text(encoding="utf-8"), "do not delete\n")

    def test_posix_only_guard_rejects_windows(self) -> None:
        smoke.require_posix_host("posix")
        with self.assertRaisesRegex(smoke.SmokeError, "POSIX-only"):
            smoke.require_posix_host("nt")

    def test_protected_path_overlap_rejects_repo_and_pinned_containment(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            tmp = pathlib.Path(td)
            pinned = tmp / "pinned" / "checkout"
            pinned.mkdir(parents=True)

            with self.assertRaisesRegex(smoke.SmokeError, "repo root"):
                smoke.reject_protected_path_overlap(ROOT / "evaluation" / "raw" / "stale", pinned=pinned, label="output")

            with self.assertRaisesRegex(smoke.SmokeError, "pinned checkout"):
                smoke.reject_protected_path_overlap(pinned / "nested-output", pinned=pinned, label="output")

            with self.assertRaisesRegex(smoke.SmokeError, "pinned checkout"):
                smoke.reject_protected_path_overlap(pinned.parent, pinned=pinned, label="temporary parent")

    def test_source_binding_records_head_status_pass_source_and_toolchain(self) -> None:
        binding = smoke.source_binding(
            "nightly-test",
            pathlib.Path("/tmp/example-sysroot"),
            "rustc 1.2.3\nhost: test",
        )

        self.assertRegex(binding["repo_head"], r"^[0-9a-f]{40}$")
        self.assertEqual(binding["scoped_status_paths"], [str(path) for path in smoke.SCOPED_STATUS_PATHS])
        self.assertIn("Cargo.toml", binding["scoped_file_hashes"])
        self.assertIn("unialloc/build.rs", binding["scoped_file_hashes"])
        self.assertIn("alloc_macros/Cargo.toml", binding["scoped_file_hashes"])
        self.assertIn("evaluation/scripts/oxipng_realapp_repro_smoke.py", binding["scoped_file_hashes"])
        self.assertIn("evaluation/scripts/test_oxipng_realapp_repro_smoke.py", binding["scoped_file_hashes"])
        self.assertIn("pass_source_sha256", binding)
        self.assertRegex(binding["pass_source_sha256"], r"^[0-9a-f]{64}$")
        self.assertRegex(
            binding["pass_source_closure_sha256"], r"^[0-9a-f]{64}$"
        )
        self.assertEqual(binding["pass_source_closure_file_count"], 3)
        self.assertIn(
            "tools/unialloc-rustc-pass/unialloc-rustc-driver-engine.rs",
            binding["scoped_file_hashes"],
        )
        self.assertIn(
            "tools/unialloc-rustc-pass/lifetime_aware.rs",
            binding["scoped_file_hashes"],
        )
        self.assertIn("repo_cargo_lock_sha256", binding)
        self.assertRegex(binding["scoped_fingerprint_sha256"], r"^[0-9a-f]{64}$")
        self.assertGreater(binding["scoped_file_count"], 0)
        self.assertEqual(binding["build_toolchain"], "nightly-test")
        self.assertEqual(binding["rustc_sysroot"], "/tmp/example-sysroot")
        self.assertEqual(binding["rustc_verbose_version"], "rustc 1.2.3\nhost: test")

    def test_source_drift_rejection_checks_scoped_fingerprint(self) -> None:
        start = {
            "repo_head": "a",
            "scoped_status": "",
            "scoped_fingerprint_sha256": "one",
            "pass_source_sha256": "p",
            "pass_source_closure_sha256": "closure",
            "repo_cargo_lock_sha256": "c",
            "rustc_sysroot": "/tmp/sysroot",
            "rustc_verbose_version": "rustc",
        }
        smoke.reject_source_drift(start, dict(start))
        end = dict(start)
        end["scoped_fingerprint_sha256"] = "two"
        with self.assertRaisesRegex(smoke.SmokeError, "drifted"):
            smoke.reject_source_drift(start, end)

    def test_pass_binary_binding_records_hash_and_boundary(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            binary = pathlib.Path(td) / "wrapper"
            binary.write_bytes(b"fake-wrapper\n")

            digest = smoke.sha256_file(binary)
            with self.assertRaisesRegex(smoke.SmokeError, "expected-pass-binary-sha256"):
                smoke.pass_binary_binding(binary, built_by_script=False)
            with self.assertRaisesRegex(smoke.SmokeError, "pass-binary-provenance"):
                smoke.pass_binary_binding(binary, built_by_script=False, expected_sha256=digest)
            with self.assertRaisesRegex(smoke.SmokeError, "hash mismatch"):
                smoke.pass_binary_binding(
                    binary, built_by_script=False, expected_sha256="0" * 64, provenance="unit-test wrapper"
                )

            provided = smoke.pass_binary_binding(
                binary, built_by_script=False, expected_sha256=digest, provenance="unit-test wrapper"
            )
            built = smoke.pass_binary_binding(binary, built_by_script=True)

            self.assertRegex(provided["sha256"], r"^[0-9a-f]{64}$")
            self.assertEqual(provided["expected_sha256"], digest)
            self.assertEqual(provided["provenance"], "unit-test wrapper")
            self.assertFalse(provided["built_by_script"])
            self.assertIn("not binary provenance", provided["source_boundary"])
            self.assertTrue(built["built_by_script"])
            self.assertIn("current run", built["source_boundary"])

    def test_verify_pinned_checkout_rejects_dirty_checkout(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            repo = pathlib.Path(td) / "repo"
            repo.mkdir()
            subprocess.run(["git", "init"], cwd=repo, check=True, stdout=subprocess.PIPE)
            (repo / "file.txt").write_text("clean\n", encoding="utf-8")
            subprocess.run(["git", "add", "file.txt"], cwd=repo, check=True)
            subprocess.run(
                [
                    "git",
                    "-c",
                    "user.name=Unit Test",
                    "-c",
                    "user.email=test@example.invalid",
                    "-c",
                    "commit.gpgsign=false",
                    "commit",
                    "-m",
                    "init",
                ],
                cwd=repo,
                check=True,
                stdout=subprocess.PIPE,
            )
            clean = smoke.verify_pinned_checkout(repo, None)
            self.assertEqual(clean["status"], "")
            (repo / "file.txt").write_text("dirty\n", encoding="utf-8")
            with self.assertRaisesRegex(smoke.SmokeError, "must be clean"):
                smoke.verify_pinned_checkout(repo, None)


if __name__ == "__main__":
    unittest.main()
