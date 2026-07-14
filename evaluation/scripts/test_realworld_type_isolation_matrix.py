#!/usr/bin/env python3
"""Unit tests for the real-world Type Isolation allocator matrix."""

from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import pathlib
import subprocess
import sys
import tempfile
import unittest
from unittest import mock


ROOT = pathlib.Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "evaluation" / "scripts" / "realworld_type_isolation_matrix.py"

spec = importlib.util.spec_from_file_location("realworld_type_isolation_matrix", SCRIPT)
assert spec is not None and spec.loader is not None
matrix = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = matrix
spec.loader.exec_module(matrix)


class RealWorldTypeIsolationMatrixTests(unittest.TestCase):
    def test_compiler_wrapper_rebuilds_when_pass_source_changes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp)
            pass_source = root / "pass.rs"
            wrapper = root / "wrapper"
            calls: list[list[object]] = []

            def fake_execute(argv, *, cwd, env, timeout):
                command = list(argv)
                calls.append(command)
                output = pathlib.Path(command[command.index("-o") + 1])
                output.write_bytes(b"compiled:" + pass_source.read_bytes())
                return {
                    "command": [str(value) for value in command],
                    "exit_code": 0,
                    "stdout": b"",
                    "stderr": b"",
                    "wall_seconds": 0.25,
                    "timed_out": False,
                }

            pass_source.write_bytes(b"first")
            with (
                mock.patch.object(matrix, "PASS_SOURCE", pass_source),
                mock.patch.object(matrix, "execute", side_effect=fake_execute),
            ):
                matrix.ensure_wrapper(wrapper, "nightly-2026-06-11", 30)
                self.assertEqual(wrapper.read_bytes(), b"compiled:first")

                matrix.ensure_wrapper(wrapper, "nightly-2026-06-11", 30)
                self.assertEqual(len(calls), 1)

                pass_source.write_bytes(b"second")
                matrix.ensure_wrapper(wrapper, "nightly-2026-06-11", 30)

            self.assertEqual(wrapper.read_bytes(), b"compiled:second")
            self.assertEqual(len(calls), 2)

    def test_typeiso_rebuild_discards_target_metadata_from_old_force_load_rlib(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp)
            typed_target = root / "typed"
            native_target = root / "native"
            typed_target.mkdir()
            native_target.mkdir()
            (typed_target / "stale.rlib").write_bytes(b"old force-load dependency")
            (native_target / "cached.rlib").write_bytes(b"allocator-independent")

            matrix.reset_target_dir_before_rebuild(typed_target, "typed_plain")
            matrix.reset_target_dir_before_rebuild(native_target, "native")

            self.assertFalse(typed_target.exists())
            self.assertTrue((native_target / "cached.rlib").is_file())

    def test_binary_reuse_requires_exact_build_inputs(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            output_binary = pathlib.Path(tmp) / "binaries" / "fd" / "typeiso_perf" / "fd"
            output_binary.parent.mkdir(parents=True)
            output_binary.write_bytes(b"recorded binary")
            binary_sha256 = matrix.sha256_file(output_binary)
            force_load = {
                "rlib_sha256": "current rlib",
                "features": ["type_isolation"],
            }
            record = {
                "success": True,
                "toolchain": "nightly-2026-06-11",
                "app": "fd",
                "variant": "typeiso_perf",
                "source_head": matrix.APP_SPECS["fd"].head,
                "implementation_sha256": "current implementation",
                "compiler_wrapper_sha256": "current compiler wrapper",
                "force_load_wrapper_sha256": "current force-load wrapper",
                "force_load": dict(force_load),
                "binary": str(output_binary.resolve()),
                "binary_sha256": binary_sha256,
            }
            arguments = {
                "spec": matrix.APP_SPECS["fd"],
                "variant": "typeiso_perf",
                "output_binary": output_binary,
                "toolchain": "nightly-2026-06-11",
                "implementation_sha256": "current implementation",
                "compiler_wrapper_sha256": "current compiler wrapper",
                "force_load_wrapper_sha256": "current force-load wrapper",
                "force_load": force_load,
            }

            self.assertTrue(matrix.is_reusable_binary_record(record, **arguments))
            for field, stale_value in (
                ("toolchain", "nightly-2026-05-01"),
                ("app", "ripgrep"),
                ("variant", "typed_plain"),
                ("source_head", "stale source"),
                ("implementation_sha256", "stale implementation"),
                ("compiler_wrapper_sha256", "stale compiler wrapper"),
                ("force_load_wrapper_sha256", "stale force-load wrapper"),
                ("binary", str(output_binary.with_name("other").resolve())),
                ("binary_sha256", "stale binary"),
            ):
                with self.subTest(field=field):
                    stale_record = dict(record)
                    stale_record[field] = stale_value
                    self.assertFalse(
                        matrix.is_reusable_binary_record(stale_record, **arguments)
                    )

            for field, stale_value in (
                ("rlib_sha256", "stale rlib"),
                ("features", ["stats", "type_isolation"]),
            ):
                with self.subTest(force_load_field=field):
                    stale_record = dict(record)
                    stale_force_load = dict(force_load)
                    stale_force_load[field] = stale_value
                    stale_record["force_load"] = stale_force_load
                    self.assertFalse(
                        matrix.is_reusable_binary_record(stale_record, **arguments)
                    )

    def test_binary_reuse_rejects_changed_on_disk_binary(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            output_binary = pathlib.Path(tmp) / "rg"
            output_binary.write_bytes(b"recorded binary")
            record = {
                "success": True,
                "toolchain": "nightly-2026-06-11",
                "app": "ripgrep",
                "variant": "native",
                "source_head": matrix.APP_SPECS["ripgrep"].head,
                "implementation_sha256": "current implementation",
                "compiler_wrapper_sha256": None,
                "force_load_wrapper_sha256": None,
                "binary": str(output_binary.resolve()),
                "binary_sha256": matrix.sha256_file(output_binary),
            }
            output_binary.write_bytes(b"changed binary")

            self.assertFalse(
                matrix.is_reusable_binary_record(
                    record,
                    spec=matrix.APP_SPECS["ripgrep"],
                    variant="native",
                    output_binary=output_binary,
                    toolchain="nightly-2026-06-11",
                    implementation_sha256="current implementation",
                    compiler_wrapper_sha256=None,
                    force_load_wrapper_sha256=None,
                    force_load=None,
                )
            )

    def test_injected_jemalloc_matches_realworld_lock_version(self) -> None:
        dependency, features = matrix.dependency_for_variant("jemalloc")
        self.assertEqual(dependency, 'jemallocator = "=0.5.4"')
        self.assertEqual(features, ())

    def test_quick_mode_uses_one_warmup_and_three_measurements(self) -> None:
        args = matrix.parse_args(["--quick"])
        self.assertEqual(matrix.measurement_counts(args), (1, 3))

    def test_runtime_keeps_libc_rseq_by_default_and_disables_it_only_on_request(
        self,
    ) -> None:
        default_args = matrix.parse_args([])
        disabled_args = matrix.parse_args(["--disable-glibc-rseq"])
        self.assertFalse(default_args.disable_glibc_rseq)
        self.assertTrue(disabled_args.disable_glibc_rseq)

        inherited = {"GLIBC_TUNABLES": "glibc.malloc.tcache_count=16"}
        self.assertEqual(
            matrix.runtime_environment(inherited, disable_glibc_rseq=False),
            inherited,
        )
        disabled = matrix.runtime_environment(inherited, disable_glibc_rseq=True)
        self.assertEqual(
            disabled["GLIBC_TUNABLES"],
            "glibc.malloc.tcache_count=16:glibc.pthread.rseq=0",
        )

    def test_typeiso_environment_enables_actual_rewrite(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp)
            wrapper = root / "wrapper"
            wrapper.write_text("wrapper", encoding="utf-8")
            env = matrix.typeiso_environment(
                {"LD_LIBRARY_PATH": "/existing"},
                wrapper=wrapper,
                audit_dir=root / "audits",
                pass_log_dir=root / "logs",
                sysroot=root / "sysroot",
                target_crates=("rg", "grep_cli"),
            )
        self.assertEqual(env["RUSTC_WRAPPER"], str(wrapper.resolve()))
        self.assertEqual(env["UNIALLOC_RUSTC_TARGET_CRATES"], "rg,grep_cli")
        self.assertEqual(env["UNIALLOC_ACTUAL_MIR_REWRITE"], "1")
        self.assertEqual(env["UNIALLOC_ACTUAL_SEMANTIC_SCOPE_REWRITE"], "1")
        self.assertEqual(env["UNIALLOC_LOWERING_POLICY_FLAGS"], "1")
        self.assertTrue(env["LD_LIBRARY_PATH"].endswith(":/existing"))

    def test_fd_force_load_targets_are_runtime_packages_locked_by_source(self) -> None:
        fd = matrix.APP_SPECS["fd"]
        self.assertEqual(
            fd.target_crates,
            ("fd", "ignore", "walkdir", "same_file", "globset"),
        )
        self.assertEqual(
            fd.force_load_lock_packages,
            ("fd-find", "ignore", "walkdir", "same-file", "globset"),
        )
        matrix.validate_force_load_lock_targets(
            fd,
            ROOT / "evaluation" / "external" / "_checkouts" / fd.checkout / "Cargo.lock",
        )

    def test_force_load_features_match_each_typeiso_variant(self) -> None:
        self.assertEqual(
            matrix.force_load_features_for_variant("typed_plain"),
            ("type_isolation",),
        )
        self.assertEqual(
            matrix.force_load_features_for_variant("typeiso_perf"),
            ("type_isolation",),
        )
        self.assertEqual(
            matrix.force_load_features_for_variant("typeiso_coverage"),
            ("stats", "type_isolation"),
        )
        with self.assertRaises(matrix.MatrixError):
            matrix.force_load_features_for_variant("unialloc")

    def test_force_load_wrapper_only_adds_extern_to_selected_runtime_crates(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp)
            wrapper = matrix.ensure_force_load_wrapper(root / "force-wrapper")
            capture = root / "argv.json"
            driver = root / "driver.py"
            driver.write_text(
                "#!/usr/bin/env python3\n"
                "import json, os, pathlib, sys\n"
                "pathlib.Path(os.environ['CAPTURE']).write_text(json.dumps(sys.argv[1:]))\n",
                encoding="utf-8",
            )
            driver.chmod(0o755)
            rlib = root / "deps" / "libunialloc-test.rlib"
            rlib.parent.mkdir()
            rlib.write_bytes(b"rlib")
            env = {
                **os.environ,
                "CAPTURE": str(capture),
                "UNIALLOC_FORCE_LOAD_DRIVER": str(driver),
                "UNIALLOC_FORCE_LOAD_RLIB": str(rlib),
                "UNIALLOC_FORCE_LOAD_DEPENDENCY_DIR": str(rlib.parent),
                "UNIALLOC_RUSTC_TARGET_CRATES": "fd,ignore,walkdir",
            }
            subprocess.run(
                [str(wrapper), "/fake/rustc", "--crate-name", "ignore", "input.rs"],
                env=env,
                check=True,
            )
            selected = json.loads(capture.read_text(encoding="utf-8"))
            self.assertIn("-Z", selected)
            self.assertIn("unstable-options", selected)
            self.assertIn(f"force:unialloc={rlib}", selected)
            self.assertIn(f"dependency={rlib.parent}", selected)

            subprocess.run(
                [
                    str(wrapper),
                    "/fake/rustc",
                    "--crate-name",
                    "build_script_build",
                    "build.rs",
                ],
                env=env,
                check=True,
            )
            unselected = json.loads(capture.read_text(encoding="utf-8"))
            self.assertNotIn("unstable-options", unselected)
            self.assertFalse(any("unialloc=" in value for value in unselected))

            duplicate = subprocess.run(
                [
                    str(wrapper),
                    "/fake/rustc",
                    "--crate-name",
                    "ignore",
                    "--extern",
                    "unialloc=/tmp/other.rlib",
                    "input.rs",
                ],
                env=env,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=False,
            )
            self.assertEqual(duplicate.returncode, 2)
            self.assertIn(b"already has a UniAlloc --extern entry", duplicate.stderr)

    def test_force_load_rlib_build_records_unwind_features_and_hash(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            raw_dir = pathlib.Path(tmp)
            calls: list[dict[str, object]] = []

            def fake_execute(argv, *, cwd, env, timeout):
                calls.append({"argv": list(argv), "cwd": cwd, "env": dict(env)})
                target_index = list(argv).index("--target-dir") + 1
                target_dir = pathlib.Path(argv[target_index])
                rlib = target_dir / "release" / "deps" / "libunialloc-test.rlib"
                rlib.parent.mkdir(parents=True)
                rlib.write_bytes(b"variant-matched-unialloc")
                return {
                    "command": [str(value) for value in argv],
                    "exit_code": 0,
                    "stdout": b"",
                    "stderr": b"",
                    "wall_seconds": 0.25,
                    "timed_out": False,
                }

            with mock.patch.object(matrix, "execute", side_effect=fake_execute):
                evidence = matrix.build_force_load_rlib(
                    "typeiso_coverage",
                    raw_dir=raw_dir,
                    toolchain="nightly-2026-06-11",
                    jobs=2,
                    timeout=30,
                )
            self.assertEqual(evidence["features"], ["stats", "type_isolation"])
            self.assertEqual(evidence["panic_strategy"], "unwind")
            self.assertEqual(evidence["mode"], "selected-crate-rustc-force-extern")
            self.assertEqual(
                evidence["rlib_sha256"],
                hashlib.sha256(b"variant-matched-unialloc").hexdigest(),
            )
            self.assertEqual(
                calls[0]["env"]["CARGO_PROFILE_RELEASE_PANIC"],
                "unwind",
            )

    def test_force_load_environment_points_wrapper_at_selective_shim(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp)
            driver = root / "driver"
            rlib = root / "deps" / "libunialloc.rlib"
            rlib.parent.mkdir()
            driver.write_text("driver", encoding="utf-8")
            rlib.write_bytes(b"rlib")
            env = matrix.force_load_environment(
                {"RUSTC_WRAPPER": str(root / "shim")},
                driver_wrapper=driver,
                force_load={
                    "rlib": str(rlib),
                    "dependency_dir": str(rlib.parent),
                },
            )
        self.assertEqual(env["UNIALLOC_FORCE_LOAD_DRIVER"], str(driver.resolve()))
        self.assertEqual(env["UNIALLOC_FORCE_LOAD_RLIB"], str(rlib.resolve()))
        self.assertEqual(
            env["UNIALLOC_FORCE_LOAD_DEPENDENCY_DIR"], str(rlib.parent.resolve())
        )

    def test_typed_plain_environment_differs_only_by_policy_flags(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp)
            common = dict(
                wrapper=root / "wrapper",
                audit_dir=root / "audits",
                pass_log_dir=root / "logs",
                sysroot=root / "sysroot",
                target_crates=("rg",),
            )
            plain = matrix.typeiso_environment({}, policy_flags=0, **common)
            isolated = matrix.typeiso_environment({}, policy_flags=1, **common)
        self.assertEqual(plain["UNIALLOC_LOWERING_POLICY_FLAGS"], "0")
        self.assertEqual(isolated["UNIALLOC_LOWERING_POLICY_FLAGS"], "1")
        plain.pop("UNIALLOC_LOWERING_POLICY_FLAGS")
        isolated.pop("UNIALLOC_LOWERING_POLICY_FLAGS")
        self.assertEqual(plain, isolated)

    def test_oxipng_compatibility_preserves_existing_rustflags(self) -> None:
        env, record = matrix.workload_compatibility_environment(
            matrix.APP_SPECS["oxipng"],
            {"RUSTFLAGS": "-Ctarget-cpu=native", "EXISTING": "kept"},
        )
        self.assertEqual(env["RUSTC_BOOTSTRAP"], "1")
        self.assertIn("-Ctarget-cpu=native", env["RUSTFLAGS"])
        self.assertIn(
            "-Zcrate-attr=feature(custom_inner_attributes)", env["RUSTFLAGS"]
        )
        self.assertEqual(env["EXISTING"], "kept")
        self.assertEqual(
            record["rustflags_added"],
            ["-Zcrate-attr=feature(custom_inner_attributes)"],
        )

    def test_selected_workspace_crates_receive_dependency_and_force_load(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            checkout = pathlib.Path(tmp)
            root_manifest = checkout / "Cargo.toml"
            root_manifest.write_text(
                '[package]\nname = "demo-app"\nversion = "1.0.0"\n\n'
                '[dependencies]\nanyhow = "1"\n',
                encoding="utf-8",
            )
            member = checkout / "crates" / "demo-lib"
            (member / "src").mkdir(parents=True)
            member_manifest = member / "Cargo.toml"
            member_manifest.write_text(
                '[package]\nname = "demo-lib"\nversion = "1.0.0"\n\n'
                '[dependencies]\n',
                encoding="utf-8",
            )
            lib_rs = member / "src" / "lib.rs"
            lib_rs.write_text("#![allow(dead_code)]\n\npub fn answer() -> u8 { 42 }\n", encoding="utf-8")

            patched = matrix.inject_unialloc_workspace_crates(
                checkout,
                target_crates=("demo_app", "demo_lib"),
                dependency=matrix.cargo_path_dependency(ROOT / "unialloc", ("type_isolation",)),
            )

            self.assertEqual(
                {path.relative_to(checkout).as_posix() for path in patched},
                {"Cargo.toml", "crates/demo-lib/Cargo.toml"},
            )
            self.assertIn("unialloc =", root_manifest.read_text(encoding="utf-8"))
            self.assertIn("unialloc =", member_manifest.read_text(encoding="utf-8"))
            source = lib_rs.read_text(encoding="utf-8")
            self.assertTrue(source.startswith("#![allow(dead_code)]"))
            self.assertTrue(source.rstrip().endswith("extern crate unialloc;"))

    def test_single_package_copy_is_detached_from_parent_workspace(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            manifest = pathlib.Path(tmp) / "Cargo.toml"
            manifest.write_text(
                '[package]\nname = "standalone"\nversion = "1.0.0"\n',
                encoding="utf-8",
            )
            matrix.ensure_standalone_workspace(manifest)
            matrix.ensure_standalone_workspace(manifest)
            text = manifest.read_text(encoding="utf-8")
        self.assertEqual(text.count("[workspace]"), 1)

    def test_stats_parser_uses_last_complete_record(self) -> None:
        stderr = (
            "diagnostic\n"
            'UNIALLOC_REALWORLD_STATS={"typed_allocations": 2, "total_allocations": 5}\n'
            'UNIALLOC_REALWORLD_STATS={"typed_allocations": 7, "total_allocations": 9}\n'
        )
        self.assertEqual(
            matrix.parse_stats_json(stderr),
            {"typed_allocations": 7, "total_allocations": 9},
        )

    def test_performance_and_coverage_instrumentation_are_separate(self) -> None:
        perf_source = matrix.allocator_source("typeiso_perf")
        plain_source = matrix.allocator_source("typed_plain")
        coverage_source = matrix.allocator_source("typeiso_coverage")
        self.assertNotIn(matrix.STATS_PREFIX, perf_source)
        self.assertNotIn(matrix.STATS_PREFIX, plain_source)
        self.assertIn(matrix.STATS_PREFIX, coverage_source)
        self.assertIn("semantic_stats_recording_enable", coverage_source)

    def test_fd_jemalloc_routes_through_original_feature(self) -> None:
        fd = matrix.APP_SPECS["fd"]
        self.assertTrue(matrix.uses_original_jemalloc(fd, "jemalloc"))
        self.assertEqual(
            matrix.cargo_feature_args(fd, "jemalloc"),
            ["--no-default-features", "--features", "use-jemalloc,completions"],
        )
        self.assertEqual(matrix.cargo_feature_args(fd, "native"), [])
        self.assertEqual(
            matrix.cargo_feature_args(fd, "mimalloc"),
            ["--no-default-features", "--features", "completions"],
        )

    def test_typed_plain_uses_unialloc_dependency_workarounds(self) -> None:
        self.assertTrue(matrix.uses_unialloc("typed_plain"))
        self.assertTrue(matrix.uses_unialloc("typeiso_perf"))
        self.assertTrue(matrix.uses_unialloc("typeiso_coverage"))
        self.assertTrue(matrix.uses_unialloc("unialloc"))
        self.assertFalse(matrix.uses_unialloc("native"))

    def test_audit_and_runtime_coverage_fail_closed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            audit_dir = pathlib.Path(tmp)
            (audit_dir / "rg.json").write_text(
                json.dumps(
                    {
                        "rustc_args": ["rustc", "--crate-name", "rg"],
                        "compiler_pass": {"semantic_scope_rewrite_applied_count": 3},
                        "summary": {"semantic_scope_drop_rewrite_applied_count": 4},
                    }
                ),
                encoding="utf-8",
            )
            audit = matrix.summarize_audits(audit_dir)
        self.assertEqual(audit["semantic_rewrites_applied"], 7)
        matrix.validate_typeiso_audits(audit, ("rg",))
        matrix.validate_typeiso_coverage(audit, {"typed_allocations": 1, "total_allocations": 2})
        with self.assertRaises(matrix.MatrixError):
            matrix.validate_typeiso_audits(audit, ("rg", "ignore"))
        with self.assertRaises(matrix.MatrixError):
            matrix.validate_typeiso_coverage(
                {"audit_file_count": 1, "semantic_rewrites_applied": 0},
                {"typed_allocations": 1, "total_allocations": 2},
            )
        with self.assertRaises(matrix.MatrixError):
            matrix.validate_typeiso_coverage(audit, {"typed_allocations": 0, "total_allocations": 2})

    def test_measured_run_captures_wall_rss_and_output_hash(self) -> None:
        if not pathlib.Path("/usr/bin/time").exists():
            self.skipTest("GNU time is unavailable")
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp)
            result = matrix.run_measured(
                [sys.executable, "-c", "print('stable-output')"],
                cwd=root,
                env=os.environ.copy(),
                time_binary=pathlib.Path("/usr/bin/time"),
                rss_path=root / "rss.txt",
                timeout=30,
            )
        expected = hashlib.sha256(b"stable-output\n").hexdigest()
        self.assertEqual(result["exit_code"], 0)
        self.assertEqual(result["stdout_sha256"], expected)
        self.assertGreater(result["wall_seconds"], 0)
        self.assertGreater(result["peak_rss_kib"], 0)

    def test_measurement_order_rotates_variants(self) -> None:
        variants = ("native", "jemalloc", "mimalloc")
        self.assertEqual(matrix.rotated_order(variants, 0), variants)
        self.assertEqual(matrix.rotated_order(variants, 1), ("jemalloc", "mimalloc", "native"))

    def test_summary_exposes_isolation_cost_against_typed_plain(self) -> None:
        common = {
            "app": "ripgrep",
            "warmup": False,
            "peak_rss_kib": 100,
            "output_sha256": "same",
            "stats": None,
        }
        summaries = matrix.summarize_measurements(
            [
                {**common, "variant": "native", "wall_seconds": 1.0},
                {**common, "variant": "typed_plain", "wall_seconds": 2.0},
                {
                    **common,
                    "variant": "typeiso_perf",
                    "wall_seconds": 2.2,
                    "peak_rss_kib": 110,
                },
            ]
        )
        isolated = next(row for row in summaries if row["variant"] == "typeiso_perf")
        self.assertAlmostEqual(isolated["wall_ratio_vs_typed_plain"], 1.1)
        self.assertAlmostEqual(isolated["rss_ratio_vs_typed_plain"], 1.1)

    def test_search_workloads_include_ignored_raw_corpus(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp)
            for name in ("ripgrep", "fd"):
                command, _cwd, output = matrix.workload_command(
                    matrix.APP_SPECS[name],
                    root / matrix.APP_SPECS[name].binary,
                    corpus=root,
                    source_checkout=root,
                    run_dir=root,
                    quick=True,
                )
                self.assertIn("--no-ignore", command)
                self.assertIsNone(output)

    def test_fd_metadata_tree_shape_and_digest_are_deterministic(self) -> None:
        self.assertEqual(matrix.fd_tree_file_count(True), 20_480)
        self.assertEqual(matrix.fd_tree_file_count(False), 204_800)
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp)
            first = matrix.prepare_metadata_tree(root / "first", file_count=17)
            second = matrix.prepare_metadata_tree(root / "second", file_count=17)
        self.assertEqual(first["file_count"], 17)
        self.assertEqual(first["file_bytes"], 0)
        self.assertEqual(first["tree_sha256"], second["tree_sha256"])


if __name__ == "__main__":
    unittest.main()
