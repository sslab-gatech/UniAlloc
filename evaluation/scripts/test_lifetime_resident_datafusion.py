#!/usr/bin/env python3
"""Focused contract tests for the resident DataFusion lifetime screen."""

from __future__ import annotations

import importlib.util
import json
import tempfile
import unittest
from pathlib import Path


SCRIPT = Path(__file__).with_name("lifetime_resident_datafusion.py")


def load_module():
    spec = importlib.util.spec_from_file_location("lifetime_resident_datafusion", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class LifetimeResidentDataFusionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.runner = load_module()

    def result_record(
        self,
        *,
        batches_per_table: int = 2,
        query_iterations: int = 3,
        target_partitions: int = 2,
    ) -> dict[str, object]:
        runner = self.runner
        rows_per_table = batches_per_table * runner.ROWS_PER_BATCH
        query_digest, result_digest = runner.expected_digests(
            rows_per_table=rows_per_table,
            query_iterations=query_iterations,
        )
        return {
            "schema_version": 1,
            "correctness": True,
            "batches_per_table": batches_per_table,
            "rows_per_batch": runner.ROWS_PER_BATCH,
            "rows_per_table": rows_per_table,
            "table_count": runner.TABLE_COUNT,
            "columns_per_table": runner.COLUMNS_PER_TABLE,
            "routable_buffer_bytes": runner.ROUTABLE_BUFFER_BYTES,
            "resident_payload_bytes": (
                batches_per_table
                * runner.TABLE_COUNT
                * runner.COLUMNS_PER_TABLE
                * runner.ROUTABLE_BUFFER_BYTES
            ),
            "query_iterations": query_iterations,
            "target_partitions": target_partitions,
            "query_output_rows": len(runner.expected_aggregate_rows(rows_per_table)),
            "query_digest": query_digest,
            "result_digest": result_digest,
            "resident_build_seconds": 0.5,
            "query_seconds": 2.0,
            "total_seconds": 2.51,
        }

    def test_source_and_dependency_pins_are_exact(self) -> None:
        runner = self.runner
        self.assertEqual("54.0.0", runner.DATAFUSION_VERSION)
        self.assertEqual(
            "45d943dfb8699dc9cb9ef2320e955b73e3e6c03b",
            runner.DATAFUSION_COMMIT,
        )
        self.assertEqual("58.3.0", runner.ARROW_VERSION)
        self.assertEqual("datafusion-54.0.0", runner.DATAFUSION_CHECKOUT_NAME)
        self.assertEqual(
            ({
                "package": "libc",
                "old_version": "0.2.183",
                "sections": ("dependencies", "build-dependencies"),
                "expected_occurrences": 2,
            },),
            runner.DATAFUSION_ALLOCATOR_COMPATIBILITY_RULES,
        )
        source = runner.generated_runner_source()
        for required in (
            "MemTable::try_new",
            "fact_table",
            "dimension_table",
            "SessionConfig::new().with_target_partitions(target_partitions)",
            "JOIN dimensions",
            "GROUP BY f.group_id",
            "selector < 8",
            "const ROUTABLE_BUFFER_BYTES: usize = ROWS_PER_BATCH * 8",
            "assert_eq!(ROUTABLE_BUFFER_BYTES, 24 * 1024)",
            "fn reserved_u64_column() -> Vec<u64>",
            "fn zeroed_u64_column() -> Vec<u64>",
            "Vec::with_capacity(ROWS_PER_BATCH)",
            "let mut values = reserved_u64_column()",
            "values.resize(ROWS_PER_BATCH, 0_u64)",
            "let expected = expected_aggregates(rows_per_table)",
            "let resident_build_seconds",
            "let query_seconds",
            "let total_seconds",
            "UNIALLOC_LIFETIME_EXPERIMENT_INITIALIZER",
            runner.RESULT_PREFIX,
            runner.campaign.MECHANISM_PREFIX,
        ):
            self.assertIn(required, source)
        self.assertNotIn("Command::new", source)
        self.assertNotIn("std::process", source)
        self.assertEqual(8, source.count("= zeroed_u64_column();"))
        self.assertNotIn("vec![0_u64; ROWS_PER_BATCH]", source)

    def test_manifest_binds_pinned_source_arrow_and_allocator(self) -> None:
        runner = self.runner
        manifest = runner.generated_manifest(
            datafusion_checkout=Path("/tmp/datafusion source"),
            allocator_snapshot=Path("/tmp/allocator snapshot"),
        )
        self.assertIn(f'name = "{runner.RUNNER_PACKAGE}"', manifest)
        self.assertIn(
            (
                f'datafusion = {{ path = {json.dumps("/tmp/datafusion source/datafusion/core")}, '
                'default-features = false, features = ["sql"] }'
            ),
            manifest,
        )
        self.assertIn('arrow = "=58.3.0"', manifest)
        self.assertIn(
            f'unialloc = {{ path = {json.dumps("/tmp/allocator snapshot/unialloc")}',
            manifest,
        )
        self.assertIn('features = ["lifetime_hugepage"]', manifest)
        self.assertIn("[workspace]", manifest)

    def test_result_parser_checks_the_full_fixed_work_contract(self) -> None:
        runner = self.runner
        record = self.result_record()
        stdout = runner.RESULT_PREFIX + json.dumps(record) + "\n"
        self.assertEqual(
            record,
            runner.parse_result_record(
                stdout,
                batches_per_table=2,
                query_iterations=3,
                target_partitions=2,
            ),
        )
        record["resident_payload_bytes"] = int(record["resident_payload_bytes"]) - 1
        with self.assertRaisesRegex(runner.ContractError, "fixed-work result"):
            runner.parse_result_record(
                runner.RESULT_PREFIX + json.dumps(record) + "\n",
                batches_per_table=2,
                query_iterations=3,
                target_partitions=2,
            )

    def test_expected_query_oracle_is_deterministic_and_selective(self) -> None:
        runner = self.runner
        rows = runner.expected_aggregate_rows(runner.ROWS_PER_BATCH * 2)
        self.assertGreater(len(rows), 0)
        self.assertLess(len(rows), 64)
        self.assertTrue(all(count > 0 for _group, count, _value, _weight in rows))
        first = runner.expected_digests(
            rows_per_table=runner.ROWS_PER_BATCH * 2, query_iterations=3
        )
        second = runner.expected_digests(
            rows_per_table=runner.ROWS_PER_BATCH * 2, query_iterations=3
        )
        self.assertEqual(first, second)
        self.assertRegex(first[0], r"^[0-9a-f]{16}$")
        changed = runner.expected_digests(
            rows_per_table=runner.ROWS_PER_BATCH * 2, query_iterations=4
        )
        self.assertEqual(first[0], changed[0])
        self.assertNotEqual(first[1], changed[1])

    def test_runtime_command_is_one_direct_fixed_work_process(self) -> None:
        runner = self.runner
        command = runner.run_command(
            Path("/tmp/runner"),
            batches_per_table=256,
            query_iterations=32,
            target_partitions=2,
        )
        self.assertEqual(["/tmp/runner", "256", "32", "2"], command)
        self.assertNotIn("cargo", command)
        self.assertNotIn("sh", command)
        self.assertNotIn("numactl", command)

    def test_resident_target_is_routable_and_coverage_fails_closed(self) -> None:
        runner = self.runner
        self.assertEqual(24 * 1024, runner.ROUTABLE_BUFFER_BYTES)
        batches = 2
        allocation_count = batches * runner.TABLE_COUNT * runner.COLUMNS_PER_TABLE
        row = {
            "callsite": 11,
            "type_id": 22,
            "module_id": 33,
            "requested_size": runner.ROUTABLE_BUFFER_BYTES,
            "align": 8,
            "allocation_count": allocation_count,
            "allocation_requested_bytes": (
                allocation_count * runner.ROUTABLE_BUFFER_BYTES
            ),
        }
        summary = runner.validate_target_routing(
            {"unsupported_layout_bypasses": 0},
            [row],
            batches_per_table=batches,
        )
        self.assertEqual(allocation_count, summary["allocation_count"])
        self.assertEqual(0, summary["unsupported_layout_bypasses"])
        with self.assertRaisesRegex(runner.ContractError, "unsupported layouts"):
            runner.validate_target_routing(
                {"unsupported_layout_bypasses": 1},
                [row],
                batches_per_table=batches,
            )
        with self.assertRaisesRegex(runner.ContractError, "allocation count"):
            runner.validate_target_routing(
                {"unsupported_layout_bypasses": 0},
                [{**row, "allocation_count": allocation_count - 1}],
                batches_per_table=batches,
            )

    def test_opportunity_then_backing_gates_fail_closed(self) -> None:
        runner = self.runner
        rejected = runner.adaptive_thp_backing_gate(
            {"thp_advice_attempts": 0, "thp_collapse_successes": 0},
            [
                {
                    "elapsed_seconds": 0.5,
                    "rss_kib": 100,
                    "pss_kib": 90,
                    "anonymous_kib": 80,
                    "anon_hugepages_kib": 0,
                }
            ],
        )
        self.assertFalse(rejected["passed"])
        self.assertEqual(3, len(rejected["reasons"]))
        admitted = runner.adaptive_thp_backing_gate(
            {"thp_advice_attempts": 2, "thp_collapse_successes": 1},
            [
                {
                    "elapsed_seconds": 0.5,
                    "rss_kib": 100,
                    "pss_kib": 90,
                    "anonymous_kib": 80,
                    "anon_hugepages_kib": 0,
                },
                {
                    "elapsed_seconds": 1.0,
                    "rss_kib": 200,
                    "pss_kib": 190,
                    "anonymous_kib": 180,
                    "anon_hugepages_kib": 2_048,
                },
            ],
        )
        self.assertTrue(admitted["passed"])
        self.assertEqual(1, admitted["positive_backing_sample_count"])

    def test_each_measured_process_has_an_independent_backing_gate(self) -> None:
        runner = self.runner
        ordinary = [{"anon_hugepages_kib": 0}, {"anon_hugepages_kib": 0}]
        thp = [{"anon_hugepages_kib": 0}, {"anon_hugepages_kib": 4_096}]
        self.assertTrue(runner.measured_pair_backing_gate(ordinary, thp)["passed"])
        contaminated = [{"anon_hugepages_kib": 2_048}]
        self.assertFalse(
            runner.measured_pair_backing_gate(contaminated, thp)["passed"]
        )
        absent = [{"anon_hugepages_kib": 0}]
        self.assertFalse(runner.measured_pair_backing_gate(ordinary, absent)["passed"])

    def test_live_survival_fields_are_forward_compatible_and_validated(self) -> None:
        runner = self.runner
        self.assertEqual(
            {
                "adaptive_live_survival_credits": 9,
                "adaptive_live_survival_scans": 12,
            },
            runner.live_survival_counters(
                {"adaptive_live_survival_scans": 12, "ordinary_extent_mappings": 3},
                {"adaptive_live_survival_credits": 9},
            ),
        )
        with self.assertRaisesRegex(runner.ContractError, "counter is invalid"):
            runner.live_survival_counters({"adaptive_live_survival_scans": -1})
        with self.assertRaisesRegex(runner.ContractError, "counter disagrees"):
            runner.live_survival_counters(
                {"adaptive_live_survival_scans": 1},
                {"adaptive_live_survival_scans": 2},
            )

    def test_checkout_identity_fails_closed(self) -> None:
        runner = self.runner
        valid = {
            "repository": runner.DATAFUSION_REPOSITORY,
            "source_ref": runner.DATAFUSION_VERSION,
            "source_commit": runner.DATAFUSION_COMMIT,
            "source_tree": "a" * 40,
            "remote": runner.DATAFUSION_REPOSITORY,
            "status": "",
            "cargo_toml_sha256": "b" * 64,
            "cargo_lock_sha256": "c" * 64,
        }
        runner.validate_checkout_identity(valid)
        for field, value in (
            ("source_commit", "0" * 40),
            ("remote", "https://example.invalid/datafusion.git"),
            ("status", " M Cargo.toml"),
            ("source_tree", None),
            ("cargo_toml_sha256", None),
        ):
            broken = dict(valid)
            broken[field] = value
            with self.assertRaises(runner.ContractError):
                runner.validate_checkout_identity(broken)

    def test_lock_verifier_distinguishes_path_datafusion_and_registry_arrow(self) -> None:
        runner = self.runner
        lock = f'''version = 4

[[package]]
name = "{runner.RUNNER_PACKAGE}"
version = "0.0.0"

[[package]]
name = "datafusion"
version = "54.0.0"

[[package]]
name = "arrow"
version = "58.3.0"
source = "registry+https://github.com/rust-lang/crates.io-index"
checksum = "{'a' * 64}"
'''
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "Cargo.lock"
            path.write_text(lock, encoding="utf-8")
            record = runner.verify_generated_lock(path)
            self.assertTrue(record["verified"])
            self.assertEqual("58.3.0", record["arrow"]["version"])
            path.write_text(lock.replace('version = "58.3.0"', 'version = "58.2.0"'), encoding="utf-8")
            with self.assertRaisesRegex(runner.ContractError, "arrow 58.3.0"):
                runner.verify_generated_lock(path)

    def test_resident_compiler_site_requires_preoptimization_return_long(self) -> None:
        runner = self.runner
        row = {
            "mir_function": "reserved_u64_column",
            "callee": "alloc::vec::Vec::with_capacity",
            "semantic_object_type": "std::vec::Vec<u64, std::alloc::Global>",
            "callsite": 11,
            "type_id": 22,
            "module_id": 33,
            "lifetime_hint": 2,
            "lifetime_hint_confidence": 70,
            "lifetime_hint_basis": "automatic_rust_lifetime_prior_return_long",
            "rewrite_status": "actual_semantic_scope_generic_type_rewrite_applied",
            "replacement_symbol": "__unialloc_semantic_scope_push_for_rust_type_hints",
            "lowering_kind": "semantic_scope_enter_exit_rewrite",
            "lifetime_analysis_features": {
                "runtime_join_key_complete": True,
                "runtime_join_key": {
                    "callsite": 11,
                    "type_id": 22,
                    "module_id": 33,
                    "requested_size_bytes": 24_576,
                    "requested_align_bytes": 8,
                },
                "requested_layout_basis": (
                    "exact_vec_with_capacity_requested_layout"
                ),
            },
        }
        compiler_pass = {
            "marker_free_heap_preoptimization_rewrite": True,
            "actual_allocator_call_replacement_requested": False,
            "actual_semantic_scope_rewrite_requested": True,
        }
        with tempfile.TemporaryDirectory() as directory:
            audit = Path(directory) / "audit.json"
            audit.write_text(
                json.dumps(
                    {
                        "compiler_pass": compiler_pass,
                        "rewrite_candidates": [row],
                    }
                ),
                encoding="utf-8",
            )
            result = runner.validate_resident_compiler_site(Path(directory))
            self.assertEqual(11, result["callsite"])
            self.assertEqual(
                "automatic_rust_lifetime_prior_return_long",
                result["lifetime_hint_basis"],
            )
            self.assertEqual(24_576, result["requested_size"])
            audit.write_text(
                json.dumps(
                    {
                        "compiler_pass": {
                            **compiler_pass,
                            "marker_free_heap_preoptimization_rewrite": False,
                        },
                        "rewrite_candidates": [row],
                    }
                ),
                encoding="utf-8",
            )
            with self.assertRaisesRegex(runner.ContractError, "pre-optimization"):
                runner.validate_resident_compiler_site(Path(directory))

    def test_resident_compiler_runtime_join_requires_exact_numeric_match(self) -> None:
        runner = self.runner
        resident = {
            "callsite": 11,
            "type_id": 22,
            "module_id": 33,
            "requested_size": 24_576,
            "align": 8,
        }
        key = dict(resident)
        runtime = {
            **key,
            "allocation_count": 2048,
            "latest_static_prior": 2,
            "long_outcomes": 2048,
            "short_outcomes": 0,
            "censored_outcomes": 0,
        }
        row = {
            "compiler_audit_key": key,
            "identity_mode": "numeric_exact",
            "matched": True,
            "applied_prior_hint": True,
            "resolution_status": "exact_numeric_match",
            "resolved_runtime_exact_key": key,
            "runtime": runtime,
        }
        result = runner.validate_resident_compiler_runtime_join(
            {"rows": [row]}, resident
        )
        self.assertTrue(result["matched"])
        self.assertEqual(2048, result["allocation_count"])

        row["identity_mode"] = "generic_runtime_type"
        with self.assertRaisesRegex(runner.ContractError, "exact join failed"):
            runner.validate_resident_compiler_runtime_join({"rows": [row]}, resident)

    def test_dry_run_is_mechanism_only_and_records_provenance(self) -> None:
        runner = self.runner
        with tempfile.TemporaryDirectory() as directory:
            args = runner.parse_args(["--raw-dir", directory, "--dry-run"])
            value = runner.plan(args)
        self.assertFalse(args.matched_three_pairs)
        self.assertFalse(value["performance_claim"])
        self.assertEqual("matched campaign disabled", value["stages"][-1])
        self.assertEqual(24 * 1024, value["fixed_work"]["routable_buffer_bytes"])
        provenance = value["evaluator_provenance"]
        self.assertRegex(provenance["digest"], r"^[0-9a-f]{64}$")
        self.assertIn("runner", provenance["files"])
        self.assertIn("runtime_export_helper", provenance["files"])


if __name__ == "__main__":
    unittest.main()
