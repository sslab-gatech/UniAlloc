#!/usr/bin/env python3
"""Focused contract tests for the resident DataFusion lifetime screen."""

from __future__ import annotations

import importlib.util
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock


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
            "resident_payload_bytes": rows_per_table * 7 * 8,
            "resident_array_memory_bytes": rows_per_table * 7 * 8 + 1_024,
            "resident_batch_count": 4,
            "materialized_rows": rows_per_table,
            "materialized_digest": runner.expected_materialized_digest(rows_per_table),
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
            ".with_target_partitions(target_partitions)",
            ".with_batch_size(ROWS_PER_BATCH)",
            ".with_enforce_batch_size_in_joins(true)",
            "MATERIALIZE_SQL",
            "resident_rows",
            "validate_materialized",
            "JOIN dimensions",
            "GROUP BY group_id",
            "selector < 8",
            "const ROUTABLE_BUFFER_BYTES: usize = ROWS_PER_BATCH * 8",
            "assert_eq!(ROUTABLE_BUFFER_BYTES, 24 * 1024)",
            "UInt64Array::from_iter_values",
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
        self.assertNotIn("reserved_u64_column", source)
        self.assertNotIn("zeroed_u64_column", source)

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
        self.assertNotIn("unialloc =", manifest)
        self.assertIn("[workspace]", manifest)

    def test_force_load_wrapper_keeps_one_allocator_and_runner_panic_strategy(self) -> None:
        runner = self.runner
        with tempfile.TemporaryDirectory() as directory:
            path = runner.ensure_lifetime_force_load_wrapper(Path(directory) / "wrapper")
            source = path.read_text(encoding="utf-8")
        self.assertIn("UNIALLOC_FORCE_LOAD_CRATES", source)
        self.assertIn("arguments.extend([\"-L\", f\"dependency={dependency_dir}\"])", source)
        self.assertIn(f"if current_crate == {runner.RUNNER_CRATE!r}:", source)
        self.assertIn('arguments.extend(["-C", "panic=abort"])', source)

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
        rows = [
            {
                "callsite": 11 + index,
                "type_id": 22 + index,
                "module_id": 33 + index,
                "requested_size": 64 << index,
                "align": 8,
                "allocation_count": 256,
                "allocation_requested_bytes": 256 * (64 << index),
            }
            for index in range(4)
        ]
        summary = runner.validate_process_wide_routing(
            {"unsupported_layout_bypasses": 7}, rows, batches_per_table=2
        )
        self.assertEqual(1_024, summary["allocation_count"])
        self.assertEqual(7, summary["unsupported_layout_bypasses"])
        self.assertEqual(4, summary["site_count"])
        with self.assertRaisesRegex(runner.ContractError, "fewer than four"):
            runner.validate_process_wide_routing(
                {"unsupported_layout_bypasses": 0}, rows[:3], batches_per_table=2
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
        self.assertEqual(4, len(rejected["reasons"]))
        admitted = runner.adaptive_thp_backing_gate(
            {"thp_advice_attempts": 2, "thp_collapse_successes": 1},
            [
                {
                    "elapsed_seconds": 0.5,
                    "rss_kib": 100,
                    "pss_kib": 90,
                    "anonymous_kib": 80,
                    "anon_hugepages_kib": 2_048,
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
        self.assertEqual(2, admitted["positive_backing_sample_count"])
        partial = runner.adaptive_thp_backing_gate(
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
        self.assertFalse(partial["passed"])

    def test_each_measured_process_has_an_independent_backing_gate(self) -> None:
        runner = self.runner
        ordinary = [{"anon_hugepages_kib": 0}, {"anon_hugepages_kib": 0}]
        thp = [{"anon_hugepages_kib": 2_048}, {"anon_hugepages_kib": 4_096}]
        ordinary_mechanism = {
            "thp_advice_attempts": 0,
            "thp_collapse_successes": 0,
            "thp_extent_mappings": 0,
        }
        thp_mechanism = {
            "thp_advice_attempts": 2,
            "thp_collapse_successes": 1,
        }
        admitted = runner.measured_pair_backing_gate(
            ordinary,
            thp,
            ordinary_mechanism=ordinary_mechanism,
            thp_mechanism=thp_mechanism,
        )
        self.assertTrue(admitted["passed"])
        self.assertEqual(0, admitted["ordinary_positive_backing_sample_count"])
        self.assertEqual(2, admitted["thp_positive_backing_sample_count"])
        self.assertFalse(
            runner.measured_pair_backing_gate(
                ordinary,
                thp,
                ordinary_mechanism=ordinary_mechanism,
                thp_mechanism={},
            )["passed"]
        )
        contaminated = [{"anon_hugepages_kib": 2_048}]
        self.assertFalse(
            runner.measured_pair_backing_gate(
                contaminated,
                thp,
                ordinary_mechanism=ordinary_mechanism,
                thp_mechanism=thp_mechanism,
            )["passed"]
        )
        absent = [{"anon_hugepages_kib": 0}]
        self.assertFalse(
            runner.measured_pair_backing_gate(
                ordinary,
                absent,
                ordinary_mechanism=ordinary_mechanism,
                thp_mechanism=thp_mechanism,
            )["passed"]
        )

    def test_three_pair_screen_only_unlocks_a_backing_mechanism_contrast(self) -> None:
        runner = self.runner
        fixed_work = self.result_record()
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            ordinary_samples = root / "ordinary.json"
            thp_samples = root / "thp.json"
            ordinary_samples.write_text(
                json.dumps([{"anon_hugepages_kib": 0}]), encoding="utf-8"
            )
            thp_samples.write_text(
                json.dumps([{"anon_hugepages_kib": 2_048}]), encoding="utf-8"
            )

            def fake_run_one(*, arm_name: str, **_kwargs):
                is_thp = arm_name == "adaptive-selective-thp-compiler-prior"
                return {
                    "fixed_work": fixed_work,
                    "wall_seconds": 3.0,
                    "procfs": {"peak_rss_kib": 200 if is_thp else 100},
                    "mechanism": {
                        "thp_advice_attempts": 1 if is_thp else 0,
                        "thp_collapse_successes": 1 if is_thp else 0,
                        "thp_extent_mappings": 1 if is_thp else 0,
                    },
                    "smaps_samples_path": str(
                        thp_samples if is_thp else ordinary_samples
                    ),
                }

            with mock.patch.object(runner, "run_one", side_effect=fake_run_one):
                result = runner.run_matched_pairs(
                    raw_dir=root,
                    build={},
                    expected_fixed_work=fixed_work,
                    args=SimpleNamespace(
                        batches_per_table=2,
                        query_iterations=3,
                        target_partitions=2,
                        minimum_seconds=0.0,
                        maximum_seconds=10.0,
                        timeout=10,
                        sample_interval=0.1,
                    ),
                )
        self.assertTrue(result["mechanism_contrast_backing_eligible"])
        self.assertFalse(result["performance_claim_eligible"])
        self.assertFalse(result["presentation_claim_eligible"])

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
            self.assertEqual(0, record["cargo_graph_unialloc_package_count"])
            path.write_text(
                lock + '\n[[package]]\nname = "unialloc"\nversion = "0.1.0"\n',
                encoding="utf-8",
            )
            with self.assertRaisesRegex(runner.ContractError, "force-loaded rlib"):
                runner.verify_generated_lock(path)
            path.write_text(
                lock.replace('version = "58.3.0"', 'version = "58.2.0"'),
                encoding="utf-8",
            )
            with self.assertRaisesRegex(runner.ContractError, "arrow 58.3.0"):
                runner.verify_generated_lock(path)

    def test_dependency_compiler_scope_excludes_generated_runner(self) -> None:
        runner = self.runner
        audit = {"audited_crates": list(runner.TARGET_CRATES)}
        sites = {
            "site_count": 12,
            "applied_prior_hinted_count": 5,
            "complete_join_key_count": 4,
        }
        result = runner.validate_dependency_compiler_scope(audit, sites)
        self.assertEqual(len(runner.TARGET_CRATES), result["target_crate_count"])
        self.assertTrue(result["generated_runner_excluded"])
        with self.assertRaisesRegex(runner.ContractError, "exactly match"):
            runner.validate_dependency_compiler_scope(
                {"audited_crates": [*runner.TARGET_CRATES, runner.RUNNER_CRATE]}, sites
            )

    def test_dependency_compiler_runtime_join_requires_an_executed_match(self) -> None:
        runner = self.runner
        runtime = {
            "allocation_count": 2048,
            "long_outcomes": 2048,
            "short_outcomes": 0,
            "censored_outcomes": 0,
        }
        row = {
            "matched": True,
            "applied_prior_hint": True,
            "runtime": runtime,
        }
        exact_join = {
            "rows": [row],
            "static_prior_join_success": True,
            "matched_applied_prior_site_count": 1,
        }
        result = runner.validate_dependency_compiler_runtime_join(exact_join)
        self.assertEqual(1, result["matched_site_count"])
        self.assertEqual(2048, result["allocation_count"])
        with self.assertRaisesRegex(runner.ContractError, "static-prior"):
            runner.validate_dependency_compiler_runtime_join(
                {
                    "rows": [{**row, "matched": False}],
                    "static_prior_join_success": False,
                    "matched_applied_prior_site_count": 0,
                }
            )

    def test_fixed_work_uses_logical_results_not_scheduler_batching(self) -> None:
        runner = self.runner
        expected = self.result_record()
        observed = dict(expected)
        observed["resident_array_memory_bytes"] = (
            int(expected["resident_array_memory_bytes"]) + 4096
        )
        observed["resident_batch_count"] = int(expected["resident_batch_count"]) + 2
        runner.validate_fixed_work_match(observed, expected)
        observed["result_digest"] = "0" * 16
        with self.assertRaisesRegex(runner.ContractError, "fixed work"):
            runner.validate_fixed_work_match(observed, expected)

    def test_query_window_intersects_the_two_phase_clocks(self) -> None:
        runner = self.runner
        samples = [
            {"elapsed_seconds": 0.6},
            {"elapsed_seconds": 0.8},
            {"elapsed_seconds": 2.4},
            {"elapsed_seconds": 2.6},
        ]
        selected, bounds = runner.conservative_query_window_samples(
            samples,
            fixed_work=self.result_record(),
            process_wall_seconds=2.7,
        )
        self.assertEqual([0.8, 2.4], [row["elapsed_seconds"] for row in selected])
        self.assertAlmostEqual(0.7, bounds["conservative_start_seconds"])
        self.assertAlmostEqual(2.5, bounds["conservative_end_seconds"])

    def test_build_directories_drop_stale_injected_inputs(self) -> None:
        runner = self.runner
        with tempfile.TemporaryDirectory() as directory:
            roots = [Path(directory) / "audit", Path(directory) / "target"]
            for root in roots:
                root.mkdir()
                (root / "stale-sentinel").write_text("stale", encoding="utf-8")
            runner.prepare_fresh_build_directories(roots)
            self.assertTrue(all(root.is_dir() for root in roots))
            self.assertTrue(all(not any(root.iterdir()) for root in roots))

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
