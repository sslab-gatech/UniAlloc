#!/usr/bin/env python3
"""Focused process-level regression tests for the MIR wrapper crate allowlist."""

from __future__ import annotations

import json
import os
import pathlib
import shutil
import signal
import subprocess
import tempfile
import unittest


ROOT = pathlib.Path(__file__).resolve().parents[2]
PASS_SOURCE = ROOT / "tools" / "unialloc-rustc-pass" / "unialloc-rustc-mir-rewrite-dry-run.rs"
TOOLCHAIN = (ROOT / "rust-toolchain").read_text(encoding="utf-8").strip()


class MirWrapperTargetAllowlistTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        temp_root = "/tmp" if pathlib.Path("/tmp").is_dir() else None
        cls._tmp = tempfile.TemporaryDirectory(
            prefix="unialloc-wrapper-allowlist-", dir=temp_root
        )
        cls.tmp = pathlib.Path(cls._tmp.name)
        rustc = shutil.which("rustc") or "rustc"
        cls.sysroot = pathlib.Path(
            subprocess.check_output(
                [rustc, f"+{TOOLCHAIN}", "--print", "sysroot"],
                cwd=ROOT,
                text=True,
            ).strip()
        )
        cls.rustc = cls.sysroot / "bin" / "rustc"
        cls.wrapper = cls.tmp / "unialloc-rustc-mir-rewrite-dry-run"
        env = os.environ.copy()
        env["RUSTC_BOOTSTRAP"] = "1"
        subprocess.run(
            [
                rustc,
                f"+{TOOLCHAIN}",
                "--cfg",
                "unialloc_rustc_current",
                str(PASS_SOURCE),
                "-o",
                str(cls.wrapper),
            ],
            cwd=ROOT,
            env=env,
            check=True,
        )

    @classmethod
    def tearDownClass(cls) -> None:
        cls._tmp.cleanup()

    def wrapper_env(self) -> dict[str, str]:
        env = os.environ.copy()
        library_path = str(self.sysroot / "lib")
        env["DYLD_LIBRARY_PATH"] = library_path
        env["LD_LIBRARY_PATH"] = library_path
        return env

    def write_fixture(self, name: str) -> pathlib.Path:
        source = self.tmp / f"{name}.rs"
        source.write_text(
            "fn main() { let mut values = Vec::new(); values.push(7_u64); "
            "assert_eq!(values[0], 7); }\n",
            encoding="utf-8",
        )
        return source

    def write_fake_compiler(self, name: str = "compiler-driver") -> pathlib.Path:
        compiler = self.tmp / name
        compiler.write_text(
            "#!/usr/bin/env python3\n"
            "import json, os, pathlib, signal, sys\n"
            "pathlib.Path(os.environ['CAPTURE_ARGS']).write_text(json.dumps(sys.argv[1:]))\n"
            "signal_name = os.environ.get('FAKE_COMPILER_SIGNAL')\n"
            "if signal_name:\n"
            "    os.kill(os.getpid(), getattr(signal, signal_name))\n"
            "raise SystemExit(int(os.environ['FAKE_COMPILER_EXIT']))\n",
            encoding="utf-8",
        )
        compiler.chmod(0o755)
        return compiler

    def write_delegating_compiler(self, directory: pathlib.Path) -> pathlib.Path:
        directory.mkdir(parents=True, exist_ok=True)
        compiler = directory / "compiler-driver"
        compiler.write_text(
            "#!/usr/bin/env python3\n"
            "import json, os, sys\n"
            f"real_rustc = {str(self.rustc)!r}\n"
            "with open(os.environ['COMPILER_DRIVER_LOG'], 'a', encoding='utf-8') as log:\n"
            "    log.write(json.dumps(sys.argv[1:]) + '\\n')\n"
            "os.execv(real_rustc, [real_rustc, *sys.argv[1:]])\n",
            encoding="utf-8",
        )
        compiler.chmod(0o755)
        return compiler

    @unittest.skipUnless(os.name == "posix", "real bare-name Cargo compiler shim")
    def test_cargo_wrapper_resolves_bare_rustc_from_path(self) -> None:
        compiler_dir = self.tmp / "cargo-compiler-path"
        self.write_delegating_compiler(compiler_dir)
        compiler_log = self.tmp / "cargo-compiler.jsonl"
        project = self.tmp / "cargo-fixture"
        dependency = project / "dependency"
        (project / "src").mkdir(parents=True)
        (dependency / "src").mkdir(parents=True)
        unialloc_path = (ROOT / "unialloc").as_posix()
        (project / "Cargo.toml").write_text(
            "[package]\nname = 'cargo-fixture'\nversion = '0.1.0'\nedition = '2021'\n"
            "\n[dependencies]\n"
            f"unialloc = {{ path = {unialloc_path!r}, features = ['stats', 'type_isolation'] }}\n"
            "fixture-dependency = { path = 'dependency' }\n"
            "\n[workspace]\nmembers = ['dependency']\nresolver = '2'\n",
            encoding="utf-8",
        )
        (dependency / "Cargo.toml").write_text(
            "[package]\nname = 'fixture-dependency'\nversion = '0.1.0'\nedition = '2021'\n"
            "\n[lib]\npath = 'src/lib.rs'\n",
            encoding="utf-8",
        )
        (dependency / "src" / "lib.rs").write_text(
            "#[inline(never)]\n"
            "pub fn dependency_value() -> u64 {\n"
            "    let mut values = Vec::with_capacity(1);\n"
            "    values.push(11_u64);\n"
            "    values[0]\n"
            "}\n",
            encoding="utf-8",
        )
        # Preserve the repository's intentionally pinned, yanked dependencies in
        # this detached offline workspace. Cargo accepts yanked versions through
        # an existing lockfile, while fresh offline resolution rejects them.
        shutil.copy2(ROOT / "Cargo.lock", project / "Cargo.lock")
        (project / "src" / "main.rs").write_text(
            "use fixture_dependency::dependency_value;\n"
            "use unialloc::UniAlloc;\n"
            "#[global_allocator]\nstatic A: UniAlloc = UniAlloc;\n"
            "#[inline(never)]\n"
            "fn selected_value() -> u64 {\n"
            "    let mut values = Vec::with_capacity(1);\n"
            "    values.push(7_u64);\n"
            "    values[0]\n"
            "}\n"
            "fn main() {\n"
            "    let target = selected_value();\n"
            "    let dependency = dependency_value();\n"
            "    println!(\"target={} dependency={} sum={}\", target, dependency, target + dependency);\n"
            "}\n",
            encoding="utf-8",
        )
        audits = self.tmp / "cargo-audits"
        audits.mkdir()
        logs = self.tmp / "cargo-pass-logs"
        logs.mkdir()
        env = self.wrapper_env()
        env.update(
            {
                "PATH": f"{compiler_dir}{os.pathsep}{env.get('PATH', '')}",
                "RUSTC": "compiler-driver",
                "RUSTC_WRAPPER": str(self.wrapper),
                "UNIALLOC_RUSTC_TARGET_CRATES": "cargo-fixture",
                "UNIALLOC_REWRITE_AUDIT_DIR": str(audits),
                "UNIALLOC_PASS_LOG_DIR": str(logs),
                "UNIALLOC_CONTINUE_COMPILATION": "1",
                "UNIALLOC_ACTUAL_SEMANTIC_SCOPE_REWRITE": "1",
                "UNIALLOC_LOWERING_POLICY_FLAGS": "1",
                "UNIALLOC_RUSTC_SYSROOT": str(self.sysroot),
                "COMPILER_DRIVER_LOG": str(compiler_log),
                "CARGO_TARGET_DIR": str(self.tmp / "cargo-target"),
                "CARGO_NET_OFFLINE": "true",
                "CARGO_INCREMENTAL": "0",
            }
        )

        result = subprocess.run(
            [
                shutil.which("cargo") or "cargo",
                f"+{TOOLCHAIN}",
                "run",
                "--quiet",
                "--manifest-path",
                str(project / "Cargo.toml"),
                "-p",
                "cargo-fixture",
            ],
            cwd=project,
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), "target=7 dependency=11 sum=18")
        self.assertTrue(compiler_log.is_file())
        compiler_invocations = [
            json.loads(line)
            for line in compiler_log.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
        invoked_crates = set()
        for invocation in compiler_invocations:
            for index, argument in enumerate(invocation):
                if argument == "--crate-name" and index + 1 < len(invocation):
                    invoked_crates.add(invocation[index + 1])
                elif argument.startswith("--crate-name="):
                    invoked_crates.add(argument.split("=", 1)[1])
        self.assertIn("fixture_dependency", invoked_crates)
        # The selected crate is compiled inside the rustc_driver wrapper, so it
        # must not be delegated through the compiler shim.  Its unique audit
        # and pass log below are the selected-crate execution evidence.
        self.assertNotIn("cargo_fixture", invoked_crates)
        audit_payloads = [
            json.loads(path.read_text(encoding="utf-8")) for path in audits.glob("*.json")
        ]
        self.assertEqual(len(audit_payloads), 1)
        self.assertEqual(len(list(logs.glob("*.log"))), 1)
        payload = audit_payloads[0]
        self.assertTrue(
            "--crate-name=cargo_fixture" in payload.get("rustc_args", [])
            or any(
                left == "--crate-name" and right == "cargo_fixture"
                for left, right in zip(
                    payload.get("rustc_args", []), payload.get("rustc_args", [])[1:]
                )
            )
        )
        summary = payload.get("summary") or {}
        self.assertTrue(summary.get("actual_semantic_scope_rewrite"))
        self.assertGreater(int(summary.get("semantic_scope_rewrite_applied_count") or 0), 0)
        selected_rows = [
            row
            for row in payload.get("rewrite_candidates", [])
            if isinstance(row, dict)
            and str(row.get("mir_function") or "").endswith("selected_value")
            and row.get("rewrite_status")
            in {
                "actual_semantic_scope_enter_exit_rewrite_applied",
                "actual_semantic_scope_generic_type_rewrite_applied",
            }
        ]
        self.assertTrue(selected_rows)
        dependency_rows = [
            row
            for row in payload.get("rewrite_candidates", [])
            if isinstance(row, dict)
            and (
                "fixture_dependency" in str(row.get("mir_function") or "")
                or "dependency_value" in str(row.get("mir_function") or "")
            )
        ]
        self.assertEqual(dependency_rows, [])

    def test_non_target_executes_arbitrary_compiler_with_exact_argv(self) -> None:
        captured = self.tmp / "captured-args.json"
        compiler = self.write_fake_compiler()
        audit = self.tmp / "bypass-audit.json"
        audit.write_text("sentinel\n", encoding="utf-8")
        log = self.tmp / "bypass.log"
        original_args = ["--crate-name", "dependency_crate", "--emit=metadata"]
        env = self.wrapper_env()
        env.update(
            {
                "UNIALLOC_RUSTC_TARGET_CRATES": "application-crate",
                "UNIALLOC_LOWERING_POLICY_FLAGS": "not-a-number-for-non-target",
                "UNIALLOC_REWRITE_AUDIT_OUT": str(audit),
                "UNIALLOC_PASS_LOG_OUT": str(log),
                "CAPTURE_ARGS": str(captured),
                "FAKE_COMPILER_EXIT": "31",
            }
        )

        result = subprocess.run([str(self.wrapper), str(compiler), *original_args], env=env)

        self.assertEqual(result.returncode, 31)
        self.assertEqual(json.loads(captured.read_text(encoding="utf-8")), original_args)
        self.assertEqual(audit.read_text(encoding="utf-8"), "sentinel\n")
        self.assertFalse(log.exists())

        probe_args = ["-vV"]
        result = subprocess.run([str(self.wrapper), str(compiler), *probe_args], env=env)
        self.assertEqual(result.returncode, 31)
        self.assertEqual(json.loads(captured.read_text(encoding="utf-8")), probe_args)
        self.assertEqual(audit.read_text(encoding="utf-8"), "sentinel\n")

    @unittest.skipUnless(os.name == "posix", "executable source ambiguity is Unix-specific")
    def test_executable_first_argument_without_dashdash_is_compiler_contract(self) -> None:
        captured = self.tmp / "executable-source-args.json"
        executable_source = self.write_fake_compiler("ambiguous-source.rs")
        env = self.wrapper_env()
        env.update(
            {
                "UNIALLOC_RUSTC_TARGET_CRATES": "application-crate",
                "CAPTURE_ARGS": str(captured),
                "FAKE_COMPILER_EXIT": "37",
            }
        )
        args = ["--crate-name", "dependency_crate"]

        result = subprocess.run([str(self.wrapper), str(executable_source), *args], env=env)

        self.assertEqual(result.returncode, 37)
        self.assertEqual(json.loads(captured.read_text(encoding="utf-8")), args)

    @unittest.skipUnless(os.name == "posix", "Unix signal propagation contract")
    def test_non_target_bypass_preserves_unix_signal_status(self) -> None:
        captured = self.tmp / "signal-args.json"
        compiler = self.write_fake_compiler("compiler-driver-signal")
        env = self.wrapper_env()
        env.update(
            {
                "UNIALLOC_RUSTC_TARGET_CRATES": "application-crate",
                "CAPTURE_ARGS": str(captured),
                "FAKE_COMPILER_EXIT": "0",
                "FAKE_COMPILER_SIGNAL": "SIGTERM",
            }
        )

        result = subprocess.run(
            [str(self.wrapper), str(compiler), "--crate-name", "dependency_crate"],
            env=env,
        )

        self.assertEqual(result.returncode, -signal.SIGTERM)

    def test_cli_allowlist_selects_hyphen_alias_and_equals_crate_name(self) -> None:
        source = self.write_fixture("selected")
        audit = self.tmp / "selected-audit.json"
        output = self.tmp / "selected-bin"
        env = self.wrapper_env()
        env["UNIALLOC_RUSTC_TARGET_CRATES"] = "not-this-crate"
        env["UNIALLOC_CONTINUE_COMPILATION"] = "1"

        subprocess.run(
            [
                str(self.wrapper),
                "--unialloc-target-crates",
                "fixture-crate, helper",
                "--unialloc-rewrite-audit-out",
                str(audit),
                str(self.rustc),
                "--crate-name=fixture_crate",
                "--crate-type=bin",
                "--edition=2021",
                str(source),
                "-o",
                str(output),
            ],
            env=env,
            check=True,
        )

        self.assertTrue(output.is_file())
        payload = json.loads(audit.read_text(encoding="utf-8"))
        self.assertIn("--crate-name=fixture_crate", payload["rustc_args"])

    def test_explicit_dashdash_compiles_executable_source_directly(self) -> None:
        source = self.write_fixture("unrestricted")
        source.chmod(0o755)
        audit = self.tmp / "unrestricted-audit.json"
        output = self.tmp / "unrestricted-bin"
        env = self.wrapper_env()
        env.pop("UNIALLOC_RUSTC_TARGET_CRATES", None)
        env["UNIALLOC_CONTINUE_COMPILATION"] = "1"
        env["UNIALLOC_RUSTC_SYSROOT"] = str(self.sysroot)

        subprocess.run(
            [
                str(self.wrapper),
                "--unialloc-rewrite-audit-out",
                str(audit),
                "--",
                "--crate-name",
                "unrestricted_crate",
                "--crate-type=bin",
                "--edition=2021",
                str(source),
                "-o",
                str(output),
            ],
            env=env,
            check=True,
        )

        self.assertTrue(output.is_file())
        self.assertTrue(audit.is_file())

    def test_help_documents_direct_source_boundary(self) -> None:
        result = subprocess.run(
            [str(self.wrapper), "--unialloc-help"],
            env=self.wrapper_env(),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            check=True,
        )
        self.assertIn("Executable source files therefore require explicit `--`", result.stdout)


if __name__ == "__main__":
    unittest.main()
