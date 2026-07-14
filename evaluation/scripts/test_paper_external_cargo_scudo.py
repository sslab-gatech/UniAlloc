#!/usr/bin/env python3
"""Fail-closed Scudo tests for the external cargo-bench JSON adapter."""

from __future__ import annotations

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
WRAPPER_PATH = ROOT / "evaluation" / "scripts" / "paper_external_cargo_bench_json.py"

spec = importlib.util.spec_from_file_location("paper_external_cargo_bench_json_scudo", WRAPPER_PATH)
assert spec is not None and spec.loader is not None
wrapper = importlib.util.module_from_spec(spec)
spec.loader.exec_module(wrapper)


def canonical_ld_preload_probe(runtime: pathlib.Path) -> dict:
    identity = {
        "path": str(runtime),
        "realpath": str(runtime),
        "size_bytes": 1234,
        "sha256": "a" * 64,
        "platform": "Linux",
        "architecture": "x86_64",
    }
    authenticity = {
        "ok": True,
        "runtime_authenticity_verified": True,
        "verification_scheme": "dpkg-compiler-rt-content-and-scudo-stats-v1",
        "runtime_library_identity": identity,
    }
    return {
        "ok": True,
        "requested_mode": "auto",
        "selected_mode": "ld-preload",
        "runtime_library": str(runtime),
        "runtime_authenticity_verified": True,
        "runtime_probe": {
            "ok": True,
            "runtime_authenticity_verified": True,
            "configured_runtime_library": str(runtime),
            "runtime_library_identity": identity,
            "runtime_authenticity": authenticity,
        },
        "toolchain_probe": {
            "ok": False,
            "rust_toolchain_provenance": {"effective_toolchain": "nightly-test"},
        },
        "execution_state": "planned",
        "runtime_verified": False,
        "paper_allocator_equivalent": False,
        "runtime_identity_marker_required": wrapper.SCUDO_RUNTIME_IDENTITY_MARKER,
        "blockers": [],
    }


def child_result(*, stderr: str) -> dict:
    stdout = "test bench_unit ... bench:       1,234 ns/iter (+/- 56)\n"
    stdout_bytes = stdout.encode()
    stderr_bytes = stderr.encode()
    return {
        "returncode": 0,
        "stdout": stdout,
        "stderr": stderr,
        "_stdout_retained_bytes": stdout_bytes,
        "_stderr_retained_bytes": stderr_bytes,
        "timed_out": False,
        "stdout_bytes": len(stdout_bytes),
        "stderr_bytes": len(stderr_bytes),
        "stdout_retained_bytes": len(stdout_bytes),
        "stderr_retained_bytes": len(stderr_bytes),
        "stdout_truncated": False,
        "stderr_truncated": False,
        "max_output_bytes": 4096,
    }


def verified_guard_probe() -> dict:
    return {
        "ok": True,
        "guard_source": "/workload/benches/bench.rs",
        "guard_source_sha256": "b" * 64,
        "runtime_identity_marker": wrapper.SCUDO_RUNTIME_IDENTITY_MARKER,
    }


def prepared_allocator_routing(args, command, *, mutation_journal=None):
    return list(command), {
        "prepared": True,
        "changed": True,
        "allocator_feature": "bench_scudo",
        "allocator_semantics": {},
    }


def json_records(output: str) -> list[dict]:
    return [
        json.loads(line)
        for line in output.splitlines()
        if line.strip().startswith("{")
    ]


class ExternalCargoScudoTests(unittest.TestCase):
    def test_external_checkout_without_toolchain_uses_system_probe_contract(self) -> None:
        with tempfile.TemporaryDirectory() as tmp, mock.patch.dict(
            wrapper.os.environ,
            {"RUSTUP_TOOLCHAIN": "", "UNIALLOC_RUST_TOOLCHAIN": ""},
            clear=False,
        ):
            args = wrapper.argparse.Namespace(
                allocator="scudo",
                scudo_mode="auto",
                scudo_runtime_library=None,
            )
            driver_args = wrapper.canonical_scudo_driver_args(
                args,
                ["cargo", "bench", "--bench", "bench"],
                pathlib.Path(tmp),
            )

        self.assertEqual(driver_args.rust_toolchain, "system")

    def test_current_libtest_decimal_ns_rows_are_parsed(self) -> None:
        rows = wrapper.parse_bench_rows(
            "test allocation_roundtrip ... bench: 32.32 ns/iter (+/- 0.54)\n"
        )
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["ns_per_iter"], 32.32)
        self.assertEqual(rows[0]["stddev_ns_per_iter"], 0.54)

    def test_missing_scudo_route_fails_before_child_timing(self) -> None:
        unavailable = {
            "ok": False,
            "requested_mode": "auto",
            "selected_mode": None,
            "runtime_probe": {"ok": False},
            "toolchain_probe": {"ok": False},
            "blockers": [
                "rustc rejected -Z sanitizer=scudo",
                "no Scudo standalone runtime library was found for LD_PRELOAD",
            ],
        }
        stdout = io.StringIO()
        with mock.patch.object(
            wrapper._paper_workload_driver,
            "scudo_execution_probe",
            return_value=unavailable,
        ), mock.patch.object(wrapper, "run_child_command") as run_child, contextlib.redirect_stdout(stdout):
            code = wrapper.main(
                [
                    "--dataset",
                    "default_performance",
                    "--benchmark",
                    "unit-cargo-bench",
                    "--allocator",
                    "scudo",
                    "--",
                    "cargo",
                    "bench",
                    "--features",
                    "bench_scudo",
                ]
            )

        run_child.assert_not_called()
        self.assertEqual(code, 2)
        record = json_records(stdout.getvalue())[-1]
        self.assertFalse(record["success"], record)
        self.assertFalse(record["child_started"], record)
        self.assertFalse(record["measurement_eligible"], record)
        self.assertEqual(record["scudo_execution_probe"]["execution_state"], "unavailable")

    def test_success_requires_marker_and_records_runtime_provenance(self) -> None:
        runtime = pathlib.Path("/opt/compiler-rt/libclang_rt.scudo_standalone-x86_64.so")
        probe = canonical_ld_preload_probe(runtime)
        canonical_env = os.environ.copy()
        canonical_env.update(
            {
                "UNIALLOC_SCUDO_RUNTIME_LIBRARY": str(runtime),
                "LD_PRELOAD": str(runtime),
            }
        )
        observed_env: dict = {}

        def fake_child(command, env, timeout, max_output_bytes, *, cwd=None):
            observed_env.update(env)
            return child_result(stderr=wrapper.SCUDO_RUNTIME_IDENTITY_MARKER)

        with tempfile.TemporaryDirectory() as tmp:
            stdout = io.StringIO()
            with mock.patch.object(
                wrapper._paper_workload_driver,
                "scudo_execution_probe",
                return_value=probe,
            ), mock.patch.object(
                wrapper._paper_workload_driver,
                "cargo_subprocess_env",
                return_value=canonical_env,
            ), mock.patch.object(
                wrapper,
                "run_child_command",
                side_effect=fake_child,
            ), mock.patch.object(
                wrapper,
                "scudo_runtime_guard_source_probe",
                return_value=verified_guard_probe(),
            ), mock.patch.object(
                wrapper,
                "prepare_allocator_feature_routing",
                side_effect=prepared_allocator_routing,
            ), contextlib.redirect_stdout(stdout):
                code = wrapper.main(
                    [
                        "--dataset",
                        "default_performance",
                        "--benchmark",
                        "unit-cargo-bench",
                        "--allocator",
                        "scudo",
                        "--criterion-dir",
                        str(pathlib.Path(tmp) / "criterion"),
                        "--max-output-bytes",
                        "4096",
                        "--allocator-feature-routing",
                        "--real-workload-dir",
                        tmp,
                        "--",
                        "cargo",
                        "bench",
                        "--features",
                        "bench_scudo",
                    ]
                )

        self.assertEqual(code, 0, stdout.getvalue())
        self.assertEqual(observed_env["UNIALLOC_SCUDO_RUNTIME_LIBRARY"], str(runtime))
        self.assertEqual(observed_env["LD_PRELOAD"].split()[0], str(runtime))
        record = json_records(stdout.getvalue())[-1]
        self.assertTrue(record["success"], record)
        self.assertTrue(record["scudo_execution_probe"]["runtime_verified"], record)
        self.assertTrue(record["allocator_semantics"]["runtime_identity_verified"], record)
        self.assertTrue(record["allocator_semantics"]["paper_allocator_equivalent"], record)
        self.assertEqual(
            record["allocator_semantics"]["scudo_runtime_library_identity"]["sha256"],
            "a" * 64,
        )
        self.assertTrue(record["target_records"][0]["scudo_execution_probe"]["runtime_verified"])

    def test_bench_scudo_without_allocator_routing_fails_before_child_timing(self) -> None:
        runtime = pathlib.Path("/opt/compiler-rt/libclang_rt.scudo_standalone-x86_64.so")
        probe = canonical_ld_preload_probe(runtime)
        canonical_env = os.environ.copy()
        canonical_env.update(
            {
                "UNIALLOC_SCUDO_RUNTIME_LIBRARY": str(runtime),
                "LD_PRELOAD": str(runtime),
            }
        )
        stdout = io.StringIO()
        with mock.patch.object(
            wrapper._paper_workload_driver,
            "scudo_execution_probe",
            return_value=probe,
        ), mock.patch.object(
            wrapper._paper_workload_driver,
            "cargo_subprocess_env",
            return_value=canonical_env,
        ), mock.patch.object(wrapper, "run_child_command") as run_child, mock.patch.object(
            wrapper,
            "scudo_runtime_guard_source_probe",
        ) as guard_probe, contextlib.redirect_stdout(stdout):
            code = wrapper.main(
                [
                    "--dataset",
                    "default_performance",
                    "--benchmark",
                    "unit-cargo-bench",
                    "--allocator",
                    "scudo",
                    "--",
                    "cargo",
                    "bench",
                    "--features",
                    "bench_scudo",
                ]
            )

        run_child.assert_not_called()
        guard_probe.assert_not_called()
        self.assertEqual(code, 2)
        record = json_records(stdout.getvalue())[-1]
        self.assertFalse(record["child_started"], record)
        self.assertFalse(record["measurement_eligible"], record)
        self.assertTrue(record["allocator_feature_routing_required"], record)
        self.assertIn("requires allocator feature routing", record["error"])

    def test_successful_child_without_marker_is_rejected(self) -> None:
        runtime = pathlib.Path("/opt/compiler-rt/libclang_rt.scudo_standalone-x86_64.so")
        probe = canonical_ld_preload_probe(runtime)
        canonical_env = os.environ.copy()
        canonical_env.update(
            {
                "UNIALLOC_SCUDO_RUNTIME_LIBRARY": str(runtime),
                "LD_PRELOAD": str(runtime),
            }
        )
        with tempfile.TemporaryDirectory() as tmp:
            stdout = io.StringIO()
            with mock.patch.object(
                wrapper._paper_workload_driver,
                "scudo_execution_probe",
                return_value=probe,
            ), mock.patch.object(
                wrapper._paper_workload_driver,
                "cargo_subprocess_env",
                return_value=canonical_env,
            ), mock.patch.object(
                wrapper,
                "run_child_command",
                return_value=child_result(stderr="plain System allocator\n"),
            ), mock.patch.object(
                wrapper,
                "scudo_runtime_guard_source_probe",
                return_value=verified_guard_probe(),
            ), mock.patch.object(
                wrapper,
                "prepare_allocator_feature_routing",
                side_effect=prepared_allocator_routing,
            ), contextlib.redirect_stdout(stdout):
                code = wrapper.main(
                    [
                        "--dataset",
                        "default_performance",
                        "--benchmark",
                        "unit-cargo-bench",
                        "--allocator",
                        "scudo",
                        "--criterion-dir",
                        str(pathlib.Path(tmp) / "criterion"),
                        "--allocator-feature-routing",
                        "--real-workload-dir",
                        tmp,
                        "--",
                        "cargo",
                        "bench",
                        "--features=bench_scudo",
                    ]
                )

        self.assertEqual(code, 1)
        record = json_records(stdout.getvalue())[-1]
        self.assertFalse(record["success"], record)
        self.assertFalse(record["measurement_eligible"], record)
        self.assertFalse(record["scudo_execution_probe"]["runtime_verified"], record)
        self.assertIn("timing was rejected", record["error"])

    def test_generated_scudo_overlay_verifies_allocator_symbol_providers(self) -> None:
        overlay = wrapper.allocator_bench_overlay_text()
        self.assertIn('b"unialloc: verified Scudo runtime identity\\n"', overlay)
        self.assertIn('b"__scudo_print_stats\\0"', overlay)
        for symbol in ("malloc", "calloc", "realloc", "free", "posix_memalign"):
            self.assertIn(f'b"{symbol}\\0"', overlay)
        self.assertIn("dladdr", overlay)
        self.assertIn("UNIALLOC_SCUDO_RUNTIME_LIBRARY", overlay)
        self.assertIn("provider_matches_configured_runtime", overlay)
        self.assertIn("realpath", overlay)
        self.assertIn("_exit(EXIT_SCUDO_UNVERIFIED)", overlay)
        self.assertIn('#[link_section = ".init_array"]', overlay)
        self.assertTrue(overlay.rstrip().endswith(wrapper.ALLOCATOR_BENCH_OVERLAY_END_MARKER))

    def test_forged_guard_tokens_do_not_satisfy_exact_overlay_probe(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp)
            (root / "benches").mkdir()
            (root / "Cargo.toml").write_text(
                '[package]\nname = "forged-guard"\nversion = "0.1.0"\n\n'
                '[[bench]]\nname = "micro"\npath = "benches/micro.rs"\n',
                encoding="utf-8",
            )
            forged_tokens = [
                wrapper.ALLOCATOR_BENCH_OVERLAY_MARKER,
                wrapper.SCUDO_RUNTIME_IDENTITY_MARKER.rstrip("\n"),
                "__scudo_print_stats",
                ".init_array",
                "dladdr",
                "_exit",
                "malloc calloc realloc free posix_memalign",
            ]
            (root / "benches" / "micro.rs").write_text(
                "\n".join(f"// {token}" for token in forged_tokens) + "\nfn main() {}\n",
                encoding="utf-8",
            )
            probe = wrapper.scudo_runtime_guard_source_probe(
                root,
                ["cargo", "bench", "--bench", "micro", "--features", "bench_scudo"],
            )

        self.assertFalse(probe["ok"], probe)
        self.assertFalse(probe["exact_overlay_verified"], probe)
        self.assertIn("exact current guarded allocator overlay", probe["error"])

    def test_pre_guard_overlay_is_upgraded_to_exact_current_overlay(self) -> None:
        legacy_overlay = f"""{wrapper.ALLOCATOR_BENCH_OVERLAY_MARKER}
#[cfg(any(feature = "bench_ptmalloc", feature = "bench_scudo"))]
#[global_allocator]
static SYSTEM_EXTERNAL_CARGO_BENCH_ALLOCATOR: std::alloc::System = std::alloc::System;
"""
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp)
            bench_dir = root / "benches"
            bench_dir.mkdir()
            manifest = root / "Cargo.toml"
            bench = bench_dir / "micro.rs"
            manifest.write_text(
                '[package]\nname = "legacy-overlay"\nversion = "0.1.0"\n\n'
                '[[bench]]\nname = "micro"\npath = "benches/micro.rs"\n',
                encoding="utf-8",
            )
            bench.write_text(
                legacy_overlay + '#![feature(test)]\nextern crate test;\n',
                encoding="utf-8",
            )
            command = ["cargo", "bench", "--bench", "micro"]
            surface = wrapper.resolve_cargo_bench_surface(root, command)
            record = wrapper.ensure_allocator_bench_overlay(root, command, surface)
            upgraded = bench.read_text(encoding="utf-8")

            self.assertTrue(record["prepared"], record)
            self.assertTrue(record["upgraded_existing_overlay"], record)
            self.assertEqual(
                record["overlay_upgrade"]["previous_overlay_kind"],
                "pre-versioned-system-overlay",
            )
            self.assertTrue(
                wrapper.allocator_overlay_at_crate_root(
                    upgraded,
                    wrapper.allocator_bench_overlay_text(),
                )
            )
            self.assertEqual(upgraded.count(wrapper.ALLOCATOR_BENCH_OVERLAY_MARKER), 1)
            self.assertLess(upgraded.find("#![feature(test)]"), upgraded.find(wrapper.ALLOCATOR_BENCH_OVERLAY_MARKER))
            self.assertLess(upgraded.find(wrapper.ALLOCATOR_BENCH_OVERLAY_MARKER), upgraded.find("extern crate test"))


if __name__ == "__main__":
    unittest.main()
