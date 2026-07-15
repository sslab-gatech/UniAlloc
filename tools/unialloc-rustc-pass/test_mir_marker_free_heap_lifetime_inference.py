#!/usr/bin/env python3
"""Focused process tests for marker-free heap lifetime inference."""

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

LOCAL_DROP_BASIS = "automatic_heap_exact_local_drop_fact_unknown"
MOVE_DROP_BASIS = "automatic_heap_exact_move_chain_drop_fact_unknown"
FORGET_BASIS = "automatic_heap_exact_mem_forget"
LEAK_BASIS = "automatic_heap_exact_box_leak"
ALIAS_UNKNOWN_BASIS = "automatic_heap_alias_or_escape_unknown"
BRANCH_UNKNOWN_BASIS = "automatic_heap_nonlinear_control_flow_unknown"
MISSING_UNKNOWN_BASIS = "automatic_heap_missing_terminal_unknown"
REFCOUNTED_UNKNOWN_BASIS = "automatic_heap_refcounted_owner_unknown"
CLEANUP_UNKNOWN_BASIS = "automatic_heap_cleanup_or_unwind_unknown"


class MirMarkerFreeHeapLifetimeInferenceTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls._tmp = tempfile.TemporaryDirectory(
            prefix="unialloc-heap-lifetime-",
            dir="/tmp" if Path("/tmp").is_dir() else None,
        )
        cls.tmp = Path(cls._tmp.name)
        cls.rustc = shutil.which("rustc") or "rustc"
        cls.sysroot = Path(
            subprocess.check_output(
                [cls.rustc, f"+{TOOLCHAIN}", "--print", "sysroot"],
                cwd=ROOT,
                text=True,
            ).strip()
        )
        cls.driver = cls.tmp / "unialloc-rustc-mir-rewrite-dry-run"
        build_env = os.environ.copy()
        build_env["RUSTC_BOOTSTRAP"] = "1"
        subprocess.run(
            [
                cls.rustc,
                f"+{TOOLCHAIN}",
                "--cfg",
                "unialloc_rustc_current",
                str(PASS_SOURCE),
                "-o",
                str(cls.driver),
            ],
            cwd=ROOT,
            env=build_env,
            check=True,
        )

        cls.stub_source = cls.tmp / "unialloc_stub.rs"
        cls.stub_rlib = cls.tmp / "libunialloc.rlib"
        cls.stub_source.write_text(
            """#![crate_name = "unialloc"]
use std::sync::atomic::{AtomicU64, Ordering};
static OBSERVED_HINTS: AtomicU64 = AtomicU64::new(0);
fn record(hint: u16) {
    let mask = match hint { 0xA101 => 1, 0xA102 => 2, 1 => 4, 2 => 8, _ => 0 };
    OBSERVED_HINTS.fetch_or(mask, Ordering::SeqCst);
}
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
                cls.rustc,
                f"+{TOOLCHAIN}",
                "--crate-type=rlib",
                "--edition=2021",
                str(cls.stub_source),
                "-o",
                str(cls.stub_rlib),
            ],
            cwd=ROOT,
            check=True,
        )

        cls.fixture = cls.tmp / "heap_lifetime_probe.rs"
        cls.fixture.write_text(
            """#![allow(dead_code, improper_ctypes_definitions)]
extern crate unialloc;
use std::sync::Arc;

#[inline(never)]
fn exact_local_drop() {
    let _owner = Vec::<u64>::with_capacity(8);
}

#[inline(never)]
fn exact_move_chain_drop() {
    let first = Vec::<u64>::with_capacity(16);
    let second = first;
    let _final_owner = second;
}

#[inline(never)]
fn exact_mem_forget() {
    let owner = Vec::<u64>::with_capacity(32);
    core::mem::forget(owner);
}

#[inline(never)]
fn exact_box_leak() {
    let owner = Box::new(7_u64);
    let leaked: &'static mut u64 = Box::leak(owner);
    *leaked += 1;
}

#[inline(never)]
fn branch_abstains(flag: bool) {
    let owner = Vec::<u64>::with_capacity(64);
    if flag { drop(owner); } else { core::mem::forget(owner); }
}

#[inline(never)]
fn mutable_alias_abstains() {
    let mut owner = Vec::<u64>::with_capacity(128);
    let alias = &mut owner;
    alias.push(1);
    drop(owner);
}

#[inline(never)]
fn return_abstains() -> Vec<u64> {
    let owner = Vec::<u64>::with_capacity(256);
    owner
}

#[inline(never)]
fn refcounted_abstains() {
    let _owner = Arc::new(11_u64);
}

#[no_mangle]
extern "C" fn consume_vec_ffi(_owner: Vec<u64>) {}

#[inline(never)]
fn ffi_escape_abstains() {
    let owner = Vec::<u64>::with_capacity(512);
    consume_vec_ffi(owner);
}

fn main() {
    exact_local_drop();
    exact_move_chain_drop();
    exact_mem_forget();
    exact_box_leak();
    branch_abstains(true);
    mutable_alias_abstains();
    drop(return_abstains());
    refcounted_abstains();
    ffi_escape_abstains();
    let observed = unialloc::observed_hints();
    println!("observed_hints={observed}");
    assert_eq!(observed & 0b0011, 0b0010);
    assert_eq!(observed & 0b1100, 0);
}
""",
            encoding="utf-8",
        )

        cls.release_box_fixture = cls.tmp / "heap_lifetime_release_box_probe.rs"
        cls.release_box_fixture.write_text(
            """#![allow(dead_code)]
extern crate unialloc;

const PAYLOAD_BYTES: usize = 4096;

#[inline(never)]
fn box_local_drop() {
    let owner = Box::new([1_u8; PAYLOAD_BYTES]);
    drop(owner);
}

#[inline(never)]
fn box_move_chain_drop() {
    let first = Box::new([2_u8; PAYLOAD_BYTES]);
    let second = first;
    let third = second;
    drop(third);
}

#[inline(never)]
fn box_mem_forget() {
    let owner = Box::new([3_u8; PAYLOAD_BYTES]);
    core::mem::forget(owner);
}

#[inline(never)]
fn box_leak() -> &'static mut [u8; PAYLOAD_BYTES] {
    let owner = Box::new([4_u8; PAYLOAD_BYTES]);
    Box::leak(owner)
}

#[inline(never)]
fn box_branch_abstains(branch: bool) {
    let owner = Box::new([5_u8; PAYLOAD_BYTES]);
    if branch {
        drop(owner);
    } else {
        core::mem::forget(owner);
    }
}

#[inline(never)]
fn allocate_and_touch_16_mib() {
    let mut payload = vec![0_u8; 16 * 1024 * 1024];
    payload[0] = 1;
    let last = payload.len() - 1;
    payload[last] = 2;
    std::hint::black_box(payload.as_ptr());
}

#[inline(never)]
fn box_long_work_then_drop() {
    let owner = Box::new([6_u8; PAYLOAD_BYTES]);
    allocate_and_touch_16_mib();
    drop(owner);
}

#[inline(never)]
fn may_unwind(flag: bool) {
    if flag {
        panic!("requested unwind");
    }
    std::hint::black_box(flag);
}

#[inline(never)]
fn box_may_unwind_then_drop(flag: bool) {
    let owner = Box::new([7_u8; PAYLOAD_BYTES]);
    may_unwind(flag);
    drop(owner);
}

#[inline(never)]
fn box_may_unwind_then_forget(flag: bool) {
    let owner = Box::new([8_u8; PAYLOAD_BYTES]);
    may_unwind(flag);
    core::mem::forget(owner);
}

#[inline(never)]
fn box_in_loop(iterations: usize) {
    for index in 0..iterations {
        let owner = Box::new([index as u8; PAYLOAD_BYTES]);
        std::hint::black_box(owner[0]);
        drop(owner);
    }
}

#[inline(never)]
fn return_box() -> Box<[u8; PAYLOAD_BYTES]> {
    let owner = Box::new([9_u8; PAYLOAD_BYTES]);
    owner
}

#[inline(never)]
fn store_box() {
    let owner = Box::new([10_u8; PAYLOAD_BYTES]);
    let stored = (owner,);
    std::hint::black_box(stored);
}

#[inline(never)]
fn receiver_owned_reserve() {
    let mut owner = Vec::<u8>::new();
    owner.reserve(128);
    std::hint::black_box(owner.capacity());
}

fn main() {
    box_local_drop();
    box_move_chain_drop();
    box_mem_forget();
    let leaked = box_leak();
    leaked[0] = leaked[0].wrapping_add(1);
    box_branch_abstains(true);
    box_long_work_then_drop();
    box_may_unwind_then_drop(false);
    box_may_unwind_then_forget(false);
    box_in_loop(2);
    drop(return_box());
    store_box();
    receiver_owned_reserve();
    let observed = unialloc::observed_hints();
    println!("observed_hints={observed}");
    assert_eq!(observed & 0b0011, 0b0010);
    assert_eq!(observed & 0b1100, 0);
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
        *,
        automatic: bool = False,
        automatic_from_env: bool = False,
        actual_rewrite: bool = False,
        global_hint: int | None = None,
        fixture: Path | None = None,
        release_optimized: bool = False,
    ) -> dict[str, object]:
        audit = self.tmp / f"{label}.json"
        command = [
            str(self.driver),
            "--unialloc-rewrite-audit-out",
            str(audit),
            "--unialloc-continue-compilation",
        ]
        if automatic and not automatic_from_env:
            command.append("--unialloc-auto-heap-lifetime-inference")
        if actual_rewrite:
            command.append("--unialloc-actual-semantic-scope-rewrite")
        if global_hint is not None:
            command.extend(["--unialloc-lifetime-hint", str(global_hint)])
        command.extend(["--", "--sysroot", str(self.sysroot)])
        if release_optimized:
            command.extend(["-C", "opt-level=3"])
        else:
            command.append("-Zmir-opt-level=0")
        command.extend(
            [
                "--edition=2021",
                "--extern",
                f"unialloc={self.stub_rlib}",
                "-o",
                str(self.tmp / label),
                str(fixture or self.fixture),
            ]
        )
        env = os.environ.copy()
        env.pop("UNIALLOC_AUTO_HEAP_LIFETIME_INFERENCE", None)
        if automatic and automatic_from_env:
            env["UNIALLOC_AUTO_HEAP_LIFETIME_INFERENCE"] = "1"
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

    def allocation_row(
        self, audit: dict[str, object], function: str
    ) -> dict[str, object]:
        rows = [
            row
            for row in audit.get("rewrite_candidates", [])  # type: ignore[union-attr]
            if isinstance(row, dict)
            and str(row.get("mir_function") or "").endswith(function)
            and row.get("lowering_kind") == "semantic_scope_enter_exit_rewrite"
            and ("with_capacity" in str(row.get("callee") or "") or "::new" in str(row.get("callee") or ""))
        ]
        self.assertEqual(len(rows), 1, rows)
        return rows[0]

    def assert_hint(
        self,
        audit: dict[str, object],
        function: str,
        hint: int,
        confidence: int,
        basis: str,
    ) -> dict[str, object]:
        row = self.allocation_row(audit, function)
        self.assertEqual(row["lifetime_hint"], hint, row)
        self.assertEqual(row["lifetime_hint_confidence"], confidence, row)
        self.assertEqual(row["lifetime_hint_basis"], basis, row)
        return row

    def test_exact_drop_move_forget_and_leak_proofs(self) -> None:
        audit = self.run_pass("heap-inference", automatic=True)
        local = self.assert_hint(
            audit, "exact_local_drop", 0, 0, CLEANUP_UNKNOWN_BASIS
        )
        moved = self.assert_hint(
            audit, "exact_move_chain_drop", 0, 0, CLEANUP_UNKNOWN_BASIS
        )
        self.assertTrue(local["lifetime_analysis_features"]["exact_drop_path"])
        self.assertTrue(moved["lifetime_analysis_features"]["exact_drop_path"])
        self.assert_hint(audit, "exact_mem_forget", 0xA102, 100, FORGET_BASIS)
        self.assert_hint(audit, "exact_box_leak", 0xA102, 100, LEAK_BASIS)

        compiler_pass = audit["compiler_pass"]
        self.assertTrue(compiler_pass["automatic_heap_lifetime_inference_enabled"])
        self.assertNotIn("automatic_heap_proven_ephemeral_hint", compiler_pass)
        self.assertNotIn("automatic_heap_proven_scoped_hint", compiler_pass)
        self.assertEqual(
            compiler_pass["automatic_heap_bounded_process_long_oracle_hint"],
            0xA102,
        )
        self.assertGreaterEqual(
            compiler_pass["automatic_heap_exact_drop_fact_allocation_site_count"], 2
        )
        self.assertGreaterEqual(
            compiler_pass[
                "automatic_heap_bounded_process_long_oracle_allocation_site_count"
            ],
            2,
        )

    def test_ambiguous_ownership_paths_fail_closed(self) -> None:
        audit = self.run_pass("heap-inference-negative", automatic=True)
        self.assert_hint(audit, "branch_abstains", 0, 0, BRANCH_UNKNOWN_BASIS)
        self.assert_hint(audit, "mutable_alias_abstains", 0, 0, ALIAS_UNKNOWN_BASIS)
        self.assert_hint(audit, "return_abstains", 0, 0, MISSING_UNKNOWN_BASIS)
        self.assert_hint(audit, "refcounted_abstains", 0, 0, REFCOUNTED_UNKNOWN_BASIS)
        self.assert_hint(audit, "ffi_escape_abstains", 0, 0, ALIAS_UNKNOWN_BASIS)

    def test_mode_is_opt_in_and_environment_matches_cli(self) -> None:
        baseline = self.run_pass("heap-inference-disabled")
        self.assert_hint(baseline, "exact_local_drop", 0, 0, "default_unknown")

        environment = self.run_pass(
            "heap-inference-env", automatic=True, automatic_from_env=True
        )
        self.assert_hint(
            environment, "exact_local_drop", 0, 0, CLEANUP_UNKNOWN_BASIS
        )

        manual = self.run_pass(
            "heap-inference-manual", automatic=True, global_hint=2
        )
        self.assert_hint(
            manual, "exact_local_drop", 2, 100, "manual_global_lifetime_hint"
        )

    def test_actual_rewrite_transports_only_proven_hint_values(self) -> None:
        audit = self.run_pass(
            "heap-inference-actual", automatic=True, actual_rewrite=True
        )
        completed = subprocess.run(
            [str(self.tmp / "heap-inference-actual")],
            cwd=ROOT,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertIn("observed_hints=", completed.stdout)
        applied = [
            row
            for row in audit.get("rewrite_candidates", [])  # type: ignore[union-attr]
            if isinstance(row, dict)
            and row.get("lifetime_hint_basis")
            in {
                LOCAL_DROP_BASIS,
                MOVE_DROP_BASIS,
                CLEANUP_UNKNOWN_BASIS,
                FORGET_BASIS,
                LEAK_BASIS,
            }
        ]
        self.assertTrue(applied)
        self.assertEqual({int(row["lifetime_hint"]) for row in applied}, {0, 0xA102})
        self.assertTrue(
            all(str(row.get("rewrite_status") or "").endswith("_applied") for row in applied),
            applied,
        )

    def test_release_optimized_box_proofs_survive_actual_rewrite(self) -> None:
        audit = self.run_pass(
            "heap-inference-release-box",
            automatic=True,
            actual_rewrite=True,
            fixture=self.release_box_fixture,
            release_optimized=True,
        )
        semantic_rows = [
            row
            for row in audit["rewrite_candidates"]
            if row.get("lowering_kind") == "semantic_scope_enter_exit_rewrite"
        ]
        self.assertTrue(semantic_rows)
        for row in semantic_rows:
            features = row.get("lifetime_analysis_features")
            self.assertIsInstance(features, dict, row)
            self.assertTrue(features["analysis_only"], row)
            self.assertFalse(features["classification_rule_applied"], row)
            join = features["runtime_join_key"]
            self.assertEqual(
                set(join),
                {
                    "callsite",
                    "type_id",
                    "module_id",
                    "requested_size_bytes",
                    "requested_align_bytes",
                },
                row,
            )
            self.assertEqual(join["callsite"], row["callsite"], row)
            self.assertEqual(join["type_id"], row["type_id"], row)
            self.assertEqual(join["module_id"], row["module_id"], row)
            self.assertEqual(
                features["runtime_join_key_complete"],
                join["requested_size_bytes"] is not None
                and join["requested_align_bytes"] is not None,
                row,
            )
            self.assertEqual(features["allocation_block"], row["basic_block"])
            self.assertEqual(
                features["allocation_source_span"], row["source_span"]
            )
        self.assert_hint(audit, "box_local_drop", 0, 0, CLEANUP_UNKNOWN_BASIS)
        self.assert_hint(
            audit, "box_move_chain_drop", 0, 0, CLEANUP_UNKNOWN_BASIS
        )
        self.assert_hint(audit, "box_mem_forget", 0xA102, 100, FORGET_BASIS)
        self.assert_hint(audit, "box_leak", 0xA102, 100, LEAK_BASIS)
        self.assert_hint(
            audit, "box_branch_abstains", 0, 0, BRANCH_UNKNOWN_BASIS
        )
        for function in (
            "box_long_work_then_drop",
            "box_may_unwind_then_drop",
            "box_may_unwind_then_forget",
        ):
            self.assert_hint(audit, function, 0, 0, CLEANUP_UNKNOWN_BASIS)

        local_features = self.allocation_row(audit, "box_local_drop")[
            "lifetime_analysis_features"
        ]
        self.assertEqual(local_features["schema_version"], 1)
        self.assertTrue(local_features["analysis_only"])
        self.assertFalse(local_features["classification_rule_applied"])
        self.assertEqual(
            local_features["analysis_phase"], "post_borrowck_pre_optimization"
        )
        self.assertTrue(local_features["runtime_join_key_complete"])
        self.assertEqual(
            local_features["runtime_join_key"]["requested_size_bytes"],
            4096,
        )
        self.assertEqual(
            local_features["runtime_join_key"]["requested_align_bytes"], 1
        )
        self.assertTrue(local_features["exact_drop_path"])
        self.assertGreaterEqual(local_features["owner_move_count"], 1)

        unwind_features = self.allocation_row(
            audit, "box_may_unwind_then_forget"
        )["lifetime_analysis_features"]
        self.assertTrue(unwind_features["cleanup_drop_path"])
        self.assertTrue(unwind_features["escape_sink"])
        self.assertTrue(unwind_features["reachable_cleanup_blocks"])

        loop_features = self.allocation_row(audit, "box_in_loop")[
            "lifetime_analysis_features"
        ]
        self.assertTrue(loop_features["allocation_in_natural_loop"])
        self.assertTrue(loop_features["reachable_backedge_after_allocation"])

        return_features = self.allocation_row(audit, "return_box")[
            "lifetime_analysis_features"
        ]
        self.assertTrue(return_features["return_sink"])
        store_features = self.allocation_row(audit, "store_box")[
            "lifetime_analysis_features"
        ]
        self.assertTrue(store_features["store_sink"])

        reserve_rows = [
            row
            for row in audit["rewrite_candidates"]
            if str(row.get("mir_function") or "").endswith("receiver_owned_reserve")
            and row.get("lowering_kind") == "semantic_scope_enter_exit_rewrite"
            and "reserve" in str(row.get("callee") or "")
        ]
        self.assertEqual(len(reserve_rows), 1, reserve_rows)
        self.assertTrue(
            reserve_rows[0]["lifetime_analysis_features"][
                "receiver_owned_allocation"
            ]
        )

        completed = subprocess.run(
            [str(self.tmp / "heap-inference-release-box")],
            cwd=ROOT,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertIn("observed_hints=", completed.stdout)

    def test_cleanup_soundness_at_mir_opt_zero(self) -> None:
        audit = self.run_pass(
            "heap-inference-opt-zero-cleanup",
            automatic=True,
            actual_rewrite=True,
            fixture=self.release_box_fixture,
            release_optimized=False,
        )
        for function in (
            "box_long_work_then_drop",
            "box_may_unwind_then_drop",
            "box_may_unwind_then_forget",
        ):
            self.assert_hint(audit, function, 0, 0, CLEANUP_UNKNOWN_BASIS)


if __name__ == "__main__":
    unittest.main()
