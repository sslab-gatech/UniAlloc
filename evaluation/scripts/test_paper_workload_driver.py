#!/usr/bin/env python3
"""Regression tests for the local paper workload driver.

These tests protect evidence fidelity rather than benchmark speed: semantic
policy timing must actually run the std_bench metadata sentinels, and those
sentinel rows must not be counted as workload timing.
"""

from __future__ import annotations

import argparse
import contextlib
import importlib.util
import io
import json
import os
import pathlib
import re
import signal
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest import mock


ROOT = pathlib.Path(__file__).resolve().parents[2]
DRIVER_PATH = ROOT / "evaluation" / "scripts" / "paper_workload_driver.py"


spec = importlib.util.spec_from_file_location("paper_workload_driver", DRIVER_PATH)
assert spec is not None and spec.loader is not None
driver = importlib.util.module_from_spec(spec)
spec.loader.exec_module(driver)


def args(**overrides: object) -> argparse.Namespace:
    values = {
        "dataset": "pac_authentication",
        "benchmark": "Collections",
        "allocator": "unialloc",
        "variant_feature": None,
        "bench_filter": "vec::bench_with_capacity_1000",
        "extra_feature": None,
        "timeout": 180,
        "build_timeout": None,
        "cargo": "cargo",
        "rust_toolchain": None,
        "dry_run": True,
        "allow_host_allocator_mismatch": False,
        "tcmalloc_lib_dir": None,
        "cmake_bin": None,
        "scudo_mode": "auto",
        "scudo_runtime_library": None,
        "compiler_site_replay_type_mapping": None,
        "compiler_site_replay_limit": 4096,
        "compiler_site_id_mode": "cyclic-replay",
        "compiler_site_recovery_scope": "thread-local",
    }
    values.update(overrides)
    return argparse.Namespace(**values)


class PaperWorkloadDriverSemanticHarnessTests(unittest.TestCase):
    def test_cli_benchmark_timeout_emits_elapsed_logs_and_cleanup(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            tmp = pathlib.Path(td)
            fake_cargo = tmp / "fake-cargo.py"
            fake_cargo.write_text(
                "\n".join(
                    [
                        "#!/usr/bin/env python3",
                        "import sys, time",
                        "args = sys.argv[1:]",
                        "if '--list' in args:",
                        "    print('vec::bench_with_capacity_1000: benchmark')",
                        "else:",
                        "    print('PATHOLOGY_MARKER benchmark-timeout', flush=True)",
                        "    while True: time.sleep(1)",
                    ]
                )
                + "\n",
                encoding="utf-8",
            )
            fake_cargo.chmod(0o755)
            stdout = io.StringIO()
            stderr = io.StringIO()
            argv = [
                "--dataset",
                "default_performance",
                "--benchmark",
                "Collections",
                "--allocator",
                "unialloc",
                "--bench-filter",
                "vec::bench_with_capacity_1000",
                "--timeout",
                "1",
                "--rust-toolchain",
                "system",
                "--cargo",
                str(fake_cargo),
            ]
            with mock.patch.object(
                driver,
                "bench_list_cache_paths",
                return_value=[str(tmp / "bench-list.json")],
            ), contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
                exit_code = driver.main(argv)

            self.assertEqual(exit_code, 124, stderr.getvalue())
            record = json.loads(stderr.getvalue())
            self.assertEqual(record["phase"], "benchmark", record)
            self.assertGreaterEqual(record["elapsed_seconds"], 1.0, record)
            self.assertIn("PATHOLOGY_MARKER benchmark-timeout", record["stdout_tail"])
            self.assertTrue(record["process_group_terminated"], record)
            self.assertTrue(record["process_group_absent_after_cleanup"], record)

    def test_bench_list_timeout_terminates_descendant_process_group(self) -> None:
        if os.name != "posix":
            self.skipTest("process-group descendant cleanup is POSIX-specific")
        with tempfile.TemporaryDirectory() as td:
            tmp = pathlib.Path(td)
            marker = tmp / "list-child-terminated"
            fake_cargo = tmp / "fake-cargo.py"
            fake_cargo.write_text(
                "\n".join(
                    [
                        "#!/usr/bin/env python3",
                        "import os, pathlib, subprocess, sys, time",
                        "marker = pathlib.Path(os.environ['UNIALLOC_LIST_TIMEOUT_MARKER'])",
                        "child = '''import pathlib, signal, sys, time",
                        "marker = pathlib.Path(sys.argv[1])",
                        "def stop(_signum, _frame):",
                        "    marker.write_text('terminated', encoding='utf-8')",
                        "    raise SystemExit(0)",
                        "signal.signal(signal.SIGTERM, stop)",
                        "while True: time.sleep(1)'''",
                        "subprocess.Popen([sys.executable, '-c', child, str(marker)])",
                        "while True: time.sleep(1)",
                    ]
                )
                + "\n",
                encoding="utf-8",
            )
            fake_cargo.chmod(0o755)
            env = os.environ.copy()
            env["UNIALLOC_LIST_TIMEOUT_MARKER"] = str(marker)
            with mock.patch.object(
                driver,
                "bench_list_cache_paths",
                return_value=[str(tmp / "bench-list.json")],
            ):
                with self.assertRaises(driver.BenchListCommandError) as caught:
                    driver.collect_bench_list(
                        str(fake_cargo),
                        ["bench_ourself"],
                        1,
                        env,
                        "",
                    )

            record = caught.exception.record
            self.assertEqual(caught.exception.code, 124, record)
            self.assertTrue(record["timed_out"], record)
            self.assertTrue(record["process_group_terminated"], record)
            self.assertTrue(record["process_group_absent_after_cleanup"], record)
            self.assertTrue(marker.exists(), record)
            self.assertEqual(marker.read_text(encoding="utf-8"), "terminated")

    def test_legacy_timeout_does_not_trigger_explicit_prebuild_without_flag(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            tmp = pathlib.Path(td)
            fake_cargo = tmp / "fake-cargo.py"
            fake_cargo.write_text(
                "\n".join(
                    [
                        "#!/usr/bin/env python3",
                        "import sys",
                        "args = sys.argv[1:]",
                        "if '--list' in args:",
                        "    print('vec::bench_with_capacity_1000: benchmark')",
                        "elif '--no-run' in args:",
                        "    raise SystemExit('unexpected explicit prebuild')",
                        "else:",
                        "    print('test vec::bench_with_capacity_1000 ... bench: 10 ns/iter (+/- 1)')",
                    ]
                )
                + "\n",
                encoding="utf-8",
            )
            fake_cargo.chmod(0o755)
            stdout = io.StringIO()
            stderr = io.StringIO()
            argv = [
                "--dataset",
                "default_performance",
                "--benchmark",
                "Collections",
                "--allocator",
                "unialloc",
                "--bench-filter",
                "vec::bench_with_capacity_1000",
                "--timeout",
                "1",
                "--rust-toolchain",
                "system",
                "--cargo",
                str(fake_cargo),
            ]
            with mock.patch.object(
                driver,
                "bench_list_cache_paths",
                return_value=[str(tmp / "bench-list.json")],
            ), contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
                exit_code = driver.main(argv)

            self.assertEqual(exit_code, 0, stderr.getvalue())
            record = json.loads(stdout.getvalue())
            self.assertTrue(record["ok"], record)
            self.assertNotIn("build", record)
            self.assertNotIn("build_command", record)
            self.assertNotIn("benchmark_timeout_seconds", record)
            self.assertNotIn("total_wall_seconds", record)

    def test_filtered_smoke_build_time_does_not_consume_per_benchmark_timeout(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            tmp = pathlib.Path(td)
            marker = tmp / "build-complete"
            fake_cargo = tmp / "fake-cargo.py"
            fake_cargo.write_text(
                "\n".join(
                    [
                        "#!/usr/bin/env python3",
                        "import os, pathlib, sys, time",
                        "args = sys.argv[1:]",
                        "marker = pathlib.Path(os.environ['UNIALLOC_FAKE_BUILD_MARKER'])",
                        "if '--list' in args:",
                        "    print('vec::bench_with_capacity_1000: benchmark')",
                        "    print('vec::bench_new: benchmark')",
                        "elif '--no-run' in args:",
                        "    time.sleep(1.25)",
                        "    marker.write_text('built', encoding='utf-8')",
                        "else:",
                        "    if not marker.exists():",
                        "        raise SystemExit('benchmark ran before build completed')",
                        "    time.sleep(0.05)",
                        "    print('test vec::bench_with_capacity_1000 ... bench: 10 ns/iter (+/- 1)')",
                    ]
                )
                + "\n",
                encoding="utf-8",
            )
            fake_cargo.chmod(0o755)
            stdout = io.StringIO()
            stderr = io.StringIO()
            argv = [
                "--dataset",
                "default_performance",
                "--benchmark",
                "Collections",
                "--allocator",
                "unialloc",
                "--bench-filter",
                "vec::bench_with_capacity_1000",
                "--timeout",
                "1",
                "--build-timeout",
                "3",
                "--rust-toolchain",
                "system",
                "--cargo",
                str(fake_cargo),
            ]
            started = time.monotonic()
            with mock.patch.object(
                driver,
                "bench_list_cache_paths",
                return_value=[str(tmp / "bench-list.json")],
            ), mock.patch.dict(
                driver.os.environ,
                {"UNIALLOC_FAKE_BUILD_MARKER": str(marker)},
                clear=False,
            ), contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
                exit_code = driver.main(argv)
            elapsed = time.monotonic() - started

            self.assertEqual(exit_code, 0, stderr.getvalue())
            self.assertGreater(elapsed, 1.0)
            record = json.loads(stdout.getvalue())
            self.assertTrue(record["ok"], record)
            self.assertFalse(record["claim_grade"], record)
            self.assertEqual(record["benchmarks"], ["vec::bench_with_capacity_1000"])
            self.assertEqual(record["build"]["timeout_seconds"], 3)
            self.assertEqual(record["build"]["status"], "passed")
            self.assertEqual(record["benchmark_timeout_seconds"], 1)

    def test_benchmark_interrupt_preserves_partial_result_and_cleans_process_group(self) -> None:
        if os.name != "posix":
            self.skipTest("process-group interrupt cleanup is POSIX-specific")
        previous_sigint = signal.signal(signal.SIGINT, signal.default_int_handler)
        self.addCleanup(signal.signal, signal.SIGINT, previous_sigint)
        with tempfile.TemporaryDirectory() as td:
            tmp = pathlib.Path(td)
            ready = tmp / "benchmark.ready"
            child_pid_file = tmp / "nested-child.pid"
            child_stopped = tmp / "nested-child.stopped"
            benchmark = tmp / "interruptible-benchmark.py"
            benchmark.write_text(
                "\n".join(
                    [
                        "import os, pathlib, signal, subprocess, sys, time",
                        "ready = pathlib.Path(sys.argv[1])",
                        "child_pid_file = pathlib.Path(sys.argv[2])",
                        "child_stopped = pathlib.Path(sys.argv[3])",
                        "child = '''import os, pathlib, signal, sys, time",
                        "pid_file = pathlib.Path(sys.argv[1])",
                        "stopped = pathlib.Path(sys.argv[2])",
                        "def stop(_signum, _frame):",
                        "    stopped.write_text('terminated', encoding='utf-8')",
                        "    raise SystemExit(0)",
                        "signal.signal(signal.SIGINT, stop)",
                        "signal.signal(signal.SIGTERM, stop)",
                        "pid_file.write_text(str(os.getpid()), encoding='utf-8')",
                        "while True: time.sleep(1)'''",
                        "subprocess.Popen([sys.executable, '-c', child, str(child_pid_file), str(child_stopped)])",
                        "while not child_pid_file.exists(): time.sleep(0.01)",
                        "print('noise-' + ('x' * 8192), flush=True)",
                        "print('PATHOLOGY_MARKER direct-driver-partial-output', flush=True)",
                        "ready.write_text('ready', encoding='utf-8')",
                        "while True: time.sleep(1)",
                    ]
                )
                + "\n",
                encoding="utf-8",
            )

            stop_sender = threading.Event()

            def interrupt_when_ready() -> None:
                deadline = time.monotonic() + 10
                while not stop_sender.is_set() and time.monotonic() < deadline:
                    if ready.exists():
                        os.kill(os.getpid(), signal.SIGINT)
                        return
                    time.sleep(0.01)

            sender = threading.Thread(target=interrupt_when_ready, daemon=True)
            sender.start()
            child_pid = None
            process_group_id = None
            result = {}
            try:
                with self.assertRaises(KeyboardInterrupt) as interrupted:
                    driver.run_benchmark_command(
                        [
                            sys.executable,
                            str(benchmark),
                            str(ready),
                            str(child_pid_file),
                            str(child_stopped),
                        ],
                        cwd=tmp,
                        env=os.environ.copy(),
                        timeout_seconds=60,
                    )
                self.assertIs(type(interrupted.exception), KeyboardInterrupt)
                result = getattr(
                    interrupted.exception,
                    "_unialloc_bounded_child_result",
                    None,
                )
                self.assertIsInstance(result, dict)
                assert isinstance(result, dict)
                process_group_id = result.get("process_group_pid")
                self.assertTrue(result.get("interrupted"), result)
                self.assertEqual(result.get("exit_code"), 130, result)
                self.assertTrue(result.get("process_group_terminated"), result)
                self.assertIn("PATHOLOGY_MARKER", str(result.get("stdout") or ""))
                self.assertTrue(child_pid_file.exists(), result)
                child_pid = int(child_pid_file.read_text(encoding="utf-8"))
                deadline = time.monotonic() + 3
                while time.monotonic() < deadline and not child_stopped.exists():
                    time.sleep(0.05)
                self.assertTrue(child_stopped.exists(), result)
                with self.assertRaises(ProcessLookupError):
                    os.kill(child_pid, 0)
                if process_group_id is not None:
                    with self.assertRaises(ProcessLookupError):
                        os.killpg(int(process_group_id), 0)
            finally:
                stop_sender.set()
                sender.join(timeout=1)
                if child_pid is None and child_pid_file.exists():
                    child_pid = int(child_pid_file.read_text(encoding="utf-8"))
                if process_group_id is None and isinstance(result, dict):
                    process_group_id = result.get("process_group_pid")
                if process_group_id is not None:
                    try:
                        os.killpg(int(process_group_id), signal.SIGKILL)
                    except ProcessLookupError:
                        pass
                if child_pid is not None:
                    try:
                        os.kill(child_pid, signal.SIGKILL)
                    except ProcessLookupError:
                        pass

    def test_benchmark_timeout_terminates_descendant_process_group(self) -> None:
        if os.name != "posix":
            self.skipTest("process-group descendant cleanup is POSIX-specific")
        with tempfile.TemporaryDirectory() as td:
            tmp = pathlib.Path(td)
            marker = tmp / "child-terminated"
            parent = tmp / "parent.py"
            parent.write_text(
                "\n".join(
                    [
                        "import pathlib, subprocess, sys, time",
                        "marker = pathlib.Path(sys.argv[1])",
                        "ready = marker.with_suffix('.ready')",
                        "child = '''import pathlib, signal, sys, time",
                        "marker = pathlib.Path(sys.argv[1])",
                        "ready = pathlib.Path(sys.argv[2])",
                        "def stop(_signum, _frame):",
                        "    marker.write_text('terminated', encoding='utf-8')",
                        "    raise SystemExit(0)",
                        "signal.signal(signal.SIGTERM, stop)",
                        "ready.write_text('ready', encoding='utf-8')",
                        "while True: time.sleep(1)'''",
                        "proc = subprocess.Popen([sys.executable, '-c', child, str(marker), str(ready)])",
                        "while not ready.exists(): time.sleep(0.01)",
                        "print(proc.pid, flush=True)",
                        "while True: time.sleep(1)",
                    ]
                )
                + "\n",
                encoding="utf-8",
            )
            result = driver.run_benchmark_command(
                [sys.executable, str(parent), str(marker)],
                cwd=tmp,
                env=os.environ.copy(),
                timeout_seconds=1,
            )

            self.assertTrue(result["timed_out"], result)
            self.assertEqual(result["exit_code"], 124)
            self.assertTrue(result["process_group_terminated"])
            self.assertTrue(marker.exists(), result)
            self.assertEqual(marker.read_text(encoding="utf-8"), "terminated")

    def test_semantic_variants_do_not_force_stats_into_timing(self) -> None:
        features = driver.resolve_features(args())
        self.assertEqual(features, ["bench_ourself", "pac"])

    def test_semantic_validation_can_request_stats_feature_explicitly(self) -> None:
        features = driver.resolve_features(args(extra_feature=["stats"]))
        self.assertEqual(features, ["bench_ourself", "pac", "stats"])

    def test_stats_validation_does_not_disable_semantic_counters(self) -> None:
        env = driver.cargo_subprocess_env(args(extra_feature=["stats"]))
        self.assertNotIn("UNIALLOC_STD_BENCH_DISABLE_TYPE_STATS", env)
        self.assertNotIn("UNIALLOC_STD_BENCH_DISABLE_AGGREGATE_STATS", env)

    def test_timing_without_stats_disables_semantic_counters(self) -> None:
        env = driver.cargo_subprocess_env(args())
        self.assertEqual(env["UNIALLOC_STD_BENCH_DISABLE_TYPE_STATS"], "1")
        self.assertEqual(env["UNIALLOC_STD_BENCH_DISABLE_AGGREGATE_STATS"], "1")

    def test_compiler_site_replay_mapping_sets_bench_env(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            mapping = pathlib.Path(tmp) / "type-mapping.json"
            mapping.write_text(
                json.dumps(
                    {
                        "type_mappings": [
                            {"type_id": "0x2a"},
                            {"compiler_type_id": 99},
                            {"type_id": 42},
                            {"type_id": "0"},
                        ]
                    }
                ),
                encoding="utf-8",
            )
            ns = args(
                compiler_site_replay_type_mapping=str(mapping),
                compiler_site_id_mode="consuming-stream",
                compiler_site_replay_limit=10,
            )
            with mock.patch.dict(driver.os.environ, {}, clear=True):
                env = driver.cargo_subprocess_env(ns)

        self.assertEqual(env["UNIALLOC_COMPILER_SITE_TYPE_IDS"], "42,99")
        self.assertEqual(env["UNIALLOC_COMPILER_SITE_TYPE_ID_MODE"], "consuming-stream")
        self.assertEqual(env["UNIALLOC_COMPILER_SITE_RECOVERY_SCOPE"], "thread-local")
        config = driver.compiler_site_replay_config(ns)
        self.assertTrue(config["enabled"], config)
        self.assertEqual(config["type_id_count"], 2)
        self.assertEqual(config["type_id_sample"], [42, 99])
        self.assertEqual(config["recovery_scope"], "thread-local")

    def test_env_delta_summarizes_compiler_site_ids_without_full_stream(self) -> None:
        long_stream = ",".join(str(value) for value in range(1, 20))
        env = {
            "UNIALLOC_COMPILER_SITE_TYPE_IDS": long_stream,
            "UNIALLOC_COMPILER_SITE_TYPE_ID_MODE": "cyclic-replay",
            "UNIALLOC_COMPILER_SITE_RECOVERY_SCOPE": "thread-local",
        }
        delta = driver.env_delta_for_record(env)
        self.assertNotIn("UNIALLOC_COMPILER_SITE_TYPE_IDS", delta)
        self.assertEqual(delta["UNIALLOC_COMPILER_SITE_TYPE_IDS_COUNT"], "19")
        self.assertEqual(delta["UNIALLOC_COMPILER_SITE_TYPE_IDS_SAMPLE"], "1,2,3,4,5,6,7,8")
        self.assertRegex(delta["UNIALLOC_COMPILER_SITE_TYPE_IDS_SHA256"], r"^[0-9a-f]{64}$")
        self.assertEqual(delta["UNIALLOC_COMPILER_SITE_TYPE_ID_MODE"], "cyclic-replay")
        self.assertEqual(delta["UNIALLOC_COMPILER_SITE_RECOVERY_SCOPE"], "thread-local")

    def test_default_dataset_does_not_force_stats(self) -> None:
        features = driver.resolve_features(
            args(dataset="default_performance", bench_filter="vec::bench_with_capacity_1000")
        )
        self.assertEqual(features, ["bench_ourself"])

    def test_semantic_filtered_dry_run_validates_and_derives_sentinel_skips(self) -> None:
        ns = args(dry_run=True)
        features = driver.resolve_features(ns)
        with mock.patch.object(
            driver,
            "collect_bench_list",
            return_value=[
                "aaa_semantic_auto_metadata_enable",
                "vec::bench_with_capacity_1000",
                "vec::bench_new",
                "zzz_semantic_auto_metadata_report",
            ],
        ):
            selection = driver.semantic_harness_selection(ns, features)
        self.assertTrue(selection["required"])
        self.assertFalse(selection["deferred_for_dry_run"])
        self.assertEqual(selection["strategy"], "sentinel-plus-target-skip-selection")
        self.assertEqual(selection["selected_target_benches"], ["vec::bench_with_capacity_1000"])
        self.assertEqual(selection["bench_list_count"], 4)
        self.assertEqual(selection["derived_skips"], ["vec::bench_new"])

    def test_plain_filtered_dry_run_validates_target_exists(self) -> None:
        ns = args(
            dataset="default_performance",
            allocator="ptmalloc",
            bench_filter="vec::bench_with_capacity_1000",
            dry_run=True,
        )
        features = ["bench_ptmalloc"]
        with mock.patch.object(
            driver,
            "collect_bench_list",
            return_value=["vec::bench_with_capacity_1000", "vec::bench_new"],
        ):
            selection = driver.semantic_harness_selection(ns, features)
        self.assertFalse(selection["required"])
        self.assertEqual(selection["strategy"], "validated-libtest-filter")
        self.assertEqual(selection["selected_target_benches"], ["vec::bench_with_capacity_1000"])
        self.assertEqual(selection["bench_list_count"], 2)

    def test_plain_filter_rejects_zero_match(self) -> None:
        ns = args(
            dataset="default_performance",
            allocator="ptmalloc",
            bench_filter="semantic_auto_metadata",
            dry_run=True,
        )
        with mock.patch.object(driver, "collect_bench_list", return_value=["vec::bench_new"]):
            with self.assertRaisesRegex(ValueError, "matched no non-sentinel"):
                driver.semantic_harness_selection(ns, ["bench_ptmalloc"])

    def test_build_command_uses_skip_selection_instead_of_hiding_sentinels(self) -> None:
        ns = args(dry_run=False)
        features = ["bench_ourself", "pac"]
        selection = {
            "derived_skips": ["binary_heap::bench_push", "vec::bench_new"],
            "deferred_for_dry_run": False,
        }
        command = driver.build_cargo_command(ns, features, selection)
        rendered = " ".join(command)
        self.assertIn("--test-threads=1", command)
        self.assertIn("--nocapture", command)
        self.assertIn("--skip", command)
        self.assertIn("binary_heap::bench_push", command)
        self.assertNotIn(" -- vec::bench_with_capacity_1000", rendered)

    def test_rust_toolchain_latest_alias_routes_cargo_command(self) -> None:
        ns = args(
            dataset="default_performance",
            allocator="unialloc",
            variant_feature=None,
            rust_toolchain="latest",
            bench_filter=None,
        )
        command = driver.build_cargo_command(ns, ["bench_ourself"], {})
        provenance = driver.rust_toolchain_provenance(ns)
        self.assertIn("+nightly", command)
        self.assertEqual(provenance["effective_toolchain"], "nightly")
        self.assertEqual(provenance["repo_toolchain"], driver.read_repo_rust_toolchain())
        self.assertTrue(provenance["source_accepted_newer_toolchain"])
        self.assertFalse(provenance["paper_exact_toolchain"])
        self.assertTrue(driver.toolchain_claim_grade_blockers(provenance))

    def test_repo_default_newer_pin_is_not_paper_exact(self) -> None:
        provenance = driver.rust_toolchain_provenance(args(rust_toolchain=None))
        self.assertTrue(provenance["uses_repo_rust_toolchain"])
        self.assertFalse(provenance["paper_exact_toolchain"])
        self.assertEqual(
            provenance["paper_exact_rust_toolchain"],
            "nightly-2022-07-01",
        )
        self.assertTrue(driver.toolchain_claim_grade_blockers(provenance))

    def test_explicit_paper_toolchain_is_paper_exact(self) -> None:
        provenance = driver.rust_toolchain_provenance(
            args(rust_toolchain="nightly-2022-07-01")
        )
        self.assertTrue(provenance["paper_exact_toolchain"])
        self.assertEqual(driver.toolchain_claim_grade_blockers(provenance), [])

    def test_paper_toolchain_workspace_graph_is_cargo_164_compatible(self) -> None:
        manifest = (ROOT / "unialloc" / "Cargo.toml").read_text(encoding="utf-8")
        lock = (ROOT / "Cargo.lock").read_text(encoding="utf-8")
        unialloc_lock_match = re.search(
            r'\[\[package\]\]\s+name = "unialloc".*?(?=\n\[\[package\]\]|\Z)',
            lock,
            flags=re.DOTALL,
        )

        self.assertRegex(manifest, r'(?m)^rust-version = "1\.64"$')
        self.assertNotRegex(manifest, r'(?m)^criterion\s*=')
        self.assertRegex(lock, r'(?m)^version = 3$')
        self.assertIsNotNone(unialloc_lock_match)
        self.assertNotIn('"criterion"', unialloc_lock_match.group(0))
        self.assertNotRegex(lock, r'(?m)^name = "criterion"$')

    def test_paper_toolchain_source_uses_conditional_old_nightly_feature_gates(self) -> None:
        build_rs = (ROOT / "unialloc" / "build.rs").read_text(encoding="utf-8")
        lib_rs = (ROOT / "unialloc" / "src" / "lib.rs").read_text(encoding="utf-8")
        bench_rs = (ROOT / "unialloc" / "benches" / "lib.rs").read_text(encoding="utf-8")
        direct_probe_rs = (
            ROOT / "unialloc" / "src" / "bin" / "rustc_driver_direct_allocator_mir_probe.rs"
        ).read_text(encoding="utf-8")

        self.assertIn('"unialloc_has_stable_raw_ref_op", 82', build_rs)
        self.assertIn('"unialloc_has_stable_map_first_last", 66', build_rs)
        self.assertIn("feature(raw_ref_op)", lib_rs)
        self.assertIn("feature(map_first_last)", bench_rs)
        self.assertIn("feature(alloc_layout_extra)", direct_probe_rs)

    def test_paper_toolchain_workspace_probe_uses_resolved_features(self) -> None:
        ns = args(rust_toolchain="nightly-2022-07-01")
        completed = driver.subprocess.CompletedProcess(
            args=[],
            returncode=0,
            stdout="",
            stderr="",
        )
        with mock.patch.object(driver.subprocess, "run", return_value=completed) as run:
            probe = driver.paper_toolchain_workspace_probe(
                ns,
                ["bench_jemalloc", "hugepage"],
            )

        self.assertTrue(probe["ok"])
        self.assertEqual(
            run.call_args.args[0],
            [
                "cargo",
                "+nightly-2022-07-01",
                "tree",
                "-p",
                "unialloc",
                "--features",
                "bench_jemalloc,hugepage",
                "--locked",
                "--offline",
            ],
        )

    def test_paper_exact_dry_run_rejects_unreadable_workspace_graph(self) -> None:
        ns = args(
            dataset="default_performance",
            allocator="unialloc",
            variant_feature=None,
            rust_toolchain="nightly-2022-07-01",
            bench_filter=None,
            dry_run=True,
        )
        failed_probe = {
            "ok": False,
            "exit_code": 101,
            "stderr_tail": "lock file version `4` was found",
        }
        with mock.patch.object(
            driver,
            "paper_toolchain_workspace_probe",
            return_value=failed_probe,
            create=True,
        ) as probe:
            with mock.patch.object(driver, "semantic_harness_selection") as selection:
                self.assertEqual(driver.run(ns), 2)
                probe.assert_called_once_with(ns, ["bench_ourself"])
                selection.assert_not_called()

    def test_rust_toolchain_system_omits_rustup_override(self) -> None:
        ns = args(
            dataset="default_performance",
            allocator="unialloc",
            variant_feature=None,
            rust_toolchain="system",
            bench_filter=None,
        )
        command = driver.build_cargo_command(ns, ["bench_ourself"], {})
        self.assertNotIn("+nightly-2022-07-01", command)
        self.assertNotIn("+nightly", command)
        provenance = driver.rust_toolchain_provenance(ns)
        self.assertTrue(provenance["uses_system_toolchain"])
        self.assertFalse(provenance["paper_exact_toolchain"])

    def test_bench_list_fingerprint_includes_effective_toolchain(self) -> None:
        repo_toolchain = driver.read_repo_rust_toolchain()
        repo_fingerprint = driver.bench_list_fingerprint(["bench_ourself"], repo_toolchain)
        alternate_toolchain = "stable" if repo_toolchain == "nightly" else "nightly"
        alternate_fingerprint = driver.bench_list_fingerprint(
            ["bench_ourself"], alternate_toolchain
        )
        self.assertNotEqual(repo_fingerprint["source_digest"], alternate_fingerprint["source_digest"])
        self.assertEqual(alternate_fingerprint["rust_toolchain"], alternate_toolchain)

    def test_timing_rows_exclude_sentinels_and_apply_filter(self) -> None:
        rows = [
            {"benchmark": "aaa_semantic_auto_metadata_enable", "ns_per_iter": 1},
            {"benchmark": "vec::bench_with_capacity_1000", "ns_per_iter": 10},
            {"benchmark": "vec::bench_new", "ns_per_iter": 2},
            {"benchmark": "zzz_semantic_auto_metadata_report", "ns_per_iter": 1},
        ]
        selected = driver.select_timing_rows(rows, args())
        self.assertEqual(
            [row["benchmark"] for row in selected["timing_rows"]],
            ["vec::bench_with_capacity_1000"],
        )
        self.assertEqual(
            [row["benchmark"] for row in selected["excluded_harness_rows"]],
            [
                "aaa_semantic_auto_metadata_enable",
                "zzz_semantic_auto_metadata_report",
            ],
        )


    def test_semantic_policy_claim_grade_rejects_layout_derived_basis(self) -> None:
        status = driver.semantic_policy_claim_grade_status(
            {"required": True},
            [
                {
                    "source": "std_bench_auto_metadata",
                    "event": "semantic_harness_state",
                    "type_id_basis": "layout-derived-size-align",
                }
            ],
        )
        self.assertFalse(status["ready"])
        self.assertIn("layout-derived", " ".join(status["blockers"]))

    def test_semantic_policy_claim_grade_accepts_compiler_assigned_basis(self) -> None:
        status = driver.semantic_policy_claim_grade_status(
            {"required": True},
            [
                {
                    "source": "std_bench_auto_metadata",
                    "event": "semantic_harness_state",
                    "type_id_basis": "compiler-assigned-allocation-site-object-type-id-consuming-stream",
                    "compiler_site_id_stream_mode": "consuming-stream",
                }
            ],
        )
        self.assertTrue(status["ready"])
        self.assertEqual(status["blockers"], [])

    def test_parse_semantic_events_keeps_probe_json(self) -> None:
        stdout = "\n".join(
            [
                "test vec::bench_with_capacity_1000 ... bench: 16 ns/iter (+/- 1)",
                'test zzz_semantic_auto_metadata_report ... {"source":"std_bench_auto_metadata","event":"semantic_harness_state","stats_feature":false}',
                '{"source":"std_bench_auto_metadata","event":"pac_metadata_auth_probe","attempted":true,"reused":true}',
                '{"source":"other","event":"ignored"}',
                "{not json",
            ]
        )
        events = driver.parse_semantic_events(stdout)
        self.assertEqual(len(events), 2)
        self.assertEqual(events[0]["event"], "semantic_harness_state")
        self.assertFalse(events[0]["stats_feature"])
        self.assertEqual(events[1]["event"], "pac_metadata_auth_probe")
        self.assertTrue(events[1]["reused"])

    def test_parse_bench_rows_accepts_newer_decimal_ns(self) -> None:
        rows = driver.parse_bench_rows(
            "test vec::bench_with_capacity_1000 ... bench:           9.37 ns/iter (+/- 0.39) = 111111 MB/s"
        )
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["benchmark"], "vec::bench_with_capacity_1000")
        self.assertAlmostEqual(rows[0]["ns_per_iter"], 9.37)
        self.assertAlmostEqual(rows[0]["deviation_ns"], 0.39)

    def test_scudo_auto_falls_back_to_ld_preload_runtime(self) -> None:
        runtime = "/usr/lib/llvm-16/lib/clang/16/lib/linux/libclang_rt.scudo_standalone-aarch64.so"
        ns = args(
            dataset="default_performance",
            allocator="scudo",
            scudo_mode="auto",
            scudo_runtime_library=runtime,
        )
        with mock.patch.object(driver, "scudo_toolchain_probe", return_value={"ok": False}), mock.patch.object(
            driver,
            "scudo_runtime_probe",
            return_value={"ok": True, "configured_runtime_library": runtime},
        ), mock.patch.dict(driver.os.environ, {}, clear=True):
            execution = driver.scudo_execution_probe(ns)
            env = driver.cargo_subprocess_env(ns)
        self.assertTrue(execution["ok"], execution)
        self.assertEqual(execution["selected_mode"], "ld-preload")
        self.assertEqual(env["UNIALLOC_SCUDO_RUNTIME_LIBRARY"], runtime)
        self.assertEqual(env["LD_PRELOAD"].split()[0], runtime)
        self.assertNotIn(driver.SCUDO_SANITIZER_RUSTFLAGS, env.get("RUSTFLAGS", ""))

    def test_scudo_validate_accepts_ld_preload_when_toolchain_rejects(self) -> None:
        runtime = "/usr/lib/llvm-16/lib/clang/16/lib/linux/libclang_rt.scudo_standalone-aarch64.so"
        ns = args(
            dataset="default_performance",
            allocator="scudo",
            scudo_mode="auto",
            scudo_runtime_library=runtime,
        )
        with mock.patch.object(driver, "scudo_toolchain_probe", return_value={"ok": False}), mock.patch.object(
            driver,
            "scudo_runtime_probe",
            return_value={"ok": True, "configured_runtime_library": runtime},
        ):
            self.assertIsNone(driver.validate_args(ns))

    def test_scudo_validate_fails_closed_without_toolchain_or_runtime(self) -> None:
        ns = args(dataset="default_performance", allocator="scudo", scudo_mode="auto")
        with mock.patch.object(driver, "scudo_toolchain_probe", return_value={"ok": False}), mock.patch.object(
            driver,
            "scudo_runtime_probe",
            return_value={"ok": False, "configured_runtime_library": None},
        ):
            self.assertEqual(driver.validate_args(ns), 2)


if __name__ == "__main__":
    unittest.main()
