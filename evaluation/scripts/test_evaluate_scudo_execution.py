#!/usr/bin/env python3
"""Focused fail-closed tests for Scudo execution in evaluate.py."""

from __future__ import annotations

import importlib.util
import json
import os
import pathlib
import tempfile
import types
import unittest
from unittest import mock


ROOT = pathlib.Path(__file__).resolve().parents[2]
EVALUATE_PATH = ROOT / "evaluation" / "scripts" / "evaluate.py"

spec = importlib.util.spec_from_file_location("unialloc_evaluate_scudo_execution", EVALUATE_PATH)
assert spec is not None and spec.loader is not None
evaluate = importlib.util.module_from_spec(spec)
spec.loader.exec_module(evaluate)


RUNTIME = "/usr/lib/llvm-test/lib/clang/test/lib/linux/libclang_rt.scudo_standalone-test.so"


def runtime_identity() -> dict:
    return {
        "path": RUNTIME,
        "realpath": RUNTIME,
        "size_bytes": 1234,
        "sha256": "a" * 64,
        "platform": "Linux",
        "architecture": "x86_64",
    }


def runtime_authenticity() -> dict:
    return {
        "ok": True,
        "runtime_authenticity_verified": True,
        "verification_scheme": "dpkg-compiler-rt-content-and-scudo-stats-v1",
        "runtime_library_identity": runtime_identity(),
    }


def planned_ld_preload_probe() -> dict:
    return {
        "ok": True,
        "requested_mode": "auto",
        "selected_mode": "ld-preload",
        "execution_state": "planned",
        "runtime_verified": False,
        "runtime_authenticity_verified": True,
        "paper_allocator_equivalent": False,
        "runtime_library": RUNTIME,
        "toolchain_probe": {"ok": False, "stderr_tail": "unsupported sanitizer scudo"},
        "runtime_probe": {
            "ok": True,
            "runtime_authenticity_verified": True,
            "configured_runtime_library": RUNTIME,
            "runtime_library_identity": runtime_identity(),
            "runtime_authenticity": runtime_authenticity(),
        },
        "blockers": [],
    }


def verified_ld_preload_probe() -> dict:
    probe = planned_ld_preload_probe()
    probe.update(
        {
            "execution_state": "verified",
            "runtime_verified": True,
            "paper_allocator_equivalent": True,
        }
    )
    return probe


def verified_semantics() -> dict:
    return {
        "requested_allocator": "scudo",
        "allocator_feature": "bench_scudo",
        "implementation_kind": "verified_external_scudo_runtime",
        "paper_allocator_equivalent": True,
        "runtime_identity_verified": True,
        "runtime_authenticity_verified": True,
        "runtime_identity_status": "verified",
        "scudo_runtime_mode": "ld-preload",
        "scudo_runtime_library": RUNTIME,
        "scudo_runtime_library_identity": runtime_identity(),
        "scudo_runtime_authenticity": runtime_authenticity(),
    }


def planned_rust_sanitizer_probe() -> dict:
    return {
        "ok": True,
        "requested_mode": "rust-sanitizer",
        "selected_mode": "rust-sanitizer",
        "execution_state": "planned",
        "runtime_verified": False,
        "runtime_authenticity_verified": True,
        "paper_allocator_equivalent": False,
        "runtime_library": None,
        "toolchain_probe": {
            "ok": True,
            "probe_cwd": "/tmp/unialloc-scudo-toolchain-probe",
            "rustc_verbose_version": "rustc test-nightly (test)",
            "rust_toolchain_provenance": {"effective_toolchain": "nightly-test"},
        },
        "runtime_probe": {"ok": False},
        "blockers": [],
    }


def current_rust_sanitizer_probe(effective_toolchain: str = "nightly-test") -> dict:
    return {
        "ok": True,
        "probe_cwd": "/tmp/unialloc-scudo-toolchain-probe",
        "rustc_verbose_version": "rustc test-nightly (test)",
        "rust_toolchain_provenance": {"effective_toolchain": effective_toolchain},
    }


class EvaluateScudoExecutionTests(unittest.TestCase):
    def setUp(self) -> None:
        evaluate._SCUDO_EXECUTION_PROBE_CACHE = None
        evaluate._SCUDO_TOOLCHAIN_PROBE_CACHE = None

    def tearDown(self) -> None:
        evaluate._SCUDO_EXECUTION_PROBE_CACHE = None
        evaluate._SCUDO_TOOLCHAIN_PROBE_CACHE = None

    def test_canonical_probe_accepts_ld_preload_when_rustc_rejects_scudo(self) -> None:
        self.assertIsNotNone(evaluate._PAPER_WORKLOAD_DRIVER)
        with mock.patch.object(
            evaluate._PAPER_WORKLOAD_DRIVER,
            "scudo_execution_probe",
            return_value=planned_ld_preload_probe(),
        ):
            probe = evaluate.scudo_execution_probe()

        self.assertTrue(evaluate.scudo_execution_available())
        self.assertEqual(probe["selected_mode"], "ld-preload")
        self.assertEqual(probe["probe_source"], "evaluation/scripts/paper_workload_driver.py")
        self.assertFalse(probe["paper_allocator_equivalent"])
        supported, reason = evaluate.local_collections_driver_support(
            {"benchmark": "Collections", "allocator": "scudo"}
        )
        self.assertTrue(supported, reason)
        feature_support = evaluate.allocator_feature_support("scudo")
        self.assertEqual(feature_support["claim_grade_mapping_blockers"], [])

    def test_ld_preload_child_env_has_runtime_and_no_invalid_rustflags(self) -> None:
        with mock.patch.object(evaluate, "scudo_execution_probe", return_value=planned_ld_preload_probe()):
            with mock.patch.dict(os.environ, {}, clear=True):
                env = evaluate.local_collections_dependency_env({"allocator": "scudo"})

        self.assertEqual(env["UNIALLOC_SCUDO_RUNTIME_LIBRARY"], RUNTIME)
        self.assertEqual(env["LD_PRELOAD"], RUNTIME)
        self.assertNotIn("RUSTFLAGS", env)

    def test_run_one_sanitizer_route_removes_inherited_preload_environment(self) -> None:
        observed_env: dict[str, str] = {}

        def fake_run(_cmd: list[str], **kwargs: object) -> types.SimpleNamespace:
            observed_env.update(kwargs["env"])
            kwargs["stdout"].write("test vec::bench_new ... bench: 10 ns/iter (+/- 1)\n")
            kwargs["stderr"].write(evaluate._PAPER_WORKLOAD_DRIVER.SCUDO_RUNTIME_IDENTITY_MARKER)
            return types.SimpleNamespace(returncode=0)

        polluted = {
            "LD_PRELOAD": "/tmp/fake.so",
            "UNIALLOC_SCUDO_RUNTIME_LIBRARY": "/tmp/fake.so",
            "SCUDO_RUNTIME_LIBRARY": "/tmp/fake.so",
            "SCUDO_STANDALONE_LIBRARY": "/tmp/fake.so",
        }
        with tempfile.TemporaryDirectory() as tmpdir, mock.patch.dict(
            evaluate.os.environ, polluted, clear=False
        ), mock.patch.object(evaluate.subprocess, "run", side_effect=fake_run):
            record = evaluate.run_one(
                ["cargo", "bench"],
                pathlib.Path(tmpdir),
                "bench_scudo-run1",
                "bench_scudo",
                1,
                10,
                None,
                env_overrides={"RUSTFLAGS": evaluate.SCUDO_SANITIZER_RUSTFLAGS},
                env_remove=list(evaluate.SCUDO_SANITIZER_REMOVED_ENV_NAMES),
                scudo_execution=planned_rust_sanitizer_probe(),
            )

        self.assertEqual(record["exit_code"], 0, record)
        for name in evaluate.SCUDO_SANITIZER_REMOVED_ENV_NAMES:
            self.assertNotIn(name, observed_env)
            self.assertTrue(record["subprocess_env_effective_absence"][name])
        self.assertEqual(
            sorted(record["subprocess_env_removed"]),
            sorted(evaluate.SCUDO_SANITIZER_REMOVED_ENV_NAMES),
        )

    def test_missing_routes_fail_closed_before_run_local_timing(self) -> None:
        unavailable = {
            "ok": False,
            "selected_mode": None,
            "paper_allocator_equivalent": False,
            "toolchain_probe": {"ok": False},
            "runtime_probe": {"ok": False},
            "blockers": ["rustc rejected Scudo", "standalone runtime missing"],
        }
        args = types.SimpleNamespace(
            run_id="scudo-unavailable",
            features=["bench_scudo"],
            runs=1,
            profile="release",
            bench_filter="vec::bench_new",
            timeout=1,
            keep_going=False,
        )
        with tempfile.TemporaryDirectory() as tmpdir:
            with (
                mock.patch.object(evaluate, "RAW", pathlib.Path(tmpdir)),
                mock.patch.object(evaluate.shutil, "which", return_value="/usr/bin/cargo"),
                mock.patch.object(evaluate, "scudo_execution_probe", return_value=unavailable),
                mock.patch.object(evaluate, "run_one") as run_one,
            ):
                status = evaluate.run_local(args)

        self.assertEqual(status, 2)
        run_one.assert_not_called()

    def test_run_one_requires_runtime_marker_before_accepting_scudo_timing(self) -> None:
        def fake_run(_cmd: list[str], **kwargs: object) -> types.SimpleNamespace:
            kwargs["stdout"].write("test vec::bench_new ... bench: 10 ns/iter (+/- 1)\n")
            kwargs["stderr"].write(evaluate._PAPER_WORKLOAD_DRIVER.SCUDO_RUNTIME_IDENTITY_MARKER)
            return types.SimpleNamespace(returncode=0)

        with tempfile.TemporaryDirectory() as tmpdir:
            with mock.patch.object(evaluate.subprocess, "run", side_effect=fake_run):
                record = evaluate.run_one(
                    ["cargo", "bench"],
                    pathlib.Path(tmpdir),
                    "bench_scudo-run1",
                    "bench_scudo",
                    1,
                    10,
                    None,
                    env_overrides={
                        "UNIALLOC_SCUDO_RUNTIME_LIBRARY": RUNTIME,
                        "LD_PRELOAD": RUNTIME,
                    },
                    scudo_execution=planned_ld_preload_probe(),
                    allocator_semantics=evaluate.scudo_allocator_semantics(planned_ld_preload_probe()),
                )

        self.assertEqual(record["exit_code"], 0)
        self.assertTrue(record["paper_allocator_equivalent"])
        self.assertTrue(record["allocator_semantics"]["runtime_identity_verified"])
        self.assertTrue(record["scudo_execution_probe"]["runtime_verified"])
        self.assertEqual(len(record["bench_results"]), 1)

    def test_run_one_rejects_successful_system_fallback_without_marker(self) -> None:
        def fake_run(_cmd: list[str], **kwargs: object) -> types.SimpleNamespace:
            kwargs["stdout"].write("test vec::bench_new ... bench: 10 ns/iter (+/- 1)\n")
            return types.SimpleNamespace(returncode=0)

        with tempfile.TemporaryDirectory() as tmpdir:
            with mock.patch.object(evaluate.subprocess, "run", side_effect=fake_run):
                record = evaluate.run_one(
                    ["cargo", "bench"],
                    pathlib.Path(tmpdir),
                    "bench_scudo-run1",
                    "bench_scudo",
                    1,
                    10,
                    None,
                    env_overrides={"LD_PRELOAD": RUNTIME},
                    scudo_execution=planned_ld_preload_probe(),
                )

        self.assertEqual(record["exit_code"], 1)
        self.assertFalse(record["paper_allocator_equivalent"])
        self.assertEqual(record["bench_results"], [])
        self.assertIn("runtime identity marker", record["error"])

    def test_run_one_drops_rows_from_failed_unverified_scudo_process(self) -> None:
        def fake_run(_cmd: list[str], **kwargs: object) -> types.SimpleNamespace:
            kwargs["stdout"].write("test vec::bench_new ... bench: 10 ns/iter (+/- 1)\n")
            return types.SimpleNamespace(returncode=86)

        with tempfile.TemporaryDirectory() as tmpdir:
            with mock.patch.object(evaluate.subprocess, "run", side_effect=fake_run):
                record = evaluate.run_one(
                    ["cargo", "bench"],
                    pathlib.Path(tmpdir),
                    "bench_scudo-run1",
                    "bench_scudo",
                    1,
                    10,
                    None,
                    env_overrides={"LD_PRELOAD": RUNTIME},
                    scudo_execution=planned_ld_preload_probe(),
                )

        self.assertEqual(record["exit_code"], 86)
        self.assertEqual(record["bench_results"], [])
        self.assertFalse(record["paper_allocator_equivalent"])

    def test_scudo_sample_import_requires_verified_semantics_and_execution(self) -> None:
        bare = {
            "source": "paper-workload-driver",
            "allocator": "scudo",
            "benchmark_owned_json": True,
            "claim_grade": True,
        }
        bare_blockers = evaluate.sample_claim_grade_blockers(bare)
        self.assertTrue(any("recognized wrapper record" in blocker for blocker in bare_blockers))

        unverified_groups = evaluate.build_sample_groups(
            [{**bare, "dataset": "default_performance", "benchmark": "Collections", "seconds": 1.0}]
        )
        self.assertNotIn(("default_performance", "Collections", "scudo"), unverified_groups)
        uppercase_groups = evaluate.build_sample_groups(
            [
                {
                    **bare,
                    "allocator": "Scudo",
                    "dataset": "default_performance",
                    "benchmark": "Collections",
                    "seconds": 1.0,
                }
            ]
        )
        self.assertNotIn(("default_performance", "Collections", "scudo"), uppercase_groups)
        with tempfile.TemporaryDirectory() as td:
            stderr_path = pathlib.Path(td) / "scudo.stderr.txt"
            stderr_path.write_text(
                evaluate._PAPER_WORKLOAD_DRIVER.SCUDO_RUNTIME_IDENTITY_MARKER,
                encoding="utf-8",
            )
            benchmark_stdout_path = pathlib.Path(td) / "scudo.stdout.txt"
            benchmark_stdout_path.write_text(
                "test vec_deque::bench_from_array_1000 ... bench: 100 ns/iter (+/- 1)\n",
                encoding="utf-8",
            )
            verified = {
                **bare,
                "dataset": "default_performance",
                "benchmark": "Collections",
                "seconds": 100.0 / 1_000_000_000.0,
                "ns_per_iter": 100.0,
                "benchmarks": ["vec_deque::bench_from_array_1000"],
                "command": ["cargo", "bench", "-p", "unialloc", "--bench", "std_bench"],
                "bench_filter": None,
                "allocator_semantics": verified_semantics(),
                "scudo_execution_probe": verified_ld_preload_probe(),
                "subprocess_env_delta": {
                    "UNIALLOC_SCUDO_RUNTIME_LIBRARY": RUNTIME,
                    "LD_PRELOAD": RUNTIME,
                },
                "stdout": str(benchmark_stdout_path),
                "stdout_sha256": evaluate.file_sha256(benchmark_stdout_path),
                "evidence": [
                    {
                        "kind": "scudo_benchmark_stdout",
                        "path": str(benchmark_stdout_path),
                        "sha256": evaluate.file_sha256(benchmark_stdout_path),
                    },
                    {
                        "kind": "scudo_benchmark_stderr",
                        "path": str(stderr_path),
                        "sha256": evaluate.file_sha256(stderr_path),
                    }
                ],
            }
            evaluate._PAPER_WORKLOAD_DRIVER.attach_scudo_timing_binding(
                verified,
                evidence_dir=pathlib.Path(td),
            )

            def rebind(candidate: dict, label: str) -> dict:
                rebound = json.loads(json.dumps(candidate))
                rebound["evidence"] = [
                    entry
                    for entry in rebound.get("evidence", [])
                    if entry.get("kind") != "scudo_timing_binding_json"
                ]
                rebound.pop("scudo_timing_binding", None)
                evaluate._PAPER_WORKLOAD_DRIVER.attach_scudo_timing_binding(
                    rebound,
                    evidence_dir=pathlib.Path(td) / label,
                )
                return rebound
            with mock.patch.object(
                evaluate._PAPER_WORKLOAD_DRIVER,
                "scudo_runtime_library_identity",
                return_value=runtime_identity(),
            ), mock.patch.object(
                evaluate._PAPER_WORKLOAD_DRIVER,
                "scudo_runtime_authenticity_probe",
                return_value=runtime_authenticity(),
            ):
                verified_blockers = evaluate.sample_claim_grade_blockers(verified)
                self.assertFalse(
                    any("scudo" in blocker.lower() for blocker in verified_blockers),
                    verified_blockers,
                )

                no_marker_evidence = {
                    **verified,
                    "evidence": [
                        entry
                        for entry in verified["evidence"]
                        if entry.get("kind")
                        not in {"scudo_benchmark_stderr", "scudo_timing_binding_json"}
                    ],
                }
                no_marker_evidence.pop("scudo_timing_binding", None)
                evaluate._PAPER_WORKLOAD_DRIVER.attach_scudo_timing_binding(
                    no_marker_evidence,
                    evidence_dir=pathlib.Path(td) / "no-marker-binding",
                )
                self.assertTrue(
                    any(
                        "digest-bound child stderr" in blocker
                        for blocker in evaluate.sample_claim_grade_blockers(no_marker_evidence)
                    )
                )

                empty_identity_probe = verified_ld_preload_probe()
                empty_identity_probe["runtime_probe"] = {
                    **empty_identity_probe["runtime_probe"],
                    "runtime_library_identity": {},
                }
                empty_identity = rebind({
                    **verified,
                    "scudo_execution_probe": empty_identity_probe,
                }, "empty-identity-binding")
                self.assertTrue(
                    any(
                        "immutable standalone-runtime identity" in blocker
                        for blocker in evaluate.sample_claim_grade_blockers(empty_identity)
                    )
                )
                suffix_only_env = rebind({
                    **verified,
                    "subprocess_env_delta": {
                        "UNIALLOC_SCUDO_RUNTIME_LIBRARY": RUNTIME,
                        "LD_PRELOAD": RUNTIME + ".not-the-attested-file",
                    },
                }, "suffix-env-binding")
                self.assertTrue(
                    any(
                        "recorded runtime path" in blocker
                        for blocker in evaluate.sample_claim_grade_blockers(suffix_only_env)
                    )
                )

                inherited_effective_env = rebind({
                    **verified,
                    "subprocess_env_delta": {},
                    "subprocess_env_effective": {
                        "UNIALLOC_SCUDO_RUNTIME_LIBRARY": RUNTIME,
                        "LD_PRELOAD": RUNTIME,
                    },
                }, "inherited-env-binding")
                inherited_blockers = evaluate.scudo_sample_identity_blockers(
                    inherited_effective_env
                )
                self.assertFalse(
                    any("child environment" in blocker for blocker in inherited_blockers),
                    inherited_blockers,
                )

                unbound_external = {
                    **verified,
                    "source": "paper-external-cargo-bench-json",
                }
                unbound_blockers = evaluate.scudo_sample_identity_blockers(unbound_external)
                self.assertTrue(
                    any(
                        "timing" in blocker.lower() and "identity" in blocker.lower()
                        for blocker in unbound_blockers
                    ),
                    unbound_blockers,
                )

                verified_groups = evaluate.build_sample_groups(
                    [verified]
                )
                self.assertIn(("default_performance", "Collections", "scudo"), verified_groups)

                transferred = {**verified, "seconds": 999.0, "benchmark": "RRedis*"}
                self.assertTrue(
                    any(
                        "relabel" in blocker or "timing" in blocker
                        for blocker in evaluate.scudo_sample_identity_blockers(transferred)
                    )
                )

                multi_stdout = pathlib.Path(td) / "scudo-multi.stdout.txt"
                multi_stdout.write_text(
                    "test bench_a ... bench: 10 ns/iter (+/- 1)\n"
                    "test bench_b ... bench: 1,000 ns/iter (+/- 1)\n",
                    encoding="utf-8",
                )
                subset_transfer = {
                    **verified,
                    "bench_filter": None,
                    "benchmarks": ["bench_a"],
                    "ns_per_iter": 10,
                    "seconds": 10 / 1_000_000_000.0,
                    "stdout": str(multi_stdout),
                    "stdout_sha256": evaluate.file_sha256(multi_stdout),
                    "evidence": [
                        {
                            "kind": "scudo_benchmark_stdout",
                            "path": str(multi_stdout),
                            "sha256": evaluate.file_sha256(multi_stdout),
                        },
                        verified["evidence"][1],
                    ],
                }
                subset_blockers = evaluate.scudo_sample_identity_blockers(subset_transfer)
                self.assertTrue(
                    any("selected benchmark set" in blocker for blocker in subset_blockers),
                    subset_blockers,
                )

                full_record = {
                    **verified,
                    "bench_filter": None,
                    "command": ["cargo", "bench", "-p", "unialloc", "--bench", "std_bench"],
                    "benchmarks": ["bench_a", "bench_b"],
                    "ns_per_iter": 100,
                    "seconds": 100 / 1_000_000_000.0,
                    "stdout": str(multi_stdout),
                    "stdout_sha256": evaluate.file_sha256(multi_stdout),
                    "evidence": [
                        {
                            "kind": "scudo_benchmark_stdout",
                            "path": str(multi_stdout),
                            "sha256": evaluate.file_sha256(multi_stdout),
                        },
                        verified["evidence"][1],
                    ],
                }
                evaluate._PAPER_WORKLOAD_DRIVER.attach_scudo_timing_binding(
                    full_record,
                    evidence_dir=pathlib.Path(td) / "full-binding",
                )
                self.assertEqual(evaluate.scudo_sample_identity_blockers(full_record), [])
                filter_transfer = {
                    **full_record,
                    "bench_filter": "bench_a",
                    "command": [
                        "cargo",
                        "bench",
                        "-p",
                        "unialloc",
                        "--bench",
                        "std_bench",
                        "--",
                        "bench_a",
                    ],
                    "benchmarks": ["bench_a"],
                    "ns_per_iter": 10,
                    "seconds": 10 / 1_000_000_000.0,
                }
                filter_transfer_blockers = evaluate.scudo_sample_identity_blockers(filter_transfer)
                self.assertTrue(
                    any(
                        "outside its recorded libtest filter" in blocker
                        or "timing" in blocker.lower()
                        for blocker in filter_transfer_blockers
                    ),
                    filter_transfer_blockers,
                )
                self.assertNotIn(
                    ("default_performance", "Collections", "scudo"),
                    evaluate.build_sample_groups([filter_transfer]),
                )

                verified_timing = dict(verified)
                plan_stdout = pathlib.Path(td) / "plan.stdout.txt"
                plan_stdout.write_text(json.dumps(verified_timing) + "\n", encoding="utf-8")
                plan = {
                    "source": "paper-performance-plan-run",
                    "dataset": "default_performance",
                    "benchmark": "Collections",
                    "allocator": "scudo",
                    "run_index": 1,
                    "seconds": verified_timing["seconds"],
                    "success": True,
                    "exit_code": 0,
                    "measurement_source": "stdout_json",
                    "stdout_truncated": False,
                    "stdout": str(plan_stdout),
                    "metric_metadata": verified_timing,
                    "evidence": [
                        {
                            "kind": "stdout",
                            "path": str(plan_stdout),
                            "sha256": evaluate.file_sha256(plan_stdout),
                        }
                    ],
                }
                self.assertEqual(evaluate.scudo_sample_identity_blockers(plan), [])

                transferred_plan = {**plan, "seconds": 999.0}
                transferred_blockers = evaluate.scudo_sample_identity_blockers(transferred_plan)
                self.assertTrue(
                    any("timing does not equal" in blocker for blocker in transferred_blockers),
                    transferred_blockers,
                )

                adapter = {
                    "source": "paper-external-workload-adapter",
                    "dataset": "default_performance",
                    "benchmark": "Collections",
                    "allocator": "scudo",
                    "run_index": 1,
                    "seconds": verified_timing["seconds"],
                    "success": True,
                    "child_record": verified_timing,
                }
                plan_stdout.write_text(
                    json.dumps(verified_timing) + "\n" + json.dumps(adapter) + "\n",
                    encoding="utf-8",
                )
                adapter_plan = {
                    **plan,
                    "metric_metadata": adapter,
                    "evidence": [
                        {
                            "kind": "stdout",
                            "path": str(plan_stdout),
                            "sha256": evaluate.file_sha256(plan_stdout),
                        }
                    ],
                }
                self.assertEqual(evaluate.scudo_sample_identity_blockers(adapter_plan), [])

                relabeled_plan = {**adapter_plan, "benchmark": "TotallyDifferentSystemWorkload"}
                relabeled_blockers = evaluate.scudo_sample_identity_blockers(relabeled_plan)
                self.assertTrue(
                    any("identity differs" in blocker for blocker in relabeled_blockers),
                    relabeled_blockers,
                )

                replay_groups = evaluate.build_sample_groups([adapter_plan] * 6)
                self.assertEqual(
                    len(replay_groups[("default_performance", "Collections", "scudo")]),
                    1,
                )
                noisy_replays = []
                for replay_index in range(6):
                    noisy_stdout = pathlib.Path(td) / f"noisy-plan-{replay_index}.stdout.txt"
                    noisy_stdout.write_text(
                        f"ignored non-JSON line {replay_index}\n"
                        + json.dumps(verified_timing)
                        + "\n"
                        + json.dumps(adapter)
                        + "\n",
                        encoding="utf-8",
                    )
                    noisy_replays.append(
                        {
                            **adapter_plan,
                            "stdout": str(noisy_stdout),
                            "evidence": [
                                {
                                    "kind": "stdout",
                                    "path": str(noisy_stdout),
                                    "sha256": evaluate.file_sha256(noisy_stdout),
                                }
                            ],
                        }
                    )
                noisy_groups = evaluate.build_sample_groups(noisy_replays)
                self.assertEqual(
                    len(noisy_groups[("default_performance", "Collections", "scudo")]),
                    1,
                )

                required_cell = {
                    "dataset": "default_performance",
                    "benchmark": "Collections",
                    "allocator": "scudo",
                    "reference": None,
                }
                source_binding = {
                    "source_binding_ready": True,
                    "source_bound_record_count": 6,
                    "source_binding_blocker_count": 0,
                    "evidence_source_binding_blockers": [],
                }
                with mock.patch.object(
                    evaluate,
                    "required_performance_sample_cells",
                    return_value=([required_cell], []),
                ), mock.patch.object(
                    evaluate,
                    "audit_paper_performance_sample_record",
                    side_effect=lambda sample, index, _path: {
                        "index": index,
                        "dataset": "default_performance",
                        "benchmark": "Collections",
                        "allocator": "scudo",
                        "run_index": index,
                        "issues": [],
                    },
                ), mock.patch.object(
                    evaluate,
                    "sample_is_claim_grade_usable",
                    return_value=True,
                ), mock.patch.object(
                    evaluate,
                    "paper_performance_source_binding_status",
                    return_value=source_binding,
                ):
                    replay_audit = evaluate.build_paper_performance_samples_audit(
                        {},
                        pathlib.Path(td),
                        pathlib.Path(td) / "samples.jsonl",
                        [adapter_plan] * 6,
                        dataset_names=["default_performance"],
                        required_runs=6,
                    )
                self.assertFalse(replay_audit["summary"]["ready_for_claim_grade_import"])
                self.assertEqual(
                    replay_audit["insufficient_usable_cells"][0]["usable_samples"],
                    1,
                )
                self.assertEqual(replay_audit["summary"]["invalid_sample_count"], 5)
                self.assertTrue(
                    all(
                        any("duplicate Scudo execution evidence replay" in issue for issue in item["issues"])
                        for item in replay_audit["invalid_samples"]
                    )
                )

                typed_metadata = json.loads(json.dumps(adapter))
                typed_metadata["success"] = 1
                typed_injection = {**adapter_plan, "metric_metadata": typed_metadata}
                typed_blockers = evaluate.scudo_sample_identity_blockers(typed_injection)
                self.assertTrue(
                    any("final digest-bound" in blocker for blocker in typed_blockers),
                    typed_blockers,
                )

                injected_adapter = {
                    **adapter_plan,
                    "metric_metadata": {
                        **adapter,
                        "child_record": {**verified_timing, "seconds": 2.0},
                    },
                }
                injected_blockers = evaluate.scudo_sample_identity_blockers(injected_adapter)
                self.assertTrue(
                    any("final digest-bound" in blocker for blocker in injected_blockers),
                    injected_blockers,
                )

    def test_scudo_sanitizer_import_revalidates_toolchain_and_clean_environment(self) -> None:
        planned = planned_rust_sanitizer_probe()
        execution = evaluate.scudo_execution_runtime_record(
            planned,
            runtime_verified=True,
            verification_status="runtime-verified",
        )
        semantics = evaluate.scudo_allocator_semantics(
            execution,
            runtime_verified=True,
            verification_status="runtime-verified",
        )
        with tempfile.TemporaryDirectory() as td:
            stderr_path = pathlib.Path(td) / "scudo-sanitizer.stderr.txt"
            stderr_path.write_text(
                evaluate._PAPER_WORKLOAD_DRIVER.SCUDO_RUNTIME_IDENTITY_MARKER,
                encoding="utf-8",
            )
            record = {
                "source": "paper-external-cargo-bench-json",
                "allocator": "scudo",
                "seconds": 1.0,
                "allocator_semantics": semantics,
                "scudo_execution_probe": execution,
                "subprocess_env_delta": {
                    "RUSTFLAGS": evaluate.SCUDO_SANITIZER_RUSTFLAGS,
                },
                "subprocess_env_removed": list(evaluate.SCUDO_SANITIZER_REMOVED_ENV_NAMES),
                "subprocess_env_effective_absence": {
                    name: True for name in evaluate.SCUDO_SANITIZER_REMOVED_ENV_NAMES
                },
                "evidence": [
                    {
                        "kind": "scudo_benchmark_stderr",
                        "path": str(stderr_path),
                        "sha256": evaluate.file_sha256(stderr_path),
                    }
                ],
            }
            evaluate._PAPER_WORKLOAD_DRIVER.attach_scudo_timing_binding(
                record,
                evidence_dir=pathlib.Path(td),
            )

            def rebind(candidate: dict, label: str) -> dict:
                rebound = json.loads(json.dumps(candidate))
                rebound["evidence"] = [
                    entry
                    for entry in rebound.get("evidence", [])
                    if entry.get("kind") != "scudo_timing_binding_json"
                ]
                rebound.pop("scudo_timing_binding", None)
                evaluate._PAPER_WORKLOAD_DRIVER.attach_scudo_timing_binding(
                    rebound,
                    evidence_dir=pathlib.Path(td) / label,
                )
                return rebound
            with mock.patch.object(
                evaluate._PAPER_WORKLOAD_DRIVER,
                "scudo_toolchain_probe",
                return_value={"ok": False},
            ):
                blockers = evaluate.scudo_sample_identity_blockers(record)
            self.assertTrue(any("route is unavailable" in blocker for blocker in blockers), blockers)

            with mock.patch.object(
                evaluate._PAPER_WORKLOAD_DRIVER,
                "scudo_toolchain_probe",
                return_value=current_rust_sanitizer_probe(),
            ):
                clean_blockers = evaluate.scudo_sample_identity_blockers(record)
            self.assertFalse(any("rust-sanitizer" in blocker for blocker in clean_blockers), clean_blockers)

            near_match_record = rebind({
                **record,
                "subprocess_env_delta": {"RUSTFLAGS": "-Z sanitizer=scudo-not"},
            }, "near-match-binding")
            with mock.patch.object(
                evaluate._PAPER_WORKLOAD_DRIVER,
                "scudo_toolchain_probe",
                return_value=current_rust_sanitizer_probe(),
            ):
                near_match_blockers = evaluate.scudo_sample_identity_blockers(near_match_record)
            self.assertTrue(
                any("sanitizer RUSTFLAGS" in blocker for blocker in near_match_blockers),
                near_match_blockers,
            )

            polluted_record = rebind({
                **record,
                "subprocess_env_removed": [],
                "subprocess_env_effective_absence": {},
            }, "polluted-binding")
            with mock.patch.object(
                evaluate._PAPER_WORKLOAD_DRIVER,
                "scudo_toolchain_probe",
                return_value=current_rust_sanitizer_probe(),
            ):
                polluted_blockers = evaluate.scudo_sample_identity_blockers(polluted_record)
            self.assertTrue(any("does not prove" in blocker for blocker in polluted_blockers), polluted_blockers)

            system_record = json.loads(json.dumps(record))
            system_record["scudo_execution_probe"]["toolchain_probe"][
                "rust_toolchain_provenance"
            ]["effective_toolchain"] = ""
            system_record = rebind(system_record, "system-toolchain-binding")
            with mock.patch.object(
                evaluate._PAPER_WORKLOAD_DRIVER,
                "scudo_toolchain_probe",
                return_value=current_rust_sanitizer_probe(""),
            ) as probe:
                system_blockers = evaluate.scudo_sample_identity_blockers(system_record)
            self.assertFalse(any("toolchain" in blocker for blocker in system_blockers), system_blockers)
            self.assertEqual(probe.call_args.args[0].rust_toolchain, "system")

    def test_summaries_drop_unverified_scudo_records_and_accept_verified_records(self) -> None:
        base = {
            "feature": "bench_ourself",
            "run_index": 1,
            "exit_code": 0,
            "wall_seconds": 1.0,
            "bench_results": [{"benchmark": "vec::bench_new", "ns_per_iter": 10, "deviation_ns": 1}],
        }
        unverified_scudo = {
            **base,
            "feature": "bench_scudo",
            "bench_results": [{"benchmark": "vec::bench_new", "ns_per_iter": 11, "deviation_ns": 1}],
        }
        invalid_summary = evaluate.summarize_benchmark_results([base, unverified_scudo])
        self.assertNotIn("bench_scudo", invalid_summary)

        crafted = {
            "bench_ourself": {
                "vec::bench_new": {"ns_per_iter_geomean_after_warmup": 10},
            },
            "bench_jemalloc": {
                "vec::bench_new": {"ns_per_iter_geomean_after_warmup": 12},
            },
            "bench_scudo": {
                "vec::bench_new": {"ns_per_iter_geomean_after_warmup": 11},
            },
        }
        jemalloc = {**base, "feature": "bench_jemalloc"}
        invalid_datasets = evaluate.build_local_current_datasets(
            crafted,
            [base, jemalloc, unverified_scudo],
            pathlib.Path("/tmp/scudo-invalid"),
        )
        self.assertNotIn("scudo", invalid_datasets["default_performance"]["columns"])

        verified_scudo = {
            **unverified_scudo,
            "paper_allocator_equivalent": True,
            "allocator_semantics": verified_semantics(),
            "scudo_execution_probe": verified_ld_preload_probe(),
        }
        valid_datasets = evaluate.build_local_current_datasets(
            crafted,
            [base, jemalloc, verified_scudo],
            pathlib.Path("/tmp/scudo-valid"),
        )
        self.assertIn("scudo", valid_datasets["default_performance"]["columns"])

    def test_generated_unialloc_only_benches_reject_scudo_labels(self) -> None:
        for target in (
            "std_bench_compiler_proto_generated",
            "std_bench_compiler_exact_dynamic_generated",
        ):
            source = evaluate.compiler_generated_bench_fallback_source(target)
            self.assertIn('feature = "bench_scudo"', source)
            self.assertIn("cannot produce Scudo timing", source)

        generated_root = evaluate.compiler_proto_root_source(
            "generated-source",
            evaluate.COMPILER_PROTO_TYPE_ID_BASIS,
        )
        self.assertIn('feature = "bench_scudo"', generated_root)
        self.assertIn("cannot produce Scudo timing", generated_root)

if __name__ == "__main__":
    unittest.main()
