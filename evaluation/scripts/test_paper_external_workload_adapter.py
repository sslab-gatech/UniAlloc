#!/usr/bin/env python3
"""Regression tests for real external workload adapter execution semantics.

The adapter must execute real child processes, but it also must not hang forever
or retain unbounded stdout/stderr.  These tests use tiny local Python children so
validation is real without running slow benchmark matrices.
"""

from __future__ import annotations

import contextlib
import importlib.util
import io
import json
import pathlib
import subprocess
import sys
import tempfile
import textwrap
import unittest
from typing import Any, Dict, List


ROOT = pathlib.Path(__file__).resolve().parents[2]
ADAPTER_PATH = ROOT / "evaluation" / "scripts" / "paper_external_workload_adapter.py"

spec = importlib.util.spec_from_file_location("paper_external_workload_adapter", ADAPTER_PATH)
assert spec is not None and spec.loader is not None
adapter = importlib.util.module_from_spec(spec)
spec.loader.exec_module(adapter)


def write_config(config_dir: pathlib.Path, command: List[str], **overrides: Any) -> None:
    config: Dict[str, Any] = {
        "configured": True,
        "claim_grade": True,
        "real_workload_dir": ".",
        "cwd": "{real_workload_dir}",
        "command_template": command,
        "env": {
            "UNIALLOC_PAPER_DATASET": "{dataset}",
            "UNIALLOC_PAPER_BENCHMARK": "{benchmark}",
            "UNIALLOC_PAPER_ALLOCATOR": "{allocator}",
            "UNIALLOC_PAPER_VARIANT": "{variant_feature}",
            "UNIALLOC_PAPER_RUN_INDEX": "{run_index}",
        },
        "time_field": "seconds",
        "timing_contract": {"benchmark_owned_json": True},
        "paper_source": {
            "upstream_url": "https://example.invalid/paper-workload.git",
            "matchers": ["example.invalid/paper-workload"],
            "ref_candidates": [{"kind": "exact", "ref": "paper-v1"}],
        },
    }
    config.update(overrides)
    (config_dir / "workload_config.json").write_text(json.dumps(config), encoding="utf-8")


def run_adapter(config_dir: pathlib.Path, *extra: str) -> tuple[int, str, str, Dict[str, Any]]:
    stdout = io.StringIO()
    stderr = io.StringIO()
    argv = [
        "--dataset",
        "default_performance",
        "--benchmark",
        "R-Polars",
        "--allocator",
        "unialloc",
        "--variant-feature",
        "",
        "--config-dir",
        str(config_dir),
        *extra,
    ]
    with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
        code = adapter.main(argv=argv)
    records = []
    for line in stdout.getvalue().splitlines():
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            records.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    assert records, stdout.getvalue()
    return code, stdout.getvalue(), stderr.getvalue(), records[-1]


class PaperExternalWorkloadAdapterExecutionTests(unittest.TestCase):
    def test_successful_real_child_json_can_be_claim_grade_under_cap(self) -> None:
        code = "import json; print(json.dumps(dict(seconds=0.125, benchmark_owned_json=True)))"
        with tempfile.TemporaryDirectory() as tmp:
            config_dir = pathlib.Path(tmp)
            write_config(config_dir, [sys.executable, "-c", code])
            status, _, _, record = run_adapter(config_dir, "--timeout", "5", "--max-output-bytes", "4096")

        self.assertEqual(status, 0, record)
        self.assertTrue(record["success"])
        self.assertTrue(record["claim_grade"])
        self.assertEqual(record["claim_grade_blockers"], [])
        self.assertEqual(record["child_returncode"], 0)
        self.assertFalse(record["child_stdout_truncated"])
        self.assertTrue(record["source_contract"]["paper_exact_ref_complete"])
        self.assertAlmostEqual(record["seconds"], 0.125)

    def test_head_only_source_runs_but_cannot_emit_claim_grade(self) -> None:
        code = "import json; print(json.dumps(dict(seconds=0.125, benchmark_owned_json=True)))"
        head_only_source = {
            "upstream_url": "https://example.invalid/paper-workload.git",
            "matchers": ["example.invalid/paper-workload"],
            "ref_candidates": [{"kind": "head", "ref": "HEAD"}],
        }
        with tempfile.TemporaryDirectory() as tmp:
            config_dir = pathlib.Path(tmp)
            write_config(config_dir, [sys.executable, "-c", code], paper_source=head_only_source)
            status, _, _, record = run_adapter(config_dir, "--timeout", "5", "--max-output-bytes", "4096")

        self.assertEqual(status, 0, record)
        self.assertTrue(record["success"])
        self.assertFalse(record["claim_grade"])
        self.assertTrue(record["source_contract"]["head_only"])
        self.assertIn("HEAD-only", " ".join(record["claim_grade_blockers"]))

    def test_explicitly_accepted_newer_source_with_valid_pin_can_emit_current_claim_grade(self) -> None:
        code = "import json; print(json.dumps(dict(seconds=0.125, benchmark_owned_json=True)))"
        newer_source = {
            "upstream_url": "https://example.invalid/paper-workload.git",
            "matchers": ["example.invalid/paper-workload"],
            "ref_candidates": [{"kind": "newer", "ref": "refs/tags/v2.0.0"}],
        }
        with tempfile.TemporaryDirectory() as tmp:
            config_dir = pathlib.Path(tmp)
            repo = config_dir / "repo"
            repo.mkdir()
            subprocess.run(["git", "init"], cwd=repo, check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            (repo / "README.md").write_text("newer pin validation\n", encoding="utf-8")
            subprocess.run(["git", "add", "README.md"], cwd=repo, check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            subprocess.run(
                [
                    "git",
                    "-c",
                    "user.email=test@example.invalid",
                    "-c",
                    "user.name=Test",
                    "-c",
                    "commit.gpgsign=false",
                    "commit",
                    "-m",
                    "init",
                ],
                cwd=repo,
                check=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
            current = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=repo, text=True).strip()
            write_config(
                config_dir,
                [sys.executable, "-c", code],
                real_workload_dir=str(repo),
                paper_source=newer_source,
                accept_newer_benchmark_source=True,
                accepted_newer_benchmark_source={
                    "accepted": True,
                    "reason": "unit test accepts pinned newer source",
                    "paper_exact": False,
                },
                claim_grade_activation={
                    "source": "unit-test-accepted-newer",
                    "checkout_commit": current,
                    "requested_ref": "refs/tags/v2.0.0",
                    "resolved_ref": "refs/tags/v2.0.0",
                    "source_provenance_class": "user_accepted_newer_pinned_source",
                    "paper_exact": False,
                },
            )
            status, _, _, record = run_adapter(config_dir, "--timeout", "5", "--max-output-bytes", "4096")

        self.assertEqual(status, 0, record)
        self.assertTrue(record["success"])
        self.assertTrue(record["claim_grade"], record)
        self.assertTrue(record["checkout_pin_validation"]["validated"], record)
        self.assertTrue(record["source_contract"]["accepted_newer_ref_complete"], record)
        self.assertFalse(record["source_contract"]["paper_exact_ref_complete"], record)
        self.assertEqual(record["claim_grade_blockers"], [])

    def test_explicitly_accepted_fallback_source_with_valid_pin_can_emit_current_claim_grade(self) -> None:
        code = "import json; print(json.dumps(dict(seconds=0.125, benchmark_owned_json=True)))"
        with tempfile.TemporaryDirectory() as tmp:
            config_dir = pathlib.Path(tmp)
            repo = config_dir / "repo"
            repo.mkdir()
            subprocess.run(["git", "init"], cwd=repo, check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            (repo / "README.md").write_text("fallback pin validation\n", encoding="utf-8")
            subprocess.run(["git", "add", "README.md"], cwd=repo, check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            subprocess.run(
                [
                    "git",
                    "-c",
                    "user.email=test@example.invalid",
                    "-c",
                    "user.name=Test",
                    "-c",
                    "commit.gpgsign=false",
                    "commit",
                    "-m",
                    "init",
                ],
                cwd=repo,
                check=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
            current = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=repo, text=True).strip()
            fallback_source = {
                "upstream_url": "https://example.invalid/paper-workload.git",
                "matchers": ["example.invalid/paper-workload"],
                "ref_candidates": [{"kind": "fallback", "ref": current}],
            }
            write_config(
                config_dir,
                [sys.executable, "-c", code],
                real_workload_dir=str(repo),
                paper_source=fallback_source,
                accept_newer_benchmark_source=True,
                accepted_newer_benchmark_source={
                    "accepted": True,
                    "reason": "unit test accepts pinned fallback source",
                    "paper_exact": False,
                },
                claim_grade_activation={
                    "source": "unit-test-accepted-fallback",
                    "checkout_commit": current,
                    "requested_ref": current,
                    "resolved_ref": current,
                    "source_provenance_class": "user_accepted_newer_pinned_source",
                    "paper_exact": False,
                },
            )
            status, _, _, record = run_adapter(config_dir, "--timeout", "5", "--max-output-bytes", "4096")

        self.assertEqual(status, 0, record)
        self.assertTrue(record["success"])
        self.assertTrue(record["claim_grade"], record)
        self.assertTrue(record["checkout_pin_validation"]["validated"], record)
        self.assertTrue(record["source_contract"]["accepted_newer_ref_complete"], record)
        self.assertTrue(record["source_contract"]["accepted_non_exact_ref_complete"], record)
        self.assertFalse(record["source_contract"]["paper_exact_ref_complete"], record)
        self.assertEqual(record["claim_grade_blockers"], [])

    def test_fallback_source_without_policy_runs_but_cannot_emit_claim_grade(self) -> None:
        code = "import json; print(json.dumps(dict(seconds=0.125, benchmark_owned_json=True)))"
        fallback_source = {
            "upstream_url": "https://example.invalid/paper-workload.git",
            "matchers": ["example.invalid/paper-workload"],
            "ref_candidates": [{"kind": "fallback", "ref": "0123456789abcdef0123456789abcdef01234567"}],
        }
        with tempfile.TemporaryDirectory() as tmp:
            config_dir = pathlib.Path(tmp)
            write_config(config_dir, [sys.executable, "-c", code], paper_source=fallback_source)
            status, _, _, record = run_adapter(config_dir, "--timeout", "5", "--max-output-bytes", "4096")

        self.assertEqual(status, 0, record)
        self.assertTrue(record["success"])
        self.assertFalse(record["claim_grade"], record)
        self.assertFalse(record["source_contract"]["paper_exact_ref_complete"], record)
        self.assertIn("not paper-exact", " ".join(record["claim_grade_blockers"]))

    def test_cargo_bench_leaf_filter_config_fails_before_child_execution(self) -> None:
        code = "raise SystemExit('child should not execute')"
        with tempfile.TemporaryDirectory() as tmp:
            config_dir = pathlib.Path(tmp)
            write_config(
                config_dir,
                [
                    sys.executable,
                    "paper_external_cargo_bench_json.py",
                    "--bench-name-filter",
                    "parser",
                    "--",
                    sys.executable,
                    "-c",
                    code,
                ],
                source="paper-external-workload-cargo-bench-json-wrapper",
                not_claim_grade_reason="representative parser selector only",
                cargo_bench_json_wrapper={
                    "bench_name_filter": "parser",
                    "not_claim_grade_reason": "focused upstream parser leaf only",
                },
            )
            status, stdout, stderr, record = run_adapter(config_dir, "--timeout", "5", "--max-output-bytes", "4096")

        self.assertEqual(status, 2, record)
        self.assertFalse(record["success"], record)
        self.assertFalse(record["claim_grade"], record)
        joined = " ".join(record["claim_grade_blockers"])
        self.assertIn("representative parser selector only", joined)
        self.assertIn("focused upstream parser leaf only", joined)
        self.assertIn("bench-filtered subset evidence: parser", joined)
        self.assertIn("does not request the required full-row claim-grade contract", joined)
        self.assertNotIn("child should not execute", stdout + stderr + json.dumps(record))

    def test_rpython_cargo_wrapper_contract_is_not_misclassified_as_rpolars(self) -> None:
        code = "raise SystemExit('child should not execute in dry-run')"
        with tempfile.TemporaryDirectory() as tmp:
            config_dir = pathlib.Path(tmp)
            write_config(
                config_dir,
                [
                    sys.executable,
                    "paper_external_cargo_bench_json.py",
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
                    "rpython-paper-run",
                    "--cargo-bench-targets",
                    "execution,microbenchmarks",
                    "--expected-cargo-bench-targets",
                    "execution,microbenchmarks",
                    "--",
                    sys.executable,
                    "-c",
                    code,
                ],
                benchmarks=["RPython"],
                cargo_bench_json_wrapper={"claim_grade_contract": "rpython-paper-run"},
            )
            status, stdout, stderr, record = run_adapter(config_dir, "--dry-run", "--timeout", "5", "--max-output-bytes", "4096")

        self.assertEqual(status, 0, record)
        self.assertTrue(record["success"], record)
        self.assertFalse(record["claim_grade"])
        joined = " ".join(record["claim_grade_blockers"])
        self.assertIn("dry-run validates shape only", joined)
        self.assertNotIn("expected rpolars-paper-run", joined)

    def test_json_literal_command_argument_is_not_treated_as_template_fields(self) -> None:
        contract = {
            "blockers": [],
            "dataset": "{dataset}",
            "expected_rows": 10_000_000,
        }
        with tempfile.TemporaryDirectory() as tmp:
            config_dir = pathlib.Path(tmp)
            write_config(
                config_dir,
                [
                    sys.executable,
                    "-c",
                    "raise SystemExit('child should not execute in dry-run')",
                    "--input-contract-json",
                    json.dumps(contract, sort_keys=True),
                ],
            )
            status, stdout, stderr, record = run_adapter(
                config_dir,
                "--dry-run",
                "--timeout",
                "5",
                "--max-output-bytes",
                "4096",
            )

        self.assertEqual(status, 0, record)
        self.assertTrue(record["success"], record)
        joined = " ".join(record["claim_grade_blockers"])
        self.assertNotIn("unknown template field", joined + stdout + stderr + json.dumps(record))
        argv = record["command"]
        rendered = json.loads(argv[argv.index("--input-contract-json") + 1])
        self.assertEqual(rendered["blockers"], [])
        self.assertEqual(rendered["dataset"], "default_performance")

    def test_promoted_exact_checkout_pin_is_revalidated_before_claim_grade(self) -> None:
        code = "import json; print(json.dumps(dict(seconds=0.125, benchmark_owned_json=True)))"
        with tempfile.TemporaryDirectory() as tmp:
            config_dir = pathlib.Path(tmp)
            repo = config_dir / "repo"
            repo.mkdir()
            subprocess.run(["git", "init"], cwd=repo, check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            (repo / "README.md").write_text("pin validation\n", encoding="utf-8")
            subprocess.run(["git", "add", "README.md"], cwd=repo, check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            subprocess.run(
                [
                    "git",
                    "-c",
                    "user.email=test@example.invalid",
                    "-c",
                    "user.name=Test",
                    "-c",
                    "commit.gpgsign=false",
                    "commit",
                    "-m",
                    "init",
                ],
                cwd=repo,
                check=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
            current = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=repo, text=True).strip()
            write_config(
                config_dir,
                [sys.executable, "-c", code],
                real_workload_dir=str(repo),
                claim_grade_activation={
                    "source": "unit-test-promotion",
                    "checkout_commit": current,
                    "requested_ref": "refs/tags/paper-v1",
                    "resolved_ref": "refs/tags/paper-v1",
                },
            )
            status, _, _, record = run_adapter(config_dir, "--timeout", "5", "--max-output-bytes", "4096")

        self.assertEqual(status, 0, record)
        self.assertTrue(record["claim_grade"], record)
        self.assertTrue(record["checkout_pin_validation"]["validated"], record)

    def test_promoted_exact_checkout_pin_mismatch_blocks_claim_grade(self) -> None:
        code = "import json; print(json.dumps(dict(seconds=0.125, benchmark_owned_json=True)))"
        with tempfile.TemporaryDirectory() as tmp:
            config_dir = pathlib.Path(tmp)
            repo = config_dir / "repo"
            repo.mkdir()
            subprocess.run(["git", "init"], cwd=repo, check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            (repo / "README.md").write_text("pin validation\n", encoding="utf-8")
            subprocess.run(["git", "add", "README.md"], cwd=repo, check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            subprocess.run(
                [
                    "git",
                    "-c",
                    "user.email=test@example.invalid",
                    "-c",
                    "user.name=Test",
                    "-c",
                    "commit.gpgsign=false",
                    "commit",
                    "-m",
                    "init",
                ],
                cwd=repo,
                check=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
            wrong = "0" * 40
            write_config(
                config_dir,
                [sys.executable, "-c", code],
                real_workload_dir=str(repo),
                claim_grade_activation={
                    "source": "unit-test-promotion",
                    "checkout_commit": wrong,
                    "requested_ref": "refs/tags/paper-v1",
                    "resolved_ref": "refs/tags/paper-v1",
                },
            )
            status, _, _, record = run_adapter(config_dir, "--timeout", "5", "--max-output-bytes", "4096")

        self.assertEqual(status, 2, record)
        self.assertFalse(record["claim_grade"], record)
        self.assertFalse(record["checkout_pin_validation"]["validated"], record)
        self.assertIn("git HEAD does not match", " ".join(record["claim_grade_blockers"]))

    def test_timeout_kills_real_child_process_group(self) -> None:
        code = "import time; print('child-started', flush=True); time.sleep(30)"
        with tempfile.TemporaryDirectory() as tmp:
            config_dir = pathlib.Path(tmp)
            write_config(config_dir, [sys.executable, "-c", code])
            status, stdout, _, record = run_adapter(config_dir, "--timeout", "1", "--max-output-bytes", "4096")

        self.assertEqual(status, 124, record)
        self.assertIn("child-started", stdout)
        self.assertFalse(record["success"])
        self.assertFalse(record["claim_grade"])
        self.assertTrue(record["timed_out"])
        self.assertEqual(record["child_returncode"], 124)
        self.assertIn("timed out after 1s", " ".join(record["claim_grade_blockers"]))

    def test_output_cap_keeps_tail_json_but_blocks_claim_grade(self) -> None:
        code = textwrap.dedent(
            """
            import json, sys
            sys.stdout.write('x' * 8192 + '\\n')
            print(json.dumps(dict(seconds=0.25, benchmark_owned_json=True)))
            """
        )
        with tempfile.TemporaryDirectory() as tmp:
            config_dir = pathlib.Path(tmp)
            write_config(config_dir, [sys.executable, "-c", code])
            status, stdout, _, record = run_adapter(config_dir, "--timeout", "5", "--max-output-bytes", "1024")

        self.assertEqual(status, 0, record)
        self.assertIn("retained last", stdout)
        self.assertTrue(record["success"])
        self.assertFalse(record["claim_grade"])
        self.assertTrue(record["child_stdout_truncated"])
        self.assertGreater(record["child_stdout_bytes"], record["child_stdout_retained_bytes"])
        self.assertAlmostEqual(record["seconds"], 0.25)
        self.assertIn("child stdout exceeded max-output-bytes=1024", " ".join(record["claim_grade_blockers"]))


if __name__ == "__main__":
    unittest.main()
