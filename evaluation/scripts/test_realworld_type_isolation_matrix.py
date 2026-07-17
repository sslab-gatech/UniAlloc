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
            pass_source = root / matrix.pass_closure.PASS_ENTRYPOINT_RELATIVE_PATH
            engine_source = root / matrix.pass_closure.PASS_ENGINE_RELATIVE_PATH
            lifetime_source = root / matrix.pass_closure.PASS_LIFETIME_POLICY_RELATIVE_PATH
            pass_source.parent.mkdir(parents=True)
            wrapper = root / "wrapper"
            calls: list[list[object]] = []

            def fake_execute(argv, *, cwd, env, timeout):
                command = list(argv)
                calls.append(command)
                output = pathlib.Path(command[command.index("-o") + 1])
                provenance = matrix.pass_closure.source_closure_provenance(root)
                output.write_bytes(
                    b"compiled:"
                    + str(provenance["pass_source_closure_sha256"]).encode()
                )
                return {
                    "command": [str(value) for value in command],
                    "exit_code": 0,
                    "stdout": b"",
                    "stderr": b"",
                    "wall_seconds": 0.25,
                    "timed_out": False,
                }

            pass_source.write_text(
                '#[path = "unialloc-rustc-driver-engine.rs"] mod engine;\n'
            )
            engine_source.write_text("mod lifetime_aware;\nfn first() {}\n")
            lifetime_source.write_text("fn policy() { /* first */ }\n")
            with (
                mock.patch.object(matrix, "PASS_SOURCE", pass_source),
                mock.patch.object(matrix, "execute", side_effect=fake_execute),
            ):
                matrix.ensure_wrapper(wrapper, "nightly-2026-06-11", 30)
                first = wrapper.read_bytes()

                matrix.ensure_wrapper(wrapper, "nightly-2026-06-11", 30)
                self.assertEqual(len(calls), 1)

                engine_source.write_text("mod lifetime_aware;\nfn second() {}\n")
                matrix.ensure_wrapper(wrapper, "nightly-2026-06-11", 30)
                second = wrapper.read_bytes()

                lifetime_source.write_text("fn policy() { /* second */ }\n")
                matrix.ensure_wrapper(wrapper, "nightly-2026-06-11", 30)
                third = wrapper.read_bytes()

            self.assertNotEqual(first, second)
            self.assertNotEqual(second, third)
            self.assertEqual(wrapper.read_bytes(), third)
            self.assertEqual(len(calls), 3)

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
            output_binary = (
                pathlib.Path(tmp) / "binaries" / "fd" / "typeiso_perf" / "fd"
            )
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

    def test_mimalloc_sys_is_pinned_to_audited_v3_source(self) -> None:
        dependency, features = matrix.dependency_for_variant("mimalloc")
        self.assertEqual(
            dependency,
            'mimalloc = { version = "=0.1.25", default-features = false }',
        )
        self.assertEqual(features, ())
        self.assertEqual(
            matrix.MIMALLOC_SYS_DEPENDENCY,
            'libmimalloc-sys = "=0.1.49"',
        )

    def test_mimalloc_thp_variants_share_the_same_allocator_source(self) -> None:
        dependency, features = matrix.dependency_for_variant("mimalloc_no_thp")
        self.assertEqual(
            dependency,
            'mimalloc = { version = "=0.1.25", default-features = false }',
        )
        self.assertEqual(features, ())
        self.assertEqual(
            matrix.allocator_source("mimalloc_no_thp"),
            matrix.allocator_source("mimalloc"),
        )

    def test_mimalloc_runtime_modes_override_inherited_environment(self) -> None:
        inherited = {
            "MIMALLOC_ALLOW_THP": "unexpected",
            "MIMALLOC_ALLOW_LARGE_OS_PAGES": "unexpected",
            "MIMALLOC_RESERVE_HUGE_OS_PAGES": "unexpected",
            "EXISTING": "kept",
        }
        enabled = matrix.allocator_runtime_environment(inherited, "mimalloc")
        disabled = matrix.allocator_runtime_environment(inherited, "mimalloc_no_thp")
        common = {
            "MIMALLOC_ALLOW_LARGE_OS_PAGES": "0",
            "MIMALLOC_RESERVE_HUGE_OS_PAGES": "0",
        }
        self.assertEqual(
            {name: enabled[name] for name in (*common, "MIMALLOC_ALLOW_THP")},
            {**common, "MIMALLOC_ALLOW_THP": "1"},
        )
        self.assertEqual(
            {name: disabled[name] for name in (*common, "MIMALLOC_ALLOW_THP")},
            {**common, "MIMALLOC_ALLOW_THP": "0"},
        )
        self.assertEqual(enabled["EXISTING"], "kept")
        self.assertEqual(disabled["EXISTING"], "kept")
        enabled_prefix = matrix.allocator_runtime_prefix("mimalloc", None)
        self.assertIn("MIMALLOC_ALLOW_THP=1", enabled_prefix)
        self.assertIn("MIMALLOC_ALLOW_LARGE_OS_PAGES=0", enabled_prefix)
        self.assertIn("MIMALLOC_RESERVE_HUGE_OS_PAGES=0", enabled_prefix)
        prefix = matrix.allocator_runtime_prefix("mimalloc_no_thp", None)
        self.assertEqual(prefix[0], "env")
        self.assertIn("MIMALLOC_ALLOW_THP=0", prefix)
        self.assertIn("MIMALLOC_ALLOW_LARGE_OS_PAGES=0", prefix)
        self.assertIn("MIMALLOC_RESERVE_HUGE_OS_PAGES=0", prefix)
        self.assertEqual(
            matrix.allocator_thp_mode("mimalloc_no_thp"),
            "process-wide-pr-set-thp-disable",
        )

    def test_mimalloc_no_thp_build_record_reuses_the_mimalloc_binary(self) -> None:
        base = {
            "app": "oxipng",
            "variant": "mimalloc",
            "success": True,
            "binary": "/tmp/oxipng-mimalloc",
            "binary_sha256": "same-binary",
            "runtime_environment_overrides": (
                matrix.allocator_runtime_environment_overrides("mimalloc")
            ),
            "thp_mode": matrix.allocator_thp_mode("mimalloc"),
        }
        aliased = matrix.mimalloc_no_thp_build_record(base)
        self.assertEqual(aliased["variant"], "mimalloc_no_thp")
        self.assertEqual(aliased["base_build_variant"], "mimalloc")
        self.assertEqual(aliased["binary"], base["binary"])
        self.assertEqual(aliased["binary_sha256"], base["binary_sha256"])
        self.assertEqual(
            aliased["runtime_environment_overrides"]["MIMALLOC_ALLOW_THP"],
            "0",
        )

    def test_unialloc_feature_variants_disable_defaults_and_select_one_feature(
        self,
    ) -> None:
        self.assertEqual(
            matrix.unialloc_build_evidence("unialloc"),
            {"unialloc_features": [], "default_features_enabled": True},
        )
        expected = {
            "unialloc_no_optional": (),
            "unialloc_rseq": ("rseq",),
            "unialloc_pthread_dtor": ("pthread_dtor",),
            "unialloc_hugepage": ("hugepage",),
            "unialloc_separate_sc": ("separate_sc_backend",),
            "unialloc_reclaim_checks": ("reclaim_checks",),
            "unialloc_metadata_segregation": ("metadata_segregation",),
            "unialloc_type_isolation": ("type_isolation",),
            "unialloc_pac": ("pac",),
        }
        for variant, selected_features in expected.items():
            with self.subTest(variant=variant):
                dependency, features = matrix.dependency_for_variant(variant)
                self.assertEqual(features, selected_features)
                self.assertIn("default-features = false", dependency)
                for feature in selected_features:
                    self.assertIn(f'"{feature}"', dependency)
                evidence = matrix.unialloc_build_evidence(variant)
                self.assertEqual(evidence["unialloc_features"], list(selected_features))
                self.assertFalse(evidence["default_features_enabled"])
                self.assertTrue(matrix.uses_unialloc(variant))
                self.assertIn("unialloc::UniAlloc", matrix.allocator_source(variant))

    def test_feature_and_thp_variants_are_explicit_opt_in_arguments(self) -> None:
        opt_in = (
            "mimalloc_no_thp",
            *matrix.UNIALLOC_FEATURE_VARIANTS,
        )
        for variant in opt_in:
            self.assertIn(variant, matrix.VARIANTS)
            self.assertNotIn(variant, matrix.DEFAULT_VARIANTS)
        parsed = matrix.parse_args(["--variants", ",".join(opt_in), "--apps", "oxipng"])
        self.assertEqual(parsed.variants, opt_in)

    def test_fd_is_an_explicit_diagnostic_app(self) -> None:
        self.assertIn("fd", matrix.APP_SPECS)
        self.assertNotIn("fd", matrix.DEFAULT_APPS)
        self.assertEqual(matrix.DEFAULT_APPS, matrix.parse_args([]).apps)
        self.assertEqual(("fd",), matrix.parse_args(["--apps", "fd"]).apps)

    def test_system_and_tcmalloc_are_explicit_realworld_variants(self) -> None:
        self.assertIn("system", matrix.VARIANTS)
        self.assertIn("tcmalloc", matrix.VARIANTS)
        self.assertIn("gperftools_legacy", matrix.VARIANTS)
        with self.assertRaises(matrix.MatrixError):
            matrix.dependency_for_variant("system")
        with self.assertRaises(matrix.MatrixError):
            matrix.dependency_for_variant("tcmalloc")

    def test_tcmalloc_uses_system_binary_with_preload_not_rust_wrapper(self) -> None:
        system = {
            "variant": "system",
            "success": True,
            "binary": "/tmp/rg-system",
            "binary_sha256": "same-binary",
            "original_allocator": "system",
            "allocator_route": "rust-system",
        }
        runtime = {
            "variant": "tcmalloc",
            "label": "google-tcmalloc-modern-hpaa-adaptive-subrelease",
            "library": "/tmp/libunialloc_google_tcmalloc.so",
            "library_sha256": "library-hash",
        }
        aliased = matrix.tcmalloc_build_record(system, runtime)
        self.assertEqual(aliased["variant"], "tcmalloc")
        self.assertEqual(aliased["base_build_variant"], "system")
        self.assertEqual(aliased["binary"], system["binary"])
        self.assertEqual(aliased["binary_sha256"], system["binary_sha256"])
        self.assertEqual(aliased["allocator_route"], "system-api-ld-preload")

        prefix = matrix.allocator_runtime_prefix("tcmalloc", runtime)
        self.assertEqual(
            prefix[:7],
            ["env", "-u", "HEAPPROFILE", "-u", "CPUPROFILE", "-u", "MALLOCSTATS"],
        )
        self.assertEqual(
            prefix[-1], "LD_PRELOAD=/tmp/libunialloc_google_tcmalloc.so"
        )
        self.assertEqual(matrix.allocator_runtime_prefix("unialloc", runtime), [])

    def test_tcmalloc_runtime_rejects_gperftools_lookalike(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            prefix = pathlib.Path(tmp)
            (prefix / "lib").mkdir()
            (prefix / "lib/libtcmalloc.so.4").write_bytes(b"gperftools")
            with self.assertRaisesRegex(
                matrix.MatrixError, "modern google/tcmalloc authentication failed"
            ):
                matrix.tcmalloc_runtime_evidence(prefix)

    def test_tcmalloc_preload_proof_requires_the_target_binary(self) -> None:
        with self.assertRaisesRegex(
            (matrix.MatrixError, FileNotFoundError), "target binary|No such file"
        ):
            matrix.prove_tcmalloc_preload(
                pathlib.Path("/definitely/missing-target-binary"),
                {
                    "variant": "tcmalloc",
                    "library": "/definitely/missing-modern-tcmalloc.so",
                },
                timeout=1,
            )

    def test_google_tcmalloc_marker_requires_an_exact_line(self) -> None:
        marker = matrix.GOOGLE_TCMALLOC_RUNTIME_IDENTITY_MARKER.encode()
        self.assertEqual(
            matrix.exact_identity_marker_count(
                marker, matrix.GOOGLE_TCMALLOC_RUNTIME_IDENTITY_MARKER
            ),
            1,
        )
        self.assertEqual(
            matrix.exact_identity_marker_count(
                b"prefix " + marker,
                matrix.GOOGLE_TCMALLOC_RUNTIME_IDENTITY_MARKER,
            ),
            0,
        )
        modern = matrix.google_tcmalloc_target_identity_evidence("tcmalloc", marker)
        self.assertTrue(modern["google_tcmalloc_target_identity_verified"])
        self.assertEqual(modern["google_tcmalloc_identity_marker_count"], 1)
        contaminated = matrix.google_tcmalloc_target_identity_evidence(
            "system", marker
        )
        self.assertFalse(
            contaminated["google_tcmalloc_target_identity_verified"]
        )

    def test_tcmalloc_runtime_binds_support_identity(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            prefix = pathlib.Path(tmp)
            lib_dir = prefix / "lib"
            lib_dir.mkdir()
            library = lib_dir / "libunialloc_google_tcmalloc.so"
            library.write_bytes(b"modern")
            identity = {
                "realpath": str(library.resolve()),
                "sha256": "a" * 64,
                "allocator_family": "google/tcmalloc",
            }
            with mock.patch.object(
                matrix.GOOGLE_TCMALLOC,
                "validate_library_dir",
                return_value=identity,
            ) as validate:
                runtime = matrix.tcmalloc_runtime_evidence(prefix)

        validate.assert_called_once_with(lib_dir.resolve())
        self.assertEqual(runtime["variant"], "tcmalloc")
        self.assertEqual(runtime["library"], str(library.resolve()))
        self.assertEqual(
            runtime["runtime_requirements"]["revision"],
            matrix.GOOGLE_TCMALLOC.UPSTREAM_REVISION,
        )

    def test_quick_mode_uses_one_warmup_and_three_measurements(self) -> None:
        args = matrix.parse_args(["--quick"])
        self.assertEqual(matrix.measurement_counts(args), (1, 3))

    def test_oxipng_threads_default_to_one_and_accept_positive_values(self) -> None:
        self.assertEqual(matrix.parse_args([]).oxipng_threads, 1)
        self.assertEqual(matrix.parse_args(["--oxipng-threads", "8"]).oxipng_threads, 8)
        with self.assertRaises(SystemExit):
            matrix.parse_args(["--oxipng-threads", "0"])

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
            ROOT
            / "evaluation"
            / "external"
            / "_checkouts"
            / fd.checkout
            / "Cargo.lock",
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

    def test_force_load_wrapper_only_adds_extern_to_selected_runtime_crates(
        self,
    ) -> None:
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
        self.assertIn("-Zcrate-attr=feature(custom_inner_attributes)", env["RUSTFLAGS"])
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
                '[package]\nname = "demo-lib"\nversion = "1.0.0"\n\n[dependencies]\n',
                encoding="utf-8",
            )
            lib_rs = member / "src" / "lib.rs"
            lib_rs.write_text(
                "#![allow(dead_code)]\n\npub fn answer() -> u8 { 42 }\n",
                encoding="utf-8",
            )

            patched = matrix.inject_unialloc_workspace_crates(
                checkout,
                target_crates=("demo_app", "demo_lib"),
                dependency=matrix.cargo_path_dependency(
                    ROOT / "unialloc", ("type_isolation",)
                ),
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

    def test_type_stats_parser_collects_complete_rows(self) -> None:
        stderr = (
            "diagnostic\n"
            'UNIALLOC_REALWORLD_TYPE_STATS={"type_id": 11, "cache_bypasses": 4}\n'
            "UNIALLOC_REALWORLD_TYPE_STATS={invalid}\n"
            'UNIALLOC_REALWORLD_TYPE_STATS={"type_id": 22, "cache_hits": 7}\n'
        )
        self.assertEqual(
            matrix.parse_type_stats_json(stderr),
            [
                {"type_id": 11, "cache_bypasses": 4},
                {"type_id": 22, "cache_hits": 7},
            ],
        )

    def test_depot_stats_parser_uses_last_complete_record(self) -> None:
        stderr = (
            "diagnostic\n"
            'UNIALLOC_REALWORLD_DEPOT_STATS={"cache_admission_depot_hits_events": 3}\n'
            "UNIALLOC_REALWORLD_DEPOT_STATS={invalid}\n"
            'UNIALLOC_REALWORLD_DEPOT_STATS={"cache_admission_depot_hits_events": 9}\n'
        )
        self.assertEqual(
            matrix.parse_depot_stats_json(stderr),
            {"cache_admission_depot_hits_events": 9},
        )

    def test_depot_stats_validator_requires_complete_nonnegative_counters(self) -> None:
        for metric in ("events", "rounded_bytes"):
            self.assertIn(
                f"cache_admission_depot_from_aggregate_entry_budget_{metric}",
                matrix.DEPOT_STATS_REQUIRED_FIELDS,
            )
        complete = {field: 0 for field in matrix.DEPOT_STATS_REQUIRED_FIELDS}
        matrix.validate_depot_stats(complete, "demo/typeiso_coverage")

        with self.assertRaisesRegex(matrix.MatrixError, "incomplete depot stats"):
            matrix.validate_depot_stats(
                {"cache_admission_depot_hits_events": 9},
                "demo/typeiso_coverage",
            )

        invalid = dict(complete)
        invalid["cache_admission_depot_hits_events"] = -1
        with self.assertRaisesRegex(matrix.MatrixError, "invalid depot stats"):
            matrix.validate_depot_stats(invalid, "demo/typeiso_coverage")

    def test_performance_and_coverage_instrumentation_are_separate(self) -> None:
        perf_source = matrix.allocator_source("typeiso_perf")
        plain_source = matrix.allocator_source("typed_plain")
        coverage_source = matrix.allocator_source("typeiso_coverage")
        self.assertNotIn(matrix.STATS_PREFIX, perf_source)
        self.assertNotIn(matrix.STATS_PREFIX, plain_source)
        self.assertNotIn(matrix.TYPE_STATS_PREFIX, perf_source)
        self.assertNotIn(matrix.TYPE_STATS_PREFIX, plain_source)
        self.assertNotIn(matrix.DEPOT_STATS_PREFIX, perf_source)
        self.assertNotIn(matrix.DEPOT_STATS_PREFIX, plain_source)
        self.assertIn(matrix.STATS_PREFIX, coverage_source)
        self.assertIn(matrix.TYPE_STATS_PREFIX, coverage_source)
        self.assertIn(matrix.DEPOT_STATS_PREFIX, coverage_source)
        self.assertIn("semantic_stats_recording_enable", coverage_source)
        self.assertIn("semantic_type_stats_snapshot", coverage_source)
        self.assertIn(
            "metadata_segregation_side_cache_snapshot", coverage_source
        )
        self.assertIn("type_cache_admission_stats_snapshot", coverage_source)
        for field, access in (
            ("side_cache_entries", "side_cache.occupied_entries"),
            ("side_cache_retained_bytes", "side_cache.retained_bytes"),
            (
                "metadata_segregation_side_cache_entries",
                "metadata_side_cache.occupied_entries",
            ),
            (
                "metadata_segregation_side_cache_retained_bytes",
                "metadata_side_cache.retained_bytes",
            ),
            (
                "metadata_segregation_side_cache_corrupt_buckets",
                "metadata_side_cache.corrupt_buckets",
            ),
        ):
            self.assertIn(f'\\"{field}\\"', coverage_source)
            self.assertIn(access, coverage_source)
        for outcome in (
            "admitted",
            "rejected_too_small",
            "rejected_too_large",
            "rejected_metadata_ineligible",
            "rejected_probe_window_full",
            "rejected_slot_depth",
            "rejected_slot_byte_budget",
            "rejected_aggregate_byte_budget",
            "rejected_aggregate_entry_budget",
            "rejected_structural_or_alignment",
            "rejected_segregated_capacity",
            "rejected_registry_pressure",
            "ownership_registry_full_failstop",
            "tiny_side_inserts",
            "tiny_side_hits",
        ):
            for metric in ("events", "rounded_bytes"):
                field = f"cache_admission_{outcome}_{metric}"
                self.assertIn(f'\\"{field}\\"', coverage_source)
                self.assertIn(f"admission.{outcome}.{metric}", coverage_source)
        self.assertIn('\\"cache_admission_terminal_events\\"', coverage_source)
        self.assertIn("admission.terminal_events()", coverage_source)
        for outcome in (
            "depot_attempts",
            "depot_inserts",
            "depot_rescued_overflow",
            "depot_hits",
            "depot_hits_after_recorded_l1_bypass",
            "depot_evictions",
            "depot_rejected_capacity",
            "depot_rejected_policy",
            "depot_rejected_registry_headroom",
            "depot_from_probe_window_full",
            "depot_from_slot_depth",
            "depot_from_slot_byte_budget",
            "depot_from_aggregate_byte_budget",
            "depot_from_aggregate_entry_budget",
            "depot_from_segregated_capacity",
            "depot_from_local_eviction",
        ):
            for metric in ("events", "rounded_bytes"):
                field = f"cache_admission_{outcome}_{metric}"
                self.assertIn(f'\\"{field}\\"', coverage_source)
                self.assertIn(f"admission.{outcome}.{metric}", coverage_source)
        for field in (
            "depot_current_entries",
            "depot_peak_entries",
            "depot_current_rounded_bytes",
            "depot_peak_rounded_bytes",
        ):
            self.assertIn(f'\\"cache_admission_{field}\\\"', coverage_source)
            self.assertIn(f"admission.{field}", coverage_source)
        for field in (
            "typed_cache_wrong_identity_denials",
            "last_wrong_identity_requested_type_id",
            "last_wrong_identity_retained_type_id",
            "last_wrong_identity_requested_module_id",
            "last_wrong_identity_retained_module_id",
            "last_wrong_identity_requested_callsite",
            "last_wrong_identity_size",
            "last_wrong_identity_align",
        ):
            self.assertIn(f'\\"{field}\\"', coverage_source)
            self.assertIn(f"stats.{field}", coverage_source)

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
        self.assertEqual(
            matrix.cargo_feature_args(fd, "system"),
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
                        "compiler_pass": {
                            "rewrite_applied_count": 2,
                            "direct_layout_allocator_rewrite_applied_count": 1,
                            "semantic_scope_rewrite_applied_count": 3,
                            "semantic_ownership_transfer_rewrite_applied_count": 5,
                        },
                        "summary": {"semantic_scope_drop_rewrite_applied_count": 4},
                    }
                ),
                encoding="utf-8",
            )
            audit = matrix.summarize_audits(audit_dir)
        self.assertEqual(audit["direct_allocator_rewrites_applied"], 2)
        self.assertEqual(audit["direct_layout_allocator_rewrites_applied"], 1)
        self.assertEqual(audit["semantic_ownership_transfer_rewrites_applied"], 5)
        self.assertEqual(audit["semantic_rewrites_applied"], 7)
        self.assertEqual(audit["total_compiler_rewrites_applied"], 14)
        matrix.validate_typeiso_audits(audit, ("rg",))
        matrix.validate_typeiso_coverage(
            audit, {"typed_allocations": 1, "total_allocations": 2}
        )
        with self.assertRaises(matrix.MatrixError):
            matrix.validate_typeiso_audits(audit, ("rg", "ignore"))
        with self.assertRaises(matrix.MatrixError):
            matrix.validate_typeiso_coverage(
                {"audit_file_count": 1, "semantic_rewrites_applied": 0},
                {"typed_allocations": 1, "total_allocations": 2},
            )
        with self.assertRaises(matrix.MatrixError):
            matrix.validate_typeiso_coverage(
                audit, {"typed_allocations": 0, "total_allocations": 2}
            )

    def test_structural_audit_presence_accepts_zero_rewrite_target_audit(self) -> None:
        audit = {
            "audit_file_count": 1,
            "semantic_rewrites_applied": 0,
            "total_compiler_rewrites_applied": 0,
            "crate_names": ["demo", "rsh_999_harness"],
        }

        matrix.validate_typeiso_audit_presence(audit, ("demo", "rsh-999-harness"))

        with self.assertRaises(matrix.MatrixError):
            matrix.validate_typeiso_audits(audit, ("demo",))

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
        self.assertGreaterEqual(result["user_cpu_seconds"], 0)
        self.assertGreaterEqual(result["system_cpu_seconds"], 0)
        self.assertGreaterEqual(result["minor_page_faults"], 0)
        self.assertGreaterEqual(result["voluntary_context_switches"], 0)

    def test_gnu_time_parser_requires_complete_prefixed_record(self) -> None:
        text = (
            "warning before metrics\n"
            "UNIALLOC_GNU_TIME\t1.25\t0.50\t97%\t6144\t1\t22\t3\t4\t0\n"
        )
        self.assertEqual(
            matrix.parse_gnu_time_metrics(text),
            {
                "user_cpu_seconds": 1.25,
                "system_cpu_seconds": 0.5,
                "cpu_percent": 97.0,
                "peak_rss_kib": 6144,
                "major_page_faults": 1,
                "minor_page_faults": 22,
                "involuntary_context_switches": 3,
                "voluntary_context_switches": 4,
                "gnu_time_exit_status": 0,
            },
        )
        with self.assertRaises(matrix.MatrixError):
            matrix.parse_gnu_time_metrics("6144\n")

    def test_affinity_prefix_uses_numactl_for_cpu_and_memory_binding(self) -> None:
        with mock.patch.object(matrix.shutil, "which", return_value="/usr/bin/numactl"):
            self.assertEqual(
                matrix.measurement_command_prefix("20", 0),
                ["numactl", "--physcpubind=20", "--membind=0"],
            )
        self.assertEqual(matrix.measurement_command_prefix(None, None), [])

    def test_measurement_order_rotates_variants(self) -> None:
        variants = ("native", "jemalloc", "mimalloc")
        self.assertEqual(matrix.rotated_order(variants, 0), variants)
        self.assertEqual(
            matrix.rotated_order(variants, 1), ("jemalloc", "mimalloc", "native")
        )

    def test_summary_exposes_isolation_cost_against_typed_plain(self) -> None:
        common = {
            "app": "ripgrep",
            "warmup": False,
            "peak_rss_kib": 100,
            "output_sha256": "same",
            "stats": None,
            "user_cpu_seconds": 0.5,
            "system_cpu_seconds": 0.25,
            "cpu_percent": 75.0,
            "major_page_faults": 0,
            "minor_page_faults": 10,
            "involuntary_context_switches": 1,
            "voluntary_context_switches": 2,
            "work_amount": 1024,
            "work_unit": "bytes",
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
        self.assertEqual(isolated["median_total_cpu_seconds"], 0.75)
        self.assertEqual(isolated["throughput_unit"], "MiB/s")

    def test_summary_uses_system_baseline_and_paired_comparator_ratios(self) -> None:
        rows = []
        for round_index, (system_wall, mimalloc_wall, unialloc_wall) in enumerate(
            ((2.0, 1.5, 1.8), (2.2, 1.6, 2.0), (1.8, 1.4, 1.6))
        ):
            for variant, wall, rss in (
                ("system", system_wall, 100),
                ("mimalloc", mimalloc_wall, 120),
                ("unialloc", unialloc_wall, 90),
            ):
                rows.append(
                    {
                        "app": "ripgrep",
                        "variant": variant,
                        "round": round_index,
                        "measurement_index": round_index,
                        "warmup": False,
                        "wall_seconds": wall,
                        "peak_rss_kib": rss,
                        "output_sha256": "same",
                        "stats": None,
                        "user_cpu_seconds": wall * 0.75,
                        "system_cpu_seconds": wall * 0.25,
                        "cpu_percent": 100.0,
                        "major_page_faults": 0,
                        "minor_page_faults": 10,
                        "involuntary_context_switches": 0,
                        "voluntary_context_switches": 1,
                        "work_amount": 1024 * 1024,
                        "work_unit": "bytes",
                    }
                )
        summaries = matrix.summarize_measurements(rows)
        unialloc = next(row for row in summaries if row["variant"] == "unialloc")
        self.assertEqual(unialloc["baseline_variant"], "system")
        self.assertAlmostEqual(unialloc["wall_ratio_vs_system"], 0.9)
        self.assertAlmostEqual(
            unialloc["paired_wall_ratio_vs_mimalloc"],
            1.2,
        )
        self.assertAlmostEqual(unialloc["paired_rss_ratio_vs_mimalloc"], 0.75)

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
                    path_repetitions=3,
                )
                self.assertIn("--no-ignore", command)
                self.assertIsNone(output)
                self.assertEqual(command.count("."), 3)

    def test_oxipng_workload_uses_requested_thread_count(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp)
            command, cwd, output = matrix.workload_command(
                matrix.APP_SPECS["oxipng"],
                root / "oxipng",
                corpus=root,
                source_checkout=root,
                run_dir=root,
                quick=True,
                oxipng_threads=8,
            )
        self.assertEqual(command[command.index("--threads") + 1], "8")
        self.assertEqual(cwd, root)
        self.assertEqual(output, root / "output.png")

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
