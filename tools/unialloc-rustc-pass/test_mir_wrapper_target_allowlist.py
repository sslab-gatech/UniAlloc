#!/usr/bin/env python3
"""Focused process-level regression tests for the MIR wrapper crate allowlist."""

from __future__ import annotations

import json
import os
import pathlib
import shutil
import subprocess
import tempfile
import unittest


ROOT = pathlib.Path(__file__).resolve().parents[2]
PASS_SOURCE = ROOT / "tools" / "unialloc-rustc-pass" / "unialloc-rustc-mir-rewrite-dry-run.rs"
TOOLCHAIN = (ROOT / "rust-toolchain").read_text(encoding="utf-8").strip()


class MirWrapperTargetAllowlistTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls._tmp = tempfile.TemporaryDirectory(prefix="unialloc-wrapper-allowlist-")
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

    def test_non_target_executes_original_rustc_without_audit_mutation(self) -> None:
        captured = self.tmp / "captured-args.json"
        # The .exe suffix also locks the Windows Cargo-wrapper argv0 path.
        fake_rustc = self.tmp / "rustc-fake.exe"
        fake_rustc.write_text(
            "#!/usr/bin/env python3\n"
            "import json, os, pathlib, sys\n"
            "pathlib.Path(os.environ['CAPTURE_ARGS']).write_text(json.dumps(sys.argv[1:]))\n"
            "raise SystemExit(int(os.environ['FAKE_RUSTC_EXIT']))\n",
            encoding="utf-8",
        )
        fake_rustc.chmod(0o755)
        audit = self.tmp / "bypass-audit.json"
        audit.write_text("sentinel\n", encoding="utf-8")
        log = self.tmp / "bypass.log"
        original_args = ["--crate-name", "dependency_crate", "--emit=metadata"]
        env = self.wrapper_env()
        env.update(
            {
                "UNIALLOC_RUSTC_TARGET_CRATES": "application-crate",
                "UNIALLOC_REWRITE_AUDIT_OUT": str(audit),
                "UNIALLOC_PASS_LOG_OUT": str(log),
                "CAPTURE_ARGS": str(captured),
                "FAKE_RUSTC_EXIT": "23",
            }
        )

        result = subprocess.run([str(self.wrapper), str(fake_rustc), *original_args], env=env)

        self.assertEqual(result.returncode, 23)
        self.assertEqual(json.loads(captured.read_text(encoding="utf-8")), original_args)
        self.assertEqual(audit.read_text(encoding="utf-8"), "sentinel\n")
        self.assertFalse(log.exists())

        probe_args = ["-vV"]
        result = subprocess.run([str(self.wrapper), str(fake_rustc), *probe_args], env=env)
        self.assertEqual(result.returncode, 23)
        self.assertEqual(json.loads(captured.read_text(encoding="utf-8")), probe_args)
        self.assertEqual(audit.read_text(encoding="utf-8"), "sentinel\n")

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

    def test_absent_allowlist_preserves_existing_audit_behavior(self) -> None:
        source = self.write_fixture("unrestricted")
        audit = self.tmp / "unrestricted-audit.json"
        output = self.tmp / "unrestricted-bin"
        env = self.wrapper_env()
        env.pop("UNIALLOC_RUSTC_TARGET_CRATES", None)
        env["UNIALLOC_CONTINUE_COMPILATION"] = "1"
        env["UNIALLOC_REWRITE_AUDIT_OUT"] = str(audit)

        subprocess.run(
            [
                str(self.wrapper),
                str(self.rustc),
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


if __name__ == "__main__":
    unittest.main()
