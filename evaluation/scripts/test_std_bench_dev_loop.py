#!/usr/bin/env python3
"""Regression tests for the bounded build-once std_bench development loop."""

from __future__ import annotations

import argparse
import contextlib
import hashlib
import importlib.util
import io
import json
import os
import pathlib
import subprocess
import sys
import tempfile
import time
import unittest
from unittest import mock


ROOT = pathlib.Path(__file__).resolve().parents[2]
EVALUATE_PATH = ROOT / "evaluation" / "scripts" / "evaluate.py"

spec = importlib.util.spec_from_file_location("unialloc_evaluate_std_bench_dev", EVALUATE_PATH)
assert spec is not None and spec.loader is not None
evaluate = importlib.util.module_from_spec(spec)
spec.loader.exec_module(evaluate)


def bounded_result(
    *,
    stdout: str = "",
    stderr: str = "",
    exit_code: int = 0,
    timed_out: bool = False,
) -> dict:
    return {
        "exit_code": exit_code,
        "error": f"timeout after 1s" if timed_out else None,
        "interrupted": False,
        "status": "timed_out" if timed_out else "completed" if exit_code == 0 else "failed",
        "interrupt_signal": None,
        "stdout_tail": stdout.encode(),
        "stderr_tail": stderr.encode(),
        "stdout_bytes": len(stdout.encode()),
        "stderr_bytes": len(stderr.encode()),
        "stdout_retained_bytes": len(stdout.encode()),
        "stderr_retained_bytes": len(stderr.encode()),
        "stdout_truncated": False,
        "stderr_truncated": False,
        "capture_complete": True,
        "process_group_pid": 123,
        "process_group_terminated": timed_out,
        "process_group_absent_after_cleanup": True,
        "child_returncode_after_cleanup": exit_code,
        "started_at": "2026-07-10T00:00:00+00:00",
        "ended_at": "2026-07-10T00:00:01+00:00",
    }


def cargo_artifact_payload(executable: str = "/tmp/std_bench-one") -> dict:
    package_root = (ROOT / "unialloc").resolve()
    return {
        "reason": "compiler-artifact",
        "package_id": f"path+{package_root.as_uri()}#0.1.0",
        "manifest_path": str(package_root / "Cargo.toml"),
        "target": {
            "name": "std_bench",
            "kind": ["bench"],
            "src_path": str(package_root / "benches/lib.rs"),
            "test": True,
        },
        "profile": {"test": True},
        "executable": executable,
        "fresh": True,
    }


def write_fake_std_bench_toolchain(
    tmp: pathlib.Path,
    *,
    hanging_benchmark=None,
):
    """Write a fake Cargo + std_bench pair for real subprocess runner tests."""

    log_path = tmp / "calls.log"
    descendant_pid_path = tmp / "descendant.pid"
    executable = tmp / "std_bench"
    executable.write_text(
        "\n".join(
            [
                "#!/usr/bin/env python3",
                "import json, os, pathlib, signal, subprocess, sys, time",
                f"root = pathlib.Path({str(ROOT)!r})",
                f"hanging_benchmark = {hanging_benchmark!r}",
                f"descendant_pid_path = pathlib.Path({str(descendant_pid_path)!r})",
                "log = pathlib.Path(os.environ['UNIALLOC_TEST_STD_BENCH_LOG'])",
                "args = sys.argv[1:]",
                "if '--list' in args:",
                "    with log.open('a', encoding='utf-8') as handle: handle.write('surface\\n')",
                "    manifest = json.loads((root / 'evaluation/config/compiler_coverage_manifest.template.json').read_text(encoding='utf-8'))",
                "    names = manifest['benchmark_suite']['expected_benchmarks']",
                "    print('aaa_semantic_auto_metadata_enable: benchmark')",
                "    for name in names: print(f'{name}: benchmark')",
                "    print('zzz_semantic_auto_metadata_report: benchmark')",
                "    print(f'0 tests, {len(names) + 2} benchmarks')",
                "    raise SystemExit(0)",
                "selected = args[args.index('--exact') + 1:]",
                "benchmarks = [name for name in selected if not name.startswith(('aaa_', 'zzz_'))]",
                "with log.open('a', encoding='utf-8') as handle:",
                "    for name in benchmarks: handle.write(f'case:{name}\\n')",
                "if hanging_benchmark and hanging_benchmark in benchmarks:",
                "    child_code = \"import signal,time; signal.signal(signal.SIGTERM, lambda *_: None); time.sleep(60)\"",
                "    child = subprocess.Popen([sys.executable, '-c', child_code])",
                "    descendant_pid_path.write_text(str(child.pid), encoding='utf-8')",
                "    print('noise-' + ('x' * 65536), flush=True)",
                "    print('PATHOLOGY_MARKER reproduced; bounded timeout is sufficient', flush=True)",
                "    signal.signal(signal.SIGTERM, lambda *_: None)",
                "    while True: time.sleep(1)",
                "print(f'running {len(selected)} tests')",
                "for name in selected: print(f'test {name} ... ok')",
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    executable.chmod(0o755)

    artifact = json.dumps(cargo_artifact_payload(str(executable)))
    fake_cargo = tmp / "cargo"
    fake_cargo.write_text(
        "\n".join(
            [
                "#!/usr/bin/env python3",
                "import os, pathlib",
                "log = pathlib.Path(os.environ['UNIALLOC_TEST_STD_BENCH_LOG'])",
                "with log.open('a', encoding='utf-8') as handle: handle.write('build\\n')",
                f"print({artifact!r})",
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    fake_cargo.chmod(0o755)
    return fake_cargo, log_path, descendant_pid_path


class StdBenchDevLoopTests(unittest.TestCase):
    def test_profile_catalog_resolves_short_e2e_family_and_pathology_presets(self) -> None:
        catalog = ROOT / "evaluation/config/std_bench_dev_profiles.json"
        raw_catalog = json.loads(catalog.read_text(encoding="utf-8"))
        self.assertEqual(
            raw_catalog["defaults"],
            {
                "recommended_libtest_mode": "test-once",
                "recommended_libtest_fail_fast": True,
            },
        )
        for profile in raw_catalog["profiles"].values():
            self.assertNotIn("claim_grade", profile)
            self.assertNotIn("recommended_libtest_mode", profile)
            self.assertNotIn("recommended_libtest_fail_fast", profile)

        quick = evaluate.load_std_bench_dev_profile(catalog, batch_index=0)
        self.assertEqual(quick["preset"], "quick-e2e")
        self.assertEqual(
            quick["benchmarks"],
            [
                "binary_heap::bench_push",
                "string::bench_exact_size_shrink_to_fit",
                "vec::bench_with_capacity_1000",
            ],
        )
        self.assertEqual(quick["timeout_seconds"], 12)
        self.assertFalse(quick["claim_grade"])
        self.assertEqual(
            hashlib.sha256(
                ("\n".join(quick["benchmarks"]) + "\n").encode("utf-8")
            ).hexdigest(),
            "be31fdddbe3910bd0a3d97d8cfaf9afd5fedaca260abbc45b1b97856f9458dc4",
        )

        family = evaluate.load_std_bench_dev_profile(
            catalog,
            preset="family-smoke",
            batch_index=0,
        )
        self.assertEqual(len(family["benchmarks"]), 10)
        self.assertEqual(family["timeout_seconds"], 20)
        self.assertEqual(
            hashlib.sha256(
                ("\n".join(family["benchmarks"]) + "\n").encode("utf-8")
            ).hexdigest(),
            "10e5a72b81b00285750b2648aa4c444981690147f5d282b403033b9c32f31be0",
        )

        pathology = evaluate.load_std_bench_dev_profile(
            catalog,
            preset="pathology",
            batch_index=1,
        )
        self.assertEqual(pathology["benchmarks"], ["btree::map::iter_1m"])
        self.assertEqual(pathology["timeout_seconds"], 5)
        self.assertFalse(pathology["claim_grade"])

        pathology_benchmarks = []
        for batch_index in range(3):
            resolved = evaluate.load_std_bench_dev_profile(
                catalog,
                preset="pathology",
                batch_index=batch_index,
            )
            pathology_benchmarks.extend(resolved["benchmarks"])
        self.assertEqual(len(pathology_benchmarks), len(set(pathology_benchmarks)))
        self.assertEqual(
            hashlib.sha256(
                ("\n".join(pathology_benchmarks) + "\n").encode("utf-8")
            ).hexdigest(),
            "5419f5bf1845f06a874652ac90b3d1802ffd209434a175ec2731843852951a40",
        )

    def test_default_profile_cli_real_subprocess_e2e(self) -> None:
        with tempfile.TemporaryDirectory() as raw_tmp:
            tmp = pathlib.Path(raw_tmp)
            fake_cargo, log_path, _ = write_fake_std_bench_toolchain(tmp)

            out_dir = tmp / "out"
            env = os.environ.copy()
            env["UNIALLOC_TEST_STD_BENCH_LOG"] = str(log_path)
            env["PYTHONPYCACHEPREFIX"] = "/tmp/unialloc-pycache"
            completed = subprocess.run(
                [
                    sys.executable,
                    str(EVALUATE_PATH),
                    "run-std-bench-dev-loop",
                    "--run-id",
                    "unit-quick-e2e",
                    "--output-dir",
                    str(out_dir),
                    "--cargo",
                    str(fake_cargo),
                    "--toolchain",
                    "system",
                    "--build-timeout",
                    "10",
                    "--case-timeout",
                    "2",
                ],
                cwd=ROOT,
                env=env,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                timeout=20,
                check=False,
            )
            self.assertEqual(completed.returncode, 0, completed.stderr or completed.stdout)
            self.assertEqual(
                log_path.read_text(encoding="utf-8").splitlines(),
                [
                    "build",
                    "surface",
                    "case:binary_heap::bench_push",
                    "case:string::bench_exact_size_shrink_to_fit",
                    "case:vec::bench_with_capacity_1000",
                ],
            )
            summary = json.loads(
                (out_dir / "std-bench-dev-loop-summary.json").read_text(encoding="utf-8")
            )
            self.assertEqual(summary["status"], "completed")
            self.assertTrue(summary["success"])
            self.assertEqual(summary["build_invocation_count"], 1)
            self.assertEqual(summary["surface"]["benchmark_count"], 430)
            self.assertEqual(
                summary["surface"]["benchmark_name_sha256"],
                "241ae2507e28f9004e29f186a9d77c6f9de48a82c2d9feb86d7031cb1d31d786",
            )
            self.assertEqual(summary["passed_case_count"], 3)
            self.assertFalse(summary["claim_grade"])
            self.assertFalse(summary["complete_for_claim"])
            self.assertFalse(summary["calibrated"])

    def test_pathology_timeout_stops_and_cleans_real_process_group(self) -> None:
        pathology = "slice::sort_unstable_by_key_lexicographic"
        control = "vec::bench_with_capacity_1000"
        with tempfile.TemporaryDirectory() as raw_tmp:
            tmp = pathlib.Path(raw_tmp)
            fake_cargo, log_path, descendant_pid_path = write_fake_std_bench_toolchain(
                tmp,
                hanging_benchmark=pathology,
            )
            out_dir = tmp / "out"
            env = os.environ.copy()
            env["UNIALLOC_TEST_STD_BENCH_LOG"] = str(log_path)
            env["PYTHONPYCACHEPREFIX"] = "/tmp/unialloc-pycache"
            started = time.monotonic()
            completed = subprocess.run(
                [
                    sys.executable,
                    str(EVALUATE_PATH),
                    "run-std-bench-dev-loop",
                    "--run-id",
                    "unit-pathology-timeout",
                    "--output-dir",
                    str(out_dir),
                    "--cargo",
                    str(fake_cargo),
                    "--toolchain",
                    "system",
                    "--build-timeout",
                    "10",
                    "--case-timeout",
                    "1",
                    "--max-output-bytes",
                    "32768",
                    "--benchmark",
                    pathology,
                    "--benchmark",
                    control,
                ],
                cwd=ROOT,
                env=env,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                timeout=20,
                check=False,
            )
            elapsed = time.monotonic() - started
            self.assertEqual(completed.returncode, 124, completed.stderr or completed.stdout)
            self.assertLess(elapsed, 10)
            self.assertEqual(
                log_path.read_text(encoding="utf-8").splitlines(),
                ["build", "surface", f"case:{pathology}"],
            )

            summary = json.loads(
                (out_dir / "std-bench-dev-loop-summary.json").read_text(encoding="utf-8")
            )
            self.assertEqual(summary["status"], "case-timed_out")
            self.assertFalse(summary["success"])
            self.assertFalse(summary["claim_grade"])
            self.assertEqual(summary["case_records"][0]["benchmark"], pathology)
            self.assertEqual(summary["case_records"][0]["status"], "timed_out")
            self.assertEqual(summary["case_records"][1]["benchmark"], control)
            self.assertEqual(summary["case_records"][1]["status"], "unattempted")
            timed_out = summary["case_records"][0]
            self.assertTrue(timed_out["process_group_terminated"])
            self.assertTrue(timed_out["process_group_absent_after_cleanup"])
            evidence = timed_out["evidence"]
            self.assertTrue(evidence["stdout_truncated"])
            self.assertLessEqual(evidence["stdout_retained_bytes"], 32768)
            self.assertIn(
                "PATHOLOGY_MARKER",
                pathlib.Path(evidence["stdout"]).read_text(encoding="utf-8"),
            )

            descendant_pid = int(descendant_pid_path.read_text(encoding="utf-8"))
            descendant_absent = False
            deadline = time.monotonic() + 3
            while time.monotonic() < deadline:
                try:
                    os.kill(descendant_pid, 0)
                except ProcessLookupError:
                    descendant_absent = True
                    break
                time.sleep(0.05)
            self.assertTrue(descendant_absent, f"descendant {descendant_pid} survived cleanup")

    def test_build_command_is_one_no_run_cargo_test_invocation(self) -> None:
        command = evaluate.std_bench_dev_build_command(
            "cargo",
            "nightly-2026-06-11",
            ["bench_ourself", "stats"],
        )
        self.assertEqual(command[:3], ["cargo", "+nightly-2026-06-11", "test"])
        self.assertEqual(command.count("cargo"), 1)
        self.assertIn("--no-run", command)
        self.assertIn("--message-format=json-render-diagnostics", command)
        self.assertNotIn("--", command)
        self.assertNotIn("--list", command)

    def test_cargo_artifact_parser_requires_one_std_bench_executable(self) -> None:
        artifact = cargo_artifact_payload()
        parsed = evaluate.parse_std_bench_cargo_executable(json.dumps(artifact) + "\n")
        self.assertEqual(parsed["executable"], "/tmp/std_bench-one")
        self.assertTrue(parsed["fresh"])

        with self.assertRaisesRegex(ValueError, "exactly one"):
            evaluate.parse_std_bench_cargo_executable("")
        with self.assertRaisesRegex(ValueError, "exactly one"):
            evaluate.parse_std_bench_cargo_executable(
                json.dumps(artifact)
                + "\n"
                + json.dumps({**artifact, "executable": "/tmp/std_bench-two"})
                + "\n"
            )

        for invalid in (
            {**artifact, "package_id": "path+file:///tmp/spoof#0.1.0"},
            {**artifact, "manifest_path": "/tmp/spoof/Cargo.toml"},
            {
                **artifact,
                "target": {**artifact["target"], "src_path": "/tmp/spoof.rs"},
            },
            {
                **artifact,
                "target": {**artifact["target"], "test": False},
            },
            {**artifact, "profile": {"test": False}},
        ):
            with self.subTest(invalid=invalid):
                with self.assertRaisesRegex(ValueError, "exactly one"):
                    evaluate.parse_std_bench_cargo_executable(json.dumps(invalid) + "\n")

        with self.assertRaisesRegex(ValueError, "complete, untruncated"):
            evaluate.parse_std_bench_cargo_executable(
                json.dumps(artifact) + "\n",
                capture_complete=True,
                stdout_truncated=True,
            )
        with self.assertRaisesRegex(ValueError, "complete, untruncated"):
            evaluate.parse_std_bench_cargo_executable(
                json.dumps(artifact) + "\n",
                capture_complete=False,
                stdout_truncated=False,
            )

    def test_surface_gate_locks_exact_canonical_430_identity(self) -> None:
        canonical = evaluate.canonical_std_bench_names()
        self.assertEqual(len(canonical), 430)
        status = evaluate.std_bench_dev_surface_status(
            [
                "aaa_semantic_auto_metadata_enable",
                *canonical,
                "zzz_semantic_auto_metadata_report",
            ],
            required_sentinels=evaluate.STD_BENCH_DEV_SENTINELS,
        )
        self.assertTrue(status["ready"], status)
        self.assertEqual(status["benchmark_count"], 430)
        self.assertEqual(
            status["benchmark_name_sha256"],
            "241ae2507e28f9004e29f186a9d77c6f9de48a82c2d9feb86d7031cb1d31d786",
        )

        missing = evaluate.std_bench_dev_surface_status(canonical[:-1])
        self.assertFalse(missing["ready"])
        self.assertIn("canonical", " ".join(missing["blockers"]))

        missing_sentinel = evaluate.std_bench_dev_surface_status(
            canonical,
            required_sentinels=evaluate.STD_BENCH_DEV_SENTINELS,
        )
        self.assertFalse(missing_sentinel["ready"])
        self.assertEqual(
            missing_sentinel["missing_required_sentinels"],
            list(evaluate.STD_BENCH_DEV_SENTINELS),
        )

        unexpected_sentinel = evaluate.std_bench_dev_surface_status(
            [
                "aaa_semantic_auto_metadata_enable",
                "aaa_semantic_auto_metadata_spoof",
                *canonical,
                "zzz_semantic_auto_metadata_report",
            ],
            required_sentinels=evaluate.STD_BENCH_DEV_SENTINELS,
        )
        self.assertFalse(unexpected_sentinel["ready"])
        self.assertEqual(
            unexpected_sentinel["unexpected_sentinels"],
            ["aaa_semantic_auto_metadata_spoof"],
        )

        duplicate_sentinel = evaluate.std_bench_dev_surface_status(
            [
                "aaa_semantic_auto_metadata_enable",
                "aaa_semantic_auto_metadata_enable",
                *canonical,
                "zzz_semantic_auto_metadata_report",
            ],
            required_sentinels=evaluate.STD_BENCH_DEV_SENTINELS,
        )
        self.assertFalse(duplicate_sentinel["ready"])
        self.assertEqual(
            duplicate_sentinel["duplicate_sentinels"],
            ["aaa_semantic_auto_metadata_enable"],
        )

    def test_case_command_runs_one_leaf_with_exact_sentinels(self) -> None:
        command = evaluate.std_bench_dev_case_command(
            pathlib.Path("/tmp/std_bench"),
            "vec::bench_with_capacity_1000",
            sentinels=[
                "aaa_semantic_auto_metadata_enable",
                "zzz_semantic_auto_metadata_report",
            ],
            fail_fast=True,
        )
        self.assertEqual(command[0], "/tmp/std_bench")
        self.assertEqual(command.count("vec::bench_with_capacity_1000"), 1)
        self.assertIn("--exact", command)
        self.assertIn("--test-threads=1", command)
        self.assertIn("--fail-fast", command)
        self.assertNotIn("cargo", command)

    def test_timeout_record_keeps_bounded_cleanup_evidence(self) -> None:
        child = bounded_result(
            stdout="pathological slowdown reproduced\n" + "x" * 32,
            exit_code=124,
            timed_out=True,
        )
        with tempfile.TemporaryDirectory() as raw_tmp:
            tmp = pathlib.Path(raw_tmp)
            evidence = evaluate.std_bench_dev_child_evidence(tmp, "timeout", child)
            record = evaluate.std_bench_dev_child_record(
                label="timeout",
                command=["/tmp/std_bench", "--exact", "btree::map::iter_1m"],
                result=child,
                wall_seconds=1.0,
                evidence=evidence,
                benchmark="btree::map::iter_1m",
            )
        self.assertEqual(record["status"], "timed_out")
        self.assertTrue(record["process_group_terminated"])
        self.assertTrue(record["process_group_absent_after_cleanup"])
        self.assertLessEqual(
            evidence["stdout_retained_bytes"],
            evidence["stdout_bytes"],
        )

        incomplete = dict(child)
        incomplete.pop("capture_complete")
        incomplete.pop("process_group_absent_after_cleanup")
        with tempfile.TemporaryDirectory() as raw_tmp:
            tmp = pathlib.Path(raw_tmp)
            incomplete_evidence = evaluate.std_bench_dev_child_evidence(
                tmp,
                "incomplete",
                incomplete,
            )
            incomplete_record = evaluate.std_bench_dev_child_record(
                label="incomplete",
                command=["/tmp/std_bench", "--exact", "btree::map::iter_1m"],
                result=incomplete,
                wall_seconds=1.0,
                evidence=incomplete_evidence,
                benchmark="btree::map::iter_1m",
            )
        self.assertFalse(incomplete_evidence["capture_complete"])
        self.assertFalse(incomplete_record["process_group_absent_after_cleanup"])

    def test_loop_builds_once_and_stops_before_second_leaf_after_failure(self) -> None:
        with tempfile.TemporaryDirectory() as raw_tmp:
            tmp = pathlib.Path(raw_tmp)
            executable = tmp / "std_bench"
            executable.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
            executable.chmod(0o755)
            artifact = json.dumps(cargo_artifact_payload(str(executable)))
            canonical = evaluate.canonical_std_bench_names()
            listed = "\n".join(
                [
                    "aaa_semantic_auto_metadata_enable: benchmark",
                    *(f"{name}: benchmark" for name in canonical),
                    "zzz_semantic_auto_metadata_report: benchmark",
                    "",
                    "0 tests, 432 benchmarks",
                ]
            )
            first_failed = "\n".join(
                [
                    "running 3 tests",
                    "test aaa_semantic_auto_metadata_enable ... ok",
                    "test binary_heap::bench_push ... FAILED",
                ]
            )
            args = argparse.Namespace(
                run_id="unit-dev-loop",
                output_dir=str(tmp / "out"),
                profile=None,
                batch_index=0,
                benchmarks=[
                    "binary_heap::bench_push",
                    "vec::bench_with_capacity_1000",
                ],
                features="bench_ourself,stats",
                toolchain="nightly-2026-06-11",
                cargo="cargo",
                build_timeout=60,
                case_timeout=5,
                max_output_bytes=65536,
                no_fail_fast=False,
            )
            source = {"source_digest": "a" * 64}
            with mock.patch.object(
                evaluate,
                "run_plan_command_bounded",
                side_effect=[
                    bounded_result(stdout=artifact),
                    bounded_result(stdout=listed),
                    bounded_result(stdout=first_failed, exit_code=1),
                ],
            ) as run_child, mock.patch.object(
                evaluate,
                "repository_source_fingerprint",
                return_value=source,
            ), mock.patch.object(evaluate, "RESULTS", tmp / "must-not-be-written"):
                with contextlib.redirect_stdout(io.StringIO()):
                    status = evaluate.run_std_bench_dev_loop(args)

            self.assertEqual(status, 1)
            self.assertEqual(run_child.call_count, 3)
            commands = [call.args[0] for call in run_child.call_args_list]
            self.assertEqual(sum("--no-run" in command for command in commands), 1)
            self.assertEqual(sum(str(executable) == command[0] for command in commands), 2)
            self.assertNotIn("vec::bench_with_capacity_1000", commands[-1])

            summary = json.loads(
                (tmp / "out/std-bench-dev-loop-summary.json").read_text(encoding="utf-8")
            )
            self.assertFalse(summary["claim_grade"])
            self.assertFalse(summary["complete_for_claim"])
            self.assertFalse(summary["calibrated"])
            self.assertEqual(summary["build_invocation_count"], 1)
            self.assertEqual(summary["case_records"][0]["status"], "failed")
            self.assertEqual(summary["case_records"][1]["status"], "unattempted")
            self.assertFalse((tmp / "must-not-be-written").exists())


if __name__ == "__main__":
    unittest.main()
