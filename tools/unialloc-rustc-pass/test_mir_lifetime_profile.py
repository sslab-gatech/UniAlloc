#!/usr/bin/env python3
"""Process-level tests for exact, fail-closed MIR lifetime profiles."""

from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[2]
PASS_SOURCE = ROOT / "tools/unialloc-rustc-pass/unialloc-rustc-mir-rewrite-dry-run.rs"
TOOLCHAIN = (ROOT / "rust-toolchain").read_text(encoding="utf-8").strip()


class MirLifetimeProfileTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls._tmp = tempfile.TemporaryDirectory(
            prefix="unialloc-lifetime-profile-", dir="/tmp" if Path("/tmp").is_dir() else None
        )
        cls.tmp = Path(cls._tmp.name)
        rustc = shutil.which("rustc") or "rustc"
        cls.rustc = rustc
        cls.sysroot = Path(
            subprocess.check_output(
                [rustc, f"+{TOOLCHAIN}", "--print", "sysroot"], cwd=ROOT, text=True
            ).strip()
        )
        cls.driver = cls.tmp / "unialloc-rustc-mir-rewrite-dry-run"
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
                str(cls.driver),
            ],
            cwd=ROOT,
            env=env,
            check=True,
        )
        cls.fixture = cls.tmp / "profile_probe.rs"
        cls.fixture.write_text(
            """#[inline(never)]
fn ephemeral_site() -> Vec<u64> { Vec::with_capacity(16) }
#[inline(never)]
fn long_lived_site() -> Vec<u64> { Vec::with_capacity(32) }
fn main() {
    assert_eq!(ephemeral_site().len() + long_lived_site().len(), 0);
}
""",
            encoding="utf-8",
        )

    @classmethod
    def tearDownClass(cls) -> None:
        cls._tmp.cleanup()

    def run_pass(
        self,
        label: str,
        profile: Path | None = None,
        *,
        fixture: Path | None = None,
        actual_semantic_rewrite: bool = False,
        extra_rustc_args: tuple[str, ...] = (),
    ) -> dict[str, object]:
        audit = self.tmp / f"{label}.json"
        command = [
            str(self.driver),
            "--unialloc-rewrite-audit-out",
            str(audit),
            "--unialloc-continue-compilation",
        ]
        if actual_semantic_rewrite:
            command.append("--unialloc-actual-semantic-scope-rewrite")
        if profile is not None:
            command.extend(["--unialloc-lifetime-profile", str(profile)])
        command.extend(
            [
                "--",
                "--sysroot",
                str(self.sysroot),
                "--edition=2021",
                *extra_rustc_args,
                "-o",
                str(self.tmp / label),
                str(fixture or self.fixture),
            ]
        )
        env = os.environ.copy()
        library_path = str(self.sysroot / "lib")
        env["LD_LIBRARY_PATH"] = library_path
        env["DYLD_LIBRARY_PATH"] = library_path
        completed = subprocess.run(
            command,
            cwd=ROOT,
            env=env,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)
        return json.loads(audit.read_text(encoding="utf-8"))

    def target_row(self, audit: dict[str, object], function: str) -> dict[str, object]:
        rows = [
            row
            for row in audit.get("rewrite_candidates", [])  # type: ignore[union-attr]
            if isinstance(row, dict)
            and str(row.get("mir_function") or "").endswith(function)
            and row.get("lowering_kind") == "semantic_scope_enter_exit_rewrite"
        ]
        self.assertEqual(len(rows), 1, rows)
        return rows[0]

    @staticmethod
    def profile_line(row: dict[str, object], hint: int) -> str:
        return f"{row['callsite']} {row['type_id']} {row['module_id']} {hint}"

    def test_exact_profile_matches_and_malformed_or_ambiguous_entries_fail_closed(self) -> None:
        baseline = self.run_pass("baseline")
        ephemeral = self.target_row(baseline, "ephemeral_site")
        long_lived = self.target_row(baseline, "long_lived_site")

        exact_profile = self.tmp / "exact.profile"
        exact_profile.write_text(
            "\n".join(
                [
                    "unialloc-lifetime-profile-v1",
                    self.profile_line(ephemeral, 1),
                    self.profile_line(long_lived, 2),
                    "",
                ]
            ),
            encoding="utf-8",
        )
        exact = self.run_pass("exact", exact_profile)
        exact_ephemeral = self.target_row(exact, "ephemeral_site")
        exact_long_lived = self.target_row(exact, "long_lived_site")
        self.assertEqual(exact_ephemeral["lifetime_hint"], 1)
        self.assertEqual(exact_long_lived["lifetime_hint"], 2)
        self.assertEqual(exact_ephemeral["lifetime_hint_basis"], "profile_exact_match")
        self.assertEqual(exact_long_lived["lifetime_hint_basis"], "profile_exact_match")
        compiler_pass = exact["compiler_pass"]
        self.assertTrue(compiler_pass["lifetime_profile_format_valid"])
        self.assertEqual(
            compiler_pass["lifetime_profile_binding"], "exact(callsite,type_id,module_id)"
        )
        self.assertEqual(compiler_pass["lifetime_profile_match_count"], 2)
        self.assertRegex(str(compiler_pass["lifetime_profile_digest"]), r"^[0-9a-f]{16}$")

        guarded_profile = self.tmp / "guarded.profile"
        wrong_type = int(ephemeral["type_id"]) ^ 1
        duplicate = self.profile_line(long_lived, 2)
        guarded_profile.write_text(
            "\n".join(
                [
                    "unialloc-lifetime-profile-v1",
                    f"{ephemeral['callsite']} {wrong_type} {ephemeral['module_id']} 1",
                    duplicate,
                    duplicate,
                    "malformed-entry",
                    "",
                ]
            ),
            encoding="utf-8",
        )
        guarded = self.run_pass("guarded", guarded_profile)
        guarded_ephemeral = self.target_row(guarded, "ephemeral_site")
        guarded_long_lived = self.target_row(guarded, "long_lived_site")
        self.assertEqual(guarded_ephemeral["lifetime_hint"], 0)
        self.assertEqual(
            guarded_ephemeral["lifetime_hint_basis"],
            "profile_type_or_module_guard_mismatch",
        )
        self.assertEqual(guarded_long_lived["lifetime_hint"], 0)
        self.assertEqual(
            guarded_long_lived["lifetime_hint_basis"], "profile_ambiguous_entry"
        )
        guarded_pass = guarded["compiler_pass"]
        self.assertEqual(guarded_pass["lifetime_profile_invalid_line_count"], 1)
        self.assertEqual(guarded_pass["lifetime_profile_duplicate_key_count"], 1)
        self.assertGreaterEqual(guarded_pass["lifetime_profile_miss_count"], 2)

    def test_actual_rewrite_passes_exact_profile_hints_to_runtime(self) -> None:
        stub_source = self.tmp / "unialloc_stub.rs"
        stub_rlib = self.tmp / "libunialloc.rlib"
        stub_source.write_text(
            """#![crate_name = "unialloc"]
use std::sync::atomic::{AtomicU64, Ordering};
static OBSERVED_HINTS: AtomicU64 = AtomicU64::new(0);
fn record(hint: u16) { OBSERVED_HINTS.fetch_or(1u64 << hint, Ordering::SeqCst); }
pub fn observed_hints() -> u64 { OBSERVED_HINTS.load(Ordering::SeqCst) }
pub mod alloc_api {
    #[no_mangle]
    pub extern "C" fn __unialloc_semantic_scope_push(_: u64, _: u64, _: u32, _: u64) {
        super::record(0);
    }
    #[no_mangle]
    pub extern "C" fn __unialloc_semantic_scope_push_local(_: u64, _: u64, _: u32, _: u64) {
        super::record(0);
    }
    #[no_mangle]
    pub extern "C" fn __unialloc_semantic_scope_push_hints(
        _: u64, _: u64, _: u32, lifetime: u16, _: u16, _: u64,
    ) { super::record(lifetime); }
    #[no_mangle]
    pub extern "C" fn __unialloc_semantic_scope_push_hints_local(
        _: u64, _: u64, _: u32, lifetime: u16, _: u16, _: u64,
    ) { super::record(lifetime); }
    #[no_mangle]
    pub extern "C" fn __unialloc_semantic_scope_pop() {}
}
""",
            encoding="utf-8",
        )
        subprocess.run(
            [
                self.rustc,
                f"+{TOOLCHAIN}",
                "--crate-type=rlib",
                "--crate-name=unialloc",
                str(stub_source),
                "-o",
                str(stub_rlib),
            ],
            cwd=ROOT,
            check=True,
        )

        fixture = self.tmp / "actual_profile_probe.rs"
        fixture.write_text(
            """extern crate unialloc;
#[inline(never)]
fn make() -> Vec<u64> { Vec::with_capacity(16) }
fn main() {
    let value = make();
    assert_eq!(value.len(), 0);
    drop(value);
    let observed = unialloc::observed_hints();
    println!("observed_hints={observed}");
    assert_eq!(observed, 1u64 << 2);
}
""",
            encoding="utf-8",
        )
        rustc_args = ("--extern", f"unialloc={stub_rlib}")
        baseline = self.run_pass(
            "actual-baseline",
            fixture=fixture,
            extra_rustc_args=rustc_args,
        )
        profiled_rows = [
            row
            for row in baseline.get("rewrite_candidates", [])  # type: ignore[union-attr]
            if isinstance(row, dict)
            and row.get("lowering_kind")
            in {"semantic_scope_enter_exit_rewrite", "semantic_scope_drop_rewrite"}
            and int(row.get("type_id") or 0) != 0
            and int(row.get("module_id") or 0) != 0
        ]
        self.assertEqual(len(profiled_rows), 2, profiled_rows)
        self.assertEqual(
            {row["lowering_kind"] for row in profiled_rows},
            {"semantic_scope_enter_exit_rewrite", "semantic_scope_drop_rewrite"},
        )

        profile = self.tmp / "actual.profile"
        profile.write_text(
            "\n".join(
                ["unialloc-lifetime-profile-v1"]
                + [self.profile_line(row, 2) for row in profiled_rows]
                + [""]
            ),
            encoding="utf-8",
        )
        actual = self.run_pass(
            "actual-profiled",
            profile,
            fixture=fixture,
            actual_semantic_rewrite=True,
            extra_rustc_args=rustc_args,
        )
        completed = subprocess.run(
            [str(self.tmp / "actual-profiled")],
            cwd=ROOT,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertEqual(completed.stdout.strip(), "observed_hints=4")

        exact_rows = [
            row
            for row in actual.get("rewrite_candidates", [])  # type: ignore[union-attr]
            if isinstance(row, dict) and row.get("lifetime_hint_basis") == "profile_exact_match"
        ]
        self.assertEqual(len(exact_rows), 2, exact_rows)
        self.assertTrue(all(row.get("lifetime_hint") == 2 for row in exact_rows), exact_rows)
        self.assertTrue(
            all(str(row.get("rewrite_status") or "").endswith("_applied") for row in exact_rows),
            exact_rows,
        )
        compiler_pass = actual["compiler_pass"]
        self.assertEqual(compiler_pass["lifetime_profile_match_count"], 2)
        self.assertEqual(compiler_pass["lifetime_profile_miss_count"], 0)


if __name__ == "__main__":
    unittest.main()
