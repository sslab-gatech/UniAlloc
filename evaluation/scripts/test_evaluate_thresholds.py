#!/usr/bin/env python3
"""Regression tests for paper-equivalent-or-better claim thresholds.

These tests intentionally exercise the user-facing acceptance policy:
current results may beat the paper, but they must not be worse than the
paper's worst acceptable boundary.
"""

from __future__ import annotations

import importlib.util
import argparse
import contextlib
import hashlib
import io
import json
import pathlib
import sys
import tempfile
import unittest
from unittest import mock


ROOT = pathlib.Path(__file__).resolve().parents[2]
EVALUATE_PATH = ROOT / "evaluation" / "scripts" / "evaluate.py"


spec = importlib.util.spec_from_file_location("unialloc_evaluate", EVALUATE_PATH)
assert spec is not None and spec.loader is not None
evaluate = importlib.util.module_from_spec(spec)
spec.loader.exec_module(evaluate)


def source_fingerprint_marker_fields(source_fingerprint: dict | None = None) -> str:
    fingerprint = source_fingerprint or evaluate.repository_source_fingerprint()
    return (
        f"schema_version={fingerprint.get('schema_version')} "
        f"algorithm={fingerprint.get('algorithm')} "
        f"source_digest={fingerprint.get('source_digest')}"
    )


def rpolars_test_source_contract() -> dict:
    """Minimal exact-paper provenance contract for claim-grade R-Polars unit fixtures."""
    return {
        "paper_exact_ref_complete": True,
        "claim_grade_complete": True,
        "exact_checkout_pin_complete": True,
        "checkout_pin": {"pinned": True, "exact_pinned": True},
        "blockers": [],
    }


def rpolars_zero_row_input_provenance(csv_path: pathlib.Path) -> dict:
    """Build real provenance for a tiny header-only CSV fixture.

    The production gate expects the canonical db-benchmark CSV shape and verifies
    the file hash/row count itself.  Tests patch the canonical row parser to zero
    where a 10M-row fixture would be wasteful.
    """
    csv_digest = hashlib.sha256(csv_path.read_bytes()).hexdigest()
    return {
        "source": "rpolars-input-provenance-audit",
        "required_for_targets": True,
        "claim_grade_ready": True,
        "blockers": [],
        "expected_source": evaluate.RPOLARS_DB_BENCHMARK_SOURCE_ID,
        "expected_csv_name": evaluate.RPOLARS_DB_BENCHMARK_DEFAULT_CSV,
        "expected_datagen_args": {"n_rows": 0},
        "input_contract": {
            "source": evaluate.RPOLARS_DB_BENCHMARK_SOURCE_ID,
            "expected_csv_name": evaluate.RPOLARS_DB_BENCHMARK_DEFAULT_CSV,
            "expected_rows": 0,
            "csv_sha256": csv_digest,
        },
        "db_benchmark_source": {"source": evaluate.RPOLARS_DB_BENCHMARK_SOURCE_ID},
        "configured_csv": {
            "exists": True,
            "missing_columns": [],
            "resolved_path": str(csv_path),
            "configured_csv_src": str(csv_path),
            "basename": csv_path.name,
            "data_row_count": 0,
            "sha256": csv_digest,
        },
    }


class MixedJsonlSampleReadTests(unittest.TestCase):
    def test_sample_entries_tolerate_retained_benchmark_stdout(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            samples = pathlib.Path(tmp) / "mixed.jsonl"
            samples.write_text(
                "\n".join(
                    [
                        json.dumps(
                            {
                                "source": "paper-external-cargo-bench-json",
                                "success": True,
                                "dataset": "default_performance",
                                "benchmark": "R-Oxipng*",
                                "allocator": "unialloc",
                                "run_index": 1,
                                "seconds": 0.01,
                            }
                        ),
                        "test deflate_8_bits_strategy_0 ... bench:  11,719,045 ns/iter (+/- 1)",
                        json.dumps(
                            {
                                "source": "paper-external-workload-adapter",
                                "success": True,
                                "dataset": "default_performance",
                                "benchmark": "R-Oxipng*",
                                "allocator": "unialloc",
                                "run_index": 1,
                                "seconds": 0.01,
                            }
                        ),
                    ]
                )
                + "\n",
                encoding="utf-8",
            )

            entries, paths, errors = evaluate.read_rpolars_sample_entries(None, [samples])

        self.assertEqual(errors, [])
        self.assertEqual(paths, [samples])
        self.assertEqual(len(entries), 2)
        self.assertEqual(entries[0][0]["source"], "paper-external-cargo-bench-json")

    def test_sample_entries_accept_claim_matrix_sample_paths(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            first = pathlib.Path(tmp) / "first.jsonl"
            second = pathlib.Path(tmp) / "second.jsonl"
            first.write_text(json.dumps({"source": "a", "success": True}) + "\n", encoding="utf-8")
            second.write_text(json.dumps({"source": "b", "success": True}) + "\n", encoding="utf-8")

            entries, paths, errors = evaluate.read_rpolars_sample_entries(
                {"sample_paths": [str(first), str(second), str(first)]},
                [],
            )

        self.assertEqual(errors, [])
        self.assertEqual(paths, [first, second])
        self.assertEqual([entry[0]["source"] for entry in entries], ["a", "b"])

    def test_rpolars_parity_and_matrix_share_default_sample_audit(self) -> None:
        default_path = evaluate.rpolars_default_claim_matrix_sample_audit_path()

        self.assertEqual(default_path, evaluate.RPOLARS_CLAIM_MATRIX_SAMPLE_AUDIT)
        self.assertIn(
            "combined_libtest_csv_groupby_collect_take_sort_plus_six_allocators",
            default_path.name,
        )

    def test_rpolars_claim_grade_sample_rejects_missing_provenance_and_non_cargo(self) -> None:
        sample = {
            "source": "paper-external-workload-adapter",
            "success": True,
            "dataset": "default_performance",
            "benchmark": "R-Polars",
            "allocator": "unialloc",
            "run_index": 1,
            "seconds": 1.0,
            "benchmark_owned_json": True,
            "claim_grade": True,
            "metric_metadata": {
                "source": "paper-external-cargo-bench-json",
                "benchmark": "R-Polars",
                "claim_grade": True,
                "benchmark_owned_json": True,
                "command": [sys.executable, "-c", "print('fake')"],
                "target_records": [
                    {
                        "cargo_bench_target": "bench",
                        "command": [sys.executable, "-c", "print('fake')"],
                    }
                ],
            },
        }

        blockers = evaluate.sample_claim_grade_blockers(sample)

        self.assertIn("R-Polars claim-grade sample lacks embedded input provenance", blockers)
        self.assertIn("R-Polars claim-grade sample target command is not cargo bench", blockers)

    def test_rpolars_sample_without_explicit_claim_grade_contract_is_not_claim_usable(self) -> None:
        sample = {
            "source": "paper-external-cargo-bench-json",
            "success": True,
            "dataset": "default_performance",
            "benchmark": "R-Polars",
            "allocator": "unialloc",
            "run_index": 1,
            "seconds": 0.001,
            "benchmark_owned_json": True,
            "measurement_source": "libtest_bench_stdout",
            "host_system": "Linux",
            "source_contract": {
                "paper_exact_ref_complete": True,
                "claim_grade_complete": True,
                "exact_checkout_pin_complete": True,
                "checkout_pin": {"pinned": True, "exact_pinned": True},
                "blockers": [],
            },
        }

        blockers = evaluate.sample_claim_grade_blockers(sample)

        self.assertIn("R-Polars sample lacks explicit child claim_grade=true", blockers)
        self.assertIn("R-Polars sample lacks child claim_grade_contract", blockers)

    def test_source_contract_missing_exact_pin_fields_fails_closed(self) -> None:
        blockers = evaluate.source_contract_claim_grade_blockers(
            {
                "source_contract": {
                    "paper_exact_ref_complete": True,
                    "claim_grade_complete": True,
                }
            }
        )

        self.assertIn(
            "sample source contract is paper-exact metadata but not claim-grade exact-checkout provenance",
            blockers,
        )
        self.assertIn("sample source checkout is not pinned to the exact paper ref", blockers)

    def test_rpolars_claim_grade_sample_accepts_embedded_real_surface_metadata(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp)
            checkout = root / "polars"
            benches = checkout / "polars" / "benches"
            benches.mkdir(parents=True)
            bench_entries = []
            for name in ["bench", "csv", "groupby", "collect", "take", "sort"]:
                (benches / f"{name}.rs").write_text("// bench\n", encoding="utf-8")
                bench_entries.append(f'[[bench]]\nname = "{name}"\npath = "benches/{name}.rs"\n')
            (checkout / "polars" / "Cargo.toml").write_text(
                '[package]\nname = "polars"\nversion = "0.13.0"\nedition = "2021"\n\n'
                + "\n".join(bench_entries),
                encoding="utf-8",
            )
            csv_path = root / evaluate.RPOLARS_DB_BENCHMARK_DEFAULT_CSV
            csv_path.write_text(
                ",".join(evaluate.RPOLARS_GROUPBY_REQUIRED_CSV_COLUMNS) + "\n",
                encoding="utf-8",
            )
            provenance = rpolars_zero_row_input_provenance(csv_path)
            source_contract = rpolars_test_source_contract()
            target_records = [
                {
                    "cargo_bench_target": name,
                    "command": ["cargo", "bench", "--package", "polars", "--bench", name],
                }
                for name in ["bench", "csv", "groupby", "collect", "take", "sort"]
            ]
            sample = {
                "source": "paper-external-workload-adapter",
                "success": True,
                "dataset": "default_performance",
                "benchmark": "R-Polars",
                "allocator": "unialloc",
                "run_index": 1,
                "seconds": 1.0,
                "benchmark_owned_json": True,
                "claim_grade": True,
                "command": ["python3", "wrapper.py", "--real-workload-dir", str(checkout)],
                "metric_metadata": {
                    "source": "paper-external-cargo-bench-json",
                    "benchmark": "R-Polars",
                    "claim_grade": True,
                    "benchmark_owned_json": True,
                    "dependency_env": {"csv_src": str(csv_path)},
                    "rpolars_input_provenance": provenance,
                    "source_contract": source_contract,
                    "source_provenance_class": "paper_exact_ref",
                    "cargo_bench_targets": ["bench", "csv", "groupby", "collect", "take", "sort"],
                    "target_records": target_records,
                    "claim_grade_contract": {
                        "mode": "rpolars-paper-run",
                        "claim_grade_ready": True,
                        "blockers": [],
                        "expected_cargo_bench_targets": ["bench", "csv", "groupby", "collect", "take", "sort"],
                        "rpolars_input_provenance": provenance,
                        "source_contract": source_contract,
                    },
                },
            }

            with mock.patch.object(evaluate, "rpolars_parse_db_benchmark_csv_name", return_value={"n_rows": 0}):
                blockers = evaluate.rpolars_sample_claim_grade_blockers(sample)

        self.assertEqual(blockers, [])

    def test_rpolars_claim_matrix_prefers_claim_grade_rerun_over_diagnostic_duplicate(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = pathlib.Path(tmp)
            evidence_path = tmp_path / "stdout.txt"
            evidence_path.write_text("test bench_unit ... bench:  1,234 ns/iter (+/- 56)\n", encoding="utf-8")
            digest = hashlib.sha256(evidence_path.read_bytes()).hexdigest()
            polars_benches = tmp_path / "polars" / "benches"
            polars_benches.mkdir(parents=True)
            (polars_benches / "bench.rs").write_text("#[bench]\nfn bench_unit() {}\n", encoding="utf-8")
            (tmp_path / "polars" / "Cargo.toml").write_text(
                '[package]\nname = "polars"\nversion = "0.13.0"\nedition = "2021"\n\n'
                '[[bench]]\nname = "bench"\npath = "benches/bench.rs"\n',
                encoding="utf-8",
            )
            csv_path = tmp_path / evaluate.RPOLARS_DB_BENCHMARK_DEFAULT_CSV
            csv_path.write_text(",".join(evaluate.RPOLARS_GROUPBY_REQUIRED_CSV_COLUMNS) + "\n", encoding="utf-8")
            input_provenance = rpolars_zero_row_input_provenance(csv_path)
            source_contract = rpolars_test_source_contract()
            base = {
                "source": "paper-external-cargo-bench-json",
                "success": True,
                "dataset": "default_performance",
                "benchmark": "R-Polars",
                "allocator": "unialloc",
                "run_index": 1,
                "seconds": 0.001,
                "ns_per_iter": 1234.0,
                "benchmark_owned_json": True,
                "measurement_source": "libtest_bench_stdout",
                "command": ["cargo", "bench", "--bench", "bench"],
                "real_workload_dir": str(tmp_path),
                "host_system": "Linux",
                "evidence": [{"path": str(evidence_path), "sha256": digest}],
                "bench_rows": [
                    {
                        "name": "bench_unit",
                        "ns_per_iter": 1234.0,
                        "measurement_source": "libtest_bench_stdout",
                        "cargo_bench_target": "bench",
                    }
                ],
                "cargo_bench_targets": ["bench"],
                "dependency_env": {"csv_src": str(csv_path)},
                "rpolars_input_provenance": input_provenance,
                "claim_grade_contract": {
                    "mode": "rpolars-paper-run",
                    "claim_grade_ready": True,
                    "blockers": [],
                    "expected_cargo_bench_targets": ["bench"],
                    "rpolars_input_provenance": input_provenance,
                },
                "source_contract": source_contract,
                "source_provenance_class": "paper_exact_ref",
            }
            old_diagnostic = {
                **base,
                "claim_grade": False,
                "claim_grade_blockers": ["old adapter diagnostic duplicate"],
            }
            new_claim_grade = {
                **base,
                "claim_grade": True,
                "claim_grade_scope": "rpolars-paper-equivalent-matrix-run",
                "claim_grade_blockers": [],
            }
            samples = tmp_path / "samples.jsonl"
            samples.write_text(
                json.dumps(old_diagnostic) + "\n" + json.dumps(new_claim_grade) + "\n",
                encoding="utf-8",
            )
            cfg = {"methodology": {"runs_per_benchmark": 1}}
            surfaces = {
                "cargo_bench_targets": [{"name": "bench"}],
                "libtest_bench_functions": ["bench_unit"],
                "bench_files": [],
            }
            required_cell = {
                "dataset": "default_performance",
                "benchmark": "R-Polars",
                "allocator": "unialloc",
                "role": "paper",
            }

            with (
                mock.patch.object(evaluate, "required_performance_sample_cells", return_value=([required_cell], [])),
                mock.patch.object(evaluate, "rpolars_parse_db_benchmark_csv_name", return_value={"n_rows": 0}),
            ):
                audit = evaluate.build_rpolars_claim_matrix_audit(
                    cfg=cfg,
                    paper_dir=tmp_path,
                    config={"claim_grade": True},
                    config_path=tmp_path / "workload_config.local.json",
                    checkout=tmp_path,
                    surfaces=surfaces,
                    sample_audit={"sample_path": str(samples)},
                    sample_audit_path=samples,
                    dataset_names=["default_performance"],
                    required_runs=1,
                )

        self.assertEqual(audit["summary"]["claim_usable_cell_count"], 1, audit)
        observed_run = audit["cells"][0]["observed_runs"][0]
        self.assertTrue(observed_run["claim_usable"], observed_run)
        self.assertEqual(observed_run["effective_claim_usable_record_count"], 1, observed_run)
        self.assertEqual(observed_run["effective_issues"], [], observed_run)

    def test_rpolars_claim_matrix_ignores_stale_wildcard_when_leaf_rerun_exists(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = pathlib.Path(tmp)
            evidence_path = tmp_path / "stdout.txt"
            evidence_path.write_text("test bench_unit ... bench:  1,234 ns/iter (+/- 56)\n", encoding="utf-8")
            digest = hashlib.sha256(evidence_path.read_bytes()).hexdigest()
            polars_benches = tmp_path / "polars" / "benches"
            polars_benches.mkdir(parents=True)
            (polars_benches / "bench.rs").write_text("#[bench]\nfn bench_unit() {}\n", encoding="utf-8")
            (tmp_path / "polars" / "Cargo.toml").write_text(
                '[package]\nname = "polars"\nversion = "0.13.0"\nedition = "2021"\n\n'
                '[[bench]]\nname = "bench"\npath = "benches/bench.rs"\n',
                encoding="utf-8",
            )
            csv_path = tmp_path / evaluate.RPOLARS_DB_BENCHMARK_DEFAULT_CSV
            csv_path.write_text(",".join(evaluate.RPOLARS_GROUPBY_REQUIRED_CSV_COLUMNS) + "\n", encoding="utf-8")
            input_provenance = rpolars_zero_row_input_provenance(csv_path)
            source_contract = rpolars_test_source_contract()
            old_wildcard_diagnostic = {
                "source": "paper-external-cargo-bench-json",
                "success": True,
                "dataset": "default_performance",
                "benchmark": "R-Polars",
                "allocator": "unialloc",
                "run_index": 1,
                "seconds": 0.002,
                "benchmark_owned_json": True,
                "measurement_source": "wall_seconds",
                "command": ["cargo", "bench", "--bench", "bench"],
                "real_workload_dir": str(tmp_path),
                "cargo_bench_targets": ["bench"],
                "dependency_env": {"csv_src": str(csv_path)},
                "rpolars_input_provenance": input_provenance,
                "claim_grade_contract": {
                    "mode": "rpolars-paper-run",
                    "claim_grade_ready": True,
                    "blockers": [],
                    "expected_cargo_bench_targets": ["bench"],
                    "rpolars_input_provenance": input_provenance,
                },
                "host_system": "Linux",
                "claim_grade": False,
                "claim_grade_blockers": ["old target-level diagnostic wildcard"],
                "evidence": [{"path": str(evidence_path), "sha256": digest}],
            }
            new_claim_grade_leaf = {
                **old_wildcard_diagnostic,
                "seconds": 0.001,
                "measurement_source": "libtest_bench_stdout",
                "bench_rows": [
                    {
                        "name": "bench_unit",
                        "ns_per_iter": 1234.0,
                        "measurement_source": "libtest_bench_stdout",
                        "cargo_bench_target": "bench",
                    }
                ],
                "claim_grade": True,
                "claim_grade_scope": "rpolars-paper-equivalent-matrix-run",
                "claim_grade_blockers": [],
                "source_contract": source_contract,
                "source_provenance_class": "paper_exact_ref",
            }
            samples = tmp_path / "samples.jsonl"
            samples.write_text(
                json.dumps(old_wildcard_diagnostic) + "\n" + json.dumps(new_claim_grade_leaf) + "\n",
                encoding="utf-8",
            )
            surfaces = {
                "cargo_bench_targets": [{"name": "bench"}],
                "libtest_bench_functions": ["bench_unit"],
                "bench_files": [],
            }
            required_cell = {
                "dataset": "default_performance",
                "benchmark": "R-Polars",
                "allocator": "unialloc",
                "role": "paper",
            }

            with (
                mock.patch.object(evaluate, "required_performance_sample_cells", return_value=([required_cell], [])),
                mock.patch.object(evaluate, "rpolars_parse_db_benchmark_csv_name", return_value={"n_rows": 0}),
            ):
                audit = evaluate.build_rpolars_claim_matrix_audit(
                    cfg={"methodology": {"runs_per_benchmark": 1}},
                    paper_dir=tmp_path,
                    config={"claim_grade": True},
                    config_path=tmp_path / "workload_config.local.json",
                    checkout=tmp_path,
                    surfaces=surfaces,
                    sample_audit={"sample_path": str(samples)},
                    sample_audit_path=samples,
                    dataset_names=["default_performance"],
                    required_runs=1,
                )

        observed_run = audit["cells"][0]["observed_runs"][0]
        self.assertTrue(observed_run["claim_usable"], observed_run)
        self.assertEqual(observed_run["effective_issues"], [], observed_run)
        self.assertEqual(
            [record["fragment_key"] for record in observed_run["effective_fragment_records"]],
            ["bench|libtest|bench_unit"],
            observed_run,
        )


class PaperPerformancePlanClaimGradeGateTests(unittest.TestCase):
    def test_sample_claim_grade_true_with_not_claim_grade_reason_is_blocked(self) -> None:
        sample = {
            "source": "paper-external-cargo-bench-json",
            "success": True,
            "dataset": "default_performance",
            "benchmark": "RJS-Compiler",
            "allocator": "unialloc",
            "run_index": 1,
            "seconds": 0.001,
            "benchmark_owned_json": True,
            "claim_grade": True,
            "not_claim_grade_reason": "representative parser selector only",
        }

        blockers = evaluate.sample_claim_grade_blockers(sample)

        self.assertIn(
            "sample claims claim_grade=true but still declares not_claim_grade_reason: representative parser selector only",
            blockers,
        )

    def test_plan_audit_rejects_claim_grade_config_with_bench_filter_and_non_claim_reason(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = pathlib.Path(tmp)
            workload = {
                "dataset": "default_performance",
                "benchmark": "RJS-Compiler",
                "allocator": "unialloc",
                "runs": 1,
                "cwd": str(tmp_path),
                "measurement": "stdout_json",
                "time_field": "seconds",
                "claim_grade": True,
                "not_claim_grade_reason": "focused parser sample is not the full paper row",
                "command": [
                    "python3",
                    "evaluation/scripts/paper_external_cargo_bench_json.py",
                    "--dataset",
                    "default_performance",
                    "--benchmark",
                    "RJS-Compiler",
                    "--allocator",
                    "unialloc",
                    "--run-index",
                    "{run_index}",
                    "--bench-name-filter",
                    "parser",
                    "--",
                    "cargo",
                    "bench",
                    "--package",
                    "swc",
                    "--bench",
                    "typescript",
                    "parser",
                ],
            }
            required_cell = {
                "dataset": "default_performance",
                "benchmark": "RJS-Compiler",
                "allocator": "unialloc",
                "role": "paper",
            }

            audited = evaluate.audit_plan_workload(workload, 1, 1)
            with mock.patch.object(evaluate, "required_performance_sample_cells", return_value=([required_cell], [])):
                audit = evaluate.build_paper_performance_plan_audit(
                    cfg={"methodology": {"runs_per_benchmark": 1}},
                    paper_dir=tmp_path,
                    plan={"workloads": [workload]},
                    dataset_names=["default_performance"],
                    default_runs=1,
                    required_runs=1,
                )

        self.assertTrue(
            any("bench-filtered workload is not full paper-row evidence: parser" in issue for issue in audited["issues"]),
            audited,
        )
        self.assertTrue(
            any(
                "workload declares non-claim-grade evidence: focused parser sample is not the full paper row" in issue
                for issue in audited["issues"]
            ),
            audited,
        )
        self.assertFalse(audit["summary"]["ready_for_claim_grade_import"], audit)
        self.assertEqual(audit["summary"]["invalid_workload_count"], 1, audit)
        self.assertEqual(audit["summary"]["missing_cell_count"], 0, audit)
        self.assertEqual(audit["summary"]["insufficient_cell_count"], 0, audit)
        self.assertEqual(audit["summary"]["non_claim_grade_subset_workload_count"], 1, audit)

    def test_plan_audit_allows_explicit_strict_roxipng_leaf_fragment(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = pathlib.Path(tmp)
            workload = {
                "dataset": "type_isolation",
                "benchmark": "R-Oxipng*",
                "allocator": "jemalloc",
                "runs": 1,
                "cwd": str(tmp_path),
                "measurement": "stdout_json",
                "time_field": "seconds",
                "claim_grade": True,
                "claim_grade_scope": "roxipng-paper-equivalent-matrix-fragment",
                "split_libtest_bench_function": "zopfli_16_bits_strategy_0",
                "command": [
                    "python3",
                    "evaluation/scripts/paper_external_cargo_bench_json.py",
                    "--dataset",
                    "type_isolation",
                    "--benchmark",
                    "R-Oxipng*",
                    "--allocator",
                    "jemalloc",
                    "--run-index",
                    "{run_index}",
                    "--claim-grade-contract",
                    "roxipng-paper-fragment",
                    "--bench-name-filter",
                    "zopfli_16_bits_strategy_0",
                    "--",
                    "cargo",
                    "+nightly-2022-07-01",
                    "bench",
                    "--bench",
                    "deflate",
                    "zopfli_16_bits_strategy_0",
                ],
            }

            audited = evaluate.audit_plan_workload(workload, 1, 1)

        self.assertEqual(audited["issues"], [], audited)

    def test_specialized_rpolars_plan_scope_does_not_require_unrelated_rows(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = pathlib.Path(tmp)
            required_cells = [
                {
                    "dataset": "default_performance",
                    "benchmark": "Collections",
                    "allocator": "unialloc",
                    "role": "paper",
                },
                {
                    "dataset": "default_performance",
                    "benchmark": "R-Polars",
                    "allocator": "unialloc",
                    "role": "paper",
                },
            ]
            plan = {
                "source": "rpolars-paper-plan",
                "workloads": [
                    {
                        "dataset": "default_performance",
                        "benchmark": "R-Polars",
                        "allocator": "unialloc",
                        "runs": 6,
                        "cwd": str(tmp_path),
                        "measurement": "stdout_json",
                        "time_field": "seconds",
                        "claim_grade": True,
                        "claim_grade_scope": "rpolars-paper-equivalent-matrix-run",
                        "command": [
                            "python3",
                            "evaluation/scripts/paper_external_cargo_bench_json.py",
                            "--dataset",
                            "default_performance",
                            "--benchmark",
                            "R-Polars",
                            "--allocator",
                            "unialloc",
                            "--run-index",
                            "{run_index}",
                            "--claim-grade-contract",
                            "rpolars-paper-run",
                            "--cargo-bench-targets",
                            "bench,csv,groupby,collect,take,sort",
                            "--expected-cargo-bench-targets",
                            "bench,csv,groupby,collect,take,sort",
                            "--",
                            "cargo",
                            "bench",
                        ],
                    }
                ],
            }

            with mock.patch.object(evaluate, "required_performance_sample_cells", return_value=(required_cells, [])):
                audit = evaluate.build_paper_performance_plan_audit(
                    cfg={"methodology": {"runs_per_benchmark": 6}},
                    paper_dir=tmp_path,
                    plan=plan,
                    dataset_names=["default_performance"],
                    default_runs=6,
                    required_runs=6,
                    benchmark_names=["R-Polars"],
                    allocator_names=["unialloc"],
                )

        self.assertTrue(audit["summary"]["ready_for_claim_grade_import"], audit)
        self.assertEqual(audit["summary"]["required_cell_count"], 1, audit)
        self.assertEqual(audit["summary"]["unscoped_required_cell_count"], 2, audit)
        self.assertEqual(audit["summary"]["missing_cell_count"], 0, audit)
        self.assertEqual(
            audit["summary"]["required_cell_scope"]["benchmarks"],
            ["R-Polars"],
            audit,
        )
        self.assertEqual(
            audit["summary"]["required_cell_scope"]["allocators"],
            ["unialloc"],
            audit,
        )

    def test_unknown_cargo_bench_bridge_does_not_borrow_rpolars_contract(self) -> None:
        config = {
            "source": "paper-external-workload-cargo-bench-json-wrapper",
            "benchmarks": ["actix"],
            "cargo_bench_json_wrapper": {
                "child_command_template": ["cargo", "bench", "--package", "actix-http"],
            },
        }
        command = [
            sys.executable,
            "evaluation/scripts/paper_external_cargo_bench_json.py",
            "--",
            "cargo",
            "bench",
            "--package",
            "actix-http",
            "--bench",
            "date-formatting",
        ]

        blockers = evaluate.external_adapter_config_claim_grade_runner_blockers(
            config,
            command,
            pathlib.Path("/tmp/nonexistent-actix-checkout"),
        )

        self.assertIn(
            "cargo-bench JSON wrapper config has no registered benchmark-specific full-row "
            "claim-grade contract for benchmark(s): actix",
            blockers,
        )
        self.assertFalse(
            any("expected rpolars-paper-run" in blocker for blocker in blockers),
            blockers,
        )

    def test_rpolars_cargo_bench_bridge_still_requires_rpolars_contract(self) -> None:
        config = {
            "source": "paper-external-workload-cargo-bench-json-wrapper",
            "benchmarks": ["R-Polars"],
            "cargo_bench_json_wrapper": {
                "child_command_template": ["cargo", "bench", "--package", "polars"],
            },
        }
        command = [
            sys.executable,
            "evaluation/scripts/paper_external_cargo_bench_json.py",
            "--",
            "cargo",
            "bench",
            "--package",
            "polars",
        ]

        blockers = evaluate.external_adapter_config_claim_grade_runner_blockers(
            config,
            command,
            pathlib.Path("/tmp/nonexistent-polars-checkout"),
        )

        self.assertIn(
            "cargo-bench JSON wrapper config does not request the required full-row "
            "claim-grade contract (expected rpolars-paper-run)",
            blockers,
        )

    def test_mixed_unknown_cargo_bench_bridge_cannot_borrow_rpolars_contract(self) -> None:
        config = {
            "source": "paper-external-workload-cargo-bench-json-wrapper",
            "benchmarks": ["actix", "R-Polars"],
            "cargo_bench_json_wrapper": {
                "child_command_template": ["cargo", "bench", "--package", "actix-http"],
            },
        }
        command = [
            sys.executable,
            "evaluation/scripts/paper_external_cargo_bench_json.py",
            "--claim-grade-contract",
            "rpolars-paper-run",
            "--",
            "cargo",
            "bench",
            "--package",
            "actix-http",
        ]

        blockers = evaluate.external_adapter_config_claim_grade_runner_blockers(
            config,
            command,
            pathlib.Path("/tmp/nonexistent-mixed-checkout"),
        )

        self.assertIn(
            "cargo-bench JSON wrapper config has no registered benchmark-specific full-row "
            "claim-grade contract for benchmark(s): actix",
            blockers,
        )

    def test_mixed_registered_cargo_bench_contracts_fail_closed(self) -> None:
        config = {
            "source": "paper-external-workload-cargo-bench-json-wrapper",
            "benchmarks": ["R-Polars", "RJS-Compiler"],
            "cargo_bench_json_wrapper": {
                "child_command_template": ["cargo", "bench"],
            },
        }
        command = [
            sys.executable,
            "evaluation/scripts/paper_external_cargo_bench_json.py",
            "--claim-grade-contract",
            "rpolars-paper-run",
            "--",
            "cargo",
            "bench",
        ]

        blockers = evaluate.external_adapter_config_claim_grade_runner_blockers(
            config,
            command,
            pathlib.Path("/tmp/nonexistent-conflicting-checkout"),
        )

        self.assertTrue(
            any("mixes benchmarks requiring different claim-grade contracts" in blocker for blocker in blockers),
            blockers,
        )


class RJSCompilerSurfaceAuditTests(unittest.TestCase):
    def test_rjs_surface_parser_expands_macro_bench_functions(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            checkout = pathlib.Path(tmp) / "swc"
            benches = checkout / "benches"
            assets = benches / "assets"
            assets.mkdir(parents=True)
            (assets / "AjaxObservable.ts").write_text("export const x = 1;\n", encoding="utf-8")
            (benches / "typescript.rs").write_text(
                "\n".join(
                    [
                        "#![feature(test)]",
                        "extern crate test;",
                        "use test::Bencher;",
                        "macro_rules! codegen {",
                        "    ($name:ident, $target:expr) => {",
                        "        #[bench]",
                        "        fn $name(b: &mut Bencher) { let _ = b; }",
                        "    };",
                        "}",
                        "codegen!(codegen_es5, Target::Es5);",
                        "#[bench]",
                        "fn parser(b: &mut Bencher) { let _ = b; }",
                    ]
                ),
                encoding="utf-8",
            )

            surface = evaluate.parse_rjs_compiler_bench_surfaces(checkout)

        self.assertTrue(surface["complete"], surface)
        self.assertEqual(surface["libtest_bench_function_count"], 2, surface)
        self.assertIn("codegen_es5", surface["libtest_bench_functions"])
        self.assertIn("parser", surface["libtest_bench_functions"])

    def test_rjs_plan_replaces_parser_leaf_config_with_full_surface_contract(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            checkout = pathlib.Path(tmp) / "swc"
            benches = checkout / "benches"
            assets = benches / "assets"
            assets.mkdir(parents=True)
            (assets / "AjaxObservable.ts").write_text("export const x = 1;\n", encoding="utf-8")
            (benches / "typescript.rs").write_text(
                "\n".join(
                    [
                        "#![feature(test)]",
                        "extern crate test;",
                        "use test::Bencher;",
                        "macro_rules! codegen {",
                        "    ($name:ident, $target:expr) => {",
                        "        #[bench]",
                        "        fn $name(b: &mut Bencher) { let _ = b; }",
                        "    };",
                        "}",
                        "codegen!(codegen_es5, Target::Es5);",
                        "#[bench]",
                        "fn parser(b: &mut Bencher) { let _ = b; }",
                    ]
                ),
                encoding="utf-8",
            )
            surfaces = evaluate.parse_rjs_compiler_bench_surfaces(checkout)
            config = {
                "claim_grade": True,
                "cargo_bench_json_wrapper": {
                    "bench_name_filter": "parser",
                    "child_command_template": [
                        "cargo",
                        "bench",
                        "--package",
                        "swc",
                        "--bench",
                        "typescript",
                        "parser",
                    ],
                },
                "env": {},
            }
            source_contract = {
                "paper_exact_ref_complete": True,
                "claim_grade_complete": True,
                "exact_checkout_pin_complete": True,
                "checkout_pin": {"pinned": True, "exact_pinned": True},
                "blockers": [],
            }
            required_cell = {
                "dataset": "default_performance",
                "benchmark": "RJS-Compiler",
                "allocator": "unialloc",
                "role": "paper",
                "category": "macro_single_thread",
            }

            with mock.patch.object(evaluate, "required_performance_sample_cells", return_value=([required_cell], [])), \
                mock.patch.object(evaluate, "rjs_compiler_paper_source_contract", return_value=source_contract), \
                mock.patch.object(
                    evaluate,
                    "rjs_compiler_paper_source_metadata",
                    return_value={"paper_benchmark": "RJS-Compiler", "paper_project": "SWC"},
                ):
                plan = evaluate.build_rjs_compiler_paper_plan(
                    cfg={"methodology": {"runs_per_benchmark": 2}},
                    paper_dir=pathlib.Path(tmp),
                    config=config,
                    config_path=pathlib.Path(tmp) / "workload_config.local.json",
                    checkout=checkout,
                    surfaces=surfaces,
                    wrapper_script=ROOT / "evaluation" / "scripts" / "paper_external_cargo_bench_json.py",
                    dataset_names=["default_performance"],
                    runs=2,
                    timeout=30,
                )
                audit = evaluate.audit_rjs_compiler_paper_plan_object(
                    cfg={"methodology": {"runs_per_benchmark": 2}},
                    paper_dir=pathlib.Path(tmp),
                    config=config,
                    config_path=pathlib.Path(tmp) / "workload_config.local.json",
                    checkout=checkout,
                    surfaces=surfaces,
                    plan=plan,
                    plan_path=None,
                    dataset_names=["default_performance"],
                    required_runs=2,
                )

        workload = plan["workloads"][0]
        command = workload["command"]
        child = evaluate.command_after_remainder_separator(command)
        self.assertIn("--claim-grade-contract", command)
        self.assertIn("rjs-compiler-paper-run", command)
        self.assertNotIn("--bench-name-filter", command)
        self.assertEqual(evaluate.command_option_value(child, "--bench"), "typescript")
        self.assertNotIn("parser", child[child.index("--bench") + 2 :], child)
        self.assertTrue(audit["summary"]["ready_for_paper_equivalent_execution"], audit)
        self.assertFalse(audit["summary"]["ready_for_claim_grade_import"], audit)
        self.assertEqual(audit["summary"]["invalid_workload_count"], 0, audit)

    def test_rjs_surface_audit_uses_strict_plan_command_not_legacy_parser_bridge(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = pathlib.Path(tmp)
            checkout = tmp_path / "swc"
            benches = checkout / "benches"
            assets = benches / "assets"
            assets.mkdir(parents=True)
            (assets / "AjaxObservable.ts").write_text("export const x = 1;\n", encoding="utf-8")
            (benches / "typescript.rs").write_text(
                "\n".join(
                    [
                        "#![feature(test)]",
                        "extern crate test;",
                        "use test::Bencher;",
                        "macro_rules! codegen {",
                        "    ($name:ident, $target:expr) => {",
                        "        #[bench]",
                        "        fn $name(b: &mut Bencher) { let _ = b; }",
                        "    };",
                        "}",
                        "codegen!(codegen_es5, Target::Es5);",
                        "#[bench]",
                        "fn parser(b: &mut Bencher) { let _ = b; }",
                    ]
                ),
                encoding="utf-8",
            )
            config_path = tmp_path / "workload_config.local.json"
            config_path.write_text(
                json.dumps(
                    {
                        "claim_grade": True,
                        "not_claim_grade_reason": "legacy parser bridge only",
                        "real_workload_dir": str(checkout),
                        "command_template": [
                            "python3",
                            str(ROOT / "evaluation" / "scripts" / "paper_external_cargo_bench_json.py"),
                            "--dataset",
                            "{dataset}",
                            "--benchmark",
                            "{benchmark}",
                            "--allocator",
                            "{allocator}",
                            "--variant-feature",
                            "{variant_feature}",
                            "--run-index",
                            "{run_index}",
                            "--bench-name-filter",
                            "parser",
                            "--real-workload-dir",
                            "{real_workload_dir}",
                            "--",
                            "cargo",
                            "bench",
                            "--package",
                            "swc",
                            "--bench",
                            "typescript",
                            "parser",
                        ],
                        "cargo_bench_json_wrapper": {
                            "script": str(ROOT / "evaluation" / "scripts" / "paper_external_cargo_bench_json.py"),
                            "bench_name_filter": "parser",
                            "child_command_template": [
                                "cargo",
                                "bench",
                                "--package",
                                "swc",
                                "--bench",
                                "typescript",
                                "parser",
                            ],
                        },
                    }
                ),
                encoding="utf-8",
            )
            source_contract = {
                "paper_exact_ref_complete": True,
                "claim_grade_complete": True,
                "exact_checkout_pin_complete": True,
                "checkout_pin": {"pinned": True, "exact_pinned": True},
                "blockers": [],
            }
            required_cell = {
                "dataset": "default_performance",
                "benchmark": "RJS-Compiler",
                "allocator": "unialloc",
                "role": "paper",
                "category": "macro_single_thread",
            }
            args = argparse.Namespace(
                config=str(config_path),
                checkout=str(checkout),
                paper_dir=str(tmp_path),
                dataset=None,
                run_id="rjs-surface",
                output_dir=str(tmp_path / "raw"),
                no_update_results=True,
                timeout=30,
            )

            with mock.patch.object(evaluate, "required_performance_sample_cells", return_value=([required_cell], [])), \
                mock.patch.object(evaluate, "rjs_compiler_paper_source_contract", return_value=source_contract), \
                mock.patch.object(
                    evaluate,
                    "rjs_compiler_paper_source_metadata",
                    return_value={"paper_benchmark": "RJS-Compiler", "paper_project": "SWC"},
                ), \
                contextlib.redirect_stdout(io.StringIO()):
                rc = evaluate.audit_rjs_compiler_paper_surface(args)

            audit = json.loads((tmp_path / "raw" / "rjs-compiler-paper-surface-audit.json").read_text())

        self.assertEqual(rc, 0, audit)
        self.assertTrue(audit["summary"]["claim_grade_ready"], audit)
        self.assertTrue(audit["summary"]["strict_plan_claim_grade_ready"], audit)
        self.assertFalse(audit["summary"]["active_config_claim_grade_ready"], audit)
        self.assertFalse(audit["summary"]["uses_bench_filter"], audit)
        self.assertTrue(audit["summary"]["active_config_uses_bench_filter"], audit)
        self.assertEqual(audit["summary"]["claim_grade_contract"], "rjs-compiler-paper-run")
        self.assertIn("legacy parser bridge only", " ".join(audit["active_config_blockers"]))
        self.assertEqual(audit["blockers"], [])

    def test_overclaim_worklist_reports_rjs_plan_frontier(self) -> None:
        missing = evaluate.overclaim_missing_requirements(
            "paper_performance",
            {
                "rjs_compiler_paper_surface_audit_summary": {
                    "surface_complete": True,
                    "libtest_bench_function_count": 28,
                    "required_rjs_cell_count": 35,
                },
                "rjs_compiler_paper_plan_audit_summary": {
                    "ready_for_paper_equivalent_execution": True,
                    "ready_for_claim_grade_import": False,
                    "planned_workload_count": 35,
                    "required_cell_count": 35,
                    "dry_run_probe_count": 3,
                    "dry_run_probe_failure_count": 0,
                    "claim_grade_import_blocker": "RJS-Compiler plan has no repeated timing sample audit/import yet",
                },
            },
        )

        joined = " ".join(missing)
        self.assertIn("RJS-Compiler paper plan is not claim-grade import-ready", joined)
        self.assertIn("execution-ready True", joined)
        self.assertIn("planned workloads 35/35", joined)
        self.assertIn("no repeated timing sample audit/import yet", joined)


class RPolarsInputProvenanceTests(unittest.TestCase):
    def test_checkout_record_fails_closed_when_git_commit_cannot_be_verified(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            checkout = pathlib.Path(tmp) / "polars"
            checkout.mkdir()
            config = {
                "workload_id": "external-R-Polars",
                "claim_grade_activation": {
                    "checkout_commit": "07da7f07e9bca6dfe3034a2176f1ab5f037bf584",
                    "requested_ref": "refs/tags/py-0.13.0",
                    "resolved_ref": "refs/tags/py-0.13.0",
                },
            }

            record = evaluate.rpolars_checkout_record_from_config(config, checkout)

        self.assertIsNotNone(record)
        assert record is not None
        self.assertFalse(record["checkout_ready"], record)
        self.assertIsNone(record["existing_checkout"]["current_commit"], record)
        self.assertFalse(record["existing_checkout"]["commit_verified"], record)
        self.assertFalse(record["existing_checkout"]["matches_resolved_commit"], record)
        self.assertIn("checkout commit could not be verified with git rev-parse HEAD", record["blockers"])

    def test_input_provenance_audit_finds_db_benchmark_source_and_blocks_small_csv(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            checkout = pathlib.Path(tmp) / "polars"
            db = checkout / "py-polars" / "tests" / "db-benchmark"
            db.mkdir(parents=True)
            (db / "groupby-datagen.R").write_text(
                '\n'.join(
                    [
                        'DT[["id1"]] = sample(sprintf("id%03d",1:K), N, TRUE)',
                        'DT[["id2"]] = sample(sprintf("id%03d",1:K), N, TRUE)',
                        'DT[["id3"]] = sample(sprintf("id%010d",1:(N/K)), N, TRUE)',
                        'DT[["id4"]] = sample(K, N, TRUE)',
                        'DT[["id5"]] = sample(K, N, TRUE)',
                        'DT[["id6"]] = sample(N/K, N, TRUE)',
                        'DT[["v1"]] = sample(5, N, TRUE)',
                        'DT[["v2"]] = sample(15, N, TRUE)',
                        'DT[["v3"]] = round(runif(N,max=100),6)',
                    ]
                ),
                encoding="utf-8",
            )
            (db / "main.py").write_text('pl.read_csv("G1_1e7_1e2_5_0.csv")\n', encoding="utf-8")
            small = checkout / "small.csv"
            small.write_text("a,b,c\n1,2,3\n", encoding="utf-8")
            config = {
                "cargo_bench_json_wrapper": {
                    "cargo_bench_targets": ["bench", "csv", "groupby", "collect", "take", "sort"]
                },
                "env": {"CSV_SRC": str(small)},
            }

            audit = evaluate.rpolars_input_provenance_audit(config, checkout)

        self.assertTrue(audit["required_for_targets"], audit)
        self.assertFalse(audit["claim_grade_ready"], audit)
        self.assertEqual(audit["expected_csv_name"], "G1_1e7_1e2_5_0.csv")
        self.assertEqual(audit["expected_datagen_args"]["n_rows"], 10_000_000)
        self.assertIn("id1", audit["configured_csv"]["missing_columns"])
        self.assertTrue(
            any("provenance contract is not claim_grade" in blocker for blocker in audit["blockers"]),
            audit["blockers"],
        )

    def test_input_provenance_claim_contract_still_checks_rows_and_sha(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            checkout = pathlib.Path(tmp) / "polars"
            db = checkout / "py-polars" / "tests" / "db-benchmark"
            db.mkdir(parents=True)
            (db / "groupby-datagen.R").write_text(
                "\n".join(f'DT[[\"{col}\"]] = 1' for col in evaluate.RPOLARS_GROUPBY_REQUIRED_CSV_COLUMNS),
                encoding="utf-8",
            )
            (db / "main.py").write_text('pl.read_csv("G1_1e7_1e2_5_0.csv")\n', encoding="utf-8")
            csv_path = checkout / "G1_1e7_1e2_5_0.csv"
            csv_path.write_text(",".join(evaluate.RPOLARS_GROUPBY_REQUIRED_CSV_COLUMNS) + "\n" + "a,b,c,1,2,3,4,5,6\n", encoding="utf-8")
            digest = hashlib.sha256(csv_path.read_bytes()).hexdigest()
            config = {
                "cargo_bench_json_wrapper": {
                    "cargo_bench_targets": ["bench", "csv", "groupby", "collect", "take", "sort"],
                    "rpolars_input_contract": {
                        "claim_grade": True,
                        "source": evaluate.RPOLARS_DB_BENCHMARK_SOURCE_ID,
                        "expected_csv_name": "G1_1e7_1e2_5_0.csv",
                        "expected_rows": 10_000_000,
                        "k_groups": 100,
                        "na_percent": 5,
                        "sort_flag": 0,
                        "csv_sha256": digest,
                    },
                },
                "env": {"CSV_SRC": str(csv_path)},
            }

            audit = evaluate.rpolars_input_provenance_audit(config, checkout)

        self.assertFalse(audit["claim_grade_ready"], audit)
        self.assertIn(
            "R-Polars configured CSV_SRC row count does not match input contract: 1 != 10000000",
            audit["blockers"],
        )
        self.assertEqual(audit["configured_csv"]["sha256"], digest)

    def test_input_provenance_claim_contract_rejects_noncanonical_csv_basename(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            checkout = pathlib.Path(tmp) / "polars"
            db = checkout / "py-polars" / "tests" / "db-benchmark"
            db.mkdir(parents=True)
            (db / "groupby-datagen.R").write_text(
                "\n".join(f'DT[[\"{col}\"]] = 1' for col in evaluate.RPOLARS_GROUPBY_REQUIRED_CSV_COLUMNS),
                encoding="utf-8",
            )
            (db / "main.py").write_text('pl.read_csv("G1_1e0_1e0_0_0.csv")\n', encoding="utf-8")
            csv_path = checkout / "renamed.csv"
            csv_path.write_text(
                ",".join(evaluate.RPOLARS_GROUPBY_REQUIRED_CSV_COLUMNS) + "\n" + "a,b,c,1,2,3,4,5,6\n",
                encoding="utf-8",
            )
            digest = hashlib.sha256(csv_path.read_bytes()).hexdigest()
            config = {
                "cargo_bench_json_wrapper": {
                    "cargo_bench_targets": ["csv", "groupby"],
                    "rpolars_input_contract": {
                        "claim_grade": True,
                        "source": evaluate.RPOLARS_DB_BENCHMARK_SOURCE_ID,
                        "expected_csv_name": "G1_1e0_1e0_0_0.csv",
                        "expected_rows": 1,
                        "k_groups": 1,
                        "na_percent": 0,
                        "sort_flag": 0,
                        "csv_sha256": digest,
                    },
                },
                "env": {"CSV_SRC": str(csv_path)},
            }

            audit = evaluate.rpolars_input_provenance_audit(config, checkout)

        self.assertFalse(audit["claim_grade_ready"], audit)
        self.assertIn(
            "R-Polars configured CSV_SRC basename does not match the canonical db-benchmark CSV: renamed.csv != G1_1e0_1e0_0_0.csv",
            audit["blockers"],
        )
        self.assertEqual(audit["configured_csv"]["sha256"], digest)

    def test_materialization_plan_refuses_generation_when_byte_budget_is_too_small(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            checkout = pathlib.Path(tmp) / "polars"
            db = checkout / "py-polars" / "tests" / "db-benchmark"
            db.mkdir(parents=True)
            (db / "groupby-datagen.R").write_text(
                "\n".join(f'DT[[\"{col}\"]] = 1' for col in evaluate.RPOLARS_GROUPBY_REQUIRED_CSV_COLUMNS),
                encoding="utf-8",
            )
            (db / "main.py").write_text('pl.read_csv("G1_1e7_1e2_5_0.csv")\n', encoding="utf-8")
            csv_dir = pathlib.Path(tmp) / "inputs"
            config = {
                "cargo_bench_json_wrapper": {"cargo_bench_targets": ["csv", "groupby"]},
                "env": {"CSV_SRC": str(csv_dir / "G1_1e7_1e2_5_0.csv")},
            }

            plan = evaluate.rpolars_paper_input_materialization_plan(
                config=config,
                checkout=checkout,
                csv_dir=csv_dir,
                max_output_bytes=1,
                safety_margin_bytes=0,
                rscript=sys.executable,
            )
            created_during_preflight = csv_dir.exists()

        self.assertFalse(plan["generation_ready"], plan)
        self.assertEqual(plan["source_contract"]["canonical_datagen_args"]["n_rows"], 10_000_000)
        self.assertEqual(plan["disk_guard"]["size_estimate"]["estimated_csv_bytes"], 960_000_033)
        self.assertIn(
            "R-Polars paper input estimated size exceeds --max-output-bytes: 960000033 > 1",
            plan["generation_blockers"],
        )
        self.assertFalse(created_during_preflight, "preflight must not create the CSV directory")

    def test_existing_canonical_csv_validation_emits_claim_grade_patch(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp)
            checkout = root / "polars"
            db = checkout / "py-polars" / "tests" / "db-benchmark"
            db.mkdir(parents=True)
            (db / "groupby-datagen.R").write_text(
                "\n".join(f'DT[[\"{col}\"]] = 1' for col in evaluate.RPOLARS_GROUPBY_REQUIRED_CSV_COLUMNS),
                encoding="utf-8",
            )
            (db / "main.py").write_text('pl.read_csv("G1_1e0_1e0_0_0.csv")\n', encoding="utf-8")
            csv_path = checkout / "G1_1e0_1e0_0_0.csv"
            csv_path.write_text(
                ",".join(evaluate.RPOLARS_GROUPBY_REQUIRED_CSV_COLUMNS) + "\n" + "a,b,c,1,2,3,4,5,6\n",
                encoding="utf-8",
            )
            config_path = root / "workload_config.local.json"
            config_path.write_text(
                json.dumps(
                    {
                        "real_workload_dir": str(checkout),
                        "cargo_bench_json_wrapper": {"cargo_bench_targets": ["csv", "groupby"]},
                        "env": {"CSV_SRC": str(checkout / "small.csv")},
                    }
                ),
                encoding="utf-8",
            )
            args = argparse.Namespace(
                config=str(config_path),
                checkout=None,
                csv_dir=None,
                generate=False,
                existing_csv=str(csv_path),
                max_output_bytes=0,
                safety_margin_bytes=0,
                rscript=sys.executable,
                overwrite=False,
                timeout=30,
                run_id="unit-existing-rpolars-input",
                output_dir=str(root / "raw"),
                contract_template_out=None,
                write_config=True,
                config_out=None,
                no_update_results=True,
            )

            exit_code = evaluate.prepare_rpolars_paper_input(args)
            result = json.loads((root / "raw" / "rpolars-paper-input-preflight.json").read_text())
            written_config = json.loads(config_path.read_text())

        self.assertEqual(exit_code, 0, result)
        self.assertEqual(result["mode"], "validate-existing")
        self.assertTrue(result["claim_grade_ready"], result)
        patch = result["suggested_config_patch"]
        self.assertEqual(patch["env"]["CSV_SRC"], str(csv_path))
        self.assertEqual(
            patch["cargo_bench_json_wrapper"]["rpolars_input_contract"]["expected_csv_name"],
            "G1_1e0_1e0_0_0.csv",
        )
        self.assertRegex(
            patch["cargo_bench_json_wrapper"]["rpolars_input_contract"]["csv_sha256"],
            r"^[0-9a-f]{64}$",
        )
        self.assertTrue(result["config_write"]["written"], result["config_write"])
        self.assertEqual(written_config["env"]["CSV_SRC"], str(csv_path))
        self.assertEqual(
            written_config["cargo_bench_json_wrapper"]["rpolars_input_contract"]["expected_csv_name"],
            "G1_1e0_1e0_0_0.csv",
        )

    def test_existing_noncanonical_csv_write_config_refuses_to_modify_config(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp)
            checkout = root / "polars"
            db = checkout / "py-polars" / "tests" / "db-benchmark"
            db.mkdir(parents=True)
            (db / "groupby-datagen.R").write_text(
                "\n".join(f'DT[[\"{col}\"]] = 1' for col in evaluate.RPOLARS_GROUPBY_REQUIRED_CSV_COLUMNS),
                encoding="utf-8",
            )
            (db / "main.py").write_text('pl.read_csv("G1_1e0_1e0_0_0.csv")\n', encoding="utf-8")
            csv_path = checkout / "small.csv"
            csv_path.write_text("a,b,c\n1,2,3\n", encoding="utf-8")
            original_config = {
                "real_workload_dir": str(checkout),
                "cargo_bench_json_wrapper": {"cargo_bench_targets": ["csv", "groupby"]},
                "env": {"CSV_SRC": str(csv_path)},
            }
            config_path = root / "workload_config.local.json"
            config_path.write_text(json.dumps(original_config), encoding="utf-8")
            args = argparse.Namespace(
                config=str(config_path),
                checkout=None,
                csv_dir=None,
                generate=False,
                existing_csv=str(csv_path),
                max_output_bytes=0,
                safety_margin_bytes=0,
                rscript=sys.executable,
                overwrite=False,
                timeout=30,
                run_id="unit-bad-existing-rpolars-input",
                output_dir=str(root / "raw"),
                contract_template_out=None,
                write_config=True,
                config_out=None,
                no_update_results=True,
            )

            exit_code = evaluate.prepare_rpolars_paper_input(args)
            result = json.loads((root / "raw" / "rpolars-paper-input-preflight.json").read_text())
            after_config = json.loads(config_path.read_text())

        self.assertEqual(exit_code, 1, result)
        self.assertFalse(result["claim_grade_ready"], result)
        self.assertFalse(result["config_write"]["written"], result["config_write"])
        self.assertEqual(after_config, original_config)
        self.assertIn(
            "refusing to write R-Polars config because canonical input validation is not claim_grade_ready",
            result["config_write"]["blockers"],
        )

    def test_generated_canonical_csv_can_write_validated_config_patch(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp)
            checkout = root / "polars"
            db = checkout / "py-polars" / "tests" / "db-benchmark"
            db.mkdir(parents=True)
            (db / "groupby-datagen.R").write_text(
                "\n".join(f'DT[[\"{col}\"]] = 1' for col in evaluate.RPOLARS_GROUPBY_REQUIRED_CSV_COLUMNS),
                encoding="utf-8",
            )
            (db / "main.py").write_text('pl.read_csv("G1_1e0_1e0_0_0.csv")\n', encoding="utf-8")
            fake_rscript = root / "fake-rscript.py"
            fake_rscript.write_text(
                "\n".join(
                    [
                        "#!/usr/bin/env python3",
                        "import pathlib, sys",
                        "path = pathlib.Path('G1_1e0_1e0_0_0.csv')",
                        f"path.write_text({(','.join(evaluate.RPOLARS_GROUPBY_REQUIRED_CSV_COLUMNS) + chr(10) + 'a,b,c,1,2,3,4,5,6' + chr(10))!r}, encoding='utf-8')",
                    ]
                )
                + "\n",
                encoding="utf-8",
            )
            fake_rscript.chmod(0o755)
            config_path = root / "workload_config.local.json"
            config_path.write_text(
                json.dumps(
                    {
                        "real_workload_dir": str(checkout),
                        "cargo_bench_json_wrapper": {"cargo_bench_targets": ["csv", "groupby"]},
                        "env": {"CSV_SRC": str(checkout / "small.csv")},
                    }
                ),
                encoding="utf-8",
            )
            csv_dir = root / "inputs"
            args = argparse.Namespace(
                config=str(config_path),
                checkout=None,
                csv_dir=str(csv_dir),
                generate=True,
                existing_csv=None,
                max_output_bytes=1024,
                safety_margin_bytes=0,
                rscript=str(fake_rscript),
                overwrite=False,
                timeout=30,
                run_id="unit-generated-rpolars-input",
                output_dir=str(root / "raw"),
                contract_template_out=None,
                write_config=True,
                config_out=None,
                no_update_results=True,
            )

            exit_code = evaluate.prepare_rpolars_paper_input(args)
            result = json.loads((root / "raw" / "rpolars-paper-input-preflight.json").read_text())
            written_config = json.loads(config_path.read_text())

        self.assertEqual(exit_code, 0, result)
        self.assertTrue(result["generated"], result)
        self.assertTrue(result["claim_grade_ready"], result)
        self.assertTrue(result["config_write"]["written"], result["config_write"])
        self.assertEqual(written_config["env"]["CSV_SRC"], str(csv_dir / "G1_1e0_1e0_0_0.csv"))
        self.assertEqual(
            written_config["cargo_bench_json_wrapper"]["rpolars_input_contract"],
            result["suggested_config_patch"]["cargo_bench_json_wrapper"]["rpolars_input_contract"],
        )

    def test_input_contract_template_is_explicitly_not_claim_evidence_without_digest(self) -> None:
        source_contract = {
            "canonical_csv_name": "G1_1e7_1e2_5_0.csv",
            "canonical_datagen_args": {
                "n_rows": 10_000_000,
                "k_groups": 100,
                "na_percent": 5,
                "sort_flag": 0,
            },
        }

        template = evaluate.rpolars_claim_grade_input_contract_template(
            source_contract=source_contract,
            csv_path=pathlib.Path("/tmp/G1_1e7_1e2_5_0.csv"),
            max_output_bytes=1_000_000_000,
            rscript="/usr/local/bin/Rscript",
        )

        self.assertFalse(template["contract_ready"], template)
        self.assertEqual(template["contract"]["csv_sha256"], "<64-hex-sha256-of-canonical-csv>")
        self.assertIn("--existing-csv", template["validation_commands"]["validate_existing_csv"])
        self.assertEqual(
            template["config_patch"]["cargo_bench_json_wrapper"]["rpolars_input_contract"]["expected_rows"],
            10_000_000,
        )

    def test_claim_grade_false_from_input_blocker_is_not_structural_plan_error(self) -> None:
        raw = {
            "claim_grade": False,
            "claim_grade_blockers": ["missing canonical R-Polars CSV"],
        }
        audited = {
            "issues": [
                "workload declares non-claim-grade evidence: workload explicitly marks claim_grade=false",
                "blocked cell claim-grade blocker: missing canonical R-Polars CSV",
            ]
        }

        structural, claim_blockers = evaluate.split_plan_workload_issues_for_claim_blockers(raw, audited)

        self.assertEqual(structural, [])
        self.assertEqual(len(claim_blockers), 2)


class ConstrainedBootLogPackagingTests(unittest.TestCase):
    def test_snmalloc_dependency_env_uses_discovered_cmake(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            fake_cmake = pathlib.Path(tmp) / "cmake"
            fake_cmake.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
            fake_cmake.chmod(0o755)
            with mock.patch.dict(evaluate.os.environ, {"UNIALLOC_CMAKE_BIN": str(fake_cmake)}, clear=False):
                env = evaluate.local_collections_dependency_env({"allocator": "snmalloc"})

        self.assertEqual(env.get("UNIALLOC_CMAKE_BIN"), str(fake_cmake))
        self.assertNotIn("UNIALLOC_CMAKE_BIN", evaluate.local_collections_dependency_env({"allocator": "jemalloc"}))

    def test_rust_toolchain_env_override_does_not_rewrite_repo_pin(self) -> None:
        pin_path = ROOT / "rust-toolchain"
        pinned = pin_path.read_text(encoding="utf-8").strip()
        with mock.patch.dict(
            evaluate.os.environ,
            {evaluate.RUST_TOOLCHAIN_ENV: "nightly"},
            clear=False,
        ):
            self.assertEqual(evaluate.rust_toolchain(), "nightly")
        self.assertEqual(pin_path.read_text(encoding="utf-8").strip(), pinned)

    def test_cargo_link_probe_warning_audit_flags_lazy_iterator_warning(self) -> None:
        probe = {
            "stderr_tail": "\n".join(
                [
                    "warning: unused `std::collections::btree_map::Range` that must be used",
                    "= note: `#[warn(unused_must_use)]` on by default",
                    "= note: iterators are lazy and do nothing unless consumed",
                ]
            )
        }

        audit = evaluate.cargo_link_probe_warning_audit(probe)

        self.assertEqual(audit["unused_must_use_warning_count"], 1)
        self.assertEqual(audit["lazy_iterator_warning_count"], 1)
        self.assertIn(
            "cargo link probe reported lazy iterator unused_must_use warnings in benchmark source",
            audit["claim_grade_blockers"],
        )

    def test_redox_boot_log_validation_can_emit_claim_ready_matrix_entry(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp)
            image = root / "redox.img"
            emulator = root / "redoxer"
            boot_config = root / "redox.toml"
            build_log = root / "redox-build.log"
            boot_log = root / "redox-boot.log"
            image.write_bytes(b"R" * (64 * 1024))
            emulator.write_text("#!/bin/sh\necho 'redoxer test runner'\n", encoding="utf-8")
            emulator.chmod(0o755)
            boot_config.write_text("kernel='redox'\n", encoding="utf-8")
            build_log.write_text("redox image build completed\n", encoding="utf-8")
            image_sha = hashlib.sha256(image.read_bytes()).hexdigest()
            config_sha = hashlib.sha256(boot_config.read_bytes()).hexdigest()
            boot_log.write_text(
                "\n".join(
                    [
                        "redox boot started",
                        "unialloc target runtime reached",
                        (
                            f"{evaluate.CONSTRAINED_BOOT_PROVENANCE_MARKER} "
                            f"platform=redox image_sha256={image_sha} boot_config_sha256={config_sha} "
                            "emulator=redoxer emulator_version=test "
                            f"{source_fingerprint_marker_fields()}"
                        ),
                        (
                            f"{evaluate.CONSTRAINED_BOOT_SAMPLE_MARKER} platform=redox boot_cycle=1 "
                            "fixed_heap_ready=true c_abi_invoked=true c_abi_ready_after_init=true "
                            "c_abi_round_trips=2 c_abi_invalid_layouts_rejected=3 "
                            "c_abi_over_page_alignment_checked=true c_abi_probe_passed=true "
                            "allocator_total_allocations=2 allocator_total_deallocations=2 "
                            "allocator_total_allocated_bytes=128 allocator_typed_allocations=1 "
                            "allocator_typed_deallocations=1 allocator_typed_allocated_bytes=64 "
                            "allocator_type_stats_rows=1 allocator_type_stats_dropped_events=0 allocator_type_stats_probe_matched=true"
                        ),
                        "BOOT_OK",
                    ]
                ),
                encoding="utf-8",
            )
            entry_out = root / "redox-entry.json"
            args = argparse.Namespace(
                platform="redox",
                boot_log=str(boot_log),
                image=str(image),
                emulator=str(emulator),
                boot_config=str(boot_config),
                build_log=str(build_log),
                target_triple="x86_64-unknown-redox",
                boot_success_pattern="BOOT_OK",
                boot_log_required_marker=None,
                run_id="unit-redox-boot-log",
                output_dir=str(root / "raw"),
                matrix_entry_out=str(entry_out),
                matrix_out=None,
            )

            exit_code = evaluate.validate_constrained_boot_log(args)
            validation = json.loads((root / "raw" / "redox-boot-log-validation.json").read_text())
            entry = json.loads(entry_out.read_text())

        self.assertEqual(exit_code, 0, validation)
        self.assertTrue(validation["ready_for_platform_import"], validation)
        self.assertTrue(entry["claim_grade"], entry)
        self.assertTrue(entry["complete_for_claim"], entry)
        self.assertEqual(entry["target_triple"], "x86_64-unknown-redox")
        self.assertIn("build_log", {item["kind"] for item in entry["evidence"]})
        self.assertIn("boot_cycles", {item["kind"] for item in entry["evidence"]})
        self.assertRegex(entry["evidence"][0]["sha256"], r"^[0-9a-f]{64}$")

    def test_redox_boot_log_validation_rejects_unhashable_boot_artifacts(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp)
            boot_log = root / "redox-boot.log"
            build_log = root / "redox-build.log"
            build_log.write_text("redox image build completed\n", encoding="utf-8")
            boot_log.write_text(
                "\n".join(
                    [
                        "redox boot started",
                        "unialloc target runtime reached",
                        (
                            f"{evaluate.CONSTRAINED_BOOT_PROVENANCE_MARKER} "
                            f"platform=redox image_sha256={'1' * 64} boot_config_sha256={'2' * 64} "
                            "emulator=redoxer emulator_version=test"
                        ),
                        (
                            f"{evaluate.CONSTRAINED_BOOT_SAMPLE_MARKER} platform=redox boot_cycle=1 "
                            "fixed_heap_ready=true c_abi_invoked=true c_abi_ready_after_init=true "
                            "c_abi_round_trips=2 c_abi_invalid_layouts_rejected=3 "
                            "c_abi_over_page_alignment_checked=true c_abi_probe_passed=true "
                            "allocator_total_allocations=2 allocator_total_deallocations=2 "
                            "allocator_total_allocated_bytes=128 allocator_typed_allocations=1 "
                            "allocator_typed_deallocations=1 allocator_typed_allocated_bytes=64 "
                            "allocator_type_stats_rows=1 allocator_type_stats_dropped_events=0 allocator_type_stats_probe_matched=true"
                        ),
                        "BOOT_OK",
                    ]
                ),
                encoding="utf-8",
            )
            args = argparse.Namespace(
                platform="redox",
                boot_log=str(boot_log),
                image="https://example.invalid/redox.img",
                emulator="redoxer://runner",
                boot_config="redox-default-profile",
                build_log=str(build_log),
                target_triple="x86_64-unknown-redox",
                boot_success_pattern="BOOT_OK",
                boot_log_required_marker=None,
                run_id="unit-redox-boot-log-unhashable",
                output_dir=str(root / "raw"),
                matrix_entry_out=None,
                matrix_out=None,
            )

            exit_code = evaluate.validate_constrained_boot_log(args)
            validation = json.loads((root / "raw" / "redox-boot-log-validation.json").read_text())

        self.assertEqual(exit_code, 1, validation)
        joined = " | ".join(validation["blockers"])
        self.assertIn("target image must be a readable local file", joined)
        self.assertIn("boot_config must be a readable local file", joined)

    def test_claim_grade_redox_boot_cycles_cross_checks_local_hashes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp)
            image = root / "redox.img"
            boot_config = root / "redox.toml"
            image.write_bytes(b"real image bytes for hash")
            boot_config.write_text("kernel='redox'\n", encoding="utf-8")
            boot_cycles = root / "redox-boot-cycles.json"
            evaluate.write_json(
                boot_cycles,
                {
                    "kind": "boot_cycles",
                    "platform": "redox",
                    "passed": True,
                    "claim_grade": True,
                    "complete_for_claim": True,
                    "boot_cycle_count": 1,
                    "image": str(image),
                    "boot_config": str(boot_config),
                    "boot_provenance_records": [
                        {
                            "source_marker": evaluate.CONSTRAINED_BOOT_PROVENANCE_MARKER,
                            "platform": "redox",
                            "image_sha256": "0" * 64,
                            "boot_config_sha256": hashlib.sha256(boot_config.read_bytes()).hexdigest(),
                            "emulator": "redoxer",
                            "emulator_version": "test",
                        }
                    ],
                    "boot_sample_records": [
                        {
                            "source_marker": evaluate.CONSTRAINED_BOOT_SAMPLE_MARKER,
                            "platform": "redox",
                            "boot_cycle": 1,
                            "fixed_heap_ready": True,
                            "c_abi_invoked": True,
                            "c_abi_ready_after_init": True,
                            "c_abi_round_trips": 2,
                            "c_abi_invalid_layouts_rejected": 3,
                            "c_abi_over_page_alignment_checked": True,
                            "c_abi_probe_passed": True,
                            "allocator_total_allocations": 2,
                            "allocator_total_deallocations": 2,
                            "allocator_total_allocated_bytes": 128,
                            "allocator_typed_allocations": 1,
                            "allocator_typed_deallocations": 1,
                            "allocator_typed_allocated_bytes": 64,
                            "allocator_type_stats_rows": 1,
                            "allocator_type_stats_dropped_events": 0,
                            "allocator_type_stats_probe_matched": True,
                        }
                    ],
                },
            )
            blockers = evaluate.platform_evidence_content_blockers(
                "boot_cycles",
                boot_cycles,
                platform_name="redox",
            )

        self.assertIn("image_sha256 does not match", " | ".join(blockers))

    def test_redoxer_target_runtime_audit_is_real_but_not_claim_grade(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp)
            binary = root / "small_heap-redox"
            binary.write_bytes(b"\x7fELF redox binary fixture")
            log = root / "redoxer-small-heap.log"
            log.write_text(
                "\n".join(
                    [
                        "redoxer: creating temporary disk",
                        "## redoxer (success) ##",
                        (
                            f"{evaluate.CONSTRAINED_BOOT_SAMPLE_MARKER} platform=redox boot_cycle=1 "
                            "fixed_heap_ready=true c_abi_invoked=true c_abi_ready_after_init=true "
                            "c_abi_round_trips=2 c_abi_invalid_layouts_rejected=3 "
                            "c_abi_over_page_alignment_checked=true c_abi_probe_passed=true "
                            "allocator_total_allocations=2 allocator_total_deallocations=2 "
                            "allocator_total_allocated_bytes=128 allocator_typed_allocations=1 "
                            "allocator_typed_deallocations=1 allocator_typed_allocated_bytes=64 "
                            "allocator_type_stats_rows=1 allocator_type_stats_dropped_events=0 allocator_type_stats_probe_matched=true"
                        ),
                        '{"passed":true,"heap_size":204800}',
                    ]
                )
                + "\n",
                encoding="utf-8",
            )
            args = argparse.Namespace(
                platform="redox",
                log=str(log),
                binary=str(binary),
                runner="redoxer",
                runner_image="redoxos/redoxer:test",
                success_pattern="UNIALLOC_CONSTRAINED_BOOT_SAMPLE",
                allow_missing_passed_json=False,
                run_id="unit-redoxer-runtime",
                output_dir=str(root / "raw"),
            )

            exit_code = evaluate.audit_redoxer_target_runtime(args)
            audit = json.loads((root / "raw" / "redox-redoxer-target-runtime-audit.json").read_text())

        self.assertEqual(exit_code, 0, audit)
        self.assertTrue(audit["target_runtime_verified"], audit)
        self.assertFalse(audit["claim_grade"], audit)
        self.assertFalse(audit["complete_for_claim"], audit)
        self.assertRegex(audit["binary_sha256"], r"^[0-9a-f]{64}$")
        self.assertEqual(audit["passed_json_record_count"], 1)
        self.assertIn("validate-constrained-boot-log", " | ".join(audit["claim_grade_blockers"]))

    def test_redoxer_target_runtime_audit_rejects_failure_marker(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp)
            binary = root / "small_heap-redox"
            binary.write_bytes(b"\x7fELF redox binary fixture")
            log = root / "redoxer-small-heap.log"
            log.write_text("## redoxer (failure) ##\n", encoding="utf-8")
            args = argparse.Namespace(
                platform="redox",
                log=str(log),
                binary=str(binary),
                runner="redoxer",
                runner_image=None,
                success_pattern=None,
                allow_missing_passed_json=False,
                run_id="unit-redoxer-runtime-fail",
                output_dir=str(root / "raw"),
            )

            exit_code = evaluate.audit_redoxer_target_runtime(args)
            audit = json.loads((root / "raw" / "redox-redoxer-target-runtime-audit.json").read_text())

        self.assertEqual(exit_code, 1, audit)
        joined = " | ".join(audit["blockers"])
        self.assertIn("does not contain the success marker", joined)
        self.assertIn("contains a failure marker", joined)
        self.assertIn("no redox UniAlloc constrained boot sample marker", joined)


class RPythonSurfacePlanTests(unittest.TestCase):
    def make_criterion_rpython_checkout(self, root: pathlib.Path) -> tuple[pathlib.Path, dict]:
        """Create the smallest RustPython-like Criterion surface we need.

        The fixture intentionally has two Cargo bench targets so readiness tests
        prove that the adapter plans the whole current checkout surface rather
        than a stale single libtest leaf such as ``bench_tokenization``.
        """

        checkout = root / "RustPython"
        checkout.mkdir()
        (checkout / "Cargo.toml").write_text(
            "\n".join(
                [
                    "[package]",
                    'name = "rustpython"',
                    'version = "0.2.0"',
                    "",
                    "[[bench]]",
                    'name = "execution"',
                    'harness = false',
                    "",
                    "[[bench]]",
                    'name = "microbenchmarks"',
                    'harness = false',
                ]
            )
            + "\n",
            encoding="utf-8",
        )
        benches = checkout / "benches"
        (benches / "benchmarks").mkdir(parents=True)
        (benches / "microbenchmarks").mkdir(parents=True)
        (benches / "benchmarks" / "pystone.py").write_text("print('ok')\n", encoding="utf-8")
        (benches / "microbenchmarks" / "loop.py").write_text("ITERATIONS=1\n", encoding="utf-8")
        for target, group in (("execution", "execution"), ("microbenchmarks", "microbenchmarks")):
            (benches / f"{target}.rs").write_text(
                "\n".join(
                    [
                        "use criterion::{criterion_group, criterion_main, Criterion};",
                        "pub fn criterion_benchmark(c: &mut Criterion) {",
                        f'  let mut group = c.benchmark_group("{group}");',
                        '  group.bench_function("rustpython", |_| {});',
                        "}",
                        "criterion_group!(benches, criterion_benchmark);",
                        "criterion_main!(benches);",
                    ]
                ),
                encoding="utf-8",
            )
        return checkout, evaluate.parse_rpython_bench_surfaces(checkout)

    def rpython_required_cell(self) -> dict:
        return {
            "dataset": "default_performance",
            "benchmark": "RPython",
            "allocator": "unialloc",
            "role": "paper",
            "category": "macro_single_thread",
        }

    def rpython_sync_fixture(
        self,
        root: pathlib.Path,
        *,
        source_contract: dict,
        audit_summary: dict,
    ) -> tuple[dict, pathlib.Path]:
        """Write a minimal config/plan/audit triple for sync tests."""

        config_path = root / "RPython" / "workload_config.local.json"
        plan_path = root / "rpython_paper_plan.json"
        audit_path = root / "rpython_paper_plan_audit.json"
        wrapper = str(ROOT / "evaluation" / "scripts" / "paper_external_cargo_bench_json.py")
        command = [
            sys.executable,
            wrapper,
            "--dataset",
            "default_performance",
            "--benchmark",
            "RPython",
            "--allocator",
            "unialloc",
            "--variant-feature",
            "baseline",
            "--run-index",
            "0",
            "--real-workload-dir",
            str(root / "RustPython"),
            "--claim-grade-contract",
            "rpython-paper-run",
            "--cargo-bench-targets",
            "execution,microbenchmarks",
            "--expected-cargo-bench-targets",
            "execution,microbenchmarks",
            "--expected-libtest-bench-functions",
            "",
            "--paper-source-contract-json",
            json.dumps(source_contract, sort_keys=True),
            "--",
            "cargo",
            "bench",
            "--package",
            "rustpython",
            "--bench",
            "execution",
        ]
        config = {
            "configured": True,
            "claim_grade": False,
            "not_claim_grade_reason": "fixture starts fail-closed",
            "command_template": [
                sys.executable,
                wrapper,
                "--dataset",
                "{dataset}",
                "--benchmark",
                "{benchmark}",
                "--allocator",
                "{allocator}",
                "--variant-feature",
                "{variant_feature}",
                "--run-index",
                "{run_index}",
                "--real-workload-dir",
                "{real_workload_dir}",
                "--",
                "cargo",
                "bench",
                "--package",
                "rustpython",
                "--bench",
                "execution",
            ],
            "cargo_bench_json_wrapper": {
                "not_claim_grade_reason": "fixture starts fail-closed",
                "child_command_template": ["cargo", "bench", "--package", "rustpython", "--bench", "execution"],
            },
        }
        plan = {
            "source_contract": source_contract,
            "workloads": [
                {
                    "dataset": "default_performance",
                    "benchmark": "RPython",
                    "allocator": "unialloc",
                    "runs": 2,
                    "claim_grade": True,
                    "command": command,
                    "expected_cargo_bench_targets": ["execution", "microbenchmarks"],
                    "expected_libtest_bench_functions": [],
                    "source_contract": source_contract,
                }
            ],
        }
        spec = {
            "label": "RPython",
            "sync_kind": "rpython_current_or_exact_claim_grade_config",
            "config": config_path,
            "plan": plan_path,
            "plan_audit": audit_path,
            "benchmark": "RPython",
            "contract": "rpython-paper-run",
            "requires_audit_key": "ready_for_reproducible_fallback_execution",
        }
        evaluate.write_json(config_path, config)
        evaluate.write_json(plan_path, plan)
        evaluate.write_json(audit_path, {"summary": audit_summary})
        return spec, config_path

    def test_rpython_plan_expands_tokenization_leaf_to_full_bench_surface_but_keeps_source_gate(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            checkout = pathlib.Path(tmp) / "rustpython"
            bench_dir = checkout / "benchmarks"
            asset_dir = bench_dir / "benchmarks"
            asset_dir.mkdir(parents=True)
            (checkout / "Cargo.toml").write_text(
                '[[bench]]\nname = "bench"\npath = "./benchmarks/bench.rs"\n',
                encoding="utf-8",
            )
            for name in ["minidom.py", "nbody.py", "mandelbrot.py"]:
                (asset_dir / name).write_text("print('ok')\n", encoding="utf-8")
            (bench_dir / "bench.rs").write_text(
                "\n".join(
                    [
                        "#![feature(test)]",
                        "extern crate test;",
                        "#[bench]",
                        "fn bench_tokenization(b: &mut test::Bencher) { let _ = include_str!(\"./benchmarks/minidom.py\"); let _ = b; }",
                        "#[bench]",
                        "fn bench_rustpy_parse_to_ast(b: &mut test::Bencher) { let _ = include_str!(\"./benchmarks/nbody.py\"); let _ = b; }",
                    ]
                ),
                encoding="utf-8",
            )
            surfaces = evaluate.parse_rpython_bench_surfaces(checkout)
            config = {
                "claim_grade": False,
                "cargo_bench_json_wrapper": {
                    "bench_name_filter": "bench_tokenization",
                    "child_command_template": [
                        "cargo",
                        "+nightly-2021-02-19",
                        "bench",
                        "--package",
                        "rustpython",
                        "--bench",
                        "bench",
                        "bench_tokenization",
                    ],
                },
                "env": {},
            }
            source_contract = {
                "paper_exact_ref_complete": True,
                "claim_grade_complete": False,
                "exact_checkout_pin_complete": False,
                "checkout_pin": {"pinned": True, "exact_pinned": False, "fallback_used": True},
                "blockers": ["adapter checkout used a fallback ref; claim-grade paper evidence requires the exact paper ref"],
            }
            required_cell = {
                "dataset": "default_performance",
                "benchmark": "RPython",
                "allocator": "unialloc",
                "role": "paper",
                "category": "macro_single_thread",
            }
            with mock.patch.object(evaluate, "required_performance_sample_cells", return_value=([required_cell], [])), \
                mock.patch.object(evaluate, "rpython_paper_source_contract", return_value=source_contract), \
                mock.patch.object(
                    evaluate,
                    "rpython_paper_source_metadata",
                    return_value={"paper_benchmark": "RPython", "paper_project": "RustPython"},
                ):
                plan = evaluate.build_rpython_paper_plan(
                    cfg={"methodology": {"runs_per_benchmark": 2}},
                    paper_dir=pathlib.Path(tmp),
                    config=config,
                    config_path=pathlib.Path(tmp) / "workload_config.local.json",
                    checkout=checkout,
                    surfaces=surfaces,
                    wrapper_script=ROOT / "evaluation" / "scripts" / "paper_external_cargo_bench_json.py",
                    dataset_names=["default_performance"],
                    runs=2,
                    timeout=30,
                )
                audit = evaluate.audit_rpython_paper_plan_object(
                    cfg={"methodology": {"runs_per_benchmark": 2}},
                    paper_dir=pathlib.Path(tmp),
                    config=config,
                    config_path=pathlib.Path(tmp) / "workload_config.local.json",
                    checkout=checkout,
                    surfaces=surfaces,
                    plan=plan,
                    plan_path=None,
                    dataset_names=["default_performance"],
                    required_runs=2,
                )

        workload = plan["workloads"][0]
        command = workload["command"]
        child = evaluate.command_after_remainder_separator(command)
        self.assertTrue(surfaces["complete"], surfaces)
        self.assertEqual(surfaces["libtest_bench_function_count"], 2, surfaces)
        self.assertIn("--claim-grade-contract", command)
        self.assertIn("rpython-paper-run", command)
        self.assertNotIn("--bench-name-filter", command)
        self.assertEqual(evaluate.command_option_value(child, "--bench"), "bench")
        self.assertNotIn("bench_tokenization", child[child.index("--bench") + 2 :], child)
        self.assertTrue(audit["summary"]["ready_for_reproducible_fallback_execution"], audit)
        self.assertFalse(audit["summary"]["ready_for_paper_equivalent_execution"], audit)
        self.assertFalse(audit["summary"]["ready_for_claim_grade_import"], audit)
        self.assertEqual(audit["summary"]["invalid_workload_count"], 0, audit)
        self.assertEqual(audit["summary"]["claim_grade_blocked_workload_count"], 1, audit)

    def test_rpython_newer_criterion_surface_generates_multi_target_fallback_plan(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            checkout = pathlib.Path(tmp) / "RustPython"
            checkout.mkdir()
            (checkout / "Cargo.toml").write_text(
                "\n".join(
                    [
                        "[package]",
                        'name = "rustpython"',
                        'version = "0.2.0"',
                        "",
                        "[[bench]]",
                        'name = "execution"',
                        'harness = false',
                        "",
                        "[[bench]]",
                        'name = "microbenchmarks"',
                        'harness = false',
                    ]
                )
                + "\n",
                encoding="utf-8",
            )
            benches = checkout / "benches"
            (benches / "benchmarks").mkdir(parents=True)
            (benches / "microbenchmarks").mkdir(parents=True)
            (benches / "benchmarks" / "pystone.py").write_text("print('ok')\n", encoding="utf-8")
            (benches / "microbenchmarks" / "loop.py").write_text("ITERATIONS=1\n", encoding="utf-8")
            (benches / "execution.rs").write_text(
                "\n".join(
                    [
                        "use criterion::{criterion_group, criterion_main, Criterion};",
                        "pub fn criterion_benchmark(c: &mut Criterion) {",
                        '  let benchmark_dir = std::path::Path::new("./benches/benchmarks/");',
                        '  let mut group = c.benchmark_group("execution");',
                        '  group.bench_function("rustpython", |_| {});',
                        "}",
                        "criterion_group!(benches, criterion_benchmark);",
                        "criterion_main!(benches);",
                    ]
                ),
                encoding="utf-8",
            )
            (benches / "microbenchmarks.rs").write_text(
                "\n".join(
                    [
                        "use criterion::{criterion_group, criterion_main, Criterion};",
                        "pub fn criterion_benchmark(c: &mut Criterion) {",
                        '  let benchmark_dir = std::path::Path::new("./benches/microbenchmarks/");',
                        '  let mut group = c.benchmark_group("microbenchmarks");',
                        '  group.bench_function("rustpython", |_| {});',
                        "}",
                        "criterion_group!(benches, criterion_benchmark);",
                        "criterion_main!(benches);",
                    ]
                ),
                encoding="utf-8",
            )
            surfaces = evaluate.parse_rpython_bench_surfaces(checkout)
            config = {
                "claim_grade": False,
                "cargo_bench_json_wrapper": {
                    "bench_name_filter": "bench_tokenization",
                    "child_command_template": [
                        "cargo",
                        "bench",
                        "--package",
                        "rustpython",
                        "--bench",
                        "bench",
                        "bench_tokenization",
                    ],
                },
                "env": {},
            }
            source_contract = {
                "paper_exact_ref_complete": True,
                "claim_grade_complete": False,
                "exact_checkout_pin_complete": False,
                "checkout_pin": {
                    "pinned": True,
                    "exact_pinned": False,
                    "newer_used": True,
                    "non_exact_ref_used": True,
                },
                "blockers": ["adapter checkout used a newer ref; claim-grade paper evidence requires the exact paper ref"],
            }
            required_cell = {
                "dataset": "default_performance",
                "benchmark": "RPython",
                "allocator": "unialloc",
                "role": "paper",
                "category": "macro_single_thread",
            }
            with mock.patch.object(evaluate, "required_performance_sample_cells", return_value=([required_cell], [])), \
                mock.patch.object(evaluate, "rpython_paper_source_contract", return_value=source_contract), \
                mock.patch.object(
                    evaluate,
                    "rpython_paper_source_metadata",
                    return_value={"paper_benchmark": "RPython", "paper_project": "RustPython", "paper_version": "v0.1.2"},
                ):
                plan = evaluate.build_rpython_paper_plan(
                    cfg={"methodology": {"runs_per_benchmark": 2}},
                    paper_dir=pathlib.Path(tmp),
                    config=config,
                    config_path=pathlib.Path(tmp) / "workload_config.local.json",
                    checkout=checkout,
                    surfaces=surfaces,
                    wrapper_script=ROOT / "evaluation" / "scripts" / "paper_external_cargo_bench_json.py",
                    dataset_names=["default_performance"],
                    runs=2,
                    timeout=30,
                )
                audit = evaluate.audit_rpython_paper_plan_object(
                    cfg={"methodology": {"runs_per_benchmark": 2}},
                    paper_dir=pathlib.Path(tmp),
                    config=config,
                    config_path=pathlib.Path(tmp) / "workload_config.local.json",
                    checkout=checkout,
                    surfaces=surfaces,
                    plan=plan,
                    plan_path=None,
                    dataset_names=["default_performance"],
                    required_runs=2,
                )

        self.assertTrue(surfaces["complete"], surfaces)
        self.assertEqual(surfaces["surface_kind"], "criterion", surfaces)
        self.assertEqual(surfaces["cargo_bench_target_names"], ["execution", "microbenchmarks"], surfaces)
        workload = plan["workloads"][0]
        command = workload["command"]
        child = evaluate.command_after_remainder_separator(command)
        self.assertEqual(evaluate.command_option_value(command, "--cargo-bench-targets"), "execution,microbenchmarks")
        self.assertEqual(evaluate.command_option_value(command, "--expected-cargo-bench-targets"), "execution,microbenchmarks")
        self.assertEqual(evaluate.command_option_value(child, "--bench"), "execution")
        self.assertNotIn("bench_tokenization", child, child)
        self.assertTrue(audit["summary"]["ready_for_reproducible_fallback_execution"], audit)
        self.assertFalse(audit["summary"]["ready_for_paper_equivalent_execution"], audit)
        self.assertEqual(audit["summary"]["expected_cargo_bench_targets"], ["execution", "microbenchmarks"], audit)
        self.assertEqual(audit["summary"]["invalid_workload_count"], 0, audit)

    def test_rpython_accepted_newer_source_is_current_ready_not_paper_equivalent(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            checkout = pathlib.Path(tmp) / "RustPython"
            checkout.mkdir()
            (checkout / "Cargo.toml").write_text(
                "\n".join(
                    [
                        "[package]",
                        'name = "rustpython"',
                        'version = "0.2.0"',
                        "",
                        "[[bench]]",
                        'name = "execution"',
                        'harness = false',
                        "",
                        "[[bench]]",
                        'name = "microbenchmarks"',
                        'harness = false',
                    ]
                )
                + "\n",
                encoding="utf-8",
            )
            benches = checkout / "benches"
            (benches / "benchmarks").mkdir(parents=True)
            (benches / "microbenchmarks").mkdir(parents=True)
            (benches / "benchmarks" / "pystone.py").write_text("print('ok')\n", encoding="utf-8")
            (benches / "microbenchmarks" / "loop.py").write_text("ITERATIONS=1\n", encoding="utf-8")
            (benches / "execution.rs").write_text(
                "\n".join(
                    [
                        "use criterion::{criterion_group, criterion_main, Criterion};",
                        "pub fn criterion_benchmark(c: &mut Criterion) {",
                        '  let mut group = c.benchmark_group("execution");',
                        '  group.bench_function("rustpython", |_| {});',
                        "}",
                        "criterion_group!(benches, criterion_benchmark);",
                        "criterion_main!(benches);",
                    ]
                ),
                encoding="utf-8",
            )
            (benches / "microbenchmarks.rs").write_text(
                "\n".join(
                    [
                        "use criterion::{criterion_group, criterion_main, Criterion};",
                        "pub fn criterion_benchmark(c: &mut Criterion) {",
                        '  let mut group = c.benchmark_group("microbenchmarks");',
                        '  group.bench_function("rustpython", |_| {});',
                        "}",
                        "criterion_group!(benches, criterion_benchmark);",
                        "criterion_main!(benches);",
                    ]
                ),
                encoding="utf-8",
            )
            surfaces = evaluate.parse_rpython_bench_surfaces(checkout)
            config = {
                "claim_grade": True,
                "accept_newer_benchmark_source": True,
                "cargo_bench_json_wrapper": {
                    "child_command_template": [
                        "cargo",
                        "bench",
                        "--package",
                        "rustpython",
                        "--bench",
                        "execution",
                    ],
                },
                "env": {},
            }
            source_contract = {
                "paper_exact_ref_complete": True,
                "claim_grade_complete": True,
                "exact_checkout_pin_complete": False,
                "accepted_newer_checkout_pin_complete": True,
                "source_provenance_class": evaluate.USER_ACCEPTED_NEWER_SOURCE_PROVENANCE_CLASS,
                "checkout_pin": {
                    "pinned": True,
                    "exact_pinned": False,
                    "newer_used": True,
                    "non_exact_ref_used": True,
                    "commit": "0123456789abcdef0123456789abcdef01234567",
                },
                "blockers": [],
            }
            required_cell = {
                "dataset": "default_performance",
                "benchmark": "RPython",
                "allocator": "unialloc",
                "role": "paper",
                "category": "macro_single_thread",
            }
            with mock.patch.object(evaluate, "required_performance_sample_cells", return_value=([required_cell], [])), \
                mock.patch.object(evaluate, "rpython_paper_source_contract", return_value=source_contract), \
                mock.patch.object(
                    evaluate,
                    "rpython_paper_source_metadata",
                    return_value={"paper_benchmark": "RPython", "paper_project": "RustPython", "paper_version": "v0.1.2"},
                ):
                plan = evaluate.build_rpython_paper_plan(
                    cfg={"methodology": {"runs_per_benchmark": 2}},
                    paper_dir=pathlib.Path(tmp),
                    config=config,
                    config_path=pathlib.Path(tmp) / "workload_config.local.json",
                    checkout=checkout,
                    surfaces=surfaces,
                    wrapper_script=ROOT / "evaluation" / "scripts" / "paper_external_cargo_bench_json.py",
                    dataset_names=["default_performance"],
                    runs=2,
                    timeout=30,
                )
                audit = evaluate.audit_rpython_paper_plan_object(
                    cfg={"methodology": {"runs_per_benchmark": 2}},
                    paper_dir=pathlib.Path(tmp),
                    config=config,
                    config_path=pathlib.Path(tmp) / "workload_config.local.json",
                    checkout=checkout,
                    surfaces=surfaces,
                    plan=plan,
                    plan_path=None,
                    dataset_names=["default_performance"],
                    required_runs=2,
                )

        self.assertTrue(plan["workloads"][0]["claim_grade"], plan["workloads"][0])
        self.assertEqual(plan["workloads"][0]["claim_grade_scope"], "rpython-current-non-paper-exact-matrix")
        self.assertTrue(audit["summary"]["ready_for_reproducible_fallback_execution"], audit)
        self.assertTrue(audit["summary"]["ready_for_accepted_current_execution"], audit)
        self.assertTrue(audit["summary"]["accepted_current_source_not_paper_exact"], audit)
        self.assertFalse(audit["summary"]["ready_for_paper_equivalent_execution"], audit)
        self.assertFalse(audit["summary"]["ready_for_claim_grade_execution"], audit)
        self.assertFalse(audit["summary"]["ready_for_claim_grade_import"], audit)

    def test_rpython_exact_checkout_without_source_claim_contract_stays_not_paper_ready(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = pathlib.Path(tmp)
            checkout, surfaces = self.make_criterion_rpython_checkout(tmp_path)
            config = {
                "claim_grade": True,
                "cargo_bench_json_wrapper": {
                    "child_command_template": [
                        "cargo",
                        "bench",
                        "--package",
                        "rustpython",
                        "--bench",
                        "execution",
                    ],
                },
                "env": {},
            }
            source_contract = {
                "paper_exact_ref_complete": True,
                "claim_grade_complete": False,
                "exact_checkout_pin_complete": True,
                "source_provenance_class": "paper_exact_ref",
                "checkout_pin": {
                    "pinned": True,
                    "exact_pinned": True,
                    "commit": "1111111111111111111111111111111111111111",
                },
                "blockers": ["exact checkout exists but source contract is still missing claim-grade provenance"],
            }
            with mock.patch.object(evaluate, "required_performance_sample_cells", return_value=([self.rpython_required_cell()], [])), \
                mock.patch.object(evaluate, "rpython_paper_source_contract", return_value=source_contract), \
                mock.patch.object(
                    evaluate,
                    "rpython_paper_source_metadata",
                    return_value={"paper_benchmark": "RPython", "paper_project": "RustPython", "paper_version": "v0.1.2"},
                ):
                plan = evaluate.build_rpython_paper_plan(
                    cfg={"methodology": {"runs_per_benchmark": 2}},
                    paper_dir=tmp_path,
                    config=config,
                    config_path=tmp_path / "workload_config.local.json",
                    checkout=checkout,
                    surfaces=surfaces,
                    wrapper_script=ROOT / "evaluation" / "scripts" / "paper_external_cargo_bench_json.py",
                    dataset_names=["default_performance"],
                    runs=2,
                    timeout=30,
                )

            # Simulate a stale hand-authored plan that forgot workload-level
            # blockers.  The audit must still fail closed from the top-level
            # source_contract instead of treating exact_checkout_pin as enough.
            for workload in plan["workloads"]:
                workload["claim_grade"] = True
                workload["claim_grade_blockers"] = []
                workload.pop("not_claim_grade_reason", None)
            with mock.patch.object(evaluate, "required_performance_sample_cells", return_value=([self.rpython_required_cell()], [])):
                audit = evaluate.audit_rpython_paper_plan_object(
                    cfg={"methodology": {"runs_per_benchmark": 2}},
                    paper_dir=tmp_path,
                    config=config,
                    config_path=tmp_path / "workload_config.local.json",
                    checkout=checkout,
                    surfaces=surfaces,
                    plan=plan,
                    plan_path=None,
                    dataset_names=["default_performance"],
                    required_runs=2,
                )

        self.assertTrue(audit["summary"]["ready_for_reproducible_fallback_execution"], audit)
        self.assertFalse(audit["summary"]["ready_for_paper_exact_execution"], audit)
        self.assertFalse(audit["summary"]["ready_for_paper_equivalent_execution"], audit)
        self.assertFalse(audit["summary"]["ready_for_claim_grade_execution"], audit)
        self.assertGreater(audit["summary"].get("source_contract_blocker_count", 0), 0, audit)

    def test_rpython_config_sync_refuses_fallback_only_readiness(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = pathlib.Path(tmp)
            source_contract = {
                "paper_exact_ref_complete": False,
                "claim_grade_complete": False,
                "exact_checkout_pin_complete": False,
                "reproducible_snapshot_complete": True,
                "source_provenance_class": "reproducible_snapshot",
                "checkout_pin": {"pinned": True, "exact_pinned": False, "commit": "2222222222222222222222222222222222222222"},
                "blockers": ["fallback checkout is reproducible but not paper exact"],
            }
            spec, config_path = self.rpython_sync_fixture(
                tmp_path,
                source_contract=source_contract,
                audit_summary={
                    "ready_for_reproducible_fallback_execution": True,
                    "ready_for_accepted_current_execution": False,
                    "ready_for_paper_exact_execution": False,
                    "ready_for_claim_grade_execution": False,
                    "source_claim_grade_complete": False,
                    "source_exact_checkout_pin_complete": False,
                    "source_provenance_class": "reproducible_snapshot",
                },
            )
            record = evaluate.sync_specialized_claim_grade_config_record("external-RPython", spec, write=True)
            updated = evaluate.read_json(config_path)

        self.assertEqual(record["action"], "skipped", record)
        self.assertFalse(updated["claim_grade"], updated)
        self.assertIn("RPython plan is only fallback-ready", " ".join(record.get("blockers", [])))

    def test_rpython_config_sync_labels_accepted_current_as_non_paper_exact(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = pathlib.Path(tmp)
            source_contract = {
                "paper_exact_ref_complete": True,
                "claim_grade_complete": True,
                "exact_checkout_pin_complete": False,
                "accepted_newer_checkout_pin_complete": True,
                "source_provenance_class": evaluate.USER_ACCEPTED_NEWER_SOURCE_PROVENANCE_CLASS,
                "accepted_newer_policy": {"accepted": True, "reason": "fixture accepts pinned newer checkout"},
                "checkout_pin": {
                    "pinned": True,
                    "exact_pinned": False,
                    "newer_used": True,
                    "commit": "3333333333333333333333333333333333333333",
                    "requested_ref": "main",
                    "resolved_ref": "main",
                },
                "blockers": [],
            }
            spec, config_path = self.rpython_sync_fixture(
                tmp_path,
                source_contract=source_contract,
                audit_summary={
                    "ready_for_reproducible_fallback_execution": True,
                    "ready_for_accepted_current_execution": True,
                    "ready_for_current_non_paper_exact_execution": True,
                    "ready_for_paper_exact_execution": False,
                    "ready_for_claim_grade_execution": False,
                    "source_claim_grade_complete": True,
                    "source_exact_checkout_pin_complete": False,
                    "source_provenance_class": evaluate.USER_ACCEPTED_NEWER_SOURCE_PROVENANCE_CLASS,
                },
            )
            record = evaluate.sync_specialized_claim_grade_config_record("external-RPython", spec, write=True)
            updated = evaluate.read_json(config_path)

        self.assertEqual(record["action"], "updated", record)
        self.assertTrue(updated["claim_grade"], updated)
        self.assertTrue(updated["accept_newer_benchmark_source"], updated)
        self.assertFalse(updated["claim_grade_activation"]["paper_exact"], updated)
        self.assertTrue(updated["claim_grade_activation"]["accepted_newer_benchmark_source"], updated)
        self.assertEqual(updated["plan_claim_grade_metadata"]["claim_grade_scope"], "rpython-current-non-paper-exact-matrix")
        self.assertFalse(updated["cargo_bench_json_wrapper"]["paper_exact"], updated)
        self.assertTrue(updated["cargo_bench_json_wrapper"]["accepted_current_source"], updated)

    def test_rpython_config_sync_labels_exact_checkout_as_paper_exact(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = pathlib.Path(tmp)
            source_contract = {
                "paper_exact_ref_complete": True,
                "claim_grade_complete": True,
                "exact_checkout_pin_complete": True,
                "source_provenance_class": "paper_exact_ref",
                "checkout_pin": {
                    "pinned": True,
                    "exact_pinned": True,
                    "commit": "4444444444444444444444444444444444444444",
                    "requested_ref": "v0.1.2",
                    "resolved_ref": "v0.1.2",
                },
                "blockers": [],
            }
            spec, config_path = self.rpython_sync_fixture(
                tmp_path,
                source_contract=source_contract,
                audit_summary={
                    "ready_for_reproducible_fallback_execution": True,
                    "ready_for_accepted_current_execution": False,
                    "ready_for_current_non_paper_exact_execution": False,
                    "ready_for_paper_exact_execution": True,
                    "ready_for_paper_equivalent_execution": True,
                    "ready_for_claim_grade_execution": True,
                    "source_claim_grade_complete": True,
                    "source_exact_checkout_pin_complete": True,
                    "source_provenance_class": "paper_exact_ref",
                },
            )
            record = evaluate.sync_specialized_claim_grade_config_record("external-RPython", spec, write=True)
            updated = evaluate.read_json(config_path)

        self.assertEqual(record["action"], "updated", record)
        self.assertTrue(updated["claim_grade"], updated)
        self.assertTrue(updated["claim_grade_activation"]["paper_exact"], updated)
        self.assertFalse(updated["claim_grade_activation"]["accepted_newer_benchmark_source"], updated)
        self.assertEqual(updated["plan_claim_grade_metadata"]["claim_grade_scope"], "rpython-paper-exact-matrix")
        self.assertTrue(updated["cargo_bench_json_wrapper"]["paper_exact"], updated)
        self.assertFalse(updated["cargo_bench_json_wrapper"]["accepted_current_source"], updated)


class RRedisPaperPlanTests(unittest.TestCase):
    def rredis_config(self, checkout: pathlib.Path) -> dict:
        return {
            "claim_grade": False,
            "real_workload_dir": str(checkout),
            "command_template": [
                "python3",
                str(ROOT / "evaluation" / "scripts" / "paper_external_rredis_runner.py"),
                "--real-workload-dir",
                "{real_workload_dir}",
                "--redis-wrapper",
                str(ROOT / "evaluation" / "scripts" / "paper_external_redis_benchmark_json.py"),
                "--dataset",
                "{dataset}",
                "--benchmark",
                "{benchmark}",
                "--allocator",
                "{allocator}",
                "--variant-feature",
                "{variant_feature}",
                "--run-index",
                "{run_index}",
                "--redis-benchmark-bin",
                "/tmp/redis-benchmark",
                "--host",
                "127.0.0.1",
                "--port",
                "16379",
                "--requests",
                "10",
                "--clients",
                "1",
                "--tests",
                "set,get",
                "--",
                "cargo",
                "run",
                "--package",
                "rsedis",
                "--bin",
                "rsedis",
            ],
            "env": {
                "UNIALLOC_PAPER_ALLOCATOR": "{allocator}",
                "UNIALLOC_PAPER_BENCHMARK": "{benchmark}",
                "UNIALLOC_PAPER_DATASET": "{dataset}",
                "UNIALLOC_PAPER_RUN_INDEX": "{run_index}",
            },
        }

    def rredis_fixture_audit_json(self, path: pathlib.Path) -> dict:
        name = pathlib.Path(path).name
        if name == "rredis_paper_command_contract_audit.json":
            return {
                "summary": {
                    "uses_redis_benchmark": True,
                    "redis_benchmark_available": True,
                    "benchmark_owned_json": True,
                    "paper_command_specified": False,
                    "paper_command_provenance_verified": False,
                    "ready_for_claim_grade_import": False,
                }
            }
        if name == "rredis_allocator_routing_audit.json":
            return {"summary": {"allocator_routing_verified": True}}
        if name == "redis_benchmark_tool_probe.json":
            return {"available": True, "selected_path": "/tmp/redis-benchmark", "claim_grade": False}
        return {}

    def test_rredis_plan_covers_required_cells_and_keeps_claim_gate_fail_closed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = pathlib.Path(tmp)
            checkout = tmp_path / "Rsedis"
            checkout.mkdir()
            config = self.rredis_config(checkout)
            source_contract = {
                "paper_exact_ref_complete": False,
                "reproducible_snapshot_complete": True,
                "claim_grade_complete": False,
                "checkout_pin": {"pinned": True, "exact_pinned": False, "fallback_used": True},
                "blockers": ["paper exact ref is not known"],
            }
            required_cells = [
                {
                    "dataset": "default_performance",
                    "benchmark": "RRedis*",
                    "allocator": allocator,
                    "role": "paper",
                    "category": "macro_multi_thread",
                    "variant_feature": "",
                    "paper_row_id": "rredis",
                    "reference": "paper/data/default.dat",
                }
                for allocator in ("unialloc", "jemalloc")
            ]

            with mock.patch.object(evaluate, "required_performance_sample_cells", return_value=(required_cells, [])), \
                mock.patch.object(evaluate, "rredis_paper_source_contract", return_value=source_contract), \
                mock.patch.object(evaluate, "rredis_paper_source_metadata", return_value={"paper_benchmark": "RRedis*"}), \
                mock.patch.object(evaluate, "read_json_if_exists", side_effect=self.rredis_fixture_audit_json):
                plan = evaluate.build_rredis_paper_plan(
                    cfg={"methodology": {"runs_per_benchmark": 2}},
                    paper_dir=tmp_path,
                    config=config,
                    config_path=tmp_path / "workload_config.local.json",
                    checkout=checkout,
                    dataset_names=["default_performance"],
                    runs=2,
                    timeout=30,
                )
                audit = evaluate.audit_rredis_paper_plan_object(
                    cfg={"methodology": {"runs_per_benchmark": 2}},
                    paper_dir=tmp_path,
                    config=config,
                    config_path=tmp_path / "workload_config.local.json",
                    checkout=checkout,
                    plan=plan,
                    plan_path=None,
                    dataset_names=["default_performance"],
                    required_runs=2,
                )

        self.assertEqual(len(plan["workloads"]), 2, plan)
        command = plan["workloads"][0]["command"]
        self.assertIn("paper_external_rredis_runner.py", " ".join(command))
        self.assertIn("paper_external_redis_benchmark_json.py", " ".join(command))
        self.assertIn("--redis-benchmark-bin", command)
        self.assertEqual(plan["workloads"][0]["env"]["UNIALLOC_PAPER_RUN_INDEX"], "{run_index}")
        self.assertTrue(audit["summary"]["ready_for_reproducible_fallback_execution"], audit)
        self.assertFalse(audit["summary"]["ready_for_paper_equivalent_execution"], audit)
        self.assertFalse(audit["summary"]["ready_for_claim_grade_import"], audit)
        self.assertEqual(audit["summary"]["planned_workload_count"], 2, audit)
        self.assertEqual(audit["summary"]["missing_cell_count"], 0, audit)
        self.assertEqual(audit["summary"]["insufficient_cell_count"], 0, audit)
        self.assertEqual(audit["summary"]["invalid_workload_count"], 0, audit)
        self.assertEqual(audit["summary"]["claim_grade_blocked_workload_count"], 2, audit)
        self.assertTrue(
            any("paper does not specify an exact redis-benchmark command" in item for item in audit["audited_workloads"][0]["claim_grade_blockers"]),
            audit["audited_workloads"][0],
        )

    def test_rredis_prepare_probe_is_bounded_and_does_not_invalidate_claim_blocked_plan(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = pathlib.Path(tmp)
            checkout = tmp_path / "Rsedis"
            checkout.mkdir()
            config = self.rredis_config(checkout)
            source_contract = {
                "paper_exact_ref_complete": False,
                "reproducible_snapshot_complete": True,
                "claim_grade_complete": False,
                "checkout_pin": {"pinned": True, "exact_pinned": False, "fallback_used": True},
                "blockers": ["paper exact ref is not known"],
            }
            required_cells = [
                {
                    "dataset": "default_performance",
                    "benchmark": "RRedis*",
                    "allocator": allocator,
                    "role": "paper",
                    "category": "macro_multi_thread",
                    "variant_feature": "",
                }
                for allocator in ("unialloc", "jemalloc", "mimalloc")
            ]

            with mock.patch.object(evaluate, "required_performance_sample_cells", return_value=(required_cells, [])), \
                mock.patch.object(evaluate, "rredis_paper_source_contract", return_value=source_contract), \
                mock.patch.object(evaluate, "rredis_paper_source_metadata", return_value={"paper_benchmark": "RRedis*"}), \
                mock.patch.object(evaluate, "read_json_if_exists", side_effect=self.rredis_fixture_audit_json), \
                mock.patch.object(
                    evaluate,
                    "rredis_plan_prepare_probe",
                    return_value={"ok": True, "returncode": 0, "prepare_record": {"prepared": True}},
                ) as prepare_probe:
                plan = evaluate.build_rredis_paper_plan(
                    cfg={"methodology": {"runs_per_benchmark": 1}},
                    paper_dir=tmp_path,
                    config=config,
                    config_path=tmp_path / "workload_config.local.json",
                    checkout=checkout,
                    dataset_names=["default_performance"],
                    runs=1,
                    timeout=30,
                )
                audit = evaluate.audit_rredis_paper_plan_object(
                    cfg={"methodology": {"runs_per_benchmark": 1}},
                    paper_dir=tmp_path,
                    config=config,
                    config_path=tmp_path / "workload_config.local.json",
                    checkout=checkout,
                    plan=plan,
                    plan_path=None,
                    dataset_names=["default_performance"],
                    required_runs=1,
                    prepare_probe=True,
                    prepare_probe_limit=1,
                    prepare_probe_timeout=5,
                )

        self.assertEqual(prepare_probe.call_count, 1)
        self.assertEqual(audit["summary"]["prepare_probe_count"], 1, audit)
        self.assertEqual(audit["summary"]["prepare_probe_skipped_count"], 2, audit)
        self.assertEqual(audit["summary"]["prepare_probe_failure_count"], 0, audit)
        self.assertTrue(audit["summary"]["ready_for_reproducible_fallback_execution"], audit)
        self.assertFalse(audit["summary"]["ready_for_paper_equivalent_execution"], audit)

    def test_rredis_accepted_current_plan_ready_after_prepare_probe_without_paper_exact_promotion(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = pathlib.Path(tmp)
            checkout = tmp_path / "Rsedis"
            checkout.mkdir()
            config = self.rredis_config(checkout)
            source_contract = {
                "paper_exact_ref_complete": False,
                "reproducible_snapshot_complete": True,
                "claim_grade_complete": True,
                "accepted_newer_checkout_pin_complete": True,
                "source_provenance_class": evaluate.USER_ACCEPTED_NEWER_SOURCE_PROVENANCE_CLASS,
                "checkout_pin": {
                    "pinned": True,
                    "exact_pinned": False,
                    "fallback_used": True,
                    "non_exact_ref_used": True,
                    "commit": "0123456789abcdef0123456789abcdef01234567",
                },
                "blockers": [],
            }
            required_cells = [
                {
                    "dataset": "default_performance",
                    "benchmark": "RRedis*",
                    "allocator": allocator,
                    "role": "paper",
                    "category": "macro_multi_thread",
                    "variant_feature": "",
                }
                for allocator in ("unialloc", "jemalloc")
            ]

            with mock.patch.object(evaluate, "required_performance_sample_cells", return_value=(required_cells, [])), \
                mock.patch.object(evaluate, "rredis_paper_source_contract", return_value=source_contract), \
                mock.patch.object(evaluate, "rredis_paper_source_metadata", return_value={"paper_benchmark": "RRedis*"}), \
                mock.patch.object(evaluate, "read_json_if_exists", side_effect=self.rredis_fixture_audit_json), \
                mock.patch.object(
                    evaluate,
                    "rredis_plan_prepare_probe",
                    return_value={"ok": True, "returncode": 0, "prepare_record": {"prepared": True}},
                ):
                plan = evaluate.build_rredis_paper_plan(
                    cfg={"methodology": {"runs_per_benchmark": 1}},
                    paper_dir=tmp_path,
                    config=config,
                    config_path=tmp_path / "workload_config.local.json",
                    checkout=checkout,
                    dataset_names=["default_performance"],
                    runs=1,
                    timeout=30,
                    allow_newer_benchmark_sources=True,
                )
                audit = evaluate.audit_rredis_paper_plan_object(
                    cfg={"methodology": {"runs_per_benchmark": 1}},
                    paper_dir=tmp_path,
                    config=config,
                    config_path=tmp_path / "workload_config.local.json",
                    checkout=checkout,
                    plan=plan,
                    plan_path=None,
                    dataset_names=["default_performance"],
                    required_runs=1,
                    prepare_probe=True,
                    prepare_probe_limit=0,
                    prepare_probe_timeout=5,
                    allow_newer_benchmark_sources=True,
                )

        self.assertTrue(audit["summary"]["ready_for_reproducible_fallback_execution"], audit)
        self.assertTrue(audit["summary"]["ready_for_accepted_current_execution"], audit)
        self.assertTrue(audit["summary"]["accepted_current_source_not_paper_exact"], audit)
        self.assertFalse(audit["summary"]["ready_for_paper_equivalent_execution"], audit)
        self.assertFalse(audit["summary"]["ready_for_claim_grade_execution"], audit)
        self.assertEqual(audit["summary"]["prepare_probe_count"], 2, audit)

    def test_sync_rredis_current_claim_grade_config_writes_non_paper_exact_contract(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = pathlib.Path(tmp)
            config_path = tmp_path / "workload_config.local.json"
            plan_path = tmp_path / "rredis-plan.json"
            audit_path = tmp_path / "rredis-plan-audit.json"
            source_contract = {
                "accepted_newer_checkout_pin_complete": True,
                "claim_grade_complete": True,
                "source_provenance_class": evaluate.USER_ACCEPTED_NEWER_SOURCE_PROVENANCE_CLASS,
                "checkout_pin": {
                    "commit": "0123456789abcdef0123456789abcdef01234567",
                    "requested_ref": "0123456789abcdef0123456789abcdef01234567",
                    "resolved_ref": "0123456789abcdef0123456789abcdef01234567",
                    "fallback_used": True,
                    "non_exact_ref_used": True,
                    "pinned": True,
                },
                "accepted_newer_policy": {"accepted": True, "reason": "unit test"},
            }
            config = self.rredis_config(tmp_path / "Rsedis")
            config["not_claim_grade_reason"] = "old paper-exact blocker"
            config_path.write_text(json.dumps(config), encoding="utf-8")
            plan_path.write_text(
                json.dumps(
                    {
                        "workloads": [
                            {
                                "benchmark": "RRedis*",
                                "command": config["command_template"],
                                "source_contract": source_contract,
                            }
                        ],
                        "source_contract": source_contract,
                    }
                ),
                encoding="utf-8",
            )
            audit_path.write_text(
                json.dumps(
                    {
                        "summary": {
                            "ready_for_accepted_current_execution": True,
                            "source_claim_grade_complete": True,
                            "redis_benchmark_available": True,
                            "allocator_routing_verified": True,
                            "redis_benchmark_json_contract_ready": True,
                            "prepare_probe_enabled": True,
                            "prepare_probe_count": 2,
                            "prepare_probe_failure_count": 0,
                        }
                    }
                ),
                encoding="utf-8",
            )

            record = evaluate.sync_rredis_claim_grade_config_record(
                "external-RRedis",
                {
                    "label": "RRedis",
                    "config": config_path,
                    "plan": plan_path,
                    "plan_audit": audit_path,
                    "benchmark": "RRedis*",
                    "contract": "rredis-redis-benchmark-current-run",
                    "requires_audit_key": "ready_for_accepted_current_execution",
                },
                write=True,
            )
            updated = json.loads(config_path.read_text(encoding="utf-8"))

        self.assertEqual(record["action"], "updated", record)
        self.assertTrue(updated["claim_grade"], updated)
        self.assertFalse(updated["claim_grade_activation"]["paper_exact"], updated)
        self.assertTrue(updated["accepted_newer_benchmark_source"]["accepted"], updated)
        self.assertNotIn("not_claim_grade_reason", updated)
        command = updated["command_template"]
        self.assertIn("--claim-grade-contract", command)
        self.assertLess(command.index("--claim-grade-contract"), command.index("--"))
        self.assertIn("--paper-source-contract-json", command)


def roxipng_test_workload(allocator: str, target: str, leaf: str = "") -> dict:
    command = [
        "python3",
        "evaluation/scripts/paper_external_cargo_bench_json.py",
        "--dataset",
        "type_isolation",
        "--benchmark",
        "R-Oxipng*",
        "--allocator",
        allocator,
        "--variant-feature",
        "type_isolation",
        "--run-index",
        "{run_index}",
        "--cargo-bench-targets",
        target,
        "--",
        "cargo",
        "bench",
        "--bench",
        "deflate",
    ]
    if leaf:
        command.append(leaf)
    workload = {
        "dataset": "type_isolation",
        "benchmark": "R-Oxipng*",
        "allocator": allocator,
        "variant_feature": "type_isolation",
        "runs": 6,
        "command": command,
        "cwd": ".",
        "measurement": "stdout_json",
        "time_fields": ["seconds"],
        "target_split": True,
        "selected_cargo_bench_targets": [target],
        "split_cargo_bench_target": target,
        "plan_fragment_id": f"type_isolation|R-Oxipng*|{allocator}|{target}" + (f"|{leaf}" if leaf else ""),
        "expected_libtest_bench_functions_by_target": {
            target: [leaf] if leaf else [f"{target}_leaf"]
        },
    }
    if leaf:
        workload["target_leaf_split"] = True
        workload["split_libtest_bench_function"] = leaf
    return workload


class RoxipngPaperPlanTargetCommandTests(unittest.TestCase):
    def test_roxipng_target_split_plan_aligns_child_bench_target(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = pathlib.Path(tmp)
            required_cell = {
                "dataset": "type_isolation",
                "benchmark": "R-Oxipng*",
                "allocator": "jemalloc",
                "role": "comparison",
                "category": "macro_multi_thread",
                "variant_feature": "type_isolation",
            }
            surfaces = {
                "cargo_bench_targets": [{"name": "deflate"}, {"name": "filters"}],
                "libtest_bench_functions": ["deflate_leaf", "filters_leaf"],
                "libtest_bench_functions_by_target": {
                    "deflate": ["deflate_leaf"],
                    "filters": ["filters_leaf"],
                },
            }
            config = {
                "claim_grade": True,
                "cargo_bench_json_wrapper": {
                    "child_command_template": ["cargo", "bench", "--bench", "deflate"]
                },
            }

            with mock.patch.object(evaluate, "required_performance_sample_cells", return_value=([required_cell], [])):
                plan = evaluate.build_roxipng_paper_plan(
                    cfg={"methodology": {"runs_per_benchmark": 1}},
                    paper_dir=tmp_path,
                    config=config,
                    config_path=tmp_path / "workload_config.local.json",
                    checkout=tmp_path,
                    surfaces=surfaces,
                    wrapper_script=pathlib.Path("evaluation/scripts/paper_external_cargo_bench_json.py"),
                    dataset_names=["type_isolation"],
                    runs=1,
                    timeout=30,
                    split_cargo_targets=True,
                )

        by_target = {workload["split_cargo_bench_target"]: workload for workload in plan["workloads"]}
        filters_child = evaluate.command_after_remainder_separator(by_target["filters"]["command"])
        self.assertEqual(evaluate.command_bench_targets(filters_child), ["filters"])
        self.assertEqual(
            evaluate.command_explicit_cargo_bench_targets(by_target["filters"]["command"]),
            ["filters"],
        )
        self.assertEqual(
            by_target["filters"]["claim_grade_scope"],
            "roxipng-paper-equivalent-matrix-fragment",
        )

    def test_roxipng_plan_execution_ready_is_not_timing_import_ready(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = pathlib.Path(tmp)
            required_cell = {
                "dataset": "type_isolation",
                "benchmark": "R-Oxipng*",
                "allocator": "jemalloc",
                "role": "comparison",
                "category": "macro_multi_thread",
                "variant_feature": "type_isolation",
            }
            surfaces = {
                "cargo_bench_targets": [{"name": "deflate"}, {"name": "filters"}],
                "libtest_bench_functions": ["deflate_leaf", "filters_leaf"],
                "libtest_bench_functions_by_target": {
                    "deflate": ["deflate_leaf"],
                    "filters": ["filters_leaf"],
                },
            }
            config = {
                "claim_grade": True,
                "cargo_bench_json_wrapper": {
                    "child_command_template": ["cargo", "bench", "--bench", "deflate"]
                },
            }
            with mock.patch.object(evaluate, "required_performance_sample_cells", return_value=([required_cell], [])):
                plan = evaluate.build_roxipng_paper_plan(
                    cfg={"methodology": {"runs_per_benchmark": 1}},
                    paper_dir=tmp_path,
                    config=config,
                    config_path=tmp_path / "workload_config.local.json",
                    checkout=tmp_path,
                    surfaces=surfaces,
                    wrapper_script=pathlib.Path("evaluation/scripts/paper_external_cargo_bench_json.py"),
                    dataset_names=["type_isolation"],
                    runs=1,
                    timeout=30,
                    split_cargo_targets=True,
                )
                audit = evaluate.audit_roxipng_paper_plan_object(
                    cfg={"methodology": {"runs_per_benchmark": 1}},
                    paper_dir=tmp_path,
                    config=config,
                    config_path=tmp_path / "workload_config.local.json",
                    checkout=tmp_path,
                    surfaces=surfaces,
                    plan=plan,
                    plan_path=None,
                    dataset_names=["type_isolation"],
                    required_runs=1,
                )

        self.assertTrue(audit["summary"]["ready_for_paper_equivalent_execution"], audit)
        self.assertTrue(audit["summary"]["ready_for_claim_grade_execution"], audit)
        self.assertFalse(audit["summary"]["ready_for_claim_grade_import"], audit)
        self.assertIn("no repeated timing sample", audit["summary"]["claim_grade_import_blocker"])

    def test_roxipng_plan_audit_rejects_wrapper_child_target_mismatch(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = pathlib.Path(tmp)
            required_cell = {
                "dataset": "type_isolation",
                "benchmark": "R-Oxipng*",
                "allocator": "jemalloc",
                "role": "comparison",
                "category": "macro_multi_thread",
                "variant_feature": "type_isolation",
            }
            surfaces = {
                "cargo_bench_targets": [{"name": "deflate"}, {"name": "filters"}],
                "libtest_bench_functions": ["deflate_leaf", "filters_leaf"],
                "libtest_bench_functions_by_target": {
                    "deflate": ["deflate_leaf"],
                    "filters": ["filters_leaf"],
                },
            }
            mismatched = roxipng_test_workload("jemalloc", "filters")
            self.assertEqual(
                evaluate.command_explicit_cargo_bench_targets(mismatched["command"]),
                ["filters"],
            )
            self.assertEqual(
                evaluate.command_bench_targets(evaluate.command_after_remainder_separator(mismatched["command"])),
                ["deflate"],
            )

            with mock.patch.object(evaluate, "required_performance_sample_cells", return_value=([required_cell], [])):
                audit = evaluate.audit_roxipng_paper_plan_object(
                    cfg={"methodology": {"runs_per_benchmark": 6}},
                    paper_dir=tmp_path,
                    config={"claim_grade": True},
                    config_path=tmp_path / "workload_config.local.json",
                    checkout=tmp_path,
                    surfaces=surfaces,
                    plan={"runs": 6, "target_split": True, "workloads": [mismatched]},
                    plan_path=None,
                    dataset_names=["type_isolation"],
                    required_runs=6,
                )

        self.assertEqual(audit["summary"]["invalid_workload_count"], 1, audit)
        self.assertTrue(
            any(
                "target-split workload wrapper target(s) do not match child cargo bench target(s)"
                in issue
                for issue in audit["audited_workloads"][0]["issues"]
            ),
            audit["audited_workloads"][0],
        )


class RoxipngClaimMatrixGapPlanTests(unittest.TestCase):
    def test_template_rendering_leaves_json_arguments_literal(self) -> None:
        value = ['--json', '{"blockers":[],"checkout_pin":{"pinned":true}}', '--run', '{run_index}']
        rendered = evaluate.render_paper_performance_template_value(
            value,
            {"dataset": "type_isolation", "benchmark": "R-Oxipng*", "allocator": "mimalloc"},
            extra_fields={"run_index": "3"},
        )
        self.assertEqual(rendered[1], value[1])
        self.assertEqual(rendered[3], "3")

    def test_raw_probe_selects_one_short_fragment_per_missing_cell(self) -> None:
        claim_matrix = {
            "summary": {"required_runs_per_cell": 6},
            "missing_raw_cells": [
                {
                    "dataset": "type_isolation",
                    "benchmark": "R-Oxipng*",
                    "allocator": "jemalloc",
                    "missing_surface_run_indexes": [1, 2, 3, 4, 5, 6],
                },
                {
                    "dataset": "type_isolation",
                    "benchmark": "R-Oxipng*",
                    "allocator": "mimalloc",
                    "missing_surface_run_indexes": [1, 2, 3, 4, 5, 6],
                },
            ],
        }
        source_plan = {
            "runs": 6,
            "workloads": [
                roxipng_test_workload("jemalloc", "zopfli", "zopfli_16_bits_strategy_0"),
                roxipng_test_workload("jemalloc", "deflate"),
                roxipng_test_workload("mimalloc", "zopfli", "zopfli_16_bits_strategy_0"),
                roxipng_test_workload("mimalloc", "deflate"),
            ],
        }

        gap_plan = evaluate.build_roxipng_claim_matrix_gap_plan(
            claim_matrix=claim_matrix,
            source_plan=source_plan,
            source_plan_path=pathlib.Path("source.json"),
            gap_plan_path=pathlib.Path("gap.json"),
            mode="raw-probe",
        )

        self.assertEqual(gap_plan["summary"]["selected_workload_count"], 2)
        self.assertEqual(
            [w["split_cargo_bench_target"] for w in gap_plan["workloads"]],
            ["deflate", "deflate"],
        )
        self.assertEqual([w["run_indexes"] for w in gap_plan["workloads"]], [[1], [1]])
        self.assertEqual([w["runs"] for w in gap_plan["workloads"]], [1, 1])

    def test_filtered_gap_cli_is_raw_only_unless_explicitly_published(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = pathlib.Path(tmp)
            results_dir = tmp_path / "results"
            results_dir.mkdir()
            source_plan_path = tmp_path / "source-plan.json"
            claim_matrix_path = tmp_path / "claim-matrix.json"
            source_plan_path.write_text(
                json.dumps(
                    {
                        "runs": 6,
                        "workloads": [
                            roxipng_test_workload("jemalloc", "deflate"),
                            roxipng_test_workload("mimalloc", "deflate"),
                        ],
                    }
                ),
                encoding="utf-8",
            )
            claim_matrix_path.write_text(
                json.dumps(
                    {
                        "summary": {"required_runs_per_cell": 6},
                        "missing_raw_cells": [
                            {
                                "dataset": "type_isolation",
                                "benchmark": "R-Oxipng*",
                                "allocator": "jemalloc",
                                "missing_surface_run_indexes": [1, 2],
                            },
                            {
                                "dataset": "type_isolation",
                                "benchmark": "R-Oxipng*",
                                "allocator": "mimalloc",
                                "missing_surface_run_indexes": [1, 2],
                            },
                        ],
                    }
                ),
                encoding="utf-8",
            )
            args = argparse.Namespace(
                plan=str(source_plan_path),
                claim_matrix_audit=str(claim_matrix_path),
                mode="raw-probe",
                dataset=None,
                allocator=["jemalloc"],
                target=None,
                prefer_target=None,
                runs_per_workload=1,
                max_workloads=1,
                run_id="filtered",
                output_dir=str(tmp_path / "raw-filtered"),
                plan_out=None,
                no_update_results=False,
                publish_filtered_results=False,
            )

            with mock.patch.object(evaluate, "RESULTS", results_dir), \
                contextlib.redirect_stdout(io.StringIO()), \
                contextlib.redirect_stderr(io.StringIO()):
                rc = evaluate.select_roxipng_claim_matrix_gaps(args)

            self.assertEqual(rc, 0)
            self.assertTrue((tmp_path / "raw-filtered" / "roxipng-claim-matrix-gap-plan.json").exists())
            self.assertFalse((results_dir / "roxipng_claim_matrix_gap_plan.json").exists())

            args.output_dir = str(tmp_path / "raw-published")
            args.publish_filtered_results = True
            with mock.patch.object(evaluate, "RESULTS", results_dir), \
                contextlib.redirect_stdout(io.StringIO()), \
                contextlib.redirect_stderr(io.StringIO()):
                rc = evaluate.select_roxipng_claim_matrix_gaps(args)

            self.assertEqual(rc, 0)
            self.assertTrue((results_dir / "roxipng_claim_matrix_gap_plan.json").exists())

    def test_overclaim_detail_reports_roxipng_gap_frontier(self) -> None:
        detail = {
            "dataset": "type_isolation",
            "current_dataset": {"complete_for_claim": False, "claim_grade": False},
            "roxipng_claim_matrix_audit_summary": {
                "ready_for_claim_grade_import": False,
                "surface_complete_cell_count": 29,
                "raw_timing_covered_cell_count": 35,
                "required_cell_count": 35,
                "claim_usable_cell_count": 0,
                "observed_libtest_bench_function_count": 97,
                "expected_libtest_bench_function_count": 97,
            },
            "roxipng_claim_matrix_gap_plan_mode": "surface",
            "roxipng_claim_matrix_gap_plan_summary": {
                "ready_to_run_gap_plan": True,
                "gap_cell_count": 6,
                "selected_workload_count": 60,
                "selected_run_item_count": 354,
            },
        }

        missing = evaluate.overclaim_missing_requirements("paper_performance", detail)
        refs = evaluate.overclaim_artifact_refs("paper_performance")

        self.assertTrue(
            any("R-Oxipng executable gap frontier is ready" in item for item in missing),
            missing,
        )
        self.assertIn("roxipng_claim_matrix_gap_plan", refs)

    def test_surface_gap_preserves_exact_missing_run_indexes(self) -> None:
        claim_matrix = {
            "summary": {"required_runs_per_cell": 6},
            "missing_surface_cells": [],
            "insufficient_surface_cells": [
                {
                    "dataset": "type_isolation",
                    "benchmark": "R-Oxipng*",
                    "allocator": "jemalloc",
                    "missing_surface_run_indexes": [3, 5],
                    "observed_runs": [
                        {
                            "run_index": 3,
                            "observed_cargo_bench_targets": ["deflate"],
                            "observed_libtest_bench_functions": ["deflate_leaf"],
                        }
                    ],
                }
            ],
        }
        source_plan = {
            "runs": 6,
            "workloads": [
                roxipng_test_workload("jemalloc", "deflate"),
                roxipng_test_workload("jemalloc", "zopfli", "zopfli_16_bits_strategy_0"),
            ],
        }

        gap_plan = evaluate.build_roxipng_claim_matrix_gap_plan(
            claim_matrix=claim_matrix,
            source_plan=source_plan,
            source_plan_path=pathlib.Path("source.json"),
            gap_plan_path=pathlib.Path("gap.json"),
            mode="surface",
        )

        self.assertEqual(gap_plan["summary"]["selected_workload_count"], 2)
        self.assertEqual(gap_plan["summary"]["selected_run_item_count"], 3)
        run_indexes_by_target = {
            workload["split_cargo_bench_target"]: workload["run_indexes"]
            for workload in gap_plan["workloads"]
        }
        self.assertEqual(run_indexes_by_target["deflate"], [5])
        self.assertEqual(run_indexes_by_target["zopfli"], [3, 5])
        for workload in gap_plan["workloads"]:
            self.assertEqual(evaluate.plan_workload_run_indexes(workload, 6), workload["run_indexes"])
            self.assertEqual(evaluate.plan_workload_runs(workload, 6), len(workload["run_indexes"]))

    def test_claim_usable_gap_skips_already_claim_grade_fragments(self) -> None:
        claim_matrix = {
            "summary": {"required_runs_per_cell": 6},
            "missing_claim_usable_cells": [],
            "insufficient_claim_usable_cells": [
                {
                    "dataset": "type_isolation",
                    "benchmark": "R-Oxipng*",
                    "allocator": "jemalloc",
                    "missing_claim_usable_run_indexes": [1, 2],
                    "observed_runs": [
                        {
                            "run_index": 1,
                            "effective_fragment_records": [
                                {
                                    "fragment_key": "deflate|deflate_leaf",
                                    "claim_usable": True,
                                    "successful_timing": True,
                                }
                            ],
                        }
                    ],
                }
            ],
        }
        source_plan = {
            "runs": 6,
            "workloads": [
                roxipng_test_workload("jemalloc", "deflate"),
                roxipng_test_workload("jemalloc", "zopfli", "zopfli_16_bits_strategy_0"),
            ],
        }

        gap_plan = evaluate.build_roxipng_claim_matrix_gap_plan(
            claim_matrix=claim_matrix,
            source_plan=source_plan,
            source_plan_path=pathlib.Path("source.json"),
            gap_plan_path=pathlib.Path("gap.json"),
            mode="claim-usable",
        )

        run_indexes_by_target = {
            workload["split_cargo_bench_target"]: workload["run_indexes"]
            for workload in gap_plan["workloads"]
        }
        self.assertEqual(run_indexes_by_target["deflate"], [2])
        self.assertEqual(run_indexes_by_target["zopfli"], [1, 2])

    def test_claim_matrix_prefers_new_claim_grade_fragment_over_old_diagnostic_duplicate(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = pathlib.Path(tmp)
            evidence_path = tmp_path / "stdout.txt"
            evidence_path.write_text("test bench_unit ... bench:  1,234 ns/iter (+/- 56)\n", encoding="utf-8")
            digest = hashlib.sha256(evidence_path.read_bytes()).hexdigest()
            source_contract = {
                "paper_exact_ref_complete": True,
                "claim_grade_complete": True,
                "exact_checkout_pin_complete": True,
                "checkout_pin": {"pinned": True, "exact_pinned": True},
                "blockers": [],
            }
            base = {
                "source": "paper-external-cargo-bench-json",
                "success": True,
                "dataset": "type_isolation",
                "benchmark": "R-Oxipng*",
                "allocator": "jemalloc",
                "run_index": 1,
                "seconds": 0.001,
                "ns_per_iter": 1234.0,
                "benchmark_owned_json": True,
                "measurement_source": "libtest_bench_stdout",
                "command": ["cargo", "bench", "--bench", "deflate"],
                "host_system": "Linux",
                "evidence": [{"path": str(evidence_path), "sha256": digest}],
                "bench_rows": [
                    {
                        "name": "bench_unit",
                        "ns_per_iter": 1234.0,
                        "measurement_source": "libtest_bench_stdout",
                        "cargo_bench_target": "deflate",
                    }
                ],
                "cargo_bench_targets": ["deflate"],
                "source_contract": source_contract,
                "source_provenance_class": "paper_exact_ref",
            }
            old_diagnostic = {
                **base,
                "claim_grade": False,
                "claim_grade_blockers": ["old adapter diagnostic duplicate"],
            }
            new_claim_grade = {
                **base,
                "claim_grade": True,
                "claim_grade_scope": "roxipng-paper-equivalent-matrix-fragment",
                "claim_grade_blockers": [],
            }
            samples = tmp_path / "samples.jsonl"
            samples.write_text(
                json.dumps(old_diagnostic) + "\n" + json.dumps(new_claim_grade) + "\n",
                encoding="utf-8",
            )
            cfg = {"methodology": {"runs_per_benchmark": 1}}
            surfaces = {
                "cargo_bench_targets": [{"name": "deflate"}],
                "libtest_bench_functions": ["bench_unit"],
            }
            required_cell = {
                "dataset": "type_isolation",
                "benchmark": "R-Oxipng*",
                "allocator": "jemalloc",
                "role": "paper",
            }

            with mock.patch.object(evaluate, "required_performance_sample_cells", return_value=([required_cell], [])):
                audit = evaluate.build_roxipng_claim_matrix_audit(
                    cfg=cfg,
                    paper_dir=tmp_path,
                    config={"claim_grade": True},
                    config_path=tmp_path / "workload_config.local.json",
                    checkout=tmp_path,
                    surfaces=surfaces,
                    sample_audit={"sample_path": str(samples)},
                    sample_audit_path=samples,
                    dataset_names=["type_isolation"],
                    required_runs=1,
                )

        self.assertEqual(audit["summary"]["claim_usable_cell_count"], 1, audit)
        observed_run = audit["cells"][0]["observed_runs"][0]
        self.assertTrue(observed_run["claim_usable"], observed_run)
        self.assertEqual(observed_run["effective_claim_usable_record_count"], 1, observed_run)
        self.assertEqual(observed_run["effective_issues"], [], observed_run)


def pac_direct_hardware_fixture() -> dict:
    return {
        "source": "pac-metadata-direct-probe",
        "run_id": "pac-direct-hardware-fixture",
        "summary": {
            "probe_validated": True,
            "claim_grade": True,
            "allocator_validated": True,
            "hardware_pac_validated": True,
            "software_fallback_validated": False,
            "context_binding_active": True,
            "software_fallback_active": False,
            "pac_probe_active_key": "ib",
            "pac_probe_signed_changed": True,
            "pac_probe_wrong_context_rejected": True,
            "typed_cache_inserts": 1,
            "typed_cache_hits": 1,
            "pac_signs": 2,
            "pac_verifications": 1,
            "pac_failures": 0,
            "pac_software_fallback_signs": 0,
            "pac_software_fallback_verifications": 0,
            "pac_software_fallback_failures": 0,
        },
    }


def pac_direct_software_fallback_fixture() -> dict:
    return {
        "source": "pac-metadata-direct-probe",
        "run_id": "pac-direct-fallback-fixture",
        "summary": {
            "probe_validated": True,
            "claim_grade": False,
            "allocator_validated": True,
            "hardware_pac_validated": False,
            "software_fallback_validated": True,
            "context_binding_active": False,
            "software_fallback_active": True,
            "pac_probe_active_key": "none",
            "pac_probe_signed_changed": False,
            "pac_probe_wrong_context_rejected": False,
            "pac_probe_key_matrix": [
                {"key": "ib", "available": False, "wrong_context_rejected": False}
            ],
            "typed_cache_inserts": 1,
            "typed_cache_hits": 1,
            "pac_signs": 0,
            "pac_verifications": 0,
            "pac_failures": 0,
            "pac_software_fallback_signs": 2,
            "pac_software_fallback_verifications": 1,
            "pac_software_fallback_failures": 0,
        },
    }


class ClaimThresholdPolicyTests(unittest.TestCase):
    def test_default_performance_accepts_faster_than_two_percent(self) -> None:
        claim = {
            "id": "C001-default-performance",
            "description": "default perf",
            "metric": "geomean_delta_percent_vs_unialloc",
            "dataset": "default_performance",
            "threshold": {"abs_lte": 2.0},
            "required_for_overclaim": True,
        }
        summary = {
            "datasets": {
                "default_performance": {
                    "path": "synthetic",
                    "claim_grade": True,
                    "geomean_delta_percent_vs_unialloc": 5.0,
                }
            }
        }
        result = evaluate.evaluate_claim(claim, summary, "current")
        self.assertEqual(result["status"], "pass")
        self.assertIn("better than", " ".join(result["evidence"]))

    def test_default_performance_rejects_slower_than_two_percent(self) -> None:
        claim = {
            "id": "C001-default-performance",
            "description": "default perf",
            "metric": "geomean_delta_percent_vs_unialloc",
            "dataset": "default_performance",
            "threshold": {"abs_lte": 2.0},
            "required_for_overclaim": True,
        }
        summary = {
            "datasets": {
                "default_performance": {
                    "path": "synthetic",
                    "claim_grade": True,
                    "geomean_delta_percent_vs_unialloc": -2.1,
                }
            }
        }
        result = evaluate.evaluate_claim(claim, summary, "current")
        self.assertEqual(result["status"], "fail")

    def test_speedup_range_accepts_higher_than_paper_upper_bound(self) -> None:
        claim = {
            "id": "C005-hugepage-metadata-speedup",
            "description": "hugepage speedup",
            "metric": "geomean_speedup_percent_vs_baselines",
            "dataset": "hugepage_metadata",
            "threshold": {"between": [2.0, 4.0]},
            "required_for_overclaim": True,
        }
        summary = {
            "datasets": {
                "hugepage_metadata": {
                    "path": "synthetic",
                    "claim_grade": True,
                    "speedup_percent_vs_baselines": 6.0,
                }
            }
        }
        direct_probe_audit = {
            "source": evaluate.HUGEPAGE_METADATA_DIRECT_PROBE_SOURCE,
            "run_id": "hugepage-direct-fixture",
            "summary": {
                "probe_validated": True,
                "claim_grade": True,
                "hugepage_backed": True,
                "side_cache_allocated": True,
                "backing": "hugepage",
                "mmap_attempts": 1,
                "mmap_successes": 1,
                "mmap_fallbacks": 0,
                "mmap_aligned_fallbacks": 0,
                "last_error_stage": "none",
                "last_error_code": 0,
                "last_error_name": "none",
                "typed_cache_inserts": 1,
                "typed_cache_hits": 1,
                "linux_smaps_mapping_found": True,
                "linux_smaps_kernel_page_kb": 2048,
                "linux_smaps_mmu_page_kb": 2048,
                "linux_smaps_anon_huge_kb": 2048,
            },
        }
        with mock.patch.object(
            evaluate,
            "latest_std_bench_feature_probe_summary",
            return_value=(
                pathlib.Path("hugepage-probe-summary.json"),
                {
                    "run_id": "hugepage-fixture",
                    "hugepage_metadata_side_cache_validated": True,
                    "hugepage_acceleration_observed": True,
                    "hugepage_metadata_side_cache_probe": {"event_count": 1},
                },
            ),
        ), mock.patch.object(
            evaluate,
            "latest_hugepage_metadata_direct_probe_audit",
            return_value=(pathlib.Path("hugepage-direct-probe.json"), direct_probe_audit),
        ):
            result = evaluate.evaluate_claim(claim, summary, "current")
        self.assertEqual(result["status"], "pass")
        self.assertIn("better-than-paper", " ".join(result["evidence"]))

    def test_hugepage_speedup_pass_shape_still_requires_direct_hugepage_backing(self) -> None:
        claim = {
            "id": "C005-hugepage-metadata-speedup",
            "description": "hugepage speedup",
            "metric": "geomean_speedup_percent_vs_baselines",
            "dataset": "hugepage_metadata",
            "threshold": {"between": [2.0, 4.0]},
            "required_for_overclaim": True,
        }
        summary = {
            "datasets": {
                "hugepage_metadata": {
                    "path": "synthetic",
                    "claim_grade": True,
                    "speedup_percent_vs_baselines": 6.0,
                }
            }
        }
        direct_probe_audit = {
            "source": evaluate.HUGEPAGE_METADATA_DIRECT_PROBE_SOURCE,
            "run_id": "hugepage-direct-aligned-fallback-fixture",
            "summary": {
                "probe_validated": True,
                "claim_grade": False,
                "hugepage_backed": False,
                "side_cache_allocated": True,
                "backing": "ordinary-fallback-aligned",
                "mmap_attempts": 1,
                "mmap_successes": 0,
                "mmap_fallbacks": 1,
                "mmap_aligned_fallbacks": 1,
                "last_error_stage": "macos-superpage-unsupported",
                "last_error_code": 4,
                "last_error_name": "KERN_INVALID_ARGUMENT",
                "typed_cache_inserts": 1,
                "typed_cache_hits": 1,
                "linux_smaps_mapping_found": False,
                "linux_smaps_kernel_page_kb": 0,
                "linux_smaps_mmu_page_kb": 0,
                "linux_smaps_anon_huge_kb": 0,
            },
        }
        with mock.patch.object(
            evaluate,
            "latest_std_bench_feature_probe_summary",
            return_value=(
                pathlib.Path("hugepage-probe-summary.json"),
                {
                    "run_id": "hugepage-fixture",
                    "hugepage_metadata_side_cache_validated": True,
                    "hugepage_acceleration_observed": True,
                    "hugepage_metadata_side_cache_probe": {"event_count": 1},
                },
            ),
        ), mock.patch.object(
            evaluate,
            "latest_hugepage_metadata_direct_probe_audit",
            return_value=(pathlib.Path("hugepage-direct-probe.json"), direct_probe_audit),
        ):
            result = evaluate.evaluate_claim(claim, summary, "current")
        self.assertEqual(result["status"], "missing")
        joined = " ".join(result["evidence"])
        self.assertIn("direct hugepage metadata allocator/PAL probe", joined)
        self.assertIn("did not observe real hugepage backing", joined)
        self.assertIn("KERN_INVALID_ARGUMENT", joined)

    def test_metadata_segregation_accepts_speedup_with_real_smoke_gate(self) -> None:
        claim = {
            "id": "C004-metadata-segregation-speedup",
            "description": "metadata segregation speedup",
            "metric": "geomean_speedup_percent_vs_baselines",
            "dataset": "metadata_segregation",
            "threshold": {"gte": 4.0},
            "required_for_overclaim": True,
        }
        summary = {
            "datasets": {
                "metadata_segregation": {
                    "path": "synthetic",
                    "claim_grade": True,
                    "speedup_percent_vs_baselines": 5.0,
                }
            }
        }
        smoke_audit = {
            "source": "rustc-driver-mir-semantic-scope-std-bench-paper-performance-smoke",
            "run_id": "c004-smoke-fixture",
            "dataset": "metadata_segregation",
            "summary": {
                "paper_performance_smoke_validated": True,
                "timing_returncode": 0,
                "validation_returncode": 0,
                "timing_bench_result_count": 1,
                "typed_allocation_site_event_count": 9,
                "lowered_module_typed_allocation_site_event_count": 9,
                "lowered_module_compiler_basis_present": True,
                "actual_semantic_scope_rewrite": True,
                "semantic_scope_rewrite_applied_count": 119,
                "selected_benchmark_count": 1,
            },
            "performance_rows": [
                {
                    "dataset": "metadata_segregation",
                    "semantic_policy_ready": True,
                    "type_id_basis_status": "compiler-assigned",
                    "ns_per_iter": 456,
                    "timing_features": "bench_ourself,metadata_segregation",
                    "validation_features": "bench_ourself,metadata_segregation,stats",
                }
            ],
        }
        with mock.patch.object(
            evaluate,
            "latest_std_bench_feature_probe_summary",
            return_value=(
                pathlib.Path("metadata-segregation-feature-probe.json"),
                {
                    "run_id": "metadata-segregation-probe-fixture",
                    "metadata_segregation_side_cache_validated": True,
                    "metadata_segregation_side_cache_probe": {"event_count": 1},
                },
            ),
        ), mock.patch.object(
            evaluate,
            "latest_mir_semantic_scope_paper_performance_smoke_audit",
            return_value=(pathlib.Path("c004-paper-performance-smoke.json"), smoke_audit),
        ):
            result = evaluate.evaluate_claim(claim, summary, "current")
        self.assertEqual(result["status"], "pass", result["evidence"])
        self.assertIn(
            "bounded rustc_driver MIR semantic-scope paper-performance smoke",
            " ".join(result["evidence"]),
        )

    def test_metadata_segregation_pass_shape_still_requires_real_smoke_gate(self) -> None:
        claim = {
            "id": "C004-metadata-segregation-speedup",
            "description": "metadata segregation speedup",
            "metric": "geomean_speedup_percent_vs_baselines",
            "dataset": "metadata_segregation",
            "threshold": {"gte": 4.0},
            "required_for_overclaim": True,
        }
        summary = {
            "datasets": {
                "metadata_segregation": {
                    "path": "synthetic",
                    "claim_grade": True,
                    "speedup_percent_vs_baselines": 5.0,
                }
            }
        }
        with mock.patch.object(
            evaluate,
            "latest_std_bench_feature_probe_summary",
            return_value=(
                pathlib.Path("metadata-segregation-feature-probe.json"),
                {
                    "run_id": "metadata-segregation-probe-fixture",
                    "metadata_segregation_side_cache_validated": True,
                    "metadata_segregation_side_cache_probe": {"event_count": 1},
                },
            ),
        ), mock.patch.object(
            evaluate,
            "latest_mir_semantic_scope_paper_performance_smoke_audit",
            return_value=(None, {}),
        ):
            result = evaluate.evaluate_claim(claim, summary, "current")
        self.assertEqual(result["status"], "missing")
        self.assertIn(
            "missing rustc_driver MIR semantic-scope paper-performance smoke for metadata_segregation",
            " ".join(result["evidence"]),
        )

    def test_metadata_segregation_smoke_gate_requires_matching_feature(self) -> None:
        blockers = evaluate.mir_semantic_scope_paper_performance_smoke_blockers(
            {
                "source": "rustc-driver-mir-semantic-scope-std-bench-paper-performance-smoke",
                "dataset": "metadata_segregation",
                "summary": {
                    "paper_performance_smoke_validated": True,
                    "timing_returncode": 0,
                    "validation_returncode": 0,
                    "timing_bench_result_count": 1,
                    "typed_allocation_site_event_count": 1,
                    "lowered_module_typed_allocation_site_event_count": 1,
                    "lowered_module_compiler_basis_present": True,
                    "actual_semantic_scope_rewrite": True,
                    "semantic_scope_rewrite_applied_count": 1,
                },
                "performance_rows": [
                    {
                        "dataset": "metadata_segregation",
                        "semantic_policy_ready": True,
                        "type_id_basis_status": "compiler-assigned",
                        "timing_features": "bench_ourself,type_isolation",
                        "validation_features": "bench_ourself,type_isolation,stats",
                    }
                ],
            },
            "metadata_segregation",
        )
        self.assertIn("feature=metadata_segregation", " ".join(blockers))

    def test_slowdown_range_accepts_lower_than_paper_lower_bound(self) -> None:
        claim = {
            "id": "C006-pac-cost",
            "description": "pac slowdown",
            "metric": "slowdown_percent_vs_baselines",
            "dataset": "pac_authentication",
            "threshold": {"between": [1.0, 3.0]},
            "required_for_overclaim": False,
        }
        # slowdown = (1 / geomean - 1) * 100 ~= 0.5%
        summary = {
            "datasets": {
                "pac_authentication": {
                    "path": "synthetic",
                    "claim_grade": True,
                    "geomean": 1.0 / 1.005,
                }
            }
        }
        with mock.patch.object(
            evaluate,
            "latest_std_bench_feature_probe_summary",
            return_value=(
                pathlib.Path("pac-probe-summary.json"),
                {
                    "run_id": "pac-fixture",
                    "pac_metadata_auth_validated": True,
                    "pac_metadata_auth_probe": {"event_count": 1},
                },
            ),
        ), mock.patch.object(
            evaluate,
            "latest_pac_metadata_direct_probe_audit",
            return_value=(
                pathlib.Path("pac-metadata-direct-probe-audit.json"),
                pac_direct_hardware_fixture(),
            ),
        ), mock.patch.object(
            evaluate,
            "latest_pac_arm64e_build_std_direct_probe_audit",
            return_value=(
                pathlib.Path("pac-arm64e-build-std-failed.json"),
                {
                    "source": "pac-metadata-direct-probe",
                    "run_id": "pac-arm64e-build-std-failed-fixture",
                    "summary": {
                        "runtime": "std",
                        "rust_target": "arm64e-apple-darwin",
                        "build_std_requested": True,
                        "probe_validated": False,
                        "claim_grade": False,
                        "hardware_pac_validated": False,
                        "exit_code": -11,
                    },
                },
            ),
        ):
            result = evaluate.evaluate_claim(claim, summary, "current")
        self.assertEqual(result["status"], "pass")
        self.assertIn("better-than-paper", " ".join(result["evidence"]))
        self.assertIn("build-std direct allocator probe", " ".join(result["evidence"]))

    def test_pac_better_than_paper_still_requires_allocator_hardware_pac(self) -> None:
        claim = {
            "id": "C006-pac-cost",
            "description": "pac slowdown",
            "metric": "slowdown_percent_vs_baselines",
            "dataset": "pac_authentication",
            "threshold": {"between": [1.0, 3.0]},
            "required_for_overclaim": False,
        }
        summary = {
            "datasets": {
                "pac_authentication": {
                    "path": "synthetic",
                    "claim_grade": True,
                    "geomean": 1.0 / 1.005,
                }
            }
        }
        pac_probe = {
            "run_id": "pac-fallback-fixture",
            "pac_metadata_auth_validated": False,
            "pac_metadata_auth_software_fallback_validated": True,
            "pac_metadata_auth_probe": {
                "event_count": 1,
                "pac_probe_key": "ib",
                "pac_probe_active_key": "none",
                "pac_probe_signed_changed": False,
                "pac_probe_strip_roundtrip": True,
                "pac_probe_correct_context_roundtrip": True,
                "pac_probe_wrong_context_rejected": False,
                "pac_probe_key_matrix": [
                    {"key": "ib", "available": False, "wrong_context_rejected": False}
                ],
            },
        }
        pac_abi = {
            "source": "pac-hardware-abi-probe",
            "run_id": "pac-abi-fixture",
            "summary": {
                "arm64_raw_pac_noop": True,
                "arm64e_sign_correct_auth": True,
                "arm64e_wrong_context_rejected": True,
                "hardware_pac_context_binding_observed": True,
                "rust_arm64e_target_available": False,
            },
        }
        with mock.patch.object(
            evaluate,
            "latest_std_bench_feature_probe_summary",
            return_value=(pathlib.Path("pac-runtime-feature-probe.json"), pac_probe),
        ), mock.patch.object(
            evaluate,
            "latest_pac_hardware_abi_probe_audit",
            return_value=(pathlib.Path("pac-hardware-abi-probe-audit.json"), pac_abi),
        ), mock.patch.object(
            evaluate,
            "latest_pac_metadata_direct_probe_audit",
            return_value=(
                pathlib.Path("pac-metadata-direct-probe-audit.json"),
                pac_direct_software_fallback_fixture(),
            ),
        ), mock.patch.object(
            evaluate,
            "latest_pac_arm64e_build_std_direct_probe_audit",
            return_value=(None, {}),
        ), mock.patch.object(
            evaluate,
            "latest_pac_arm64e_shim_compat_probe_audit",
            return_value=(None, {}),
        ):
            result = evaluate.evaluate_claim(claim, summary, "current")
        self.assertEqual(result["status"], "missing", result["evidence"])
        evidence = " ".join(result["evidence"])
        self.assertIn("PAC hardware ABI probe", evidence)
        self.assertIn("direct PAC metadata allocator probe", evidence)
        self.assertIn("software fallback", evidence)
        self.assertIn("current Rust allocator target cannot build/run as arm64e", evidence)

    def test_pac_direct_hardware_probe_supersedes_legacy_runtime_fallback(self) -> None:
        claim = {
            "id": "C006-pac-cost",
            "description": "pac slowdown",
            "metric": "slowdown_percent_vs_baselines",
            "dataset": "pac_authentication",
            "threshold": {"between": [1.0, 3.0]},
            "required_for_overclaim": False,
        }
        summary = {
            "datasets": {
                "pac_authentication": {
                    "path": "synthetic",
                    "claim_grade": True,
                    "geomean": 1.0 / 1.005,
                }
            }
        }
        legacy_fallback_probe = {
            "run_id": "legacy-pac-fallback",
            "pac_metadata_auth_validated": False,
            "pac_metadata_auth_software_fallback_validated": True,
            "pac_metadata_auth_probe": {
                "event_count": 1,
                "pac_probe_active_key": "none",
                "pac_probe_wrong_context_rejected": False,
            },
        }
        with mock.patch.object(
            evaluate,
            "latest_std_bench_feature_probe_summary",
            return_value=(pathlib.Path("legacy-pac-runtime-probe.json"), legacy_fallback_probe),
        ), mock.patch.object(
            evaluate,
            "latest_pac_metadata_direct_probe_audit",
            return_value=(
                pathlib.Path("pac-metadata-direct-probe-audit.json"),
                pac_direct_hardware_fixture(),
            ),
        ), mock.patch.object(
            evaluate,
            "latest_pac_arm64e_build_std_direct_probe_audit",
            return_value=(None, {}),
        ), mock.patch.object(
            evaluate,
            "latest_pac_hardware_abi_probe_audit",
            return_value=(None, {}),
        ):
            result = evaluate.evaluate_claim(claim, summary, "current")
        self.assertEqual(result["status"], "pass", result["evidence"])
        evidence = " ".join(result["evidence"])
        self.assertIn("direct arm64e allocator PAC probe", evidence)
        self.assertIn("diagnostic evidence only", evidence)
        self.assertNotIn("claim-grade blocker: PAC runtime probe used software fallback", evidence)

    def test_type_isolation_rejects_above_paper_max_slowdown(self) -> None:
        claim = {
            "id": "C003-type-isolation-cost",
            "description": "type isolation slowdown",
            "metric": "slowdown_percent_range_vs_default_unialloc",
            "dataset": "type_isolation",
            "baseline_dataset": "default_performance",
            "threshold": {"min_gte": 5.0, "max_lte": 14.0},
            "required_for_overclaim": True,
        }
        summary = {
            "datasets": {
                "default_performance": {
                    "path": "default",
                    "claim_grade": True,
                    "rows": [
                        {"benchmark": "B", "values": {"jemalloc": 1.0}},
                    ],
                },
                "type_isolation": {
                    "path": "type",
                    "claim_grade": True,
                    "rows": [
                        {"benchmark": "B", "values": {"jemalloc": 1.0 / 1.15}},
                    ],
                },
            }
        }
        result = evaluate.evaluate_claim(claim, summary, "current")
        self.assertEqual(result["status"], "fail")

    def test_type_isolation_accepts_better_than_paper_with_real_smoke_gate(self) -> None:
        claim = {
            "id": "C003-type-isolation-cost",
            "description": "type isolation slowdown",
            "metric": "slowdown_percent_range_vs_default_unialloc",
            "dataset": "type_isolation",
            "baseline_dataset": "default_performance",
            "threshold": {"min_gte": 5.0, "max_lte": 14.0},
            "required_for_overclaim": True,
        }
        summary = {
            "datasets": {
                "default_performance": {
                    "path": "default",
                    "claim_grade": True,
                    "rows": [{"benchmark": "B", "values": {"jemalloc": 1.0}}],
                },
                "type_isolation": {
                    "path": "type",
                    "claim_grade": True,
                    "rows": [{"benchmark": "B", "values": {"jemalloc": 1.0}}],
                },
            }
        }
        smoke_audit = {
            "source": "rustc-driver-mir-semantic-scope-std-bench-paper-performance-smoke",
            "run_id": "c003-smoke-fixture",
            "dataset": "type_isolation",
            "summary": {
                "paper_performance_smoke_validated": True,
                "timing_returncode": 0,
                "validation_returncode": 0,
                "timing_bench_result_count": 1,
                "typed_allocation_site_event_count": 9,
                "lowered_module_typed_allocation_site_event_count": 9,
                "lowered_module_compiler_basis_present": True,
                "actual_semantic_scope_rewrite": True,
                "semantic_scope_rewrite_applied_count": 119,
                "selected_benchmark_count": 1,
            },
            "performance_rows": [
                {
                    "dataset": "type_isolation",
                    "semantic_policy_ready": True,
                    "type_id_basis_status": "compiler-assigned",
                    "ns_per_iter": 123,
                    "timing_features": "bench_ourself,type_isolation",
                    "validation_features": "bench_ourself,type_isolation,stats",
                }
            ],
        }
        with mock.patch.object(
            evaluate,
            "latest_std_bench_feature_probe_summary",
            return_value=(
                pathlib.Path("type-isolation-feature-probe.json"),
                {
                    "run_id": "type-isolation-probe-fixture",
                    "type_isolation_side_cache_validated": True,
                    "type_isolation_side_cache_probe": {"event_count": 1},
                },
            ),
        ), mock.patch.object(
            evaluate,
            "latest_mir_semantic_scope_paper_performance_smoke_audit",
            return_value=(pathlib.Path("c003-paper-performance-smoke.json"), smoke_audit),
        ):
            result = evaluate.evaluate_claim(claim, summary, "current")
        self.assertEqual(result["status"], "pass", result["evidence"])
        evidence = " ".join(result["evidence"])
        self.assertIn("better-than-paper", evidence)
        self.assertIn("bounded rustc_driver MIR semantic-scope paper-performance smoke", evidence)

    def test_type_isolation_pass_shape_still_requires_real_smoke_gate(self) -> None:
        claim = {
            "id": "C003-type-isolation-cost",
            "description": "type isolation slowdown",
            "metric": "slowdown_percent_range_vs_default_unialloc",
            "dataset": "type_isolation",
            "baseline_dataset": "default_performance",
            "threshold": {"min_gte": 5.0, "max_lte": 14.0},
            "required_for_overclaim": True,
        }
        summary = {
            "datasets": {
                "default_performance": {
                    "path": "default",
                    "claim_grade": True,
                    "rows": [{"benchmark": "B", "values": {"jemalloc": 1.0}}],
                },
                "type_isolation": {
                    "path": "type",
                    "claim_grade": True,
                    "rows": [{"benchmark": "B", "values": {"jemalloc": 1.0}}],
                },
            }
        }
        with mock.patch.object(
            evaluate,
            "latest_std_bench_feature_probe_summary",
            return_value=(
                pathlib.Path("type-isolation-feature-probe.json"),
                {
                    "run_id": "type-isolation-probe-fixture",
                    "type_isolation_side_cache_validated": True,
                    "type_isolation_side_cache_probe": {"event_count": 1},
                },
            ),
        ), mock.patch.object(
            evaluate,
            "latest_mir_semantic_scope_paper_performance_smoke_audit",
            return_value=(None, {}),
        ):
            result = evaluate.evaluate_claim(claim, summary, "current")
        self.assertEqual(result["status"], "missing")
        self.assertIn("missing rustc_driver MIR semantic-scope paper-performance smoke", " ".join(result["evidence"]))


if __name__ == "__main__":
    unittest.main()
