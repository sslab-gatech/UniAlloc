#!/usr/bin/env python3
"""Regression tests for external workload adapter config claim-grade gates."""

from __future__ import annotations

import importlib.util
import argparse
import hashlib
import json
import pathlib
import subprocess
import tempfile
import unittest


ROOT = pathlib.Path(__file__).resolve().parents[2]
EVALUATE_PATH = ROOT / "evaluation" / "scripts" / "evaluate.py"


spec = importlib.util.spec_from_file_location("unialloc_evaluate", EVALUATE_PATH)
assert spec is not None and spec.loader is not None
evaluate = importlib.util.module_from_spec(spec)
spec.loader.exec_module(evaluate)


def write_json(path: pathlib.Path, value: object) -> None:
    path.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8")


class ExternalWorkloadConfigAuditSourceContractTests(unittest.TestCase):
    def make_workload_dir(self, tmp: pathlib.Path, *, ref_kind: str, ref: str) -> tuple[dict, pathlib.Path]:
        workload_dir = tmp / f"workload-{ref_kind}-{ref.replace('/', '-')}"
        workload_dir.mkdir(parents=True)
        (workload_dir / "run_paper_workload.py").write_text("#!/usr/bin/env python3\n", encoding="utf-8")
        (workload_dir / "workload_config.template.json").write_text("{}\n", encoding="utf-8")
        paper_source = {
            "upstream_url": "https://example.invalid/paper-workload.git",
            "matchers": ["example.invalid/paper-workload"],
            "ref_candidates": [{"kind": ref_kind, "ref": ref}],
        }
        config = {
            "schema_version": 1,
            "source": "unit-test-active-config",
            "configured": True,
            "claim_grade": True,
            "paper_source": paper_source,
            "real_workload_dir": str(workload_dir),
            "cwd": "{real_workload_dir}",
            "command_template": [
                "python3",
                "run_paper_workload.py",
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
                "--json",
            ],
            "time_fields": ["seconds"],
            "benchmark_owned_json": True,
            "measurement_source": "benchmark_owned_json",
        }
        write_json(workload_dir / "workload_config.json", config)
        rule = {
            "id": f"external-unit-{ref_kind}",
            "workload_dir": str(workload_dir),
            "paper_source": paper_source,
            "benchmarks": ["unit-benchmark"],
            "datasets": ["default_performance"],
        }
        return rule, workload_dir

    def test_head_only_source_allows_runner_but_blocks_claim_grade_runner(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            rule, _ = self.make_workload_dir(pathlib.Path(td), ref_kind="head", ref="HEAD")

            record = evaluate.audit_external_workload_adapter_config(rule)

            self.assertTrue(record["adapter_configured"], record)
            self.assertTrue(record["runner_ready"], record)
            self.assertFalse(record["claim_grade_runner_ready"], record)
            self.assertTrue(record["source_contract"]["head_only"], record)
            self.assertFalse(record["source_contract"]["complete"], record)
            self.assertTrue(
                any("claim-grade complete" in str(issue) for issue in record["issues"]),
                record["issues"],
            )

    def test_exact_source_ref_allows_claim_grade_runner_contract(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            rule, _ = self.make_workload_dir(pathlib.Path(td), ref_kind="exact", ref="v1.2.3")
            checkout_records = {
                rule["id"]: {
                    "rule_id": rule["id"],
                    "checkout_ready": True,
                    "checkout_dir": rule["workload_dir"],
                    "existing_checkout": {
                        "current_commit": "0123456789abcdef0123456789abcdef01234567",
                        "matches_resolved_commit": True,
                    },
                    "resolution": {
                        "status": "exact",
                        "ref": "v1.2.3",
                        "resolved_ref": "refs/tags/v1.2.3",
                        "commit": "0123456789abcdef0123456789abcdef01234567",
                    },
                }
            }

            record = evaluate.audit_external_workload_adapter_config(rule, checkout_records)

            self.assertTrue(record["adapter_configured"], record)
            self.assertTrue(record["runner_ready"], record)
            self.assertTrue(record["source_contract"]["complete"], record)
            self.assertTrue(record["source_contract"]["claim_grade_complete"], record)
            self.assertTrue(record["claim_grade_runner_ready"], record)
            self.assertFalse(record["issues"], record["issues"])

    def test_json_command_argument_is_not_treated_as_adapter_placeholder(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            tmp = pathlib.Path(td)
            rule, workload_dir = self.make_workload_dir(tmp, ref_kind="exact", ref="v1.2.3")
            config_path = workload_dir / "workload_config.json"
            checkout = workload_dir
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
            digest = hashlib.sha256(csv_path.read_bytes()).hexdigest()
            input_contract = {
                "claim_grade": True,
                "source": evaluate.RPOLARS_DB_BENCHMARK_SOURCE_ID,
                "expected_csv_name": "G1_1e0_1e0_0_0.csv",
                "expected_rows": 1,
                "k_groups": 1,
                "na_percent": 0,
                "sort_flag": 0,
                "csv_sha256": digest,
            }
            input_audit_json = json.dumps(
                {
                    "blockers": [],
                    "claim_grade_ready": True,
                    "note": "literal JSON object; braces are not adapter placeholders",
                },
                sort_keys=True,
            )
            config = json.loads(config_path.read_text(encoding="utf-8"))
            config.update(
                {
                    "source": "paper-external-workload-cargo-bench-json-wrapper",
                    "benchmarks": ["R-Polars"],
                    "datasets": ["default_performance"],
                    "categories": ["macro_single_thread"],
                    "real_workload_dir": str(checkout),
                    "env": {"CSV_SRC": str(csv_path)},
                    "cargo_bench_json_wrapper": {
                        "cargo_bench_targets": ["csv", "groupby"],
                        "rpolars_input_contract": input_contract,
                    },
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
                        "--claim-grade-contract",
                        "rpolars-paper-run",
                        "--rpolars-input-contract-json",
                        input_audit_json,
                        "--",
                        "cargo",
                        "bench",
                    ],
                }
            )
            write_json(config_path, config)
            rule.update(
                {
                    "benchmarks": ["R-Polars"],
                    "datasets": ["default_performance"],
                    "categories": ["macro_single_thread"],
                }
            )
            checkout_records = {
                rule["id"]: {
                    "rule_id": rule["id"],
                    "checkout_ready": True,
                    "checkout_dir": str(checkout),
                    "existing_checkout": {
                        "current_commit": "0123456789abcdef0123456789abcdef01234567",
                        "matches_resolved_commit": True,
                    },
                    "resolution": {
                        "status": "exact",
                        "ref": "v1.2.3",
                        "resolved_ref": "refs/tags/v1.2.3",
                        "commit": "0123456789abcdef0123456789abcdef01234567",
                    },
                }
            }

            record = evaluate.audit_external_workload_adapter_config(rule, checkout_records)

        self.assertNotIn("unknown adapter config placeholder", " ".join(record["issues"]))
        self.assertTrue(record["runner_ready"], record)
        self.assertTrue(record["claim_grade_runner_ready"], record)
        self.assertIn("--rpolars-input-contract-json", record["representative_command"])
        json_arg = record["representative_command"][
            record["representative_command"].index("--rpolars-input-contract-json") + 1
        ]
        self.assertEqual(json.loads(json_arg)["blockers"], [])

    def test_cargo_bench_leaf_filter_blocks_claim_grade_runner_even_with_exact_source(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            rule, workload_dir = self.make_workload_dir(pathlib.Path(td), ref_kind="exact", ref="v1.2.3")
            config_path = workload_dir / "workload_config.json"
            config = json.loads(config_path.read_text(encoding="utf-8"))
            config.update(
                {
                    "source": "paper-external-workload-cargo-bench-json-wrapper",
                    "not_claim_grade_reason": "representative parser selector only",
                    "cargo_bench_json_wrapper": {
                        "bench_name_filter": "parser",
                        "not_claim_grade_reason": "focused upstream parser leaf only",
                    },
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
                }
            )
            write_json(config_path, config)
            checkout_records = {
                rule["id"]: {
                    "rule_id": rule["id"],
                    "checkout_ready": True,
                    "checkout_dir": rule["workload_dir"],
                    "existing_checkout": {
                        "current_commit": "0123456789abcdef0123456789abcdef01234567",
                        "matches_resolved_commit": True,
                    },
                    "resolution": {
                        "status": "exact",
                        "ref": "v1.2.3",
                        "resolved_ref": "refs/tags/v1.2.3",
                        "commit": "0123456789abcdef0123456789abcdef01234567",
                    },
                }
            }

            record = evaluate.audit_external_workload_adapter_config(rule, checkout_records)

        self.assertTrue(record["adapter_configured"], record)
        self.assertTrue(record["runner_ready"], record)
        self.assertFalse(record["claim_grade_runner_ready"], record)
        joined = " ".join(record["claim_grade_runner_blockers"])
        self.assertIn("representative parser selector only", joined)
        self.assertIn("focused upstream parser leaf only", joined)
        self.assertIn("bench-filtered subset evidence: parser", joined)
        self.assertIn("no registered benchmark-specific full-row claim-grade contract", joined)
        self.assertTrue(
            any("adapter claim-grade runner blocker" in issue for issue in record["issues"]),
            record["issues"],
        )
        capability = evaluate.paper_performance_cell_capability(
            {
                "dataset": "default_performance",
                "benchmark": "unit-benchmark",
                "allocator": "unialloc",
                "category": "macro_single_thread",
                "variant_feature": "",
            },
            external_config_audit={"configs": [record]},
        )
        wrapper_rule = evaluate.paper_workload_wrapper_manifest_rule(
            {
                "dataset": "default_performance",
                "benchmark": "unit-benchmark",
                "allocator": "unialloc",
                "category": "macro_single_thread",
                "variant_feature": "",
                "capability": capability,
            }
        )
        self.assertEqual(capability["kind"], "external_workload_required", capability)
        self.assertIn("external adapter is runnable but not claim-grade ready", wrapper_rule["claim_grade_blockers"])
        self.assertTrue(
            any("bench-filtered subset evidence" in item for item in wrapper_rule["claim_grade_blockers"]),
            wrapper_rule,
        )

    def test_promote_blocks_accepted_newer_cargo_bench_subset_runner(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            tmp = pathlib.Path(td)
            rule, workload_dir = self.make_workload_dir(tmp, ref_kind="newer", ref="v2.0.0")
            config_path = workload_dir / "workload_config.json"
            config = json.loads(config_path.read_text(encoding="utf-8"))
            config.update(
                {
                    "claim_grade": False,
                    "source": "paper-external-workload-cargo-bench-json-wrapper",
                    "not_claim_grade_reason": "representative parser selector only",
                    "cargo_bench_json_wrapper": {
                        "bench_name_filter": "parser",
                        "not_claim_grade_reason": "focused upstream parser leaf only",
                    },
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
                }
            )
            write_json(config_path, config)
            checkout_records = {
                rule["id"]: {
                    "rule_id": rule["id"],
                    "checkout_ready": True,
                    "checkout_dir": str(workload_dir),
                    "existing_checkout": {
                        "current_commit": "89abcdef0123456789abcdef0123456789abcdef",
                        "matches_resolved_commit": True,
                    },
                    "resolution": {
                        "status": "newer",
                        "ref": "v2.0.0",
                        "resolved_ref": "refs/tags/v2.0.0",
                        "commit": "89abcdef0123456789abcdef0123456789abcdef",
                        "newer_used": True,
                        "non_exact_ref_used": True,
                    },
                }
            }
            bundle_path = tmp / "bundle.json"
            output_dir = tmp / "promotion-out"
            write_json(bundle_path, {"workloads": [rule]})

            original = evaluate.external_checkout_records_by_rule_id
            evaluate.external_checkout_records_by_rule_id = lambda checkout_audit=None: checkout_records
            try:
                rc = evaluate.promote_paper_exact_external_workload_configs(
                    argparse.Namespace(
                        bundle=str(bundle_path),
                        family=[],
                        run_id="unit-accepted-newer-subset",
                        output_dir=str(output_dir),
                        write=True,
                        allow_newer_benchmark_sources=True,
                        no_update_results=True,
                    )
                )
            finally:
                evaluate.external_checkout_records_by_rule_id = original

            promotion = json.loads(
                (output_dir / "paper-exact-external-workload-config-promotion.json").read_text(
                    encoding="utf-8"
                )
            )
            promoted_config = json.loads(config_path.read_text(encoding="utf-8"))

        self.assertEqual(rc, 0)
        self.assertEqual(promotion["summary"]["promoted_family_count"], 0, promotion)
        self.assertEqual(promotion["summary"]["blocked_family_count"], 1, promotion)
        [record] = promotion["configs"]
        self.assertEqual(record["action"], "blocked", record)
        blockers = " ".join(record["blockers"])
        self.assertIn("claim-grade runner surface is not complete", blockers)
        self.assertIn("representative parser selector only", blockers)
        self.assertIn("bench-filtered subset evidence: parser", blockers)
        self.assertIn("no registered benchmark-specific full-row claim-grade contract", blockers)
        self.assertFalse(promoted_config["claim_grade"], promoted_config)
        self.assertNotIn("claim_grade_activation", promoted_config)
        self.assertNotIn("accepted_newer_benchmark_source", promoted_config)

    def test_rjs_compiler_full_surface_contract_can_promote_claim_grade_runner(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            rule, workload_dir = self.make_workload_dir(pathlib.Path(td), ref_kind="exact", ref="v1.2.51")
            rule["id"] = "external-RJS-Compiler"
            rule["benchmarks"] = ["RJS-Compiler"]
            rule["paper_source"] = {
                "upstream_url": "https://github.com/swc-project/swc",
                "matchers": ["swc-project/swc", "github.com/swc-project/swc"],
                "ref_candidates": [{"kind": "exact", "ref": "refs/tags/v1.2.51"}],
            }
            config_path = workload_dir / "workload_config.json"
            config = json.loads(config_path.read_text(encoding="utf-8"))
            config.update(
                {
                    "source": "paper-external-workload-cargo-bench-json-wrapper",
                    "benchmarks": ["RJS-Compiler"],
                    "paper_source": rule["paper_source"],
                    "cargo_bench_json_wrapper": {
                        "child_command_template": ["cargo", "bench", "--package", "swc", "--bench", "typescript"],
                    },
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
                        "--real-workload-dir",
                        "{real_workload_dir}",
                        "--claim-grade-contract",
                        "rjs-compiler-paper-run",
                        "--expected-cargo-bench-targets",
                        "typescript",
                        "--expected-libtest-bench-functions",
                        "parser,full_es5",
                        "--",
                        "cargo",
                        "bench",
                        "--package",
                        "swc",
                        "--bench",
                        "typescript",
                    ],
                }
            )
            write_json(config_path, config)
            checkout_records = {
                rule["id"]: {
                    "rule_id": rule["id"],
                    "checkout_ready": True,
                    "checkout_dir": rule["workload_dir"],
                    "existing_checkout": {
                        "current_commit": "0123456789abcdef0123456789abcdef01234567",
                        "matches_resolved_commit": True,
                    },
                    "resolution": {
                        "status": "exact",
                        "ref": "refs/tags/v1.2.51",
                        "resolved_ref": "refs/tags/v1.2.51",
                        "commit": "0123456789abcdef0123456789abcdef01234567",
                    },
                }
            }

            record = evaluate.audit_external_workload_adapter_config(rule, checkout_records)

        self.assertTrue(record["adapter_configured"], record)
        self.assertTrue(record["runner_ready"], record)
        self.assertTrue(record["claim_grade_runner_ready"], record)
        self.assertEqual(record["claim_grade_runner_blockers"], [], record)
        self.assertEqual(record["issues"], [], record)

    def test_roxipng_bridge_reports_roxipng_fragment_contract_not_rpolars(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            rule, workload_dir = self.make_workload_dir(pathlib.Path(td), ref_kind="exact", ref="v9.1.2")
            rule["id"] = "external-R-Oxipng"
            rule["benchmarks"] = ["R-Oxipng*"]
            rule["paper_source"] = {
                "upstream_url": "https://github.com/shssoichiro/oxipng",
                "matchers": ["shssoichiro/oxipng", "github.com/shssoichiro/oxipng"],
                "ref_candidates": [{"kind": "exact", "ref": "refs/tags/v9.1.2"}],
            }
            config_path = workload_dir / "workload_config.json"
            config = json.loads(config_path.read_text(encoding="utf-8"))
            config.update(
                {
                    "source": "paper-external-workload-cargo-bench-json-wrapper",
                    "benchmarks": ["R-Oxipng*"],
                    "paper_source": rule["paper_source"],
                    "cargo_bench_json_wrapper": {
                        "bench_name_filter": "deflate_8_bits_strategy_0",
                        "child_command_template": [
                            "cargo",
                            "bench",
                            "--bench",
                            "deflate",
                            "deflate_8_bits_strategy_0",
                        ],
                    },
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
                        "deflate_8_bits_strategy_0",
                        "--real-workload-dir",
                        "{real_workload_dir}",
                        "--",
                        "cargo",
                        "bench",
                        "--bench",
                        "deflate",
                        "deflate_8_bits_strategy_0",
                    ],
                }
            )
            write_json(config_path, config)
            checkout_records = {
                rule["id"]: {
                    "rule_id": rule["id"],
                    "checkout_ready": True,
                    "checkout_dir": rule["workload_dir"],
                    "existing_checkout": {
                        "current_commit": "0123456789abcdef0123456789abcdef01234567",
                        "matches_resolved_commit": True,
                    },
                    "resolution": {
                        "status": "exact",
                        "ref": "refs/tags/v9.1.2",
                        "resolved_ref": "refs/tags/v9.1.2",
                        "commit": "0123456789abcdef0123456789abcdef01234567",
                    },
                }
            }

            record = evaluate.audit_external_workload_adapter_config(rule, checkout_records)

        self.assertFalse(record["claim_grade_runner_ready"], record)
        joined = " ".join(record["claim_grade_runner_blockers"])
        self.assertIn("expected roxipng-paper-fragment", joined)
        self.assertIn("bench-filtered subset evidence: deflate_8_bits_strategy_0", joined)
        self.assertNotIn("expected rpolars-paper-run", joined)
        self.assertNotIn("R-Polars CSV/input provenance", joined)

    def test_fallback_checkout_does_not_promote_exact_source_metadata(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            rule, _ = self.make_workload_dir(pathlib.Path(td), ref_kind="exact", ref="v1.2.3")
            checkout_records = {
                rule["id"]: {
                    "rule_id": rule["id"],
                    "checkout_ready": True,
                    "checkout_dir": rule["workload_dir"],
                    "existing_checkout": {
                        "current_commit": "fedcba9876543210fedcba9876543210fedcba98",
                        "matches_resolved_commit": True,
                    },
                    "resolution": {
                        "status": "fallback",
                        "fallback_used": True,
                        "ref": "v1.2.0",
                        "resolved_ref": "refs/tags/v1.2.0",
                        "commit": "fedcba9876543210fedcba9876543210fedcba98",
                    },
                }
            }

            record = evaluate.audit_external_workload_adapter_config(rule, checkout_records)

            self.assertTrue(record["source_contract"]["complete"], record)
            self.assertFalse(record["source_contract"]["claim_grade_complete"], record)
            self.assertFalse(record["claim_grade_runner_ready"], record)
            self.assertIn("fallback ref", " ".join(record["issues"]))

    def test_head_only_pinned_checkout_is_reproducible_but_not_paper_exact(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            rule, workload_dir = self.make_workload_dir(pathlib.Path(td), ref_kind="head", ref="HEAD")
            checkout_records = {
                rule["id"]: {
                    "rule_id": rule["id"],
                    "checkout_ready": True,
                    "checkout_dir": str(workload_dir),
                    "existing_checkout": {
                        "current_commit": "0123456789abcdef0123456789abcdef01234567",
                        "matches_resolved_commit": True,
                    },
                    "resolution": {
                        "status": "head",
                        "ref": "HEAD",
                        "resolved_ref": "HEAD",
                        "symref": "refs/heads/main",
                        "commit": "0123456789abcdef0123456789abcdef01234567",
                    },
                }
            }

            record = evaluate.audit_external_workload_adapter_config(rule, checkout_records)

            self.assertTrue(record["runner_ready"], record)
            self.assertFalse(record["claim_grade_runner_ready"], record)
            self.assertFalse(record["source_contract"]["complete"], record)
            self.assertFalse(record["source_contract"]["paper_exact_ref_complete"], record)
            self.assertTrue(record["source_contract"]["reproducible_snapshot_complete"], record)
            self.assertTrue(record["source_contract"]["checkout_pin"]["pinned"], record)
            self.assertIn(
                "paper exact-ref provenance is still missing",
                " ".join(record["source_contract"]["blockers"]),
            )

    def test_newer_pinned_checkout_is_reproducible_but_not_claim_grade(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            rule, workload_dir = self.make_workload_dir(pathlib.Path(td), ref_kind="newer", ref="v2.0.0")
            checkout_records = {
                rule["id"]: {
                    "rule_id": rule["id"],
                    "checkout_ready": True,
                    "checkout_dir": str(workload_dir),
                    "existing_checkout": {
                        "current_commit": "89abcdef0123456789abcdef0123456789abcdef",
                        "matches_resolved_commit": True,
                    },
                    "resolution": {
                        "status": "newer",
                        "ref": "v2.0.0",
                        "resolved_ref": "refs/tags/v2.0.0",
                        "commit": "89abcdef0123456789abcdef0123456789abcdef",
                        "newer_used": True,
                        "non_exact_ref_used": True,
                    },
                }
            }

            record = evaluate.audit_external_workload_adapter_config(rule, checkout_records)

            self.assertTrue(record["runner_ready"], record)
            self.assertFalse(record["claim_grade_runner_ready"], record)
            self.assertFalse(record["source_contract"]["complete"], record)
            self.assertFalse(record["source_contract"]["paper_exact_ref_complete"], record)
            self.assertTrue(record["source_contract"]["reproducible_snapshot_complete"], record)
            self.assertTrue(record["source_contract"]["checkout_pin"]["pinned"], record)
            self.assertTrue(record["source_contract"]["checkout_pin"]["newer_used"], record)
            self.assertTrue(record["source_contract"]["checkout_pin"]["non_exact_ref_used"], record)
            self.assertIn(
                "newer ref",
                " ".join(record["source_contract"]["blockers"]),
            )

    def test_fallback_pinned_checkout_is_reproducible_but_not_claim_grade_without_policy(self) -> None:
        commit = "fedcba9876543210fedcba9876543210fedcba98"
        with tempfile.TemporaryDirectory() as td:
            rule, workload_dir = self.make_workload_dir(pathlib.Path(td), ref_kind="fallback", ref=commit)
            checkout_records = {
                rule["id"]: {
                    "rule_id": rule["id"],
                    "checkout_ready": True,
                    "checkout_dir": str(workload_dir),
                    "existing_checkout": {
                        "current_commit": commit,
                        "matches_resolved_commit": True,
                    },
                    "resolution": {
                        "status": "fallback",
                        "ref": commit,
                        "resolved_ref": commit,
                        "commit": commit,
                        "fallback_used": True,
                        "non_exact_ref_used": True,
                    },
                }
            }

            record = evaluate.audit_external_workload_adapter_config(rule, checkout_records)

            self.assertTrue(record["runner_ready"], record)
            self.assertFalse(record["claim_grade_runner_ready"], record)
            self.assertFalse(record["source_contract"]["paper_exact_ref_complete"], record)
            self.assertFalse(record["source_contract"]["accepted_newer_checkout_pin_complete"], record)
            self.assertFalse(record["source_contract"]["claim_grade_complete"], record)
            self.assertTrue(record["source_contract"]["reproducible_snapshot_complete"], record)
            self.assertTrue(record["source_contract"]["checkout_pin"]["pinned"], record)
            self.assertTrue(record["source_contract"]["checkout_pin"]["fallback_used"], record)
            self.assertIn(
                "fallback ref",
                " ".join(record["source_contract"]["blockers"]),
            )

    def test_explicitly_accepted_newer_pinned_checkout_can_be_current_claim_runner(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            rule, workload_dir = self.make_workload_dir(pathlib.Path(td), ref_kind="newer", ref="v2.0.0")
            checkout_records = {
                rule["id"]: {
                    "rule_id": rule["id"],
                    "checkout_ready": True,
                    "checkout_dir": str(workload_dir),
                    "existing_checkout": {
                        "current_commit": "89abcdef0123456789abcdef0123456789abcdef",
                        "matches_resolved_commit": True,
                    },
                    "resolution": {
                        "status": "newer",
                        "ref": "v2.0.0",
                        "resolved_ref": "refs/tags/v2.0.0",
                        "commit": "89abcdef0123456789abcdef0123456789abcdef",
                        "newer_used": True,
                        "non_exact_ref_used": True,
                    },
                }
            }

            record = evaluate.audit_external_workload_adapter_config(
                rule,
                checkout_records,
                allow_newer_benchmark_sources=True,
            )

            self.assertTrue(record["runner_ready"], record)
            self.assertTrue(record["claim_grade_runner_ready"], record)
            self.assertFalse(record["source_contract"]["paper_exact_ref_complete"], record)
            self.assertFalse(record["source_contract"]["paper_exact_claim_grade_complete"], record)
            self.assertTrue(record["source_contract"]["accepted_newer_checkout_pin_complete"], record)
            self.assertTrue(record["source_contract"]["claim_grade_complete"], record)
            self.assertEqual(
                record["source_contract"]["source_provenance_class"],
                evaluate.USER_ACCEPTED_NEWER_SOURCE_PROVENANCE_CLASS,
            )
            self.assertFalse(record["issues"], record["issues"])

    def test_explicitly_accepted_fallback_pinned_checkout_can_be_current_claim_runner(self) -> None:
        commit = "fedcba9876543210fedcba9876543210fedcba98"
        with tempfile.TemporaryDirectory() as td:
            rule, workload_dir = self.make_workload_dir(pathlib.Path(td), ref_kind="fallback", ref=commit)
            checkout_records = {
                rule["id"]: {
                    "rule_id": rule["id"],
                    "checkout_ready": True,
                    "checkout_dir": str(workload_dir),
                    "existing_checkout": {
                        "current_commit": commit,
                        "matches_resolved_commit": True,
                    },
                    "resolution": {
                        "status": "fallback",
                        "ref": commit,
                        "resolved_ref": commit,
                        "commit": commit,
                        "fallback_used": True,
                        "non_exact_ref_used": True,
                        "raw_commit_ref": True,
                    },
                }
            }

            record = evaluate.audit_external_workload_adapter_config(
                rule,
                checkout_records,
                allow_newer_benchmark_sources=True,
            )

            self.assertTrue(record["runner_ready"], record)
            self.assertTrue(record["claim_grade_runner_ready"], record)
            self.assertFalse(record["source_contract"]["paper_exact_ref_complete"], record)
            self.assertTrue(record["source_contract"]["accepted_newer_checkout_pin_complete"], record)
            self.assertTrue(record["source_contract"]["accepted_non_exact_checkout_pin_complete"], record)
            self.assertTrue(record["source_contract"]["claim_grade_complete"], record)
            self.assertEqual(
                record["source_contract"]["source_provenance_class"],
                evaluate.USER_ACCEPTED_NEWER_SOURCE_PROVENANCE_CLASS,
            )
            self.assertFalse(record["issues"], record["issues"])

    def test_wrapper_options_import_source_contract_as_claim_grade_blocker(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            rule, workload_dir = self.make_workload_dir(pathlib.Path(td), ref_kind="head", ref="HEAD")
            checkout_records = {
                rule["id"]: {
                    "rule_id": rule["id"],
                    "checkout_ready": True,
                    "checkout_dir": str(workload_dir),
                    "existing_checkout": {
                        "current_commit": "0123456789abcdef0123456789abcdef01234567",
                        "matches_resolved_commit": True,
                    },
                    "resolution": {
                        "status": "head",
                        "ref": "HEAD",
                        "resolved_ref": "HEAD",
                        "commit": "0123456789abcdef0123456789abcdef01234567",
                    },
                }
            }
            original = evaluate.external_checkout_records_by_rule_id
            evaluate.external_checkout_records_by_rule_id = lambda checkout_audit=None: checkout_records
            try:
                options = evaluate.wrapper_manifest_workload_options(
                    {
                        **rule,
                        "command_template": ["python3", "run_paper_workload.py", "--run-index", "{run_index}"],
                        "cwd": str(workload_dir),
                    },
                    {"dataset": "default_performance", "benchmark": "unit-benchmark", "allocator": "unialloc"},
                    default_cwd=str(workload_dir),
                    default_timeout=30,
                    default_measurement="stdout_json",
                    default_time_field="seconds",
                )
            finally:
                evaluate.external_checkout_records_by_rule_id = original

            self.assertEqual(options["source_provenance_class"], "head_pinned")
            self.assertFalse(options["source_contract"]["paper_exact_ref_complete"], options)
            self.assertFalse(options["claim_grade"], options)
            self.assertIn("pinned/reproducible", " ".join(options["claim_grade_blockers"]))

    def test_wrapper_options_reject_exact_metadata_with_fallback_checkout(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            rule, workload_dir = self.make_workload_dir(pathlib.Path(td), ref_kind="exact", ref="v1.2.3")
            checkout_records = {
                rule["id"]: {
                    "rule_id": rule["id"],
                    "checkout_ready": True,
                    "checkout_dir": str(workload_dir),
                    "existing_checkout": {
                        "current_commit": "fedcba9876543210fedcba9876543210fedcba98",
                        "matches_resolved_commit": True,
                    },
                    "resolution": {
                        "status": "fallback",
                        "fallback_used": True,
                        "ref": "v1.2.0",
                        "resolved_ref": "refs/tags/v1.2.0",
                        "commit": "fedcba9876543210fedcba9876543210fedcba98",
                    },
                }
            }
            original = evaluate.external_checkout_records_by_rule_id
            evaluate.external_checkout_records_by_rule_id = lambda checkout_audit=None: checkout_records
            try:
                options = evaluate.wrapper_manifest_workload_options(
                    {
                        **rule,
                        "command_template": ["python3", "run_paper_workload.py", "--run-index", "{run_index}"],
                        "cwd": str(workload_dir),
                    },
                    {"dataset": "default_performance", "benchmark": "unit-benchmark", "allocator": "unialloc"},
                    default_cwd=str(workload_dir),
                    default_timeout=30,
                    default_measurement="stdout_json",
                    default_time_field="seconds",
                )
            finally:
                evaluate.external_checkout_records_by_rule_id = original

            self.assertTrue(options["source_contract"]["paper_exact_ref_complete"], options)
            self.assertFalse(options["source_contract"]["claim_grade_complete"], options)
            self.assertFalse(options["claim_grade"], options)
            blockers = " ".join(options["claim_grade_blockers"])
            self.assertIn("not pinned to the exact paper ref", blockers)
            self.assertIn("fallback ref", blockers)

    def test_wrapper_options_skip_source_contract_for_blocked_collections_cell(self) -> None:
        options = evaluate.wrapper_manifest_workload_options(
            {
                "id": "wrap-default_performance-Collections-ptmalloc",
                "command_template": ["python3", "evaluation/scripts/paper_blocked_workload.py"],
                "cwd": ".",
                "measurement": "stdout_json",
                "time_field": "seconds",
                "capability": {
                    "kind": "host_blocked_allocator",
                    "locally_runnable": False,
                    "claim_grade_blocker": "Linux/glibc host required for ptmalloc paper evidence",
                    "reason": "ptmalloc paper evidence requires a Linux/glibc host",
                },
                "claim_grade_blockers": [
                    "Linux/glibc host required for ptmalloc paper evidence",
                    "ptmalloc paper evidence requires a Linux/glibc host",
                ],
            },
            {"dataset": "default_performance", "benchmark": "Collections", "allocator": "ptmalloc"},
            default_cwd=".",
            default_timeout=30,
            default_measurement="stdout_json",
            default_time_field="seconds",
        )

        self.assertNotIn("paper_source", options)
        self.assertNotIn("source_contract", options)
        self.assertNotIn("source_provenance_class", options)
        self.assertNotIn("sample source", " ".join(options.get("claim_grade_blockers", [])))

    def test_wrapper_options_resolve_external_bundle_source_contract_from_capability(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            rule, workload_dir = self.make_workload_dir(pathlib.Path(td), ref_kind="head", ref="HEAD")
            checkout_records = {
                rule["id"]: {
                    "rule_id": rule["id"],
                    "checkout_ready": True,
                    "checkout_dir": str(workload_dir),
                    "existing_checkout": {
                        "current_commit": "0123456789abcdef0123456789abcdef01234567",
                        "matches_resolved_commit": True,
                    },
                    "resolution": {
                        "status": "head",
                        "ref": "HEAD",
                        "resolved_ref": "HEAD",
                        "commit": "0123456789abcdef0123456789abcdef01234567",
                    },
                }
            }
            original_checkout = evaluate.external_checkout_records_by_rule_id
            original_rule_lookup = evaluate.default_external_workload_bundle_rule_by_id
            evaluate.external_checkout_records_by_rule_id = lambda checkout_audit=None: checkout_records
            evaluate.default_external_workload_bundle_rule_by_id = lambda rule_id: rule if rule_id == rule["id"] else None
            try:
                options = evaluate.wrapper_manifest_workload_options(
                    {
                        "id": "wrap-default_performance-unit-benchmark-unialloc",
                        "command_template": [
                            "python3",
                            "evaluation/scripts/paper_external_workload.py",
                            "--bundle",
                            "evaluation/config/paper_external_workloads.template.json",
                        ],
                        "cwd": ".",
                        "measurement": "stdout_json",
                        "time_field": "seconds",
                        "capability": {
                            "kind": "external_workload_required",
                            "external_rule_id": rule["id"],
                            "external_adapter_runner_ready": True,
                        },
                    },
                    {
                        "dataset": "default_performance",
                        "benchmark": "unit-benchmark",
                        "allocator": "unialloc",
                    },
                    default_cwd=".",
                    default_timeout=30,
                    default_measurement="stdout_json",
                    default_time_field="seconds",
                )
            finally:
                evaluate.external_checkout_records_by_rule_id = original_checkout
                evaluate.default_external_workload_bundle_rule_by_id = original_rule_lookup

            self.assertEqual(options["paper_source"]["upstream_url"], "https://example.invalid/paper-workload.git")
            self.assertTrue(options["source_contract"]["head_only"], options)
            self.assertEqual(options["source_provenance_class"], "head_pinned")
            self.assertFalse(options["claim_grade"], options)
            blockers = " ".join(options["claim_grade_blockers"])
            self.assertIn("pinned/reproducible", blockers)
            self.assertNotIn("metadata is missing", blockers)


class RRedisParityProvenanceTests(unittest.TestCase):
    def test_sample_host_system_reads_child_record_and_runner_host(self) -> None:
        self.assertEqual(
            evaluate.sample_host_system(
                {
                    "source": "paper-external-workload-adapter",
                    "child_record": {
                        "source": "paper-external-redis-benchmark-json",
                        "host": "127.0.0.1",
                        "runner_host": {"system": "Linux"},
                    },
                }
            ),
            "Linux",
        )
        self.assertEqual(
            evaluate.sample_host_system(
                {
                    "source": "paper-external-redis-benchmark-json",
                    "host": "127.0.0.1",
                    "host_system": "Darwin",
                }
            ),
            "Darwin",
        )


class ExternalWorkloadNewerSourceCandidateTests(unittest.TestCase):
    def init_git_repo(self, repo: pathlib.Path) -> None:
        subprocess.run(["git", "init"], cwd=repo, check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        subprocess.run(
            ["git", "config", "user.email", "unialloc-test@example.invalid"],
            cwd=repo,
            check=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        subprocess.run(
            ["git", "config", "user.name", "UniAlloc Test"],
            cwd=repo,
            check=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        subprocess.run(
            ["git", "config", "commit.gpgsign", "false"],
            cwd=repo,
            check=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        subprocess.run(
            ["git", "config", "tag.gpgsign", "false"],
            cwd=repo,
            check=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )

    def make_tagged_repo(self, tmp: pathlib.Path) -> tuple[pathlib.Path, str, str]:
        repo = tmp / "tagged-repo"
        repo.mkdir()
        self.init_git_repo(repo)
        (repo / "Cargo.toml").write_text("[package]\nname='tagged_repo'\nversion='0.1.0'\n", encoding="utf-8")
        subprocess.run(["git", "add", "Cargo.toml"], cwd=repo, check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        subprocess.run(
            ["git", "commit", "-m", "initial tagged version"],
            cwd=repo,
            check=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        subprocess.run(["git", "tag", "v1.0.0"], cwd=repo, check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        tag_commit = subprocess.run(
            ["git", "rev-parse", "v1.0.0^{commit}"],
            cwd=repo,
            check=True,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        ).stdout.strip()
        (repo / "src.txt").write_text("newer head\n", encoding="utf-8")
        subprocess.run(["git", "add", "src.txt"], cwd=repo, check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        subprocess.run(
            ["git", "commit", "-m", "head after tag"],
            cwd=repo,
            check=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        head_commit = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=repo,
            check=True,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        ).stdout.strip()
        return repo, head_commit, tag_commit

    def make_untagged_repo(self, tmp: pathlib.Path) -> tuple[pathlib.Path, str]:
        repo = tmp / "untagged-repo"
        repo.mkdir()
        self.init_git_repo(repo)
        (repo / "Cargo.toml").write_text("[package]\nname='untagged_repo'\nversion='0.1.0'\n", encoding="utf-8")
        subprocess.run(["git", "add", "Cargo.toml"], cwd=repo, check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        subprocess.run(
            ["git", "commit", "-m", "initial untagged version"],
            cwd=repo,
            check=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        head_commit = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=repo,
            check=True,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        ).stdout.strip()
        return repo, head_commit

    def head_only_rule(self, repo: pathlib.Path) -> dict:
        return {
            "id": "external-unit-head-only",
            "workload_dir": str(repo),
            "benchmarks": ["unit-web"],
            "datasets": ["default_performance"],
            "paper_source": {
                "paper_benchmark": "unit-web",
                "paper_project": "Unit Web",
                "paper_version": None,
                "upstream_url": "https://example.invalid/unit-web.git",
                "matchers": ["example.invalid/unit-web"],
                "ref_candidates": [
                    {
                        "kind": "head",
                        "ref": "HEAD",
                        "reason": "paper row names the benchmark but no exact version/ref is available",
                    }
                ],
            },
        }

    def checkout_record(self, rule: dict, repo: pathlib.Path, head_commit: str) -> dict:
        return {
            "rule_id": rule["id"],
            "checkout_ready": True,
            "checkout_dir": str(repo),
            "existing_checkout": {
                "current_commit": head_commit,
                "matches_resolved_commit": True,
            },
            "resolution": {
                "status": "head",
                "ref": "HEAD",
                "resolved_ref": "HEAD",
                "commit": head_commit,
            },
        }

    def test_head_only_checkout_proposes_explicit_newer_tag_candidate(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            repo, head_commit, tag_commit = self.make_tagged_repo(pathlib.Path(td))
            rule = self.head_only_rule(repo)

            record = evaluate.audit_external_workload_newer_source_candidate(
                rule,
                self.checkout_record(rule, repo, head_commit),
            )

        self.assertTrue(record["head_only"], record)
        self.assertFalse(record["claim_grade"], record)
        candidate = record["proposed_newer_ref_candidate"]
        self.assertEqual(candidate["kind"], "newer")
        self.assertEqual(candidate["ref"], "refs/tags/v1.0.0")
        self.assertEqual(candidate["observed_head_commit"], head_commit)
        self.assertEqual(candidate["observed_tag_commit"], tag_commit)
        self.assertIn("non-paper-exact", candidate["reason"])

    def test_head_only_checkout_without_tags_proposes_local_commit_fallback_candidate(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            repo, head_commit = self.make_untagged_repo(pathlib.Path(td))
            rule = self.head_only_rule(repo)

            record = evaluate.audit_external_workload_newer_source_candidate(
                rule,
                self.checkout_record(rule, repo, head_commit),
            )

        self.assertTrue(record["head_only"], record)
        self.assertFalse(record["claim_grade"], record)
        candidate = record["proposed_fallback_ref_candidate"]
        self.assertEqual(candidate["kind"], "fallback")
        self.assertEqual(candidate["ref"], head_commit)
        self.assertTrue(candidate["raw_git_commit_ref"], candidate)
        self.assertEqual(record["local_commit_pin_candidate"]["ref"], head_commit)
        self.assertEqual(record["next_action"], "write-fallback-commit-before-head-then-rerun-fetch-with-allow-fallback")

    def test_write_bundle_inserts_newer_candidate_before_head(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            tmp = pathlib.Path(td)
            repo, head_commit, _ = self.make_tagged_repo(tmp)
            rule = self.head_only_rule(repo)
            bundle_path = tmp / "bundle.json"
            checkouts_path = tmp / "checkouts.json"
            bundle_out = tmp / "bundle.out.json"
            output_dir = tmp / "raw"
            write_json(bundle_path, {"schema_version": 1, "workloads": [rule]})
            write_json(
                checkouts_path,
                {
                    "schema_version": 1,
                    "checkouts": [self.checkout_record(rule, repo, head_commit)],
                },
            )

            rc = evaluate.audit_paper_external_workload_newer_source_candidates(
                argparse.Namespace(
                    bundle=str(bundle_path),
                    checkouts=str(checkouts_path),
                    write_bundle=True,
                    write_active_configs=False,
                    bundle_out=str(bundle_out),
                    run_id="unit-newer-source-candidates",
                    output_dir=str(output_dir),
                    no_update_results=True,
                )
            )

            updated = json.loads(bundle_out.read_text(encoding="utf-8"))

        self.assertEqual(rc, 0)
        refs = updated["workloads"][0]["paper_source"]["ref_candidates"]
        self.assertEqual(refs[0]["kind"], "newer")
        self.assertEqual(refs[0]["ref"], "refs/tags/v1.0.0")
        self.assertEqual(refs[1]["kind"], "head")

    def test_write_bundle_inserts_local_commit_fallback_before_head(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            tmp = pathlib.Path(td)
            repo, head_commit = self.make_untagged_repo(tmp)
            rule = self.head_only_rule(repo)
            bundle_path = tmp / "bundle.json"
            checkouts_path = tmp / "checkouts.json"
            bundle_out = tmp / "bundle.out.json"
            output_dir = tmp / "raw"
            write_json(bundle_path, {"schema_version": 1, "workloads": [rule]})
            write_json(
                checkouts_path,
                {
                    "schema_version": 1,
                    "checkouts": [self.checkout_record(rule, repo, head_commit)],
                },
            )

            rc = evaluate.audit_paper_external_workload_newer_source_candidates(
                argparse.Namespace(
                    bundle=str(bundle_path),
                    checkouts=str(checkouts_path),
                    write_bundle=True,
                    write_active_configs=False,
                    bundle_out=str(bundle_out),
                    run_id="unit-fallback-source-candidates",
                    output_dir=str(output_dir),
                    no_update_results=True,
                )
            )

            updated = json.loads(bundle_out.read_text(encoding="utf-8"))

        self.assertEqual(rc, 0)
        refs = updated["workloads"][0]["paper_source"]["ref_candidates"]
        self.assertEqual(refs[0]["kind"], "fallback")
        self.assertEqual(refs[0]["ref"], head_commit)
        self.assertEqual(refs[1]["kind"], "head")

    def test_allow_fallback_resolution_prefers_newer_tag_over_head(self) -> None:
        source = {
            "upstream_url": "https://example.invalid/unit-web.git",
            "ref_candidates": [
                {"kind": "head", "ref": "HEAD"},
                {"kind": "newer", "ref": "refs/tags/v1.0.0"},
            ],
        }
        calls: list[str] = []
        original = evaluate.git_ls_remote_candidate

        def fake_ls_remote(url: str, ref: str, *, timeout: int = 60) -> dict:
            calls.append(ref)
            return {
                "ref": ref,
                "resolved": True,
                "resolved_ref": ref,
                "commit": "0123456789abcdef0123456789abcdef01234567",
            }

        evaluate.git_ls_remote_candidate = fake_ls_remote
        try:
            resolution = evaluate.resolve_external_source_ref(source, allow_fallback=True)
        finally:
            evaluate.git_ls_remote_candidate = original

        self.assertEqual(calls[0], "refs/tags/v1.0.0")
        self.assertEqual(resolution["status"], "newer")
        self.assertTrue(resolution["newer_used"])

    def test_allow_fallback_resolution_accepts_raw_commit_candidate_without_ls_remote(self) -> None:
        commit = "0123456789abcdef0123456789abcdef01234567"
        source = {
            "upstream_url": "https://example.invalid/unit-web.git",
            "ref_candidates": [
                {"kind": "head", "ref": "HEAD"},
                {"kind": "fallback", "ref": commit},
            ],
        }
        calls: list[str] = []
        original = evaluate.git_ls_remote_candidate

        def fake_ls_remote(url: str, ref: str, *, timeout: int = 60) -> dict:
            calls.append(ref)
            return {"ref": ref, "resolved": False}

        evaluate.git_ls_remote_candidate = fake_ls_remote
        try:
            resolution = evaluate.resolve_external_source_ref(source, allow_fallback=True)
        finally:
            evaluate.git_ls_remote_candidate = original

        self.assertEqual(calls, [])
        self.assertEqual(resolution["status"], "fallback")
        self.assertEqual(resolution["commit"], commit)
        self.assertTrue(resolution["fallback_used"])
        self.assertTrue(resolution["non_exact_ref_used"])
        self.assertTrue(resolution["raw_commit_ref"])

    def test_adapter_template_literal_survives_command_formatting(self) -> None:
        literal = evaluate.adapter_template_literal({"blockers": [], "checkout_pin": {"commit": "abc"}})

        rendered = evaluate.render_adapter_config_value(literal, {"dataset": "default_performance"})

        self.assertEqual(json.loads(rendered)["checkout_pin"]["commit"], "abc")


if __name__ == "__main__":
    unittest.main()
