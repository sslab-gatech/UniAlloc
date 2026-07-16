#!/usr/bin/env python3
"""Focused contract tests for the resident Tantivy lifetime screen."""

from __future__ import annotations

import importlib.util
import json
import tempfile
import unittest
from pathlib import Path


SCRIPT = Path(__file__).with_name("lifetime_resident_tantivy.py")


def load_module():
    spec = importlib.util.spec_from_file_location("lifetime_resident_tantivy", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class LifetimeResidentTantivyTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.runner = load_module()

    def test_source_pin_and_runner_contract_are_exact(self) -> None:
        runner = self.runner
        self.assertEqual("0.26.1", runner.TANTIVY_VERSION)
        self.assertEqual(
            "d8f4c0b703120ed98f06297724dc1522df6019b9",
            runner.TANTIVY_COMMIT,
        )
        self.assertEqual(
            {
                "tantivy",
                "tantivy_stacker",
                "tantivy_common",
                "tantivy_columnar",
                "tantivy_query_grammar",
                "tantivy_bitpacker",
                "tantivy_tokenizer_api",
                "ownedbytes",
                "tantivy_sstable",
            },
            set(runner.TANTIVY_TARGET_CRATES),
        )
        source = runner.generated_runner_source()
        for required in (
            "Index::create_in_ram",
            "writer_with_num_threads(1",
            "writer.set_merge_policy(Box::new(NoMergePolicy))",
            "ReloadPolicy::Manual",
            "let reader: IndexReader",
            "let mut searcher: Searcher",
            "reader.reload()",
            "for batch_index in 1..batches",
            "generated_document",
            "Background segment merges can choose different equally-scored",
            "UNIALLOC_LIFETIME_EXPERIMENT_INITIALIZER",
            runner.RESULT_PREFIX,
            runner.campaign.MECHANISM_PREFIX,
        ):
            self.assertIn(required, source)
        self.assertNotIn("include_str!", source)
        self.assertNotIn("Command::new", source)

    def test_manifest_binds_pinned_checkout_and_frozen_allocator(self) -> None:
        runner = self.runner
        manifest = runner.generated_manifest(
            tantivy_checkout=Path("/tmp/tantivy checkout"),
            allocator_snapshot=Path("/tmp/allocator snapshot"),
        )
        self.assertIn('name = "unialloc-lifetime-resident-tantivy"', manifest)
        self.assertIn(
            f'tantivy = {{ path = {json.dumps("/tmp/tantivy checkout")} }}',
            manifest,
        )
        self.assertIn(
            f'unialloc = {{ path = {json.dumps("/tmp/allocator snapshot/unialloc")}',
            manifest,
        )
        self.assertIn('features = ["lifetime_hugepage"]', manifest)
        self.assertIn("[workspace]", manifest)

    def test_result_parser_checks_all_fixed_work_counts(self) -> None:
        runner = self.runner
        batches = 4
        documents_per_batch = 128
        queries_per_batch = 16
        record = {
            "schema_version": 1,
            "correctness": True,
            "batches": batches,
            "documents_per_batch": documents_per_batch,
            "queries_per_batch": queries_per_batch,
            "documents_indexed": batches * documents_per_batch,
            "commits": batches,
            "query_phases": batches,
            "query_evaluations": batches * queries_per_batch,
            "matched_documents": runner.expected_matched_documents(
                batches=batches,
                documents_per_batch=documents_per_batch,
                queries_per_batch=queries_per_batch,
            ),
            "final_num_docs": batches * documents_per_batch,
            "result_digest": runner.expected_correctness_digest(
                batches=batches,
                documents_per_batch=documents_per_batch,
                queries_per_batch=queries_per_batch,
            ),
            "elapsed_seconds": 1.25,
        }
        stdout = runner.RESULT_PREFIX + json.dumps(record) + "\n"
        self.assertEqual(
            record,
            runner.parse_result_record(
                stdout,
                batches=batches,
                documents_per_batch=documents_per_batch,
                queries_per_batch=queries_per_batch,
            ),
        )
        record["final_num_docs"] -= 1
        with self.assertRaisesRegex(runner.ContractError, "fixed-work result"):
            runner.parse_result_record(
                runner.RESULT_PREFIX + json.dumps(record) + "\n",
                batches=batches,
                documents_per_batch=documents_per_batch,
                queries_per_batch=queries_per_batch,
            )

    def test_correctness_digest_is_exact_and_segment_layout_independent(self) -> None:
        runner = self.runner
        arguments = {
            "batches": 216,
            "documents_per_batch": 2_048,
            "queries_per_batch": 128,
        }
        self.assertEqual(95_993_856, runner.expected_matched_documents(**arguments))
        first = runner.expected_correctness_digest(**arguments)
        second = runner.expected_correctness_digest(**arguments)
        self.assertRegex(first, r"^[0-9a-f]{16}$")
        self.assertEqual(first, second)
        self.assertEqual("ecb1db7f21ecf3df", first)

        default_arguments = {
            "batches": 256,
            "documents_per_batch": 2_048,
            "queries_per_batch": 128,
        }
        self.assertEqual(
            134_742_016, runner.expected_matched_documents(**default_arguments)
        )
        self.assertEqual(
            "92ce3840ce6583df",
            runner.expected_correctness_digest(**default_arguments),
        )

    def test_cross_arm_fixed_work_and_backing_gate_fail_closed(self) -> None:
        runner = self.runner
        expected = {
            field: index for index, field in enumerate(runner.FIXED_WORK_INVARIANT_FIELDS)
        }
        runner.validate_fixed_work_match(dict(expected), expected)
        divergent = dict(expected)
        divergent["result_digest"] = "different"
        with self.assertRaisesRegex(runner.ContractError, "fixed work diverged"):
            runner.validate_fixed_work_match(divergent, expected)

        mechanism = {"thp_advice_attempts": 0, "thp_collapse_successes": 0}
        procfs = {"sample_count": 7, "peak_anon_hugepages_kib": 0}
        rejected = runner.adaptive_thp_backing_gate(mechanism, procfs)
        self.assertFalse(rejected["passed"])
        self.assertEqual(3, len(rejected["reasons"]))
        admitted = runner.adaptive_thp_backing_gate(
            {"thp_advice_attempts": 1, "thp_collapse_successes": 1},
            {"sample_count": 7, "peak_anon_hugepages_kib": 2_048},
        )
        self.assertTrue(admitted["passed"])

    def test_mechanism_record_is_fail_closed_and_complete(self) -> None:
        campaign = self.runner.campaign
        row = {field: 0 for field in campaign.REQUIRED_MECHANISM_FIELDS}
        row["all_mappings_released"] = True
        stderr = campaign.MECHANISM_PREFIX + json.dumps(row) + "\n"
        self.assertEqual(row, campaign.parse_mechanism(stderr))
        row.pop("retained_empty_extent_reuse_hits")
        with self.assertRaisesRegex(
            campaign.CampaignContractError, "does not match schema"
        ):
            campaign.parse_mechanism(
                campaign.MECHANISM_PREFIX + json.dumps(row) + "\n"
            )

    def test_checkout_identity_fails_closed(self) -> None:
        runner = self.runner
        valid = {
            "repository": runner.TANTIVY_REPOSITORY,
            "source_ref": runner.TANTIVY_VERSION,
            "source_commit": runner.TANTIVY_COMMIT,
            "source_tree": "a" * 40,
            "remote": runner.TANTIVY_REPOSITORY,
            "status": "",
            "cargo_lock_tracked": False,
            "cargo_lock_sha256": None,
            "cargo_toml_sha256": "b" * 64,
        }
        runner.validate_checkout_identity(valid)
        for field, value in (
            ("source_commit", "0" * 40),
            ("remote", "https://example.invalid/tantivy.git"),
            ("status", " M Cargo.toml"),
            ("cargo_lock_tracked", True),
            ("cargo_toml_sha256", None),
        ):
            broken = dict(valid)
            broken[field] = value
            with self.assertRaises(runner.ContractError):
                runner.validate_checkout_identity(broken)

    def test_cli_defaults_describe_one_bounded_diagnostic_run(self) -> None:
        runner = self.runner
        with tempfile.TemporaryDirectory() as directory:
            args = runner.parse_args(["--raw-dir", directory, "--dry-run"])
        self.assertGreater(args.batches, 0)
        self.assertGreater(args.documents_per_batch, 0)
        self.assertGreater(args.queries_per_batch, 0)
        self.assertGreaterEqual(args.minimum_seconds, 20.0)
        self.assertLessEqual(args.maximum_seconds, 60.0)
        self.assertFalse(args.performance_claim)


if __name__ == "__main__":
    unittest.main()
