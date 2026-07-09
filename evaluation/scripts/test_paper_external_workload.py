#!/usr/bin/env python3
"""Regression tests for the generic external workload bundle runner.

These tests use tiny real Python child programs: no benchmark matrix is run, but
we verify the wrapper's timeout and bounded-log behavior against actual child
processes and raw log files.
"""

from __future__ import annotations

import contextlib
import importlib.util
import io
import json
import pathlib
import sys
import tempfile
import textwrap
import unittest
from typing import Any, Dict


ROOT = pathlib.Path(__file__).resolve().parents[2]
WRAPPER_PATH = ROOT / "evaluation" / "scripts" / "paper_external_workload.py"

spec = importlib.util.spec_from_file_location("paper_external_workload", WRAPPER_PATH)
assert spec is not None and spec.loader is not None
wrapper = importlib.util.module_from_spec(spec)
spec.loader.exec_module(wrapper)


def write_script(path: pathlib.Path, body: str) -> None:
    path.write_text("#!/usr/bin/env python3\n" + textwrap.dedent(body).lstrip(), encoding="utf-8")
    path.chmod(path.stat().st_mode | 0o111)


def write_bundle(bundle: pathlib.Path, workload_dir: pathlib.Path, script_name: str, **rule_overrides: Any) -> None:
    rule: Dict[str, Any] = {
        "id": "real-child",
        "datasets": ["default_performance"],
        "benchmarks": ["RJS-Compiler"],
        "allocators": ["unialloc"],
        "workload_dir": str(workload_dir),
        "cwd": "{workload_dir}",
        "command_template": [sys.executable, "{workload_dir}/" + script_name],
        "measurement": "stdout_json",
        "time_fields": ["seconds"],
        "claim_grade": True,
        "benchmark_owned_json": True,
        "timeout": 5,
    }
    rule.update(rule_overrides)
    bundle.write_text(json.dumps({"schema_version": 1, "workloads": [rule]}), encoding="utf-8")


def run_wrapper(bundle: pathlib.Path, output_dir: pathlib.Path, *extra: str) -> tuple[int, str, Dict[str, Any]]:
    stdout = io.StringIO()
    argv = [
        "--bundle",
        str(bundle),
        "--dataset",
        "default_performance",
        "--benchmark",
        "RJS-Compiler",
        "--allocator",
        "unialloc",
        "--run-id",
        "unit-real-child",
        "--output-dir",
        str(output_dir),
        *extra,
    ]
    with contextlib.redirect_stdout(stdout):
        code = wrapper.main(argv=argv)
    records = [json.loads(line) for line in stdout.getvalue().splitlines() if line.strip().startswith("{")]
    assert records, stdout.getvalue()
    return code, stdout.getvalue(), records[-1]


class PaperExternalWorkloadBoundedRunnerTests(unittest.TestCase):
    def test_successful_real_child_writes_bounded_evidence_and_can_be_claim_grade(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            base = pathlib.Path(tmp)
            workload_dir = base / "workload"
            workload_dir.mkdir()
            write_script(workload_dir / "emit.py", """
                import json
                print(json.dumps(dict(seconds=0.2, benchmark_owned_json=True)))
            """)
            bundle = base / "bundle.json"
            out = base / "out"
            write_bundle(bundle, workload_dir, "emit.py")
            status, _, record = run_wrapper(bundle, out, "--max-output-bytes", "4096")

            stdout_log = pathlib.Path(record["stdout"])
            self.assertEqual(status, 0, record)
            self.assertTrue(record["success"])
            self.assertTrue(record["claim_grade"])
            self.assertEqual(record["claim_grade_blockers"], [])
            self.assertFalse(record["stdout_truncated"])
            self.assertLessEqual(stdout_log.stat().st_size, 4096)
            self.assertAlmostEqual(record["seconds"], 0.2)

    def test_noisy_child_keeps_tail_json_but_blocks_claim_grade_and_caps_log_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            base = pathlib.Path(tmp)
            workload_dir = base / "workload"
            workload_dir.mkdir()
            write_script(workload_dir / "noisy.py", """
                import json, sys
                sys.stdout.write('x' * 8192 + '\\n')
                print(json.dumps(dict(seconds=0.3, benchmark_owned_json=True)))
            """)
            bundle = base / "bundle.json"
            out = base / "out"
            write_bundle(bundle, workload_dir, "noisy.py", max_output_bytes=1024)
            status, _, record = run_wrapper(bundle, out)

            stdout_log = pathlib.Path(record["stdout"])
            self.assertEqual(status, 0, record)
            self.assertTrue(record["success"])
            self.assertFalse(record["claim_grade"])
            self.assertTrue(record["stdout_truncated"])
            self.assertGreater(record["stdout_bytes"], record["stdout_retained_bytes"])
            self.assertLessEqual(stdout_log.stat().st_size, 1024)
            self.assertAlmostEqual(record["seconds"], 0.3)
            self.assertIn("child stdout exceeded max-output-bytes=1024", " ".join(record["claim_grade_blockers"]))

    def test_child_json_without_benchmark_owned_marker_is_not_claim_grade(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            base = pathlib.Path(tmp)
            workload_dir = base / "workload"
            workload_dir.mkdir()
            write_script(workload_dir / "weak.py", """
                import json
                print(json.dumps(dict(seconds=0.4)))
            """)
            bundle = base / "bundle.json"
            out = base / "out"
            write_bundle(bundle, workload_dir, "weak.py")
            status, _, record = run_wrapper(bundle, out, "--max-output-bytes", "4096")

            self.assertEqual(status, 0, record)
            self.assertTrue(record["success"])
            self.assertFalse(record["claim_grade"])
            self.assertTrue(record["rule_benchmark_owned_json"])
            self.assertFalse(record["child_benchmark_owned_json"])
            self.assertIn(
                "child stdout JSON timing record does not mark benchmark_owned_json=true",
                " ".join(record["claim_grade_blockers"]),
            )

    def test_claim_grade_rule_without_benchmark_owned_contract_is_not_claim_grade(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            base = pathlib.Path(tmp)
            workload_dir = base / "workload"
            workload_dir.mkdir()
            write_script(workload_dir / "emit.py", """
                import json
                print(json.dumps(dict(seconds=0.45, benchmark_owned_json=True)))
            """)
            bundle = base / "bundle.json"
            out = base / "out"
            write_bundle(bundle, workload_dir, "emit.py", benchmark_owned_json=False)
            status, _, record = run_wrapper(bundle, out, "--max-output-bytes", "4096")

            self.assertEqual(status, 0, record)
            self.assertTrue(record["success"])
            self.assertFalse(record["claim_grade"])
            self.assertFalse(record["rule_benchmark_owned_json"])
            self.assertTrue(record["child_benchmark_owned_json"])
            self.assertIn(
                "bundle rule timing contract does not mark benchmark_owned_json=true",
                " ".join(record["claim_grade_blockers"]),
            )

    def test_child_claim_grade_blockers_propagate_to_bundle_record(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            base = pathlib.Path(tmp)
            workload_dir = base / "workload"
            workload_dir.mkdir()
            write_script(workload_dir / "blocked.py", """
                import json
                print(json.dumps(dict(
                    seconds=0.5,
                    benchmark_owned_json=True,
                    claim_grade=False,
                    claim_grade_blockers=['synthetic child blocker'],
                )))
            """)
            bundle = base / "bundle.json"
            out = base / "out"
            write_bundle(bundle, workload_dir, "blocked.py")
            status, _, record = run_wrapper(bundle, out, "--max-output-bytes", "4096")

            self.assertEqual(status, 0, record)
            self.assertTrue(record["success"])
            self.assertFalse(record["claim_grade"])
            joined = " ".join(record["claim_grade_blockers"])
            self.assertIn("synthetic child blocker", joined)
            self.assertIn("child timing JSON explicitly marks claim_grade=false", joined)

    def test_timeout_kills_real_child_and_writes_capped_logs(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            base = pathlib.Path(tmp)
            workload_dir = base / "workload"
            workload_dir.mkdir()
            write_script(workload_dir / "sleepy.py", """
                import time
                print('started', flush=True)
                time.sleep(30)
            """)
            bundle = base / "bundle.json"
            out = base / "out"
            write_bundle(bundle, workload_dir, "sleepy.py", timeout=1, max_output_bytes=1024)
            status, _, record = run_wrapper(bundle, out)

            stdout_log = pathlib.Path(record["stdout"])
            self.assertEqual(status, 1, record)
            self.assertFalse(record["success"])
            self.assertFalse(record["claim_grade"])
            self.assertEqual(record["exit_code"], 124)
            self.assertTrue(record["process_group_terminated"])
            self.assertLessEqual(stdout_log.stat().st_size, 1024)
            self.assertIn("timeout after 1s", " ".join(record["claim_grade_blockers"]))

    def test_json_literal_command_argument_is_not_treated_as_template_fields(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            base = pathlib.Path(tmp)
            workload_dir = base / "workload"
            workload_dir.mkdir()
            write_script(workload_dir / "noop.py", "raise SystemExit('child should not execute in dry-run')\n")
            bundle = base / "bundle.json"
            out = base / "out"
            contract = {
                "blockers": [],
                "dataset": "{dataset}",
                "expected_rows": 10_000_000,
            }
            write_bundle(
                bundle,
                workload_dir,
                "noop.py",
                command_template=[
                    sys.executable,
                    "{workload_dir}/noop.py",
                    "--input-contract-json",
                    json.dumps(contract, sort_keys=True),
                ],
            )
            status, stdout, record = run_wrapper(bundle, out, "--dry-run", "--max-output-bytes", "4096")

            self.assertEqual(status, 0, record)
            self.assertTrue(record["success"], record)
            self.assertNotIn("unknown template field", stdout + json.dumps(record))
            argv = record["command"]
            rendered = json.loads(argv[argv.index("--input-contract-json") + 1])
            self.assertEqual(rendered["blockers"], [])
            self.assertEqual(rendered["dataset"], "default_performance")


if __name__ == "__main__":
    unittest.main()
