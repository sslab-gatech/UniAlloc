#!/usr/bin/env python3
"""Fail-closed Scudo routing tests for the external RRedis surfaces."""

from __future__ import annotations

import argparse
import contextlib
import importlib.util
import io
import json
import os
import pathlib
import tempfile
import unittest
from unittest import mock


ROOT = pathlib.Path(__file__).resolve().parents[2]
SCRIPTS = ROOT / "evaluation" / "scripts"


def load_script(name: str):
    path = SCRIPTS / f"{name}.py"
    spec = importlib.util.spec_from_file_location(f"_test_{name}", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


redis_load = load_script("paper_external_redis_load_json")
redis_benchmark = load_script("paper_external_redis_benchmark_json")
rredis_runner = load_script("paper_external_rredis_runner")


class FakeScudoDriver:
    SCUDO_MODES = ("auto", "rust-sanitizer", "ld-preload")

    @staticmethod
    def scudo_execution_probe(_args):
        return {
            "ok": True,
            "selected_mode": "ld-preload",
            "runtime_library": "/tmp/libclang_rt.scudo_standalone-test.so",
            "runtime_probe": {
                "ok": True,
                "runtime_library_identity": {
                    "realpath": "/tmp/libclang_rt.scudo_standalone-test.so",
                    "sha256": "a" * 64,
                },
            },
            "toolchain_probe": {"ok": False},
            "blockers": [],
        }

    @staticmethod
    def scudo_child_probe_args(args, *, command, cwd):
        return argparse.Namespace(
            scudo_mode=args.scudo_mode,
            scudo_runtime_library=args.scudo_runtime_library,
            rust_toolchain=args.rust_toolchain,
            scudo_probe_cwd=str(pathlib.Path(cwd).resolve()),
            scudo_command_toolchain=None,
        )

    @staticmethod
    def align_scudo_child_toolchain_env(env, probe_args):
        env["UNIALLOC_RUST_TOOLCHAIN"] = probe_args.rust_toolchain
        env["RUSTUP_TOOLCHAIN"] = probe_args.rust_toolchain
        return {
            "rust_toolchain_provenance": {
                "effective_toolchain": probe_args.rust_toolchain,
                "uses_system_toolchain": False,
            },
            "probe_cwd": probe_args.scudo_probe_cwd,
            "command_toolchain": None,
            "subprocess_env_removed": [],
            "subprocess_env_effective": {
                "UNIALLOC_RUST_TOOLCHAIN": probe_args.rust_toolchain,
                "RUSTUP_TOOLCHAIN": probe_args.rust_toolchain,
            },
            "subprocess_env_effective_absence": {
                "UNIALLOC_RUST_TOOLCHAIN": False,
                "RUSTUP_TOOLCHAIN": False,
            },
        }

    @staticmethod
    def prepend_preload_library(existing, runtime):
        values = [str(runtime)]
        if existing:
            values.append(str(existing))
        return " ".join(values)

    @staticmethod
    def append_env_flags(existing, flags):
        return " ".join(part for part in (existing, flags) if part)

    @staticmethod
    def scudo_execution_runtime_record(execution, *, runtime_verified, verification_status):
        return {
            **execution,
            "execution_state": verification_status,
            "runtime_verified": runtime_verified,
            "paper_allocator_equivalent": runtime_verified,
        }

    @staticmethod
    def scudo_allocator_semantics(execution, *, runtime_verified, verification_status):
        return {
            "requested_allocator": "scudo",
            "allocator_feature": "bench_scudo",
            "implementation_kind": "verified_external_scudo_runtime",
            "paper_allocator_equivalent": runtime_verified,
            "runtime_identity_verified": runtime_verified,
            "runtime_identity_status": verification_status,
            "scudo_runtime_mode": execution.get("selected_mode"),
            "scudo_runtime_library": execution.get("runtime_library"),
        }

    @staticmethod
    def env_delta_for_record(env):
        return {
            key: env[key]
            for key in ("UNIALLOC_SCUDO_RUNTIME_LIBRARY", "LD_PRELOAD", "RUSTFLAGS")
            if key in env
        }


class FakeServerProcess:
    def __init__(self, stderr: bytes = b"") -> None:
        self.pid = 4242
        self.stdout = io.BytesIO(b"")
        self.stderr = io.BytesIO(stderr)
        self.returncode = None

    def poll(self):
        return self.returncode


def scudo_args() -> argparse.Namespace:
    return argparse.Namespace(
        allocator="scudo",
        scudo_mode="ld-preload",
        scudo_runtime_library="/tmp/libclang_rt.scudo_standalone-test.so",
        rust_toolchain="repo",
    )


def attested_checkout(checkout: pathlib.Path):
    (checkout / "src").mkdir(parents=True, exist_ok=True)
    (checkout / "Cargo.toml").write_text(
        '[package]\nname = "guarded-rredis"\nversion = "0.1.0"\nedition = "2018"\n'
        '\n[[bin]]\nname = "guarded-rredis"\n'
        '\n[features]\ndefault = []\nbench_scudo = []\n',
        encoding="utf-8",
    )
    main_rs = checkout / "src" / "main.rs"
    main_rs.write_text("fn main() {}\n", encoding="utf-8")
    prepared = rredis_runner.ensure_allocator_main_overlay(checkout)
    assert prepared["prepared"], prepared
    wrapper_args, route = rredis_runner.inject_cargo_feature(
        ["--allocator", "scudo", "--", "cargo", "run"],
        "bench_scudo",
    )
    attestation = rredis_runner.build_scudo_runner_attestation(checkout, wrapper_args, route)
    assert attestation["ok"], attestation
    separator = wrapper_args.index("--")
    return main_rs, wrapper_args[separator + 1 :], attestation


class ExternalRedisScudoRuntimeTests(unittest.TestCase):
    def test_scudo_and_bench_scudo_select_the_same_feature(self) -> None:
        for module in (redis_load, redis_benchmark, rredis_runner):
            with self.subTest(module=module.__name__):
                self.assertEqual(module.allocator_feature("scudo"), "bench_scudo")
                self.assertEqual(module.allocator_feature("bench_scudo"), "bench_scudo")

    def test_wrappers_apply_canonical_ld_preload_only_to_server_environment(self) -> None:
        for module in (redis_load, redis_benchmark):
            with self.subTest(module=module.__name__), mock.patch.object(
                module, "_PAPER_WORKLOAD_DRIVER", FakeScudoDriver
            ):
                env, setup = module.prepare_scudo_server_execution(
                    scudo_args(),
                    base_env={"LD_PRELOAD": "/tmp/existing.so"},
                )

                self.assertTrue(setup["ok"], setup)
                self.assertEqual(setup["execution"]["execution_state"], "planned")
                self.assertTrue(env["LD_PRELOAD"].startswith("/tmp/libclang_rt.scudo_standalone-test.so"))
                self.assertEqual(
                    env["UNIALLOC_SCUDO_RUNTIME_LIBRARY"],
                    "/tmp/libclang_rt.scudo_standalone-test.so",
                )
                self.assertEqual(setup["subprocess_env_delta"]["LD_PRELOAD"], env["LD_PRELOAD"])

    def test_wrappers_bind_sanitizer_probe_and_server_to_one_toolchain_and_cwd(self) -> None:
        for module in (redis_load, redis_benchmark):
            observed = []

            def fake_probe(probe_args):
                observed.append(probe_args)
                return {
                    "ok": True,
                    "requested_mode": "rust-sanitizer",
                    "selected_mode": "rust-sanitizer",
                    "execution_state": "planned",
                    "runtime_verified": False,
                    "runtime_authenticity_verified": True,
                    "paper_allocator_equivalent": False,
                    "runtime_library": None,
                    "rust_sanitizer_rustflags": "-Zsanitizer=scudo",
                    "toolchain_probe": {
                        "ok": True,
                        "probe_cwd": probe_args.scudo_probe_cwd,
                        "rustc_verbose_version": "rustc test",
                        "rust_toolchain_provenance": {
                            "effective_toolchain": probe_args.rust_toolchain,
                        },
                    },
                    "runtime_probe": {"ok": False},
                    "blockers": [],
                }

            with self.subTest(module=module.__name__), tempfile.TemporaryDirectory() as td, mock.patch.object(
                module._PAPER_WORKLOAD_DRIVER,
                "scudo_execution_probe",
                side_effect=fake_probe,
            ):
                args = argparse.Namespace(
                    allocator="scudo",
                    scudo_mode="rust-sanitizer",
                    scudo_runtime_library="",
                    rust_toolchain="nightly-test",
                )
                env, setup = module.prepare_scudo_server_execution(
                    args,
                    command=["cargo", "run"],
                    cwd=td,
                    base_env={"RUSTUP_TOOLCHAIN": "stable"},
                )

            self.assertTrue(setup["ok"], setup)
            self.assertEqual(observed[0].rust_toolchain, "nightly-test")
            self.assertEqual(observed[0].scudo_probe_cwd, str(pathlib.Path(td).resolve()))
            self.assertEqual(env["UNIALLOC_RUST_TOOLCHAIN"], "nightly-test")
            self.assertEqual(env["RUSTUP_TOOLCHAIN"], "nightly-test")
            self.assertEqual(env["RUSTFLAGS"], "-Zsanitizer=scudo")
            self.assertEqual(
                setup["scudo_toolchain_alignment"]["probe_cwd"],
                str(pathlib.Path(td).resolve()),
            )

    def test_rredis_preflight_prefers_cargo_toolchain_and_workload_cwd(self) -> None:
        observed = []

        def fake_probe(probe_args):
            observed.append(probe_args)
            return {
                "ok": True,
                "requested_mode": "rust-sanitizer",
                "selected_mode": "rust-sanitizer",
                "execution_state": "planned",
                "runtime_verified": False,
                "runtime_authenticity_verified": True,
                "paper_allocator_equivalent": False,
                "runtime_library": None,
                "toolchain_probe": {"ok": True},
                "runtime_probe": {"ok": False},
                "blockers": [],
            }

        with tempfile.TemporaryDirectory() as td, mock.patch.object(
            rredis_runner._PAPER_WORKLOAD_DRIVER,
            "scudo_execution_probe",
            side_effect=fake_probe,
        ):
            preflight = rredis_runner.scudo_execution_preflight(
                [
                    "--allocator",
                    "scudo",
                    "--rust-toolchain",
                    "nightly-ignored",
                    "--",
                    "cargo",
                    "+stable",
                    "run",
                ],
                real_workload_dir=pathlib.Path(td),
            )

        self.assertTrue(preflight["ok"], preflight)
        self.assertEqual(observed[0].rust_toolchain, "stable")
        self.assertEqual(observed[0].scudo_probe_cwd, str(pathlib.Path(td).resolve()))
        self.assertEqual(observed[0].scudo_command_toolchain, "stable")

    def test_runner_attestation_binds_cwd_main_guard_and_routed_command(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            checkout = pathlib.Path(temp_dir)
            _main_rs, server_command, attestation = attested_checkout(checkout)
            raw = json.dumps(attestation, sort_keys=True)
            args = argparse.Namespace(
                allocator="scudo",
                scudo_runner_attestation_json=raw,
            )

            for module in (redis_load, redis_benchmark):
                with self.subTest(module=module.__name__):
                    record = module.verify_scudo_runner_attestation(
                        args,
                        server_command,
                        str(checkout),
                    )
                    self.assertTrue(record["ok"], record)
                    self.assertEqual(record["allocator_feature"], "bench_scudo")
                    self.assertEqual(record["guard_block_occurrences"], 1)
                    self.assertEqual(record["main_rs_sha256"], attestation["main_rs_sha256"])

            explicit_args, explicit_route = rredis_runner.inject_cargo_feature(
                [
                    "--allocator",
                    "scudo",
                    "--",
                    "cargo",
                    "run",
                    "--package",
                    "guarded-rredis",
                    "--bin",
                    "guarded-rredis",
                ],
                "bench_scudo",
            )
            explicit = rredis_runner.build_scudo_runner_attestation(
                checkout,
                explicit_args,
                explicit_route,
            )
            self.assertTrue(explicit["ok"], explicit)
            self.assertEqual(explicit["cargo_target_contract"]["selected_package"], "guarded-rredis")
            self.assertEqual(explicit["cargo_target_contract"]["selected_binary"], "guarded-rredis")

    def test_cargo_feature_after_program_separator_cannot_spoof_scudo_routing(self) -> None:
        routed, record = rredis_runner.inject_cargo_feature(
            [
                "--allocator",
                "scudo",
                "--",
                "cargo",
                "run",
                "--",
                "application-arg",
                "--features",
                "bench_scudo",
            ],
            "bench_scudo",
        )
        separator = routed.index("--")
        server = routed[separator + 1 :]
        cargo_separator = server.index("--", 1)

        self.assertTrue(record["routed"], record)
        self.assertEqual(server[:cargo_separator], ["cargo", "run", "--features", "bench_scudo"])
        self.assertEqual(server[cargo_separator + 1 :], ["application-arg", "--features", "bench_scudo"])

        unchanged, rejected = rredis_runner.inject_cargo_feature(
            ["--allocator", "scudo", "--", "cargo", "metadata", "run"],
            "bench_scudo",
        )
        self.assertFalse(rejected["routed"], rejected)
        self.assertEqual(unchanged[-3:], ["cargo", "metadata", "run"])

    def test_alternate_cargo_target_is_rejected_before_wrapper_start(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            checkout = pathlib.Path(temp_dir)
            attested_checkout(checkout)
            preflight = {
                "requested": True,
                "ok": True,
                "execution": FakeScudoDriver.scudo_execution_runtime_record(
                    FakeScudoDriver.scudo_execution_probe(None),
                    runtime_verified=False,
                    verification_status="planned",
                ),
            }
            output = io.StringIO()
            with mock.patch.object(
                rredis_runner, "scudo_execution_preflight", return_value=preflight
            ), mock.patch.object(
                rredis_runner.subprocess,
                "run",
                side_effect=AssertionError("alternate cargo target must not start"),
            ), contextlib.redirect_stdout(output):
                code = rredis_runner.main(
                    [
                        "--real-workload-dir",
                        temp_dir,
                        "--skip-prepare",
                        "--allocator",
                        "scudo",
                        "--",
                        "cargo",
                        "run",
                        "--bin",
                        "other",
                    ]
                )

        self.assertEqual(code, rredis_runner.SCUDO_CONFIGURATION_ERROR)
        self.assertIn("selected binary", output.getvalue())

    def test_manifest_path_selector_is_rejected_by_attestation(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            checkout = pathlib.Path(temp_dir)
            attested_checkout(checkout)
            wrapper_args, route = rredis_runner.inject_cargo_feature(
                [
                    "--allocator",
                    "scudo",
                    "--",
                    "cargo",
                    "run",
                    "--manifest-path",
                    "../other/Cargo.toml",
                ],
                "bench_scudo",
            )
            attestation = rredis_runner.build_scudo_runner_attestation(checkout, wrapper_args, route)

        self.assertFalse(attestation["ok"], attestation)
        self.assertIn("--manifest-path", " ".join(attestation["blockers"]))

    def test_commented_or_literal_guard_is_rejected_then_upgraded_to_active_crate_root(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            checkout = pathlib.Path(temp_dir)
            (checkout / "src").mkdir()
            (checkout / "Cargo.toml").write_text(
                '[package]\nname = "forged-rredis"\nversion = "0.1.0"\nedition = "2018"\n'
                '\n[features]\ndefault = []\nbench_scudo = []\n',
                encoding="utf-8",
            )
            guard = rredis_runner.rredis_scudo_runtime_guard_overlay()
            main_rs = checkout / "src" / "main.rs"
            main_rs.write_text(
                "/*\n"
                + rredis_runner.RREDIS_MAIN_OVERLAY_MARKER
                + "\n"
                + guard
                + "*/\n"
                + 'const FORGED_GUARD: &str = r####"\n'
                + guard
                + '"####;\n'
                + 'fn main() { eprintln!("unialloc: verified Scudo runtime identity"); }\n',
                encoding="utf-8",
            )
            wrapper_args, route = rredis_runner.inject_cargo_feature(
                ["--allocator", "scudo", "--", "cargo", "run"],
                "bench_scudo",
            )
            forged = rredis_runner.build_scudo_runner_attestation(checkout, wrapper_args, route)
            self.assertFalse(forged["ok"], forged)
            self.assertIn("active Scudo guard at the crate root", " ".join(forged["blockers"]))

            preflight = {
                "requested": True,
                "ok": True,
                "execution": FakeScudoDriver.scudo_execution_runtime_record(
                    FakeScudoDriver.scudo_execution_probe(None),
                    runtime_verified=False,
                    verification_status="planned",
                ),
            }
            with mock.patch.object(
                rredis_runner, "scudo_execution_preflight", return_value=preflight
            ), mock.patch.object(
                rredis_runner.subprocess,
                "run",
                side_effect=AssertionError("literal-forged guard server must not start"),
            ), contextlib.redirect_stdout(io.StringIO()):
                code = rredis_runner.main(
                    [
                        "--real-workload-dir",
                        temp_dir,
                        "--skip-prepare",
                        "--allocator",
                        "scudo",
                        "--",
                        "cargo",
                        "run",
                    ]
                )
            self.assertEqual(code, rredis_runner.SCUDO_CONFIGURATION_ERROR)

            upgraded = rredis_runner.ensure_allocator_main_overlay(checkout)
            upgraded_text = main_rs.read_text(encoding="utf-8")
            self.assertTrue(upgraded["prepared"], upgraded)
            self.assertTrue(upgraded["scudo_guard_at_crate_root"], upgraded)
            self.assertEqual(upgraded["stale_scudo_guard_occurrences_removed"], 2)
            self.assertTrue(rredis_runner.scudo_guard_at_crate_root(upgraded_text, guard))

    def test_arbitrary_marker_printing_server_is_rejected_before_start(self) -> None:
        setup = {
            "requested": True,
            "ok": True,
            "execution": FakeScudoDriver.scudo_execution_runtime_record(
                FakeScudoDriver.scudo_execution_probe(None),
                runtime_verified=False,
                verification_status="planned",
            ),
            "subprocess_env_delta": {"LD_PRELOAD": "/tmp/scudo.so"},
        }
        load_argv = [
            "--dataset",
            "default_performance",
            "--benchmark",
            "RRedis*",
            "--allocator",
            "scudo",
            "--run-index",
            "1",
            "--",
            "sh",
            "-c",
            f"printf '{redis_load.SCUDO_RUNTIME_IDENTITY_MARKER}' >&2; exec fake-server",
        ]
        with mock.patch.object(redis_load, "prepare_scudo_server_execution", return_value=({}, setup)), mock.patch.object(
            redis_load.subprocess, "Popen", side_effect=AssertionError("arbitrary server must not start")
        ), mock.patch.object(redis_load, "run_warmup", side_effect=AssertionError("warmup must not start")), mock.patch.object(
            redis_load, "run_load", side_effect=AssertionError("timing must not start")
        ), contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(redis_load.main(load_argv), redis_load.SCUDO_CONFIGURATION_ERROR)

        benchmark_argv = [
            "--dataset",
            "default_performance",
            "--benchmark",
            "RRedis*",
            "--allocator",
            "scudo",
            "--run-index",
            "1",
            "--redis-benchmark-bin",
            "/tmp/redis-benchmark",
            "--",
            "sh",
            "-c",
            f"printf '{redis_benchmark.SCUDO_RUNTIME_IDENTITY_MARKER}' >&2; exec fake-server",
        ]
        with mock.patch.object(
            redis_benchmark, "prepare_scudo_server_execution", return_value=({}, setup)
        ), mock.patch.object(
            redis_benchmark, "host_metadata", return_value={"system": "Linux", "machine": "x86_64"}
        ), mock.patch.object(
            redis_benchmark.platform, "platform", return_value="Linux-test"
        ), mock.patch.object(
            redis_benchmark,
            "resolve_redis_benchmark_binary",
            return_value=("/tmp/redis-benchmark", {"available": True}),
        ), mock.patch.object(
            redis_benchmark.subprocess, "Popen", side_effect=AssertionError("arbitrary server must not start")
        ), mock.patch.object(
            redis_benchmark, "run_command_bounded", side_effect=AssertionError("timing must not start")
        ), contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(redis_benchmark.main(benchmark_argv), redis_benchmark.SCUDO_CONFIGURATION_ERROR)

    def test_main_hash_mismatch_is_rejected_before_server_start(self) -> None:
        setup = {
            "requested": True,
            "ok": True,
            "execution": FakeScudoDriver.scudo_execution_runtime_record(
                FakeScudoDriver.scudo_execution_probe(None),
                runtime_verified=False,
                verification_status="planned",
            ),
            "subprocess_env_delta": {"LD_PRELOAD": "/tmp/scudo.so"},
        }
        with tempfile.TemporaryDirectory() as temp_dir:
            checkout = pathlib.Path(temp_dir)
            main_rs, server_command, attestation = attested_checkout(checkout)
            main_rs.write_text(main_rs.read_text(encoding="utf-8") + "// mutated after attestation\n", encoding="utf-8")
            argv = [
                "--dataset",
                "default_performance",
                "--benchmark",
                "RRedis*",
                "--allocator",
                "scudo",
                "--run-index",
                "1",
                "--scudo-runner-attestation-json",
                json.dumps(attestation, sort_keys=True),
                "--",
                *server_command,
            ]
            output = io.StringIO()
            with mock.patch.object(redis_load, "prepare_scudo_server_execution", return_value=({}, setup)), mock.patch.object(
                redis_load.os, "getcwd", return_value=str(checkout)
            ), mock.patch.object(
                redis_load.subprocess, "Popen", side_effect=AssertionError("hash-mismatched server must not start")
            ), mock.patch.object(
                redis_load, "run_warmup", side_effect=AssertionError("warmup must not start")
            ), mock.patch.object(
                redis_load, "run_load", side_effect=AssertionError("timing must not start")
            ), contextlib.redirect_stdout(output):
                code = redis_load.main(argv)

            self.assertEqual(code, redis_load.SCUDO_CONFIGURATION_ERROR)
            self.assertIn("main.rs hash no longer matches", output.getvalue())

    def test_wrappers_fail_before_server_start_when_no_scudo_route_exists(self) -> None:
        blocked = {
            "requested": True,
            "ok": False,
            "error": "no canonical Scudo execution route is available",
            "execution": {"ok": False, "blockers": ["missing runtime"]},
            "subprocess_env_delta": {},
        }
        load_argv = [
            "--dataset",
            "default_performance",
            "--benchmark",
            "RRedis*",
            "--allocator",
            "scudo",
            "--run-index",
            "1",
            "--",
            "fake-server",
        ]
        with mock.patch.object(redis_load, "prepare_scudo_server_execution", return_value=({}, blocked)), mock.patch.object(
            redis_load.subprocess, "Popen", side_effect=AssertionError("server must not start")
        ), mock.patch.object(redis_load, "run_warmup", side_effect=AssertionError("warmup must not start")), mock.patch.object(
            redis_load, "run_load", side_effect=AssertionError("timing must not start")
        ), contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(redis_load.main(load_argv), redis_load.SCUDO_CONFIGURATION_ERROR)

        benchmark_argv = [
            "--dataset",
            "default_performance",
            "--benchmark",
            "RRedis*",
            "--allocator",
            "bench_scudo",
            "--run-index",
            "1",
            "--redis-benchmark-bin",
            "/tmp/redis-benchmark",
            "--",
            "fake-server",
        ]
        with mock.patch.object(
            redis_benchmark, "prepare_scudo_server_execution", return_value=({}, blocked)
        ), mock.patch.object(
            redis_benchmark, "host_metadata", return_value={"system": "Linux", "machine": "x86_64"}
        ), mock.patch.object(
            redis_benchmark.platform, "platform", return_value="Linux-test"
        ), mock.patch.object(
            redis_benchmark,
            "resolve_redis_benchmark_binary",
            return_value=("/tmp/redis-benchmark", {"available": True}),
        ), mock.patch.object(
            redis_benchmark.subprocess, "Popen", side_effect=AssertionError("server must not start")
        ), mock.patch.object(
            redis_benchmark, "run_command_bounded", side_effect=AssertionError("timing must not start")
        ), contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(redis_benchmark.main(benchmark_argv), redis_benchmark.SCUDO_CONFIGURATION_ERROR)

    def test_load_wrapper_requires_marker_before_warmup_or_timing(self) -> None:
        setup = {
            "requested": True,
            "ok": True,
            "execution": FakeScudoDriver.scudo_execution_runtime_record(
                FakeScudoDriver.scudo_execution_probe(None),
                runtime_verified=False,
                verification_status="planned",
            ),
            "subprocess_env_delta": {"LD_PRELOAD": "/tmp/scudo.so"},
        }
        argv = [
            "--dataset",
            "default_performance",
            "--benchmark",
            "RRedis*",
            "--allocator",
            "scudo",
            "--run-index",
            "1",
            "--",
            "fake-server",
        ]
        with mock.patch.object(redis_load, "prepare_scudo_server_execution", return_value=({}, setup)), mock.patch.object(
            redis_load,
            "verify_scudo_runner_attestation",
            return_value={"required": True, "ok": True, "blockers": []},
        ), mock.patch.object(
            redis_load.subprocess, "Popen", return_value=FakeServerProcess()
        ), mock.patch.object(redis_load, "wait_until_ready", return_value=(True, "ready", 1, None)), mock.patch.object(
            redis_load, "wait_for_scudo_runtime_identity_marker", return_value=False
        ), mock.patch.object(redis_load, "terminate_process_group", return_value=(True, 86)), mock.patch.object(
            redis_load, "run_warmup", side_effect=AssertionError("warmup must not start")
        ), mock.patch.object(redis_load, "run_load", side_effect=AssertionError("timing must not start")), contextlib.redirect_stdout(
            io.StringIO()
        ):
            self.assertEqual(redis_load.main(argv), 86)

    def test_redis_benchmark_requires_marker_before_client_timing(self) -> None:
        setup = {
            "requested": True,
            "ok": True,
            "execution": FakeScudoDriver.scudo_execution_runtime_record(
                FakeScudoDriver.scudo_execution_probe(None),
                runtime_verified=False,
                verification_status="planned",
            ),
            "subprocess_env_delta": {"LD_PRELOAD": "/tmp/scudo.so"},
        }
        argv = [
            "--dataset",
            "default_performance",
            "--benchmark",
            "RRedis*",
            "--allocator",
            "scudo",
            "--run-index",
            "1",
            "--redis-benchmark-bin",
            "/tmp/redis-benchmark",
            "--",
            "fake-server",
        ]
        with mock.patch.object(
            redis_benchmark, "prepare_scudo_server_execution", return_value=({}, setup)
        ), mock.patch.object(
            redis_benchmark,
            "verify_scudo_runner_attestation",
            return_value={"required": True, "ok": True, "blockers": []},
        ), mock.patch.object(
            redis_benchmark, "host_metadata", return_value={"system": "Linux", "machine": "x86_64"}
        ), mock.patch.object(
            redis_benchmark.platform, "platform", return_value="Linux-test"
        ), mock.patch.object(
            redis_benchmark,
            "resolve_redis_benchmark_binary",
            return_value=("/tmp/redis-benchmark", {"available": True}),
        ), mock.patch.object(
            redis_benchmark.subprocess, "Popen", return_value=FakeServerProcess()
        ), mock.patch.object(
            redis_benchmark, "wait_until_ready", return_value=(True, "ready", 1, None)
        ), mock.patch.object(
            redis_benchmark, "wait_for_scudo_runtime_identity_marker", return_value=False
        ), mock.patch.object(
            redis_benchmark, "terminate_process_group", return_value=(True, 86)
        ), mock.patch.object(
            redis_benchmark, "run_command_bounded", side_effect=AssertionError("timing must not start")
        ), contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(redis_benchmark.main(argv), 86)

    def test_rredis_overlay_emits_marker_only_after_allocator_provider_checks(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            checkout = pathlib.Path(temp_dir)
            (checkout / "src").mkdir()
            main_rs = checkout / "src" / "main.rs"
            main_rs.write_text("pub mod release;\nfn main() {}\n", encoding="utf-8")

            first = rredis_runner.ensure_allocator_main_overlay(checkout)
            second = rredis_runner.ensure_allocator_main_overlay(checkout)
            text = main_rs.read_text(encoding="utf-8")

            self.assertTrue(first["prepared"], first)
            self.assertTrue(first["scudo_runtime_identity_guard"], first)
            self.assertFalse(second["changed"], second)
            self.assertEqual(text.count(rredis_runner.RREDIS_SCUDO_GUARD_MARKER), 1)
            self.assertIn("__scudo_print_stats", text)
            self.assertIn('verify_allocator_symbol(&scudo_provider, b"malloc\\0")', text)
            self.assertIn('verify_allocator_symbol(&scudo_provider, b"free\\0")', text)
            self.assertIn("provider_matches_configured_runtime", text)
            self.assertIn("UNIALLOC_SCUDO_RUNTIME_LIBRARY", text)
            self.assertLess(text.index('verify_allocator_symbol(&scudo_provider, b"free\\0")'), text.index("emit(SUCCESS)"))
            self.assertIn("unialloc: verified Scudo runtime identity\\n", text)
            self.assertIn('static RREDIS_SCUDO_SYSTEM_ALLOCATOR: std::alloc::System', text)

    def test_runner_passes_route_configuration_without_preloading_python(self) -> None:
        preflight = {
            "ok": True,
            "execution": {
                "selected_mode": "ld-preload",
                "runtime_library": "/tmp/libclang_rt.scudo_standalone-test.so",
            },
        }
        with mock.patch.dict(rredis_runner.os.environ, {"LD_PRELOAD": ""}, clear=False):
            env = rredis_runner.external_workload_env(preflight)

        self.assertEqual(env["UNIALLOC_SCUDO_MODE"], "ld-preload")
        self.assertEqual(
            env["UNIALLOC_SCUDO_RUNTIME_LIBRARY"],
            "/tmp/libclang_rt.scudo_standalone-test.so",
        )
        self.assertEqual(env.get("LD_PRELOAD"), "")

    def test_runner_fails_scudo_before_preparation_or_delegation_when_route_is_missing(self) -> None:
        blocked = {
            "requested": True,
            "ok": False,
            "error": "no canonical Scudo execution route is available",
            "execution": {"ok": False, "blockers": ["missing runtime"]},
        }
        with tempfile.TemporaryDirectory() as temp_dir, mock.patch.object(
            rredis_runner, "scudo_execution_preflight", return_value=blocked
        ), mock.patch.object(
            rredis_runner,
            "prepare_rsedis_host_target_dependencies",
            side_effect=AssertionError("preparation must not start after failed Scudo preflight"),
        ), mock.patch.object(
            rredis_runner.subprocess,
            "run",
            side_effect=AssertionError("wrapper must not start after failed Scudo preflight"),
        ), contextlib.redirect_stdout(io.StringIO()):
            code = rredis_runner.main(
                [
                    "--real-workload-dir",
                    temp_dir,
                    "--allocator",
                    "scudo",
                    "--",
                    "cargo",
                    "run",
                ]
            )

        self.assertEqual(code, rredis_runner.SCUDO_CONFIGURATION_ERROR)

    def test_marker_capture_survives_chunk_boundaries_and_tail_truncation(self) -> None:
        marker = redis_load.SCUDO_RUNTIME_IDENTITY_MARKER.encode("utf-8")

        class ChunkPipe:
            def __init__(self):
                self.chunks = [marker[:9], marker[9:], b"x" * 100]

            def read(self, _size):
                return self.chunks.pop(0) if self.chunks else b""

            def close(self):
                return None

        capture = redis_load.BoundedPipeCapture(ChunkPipe(), max_bytes=8)
        capture.start()
        capture.join()

        self.assertTrue(capture.scudo_runtime_identity_marker_seen)
        self.assertNotIn(marker, capture.bytes())

        prefixed = redis_load.BoundedPipeCapture(io.BytesIO(b"log-prefix: " + marker), max_bytes=256)
        prefixed.start()
        prefixed.join()
        self.assertFalse(prefixed.scudo_runtime_identity_marker_seen)

    def test_marker_is_observed_from_live_pipe_before_server_exits(self) -> None:
        read_fd, write_fd = os.pipe()
        read_pipe = os.fdopen(read_fd, "rb")
        capture = redis_load.BoundedPipeCapture(read_pipe, max_bytes=256)
        capture.start()
        try:
            os.write(write_fd, redis_load.SCUDO_RUNTIME_IDENTITY_MARKER.encode("utf-8"))
            self.assertTrue(
                redis_load.wait_for_scudo_runtime_identity_marker(capture, timeout=0.5),
                "marker capture must not wait for server stderr EOF",
            )
        finally:
            os.close(write_fd)
            capture.join()


if __name__ == "__main__":
    unittest.main()
