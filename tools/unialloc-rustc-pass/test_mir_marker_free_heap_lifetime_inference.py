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
RUST_PRIOR_LOCAL_RELEASE_SHORT_BASIS = (
    "automatic_rust_lifetime_prior_all_path_local_release_short"
)
RUST_PRIOR_RECEIVER_SHORT_BASIS = (
    "automatic_rust_lifetime_prior_receiver_owned_short"
)
RUST_PRIOR_RETURN_LONG_BASIS = "automatic_rust_lifetime_prior_return_long"
RUST_PRIOR_RETURN_LAYOUT_UNKNOWN_BASIS = (
    "automatic_rust_lifetime_prior_return_long_unjoinable_layout_unknown"
)
RUST_PRIOR_RETURN_OUT_OF_BAND_LAYOUT_UNKNOWN_BASIS = (
    "automatic_rust_lifetime_prior_return_long_out_of_band_layout_unknown"
)
RUST_PRIOR_BORROWED_VEC_RESERVE_LONG_BASIS = (
    "automatic_rust_lifetime_prior_borrowed_vec_reserve_long"
)
RUST_PRIOR_RECEIVER_LAYOUT_UNKNOWN_BASIS = (
    "automatic_rust_lifetime_prior_receiver_owned_short_unjoinable_layout_unknown"
)
RUST_PRIOR_CLEANUP_UNKNOWN_BASIS = (
    "automatic_rust_lifetime_prior_cleanup_or_unwind_unknown"
)
RUST_PRIOR_OWNER_LIVE_CALL_UNKNOWN_BASIS = (
    "automatic_rust_lifetime_prior_owner_live_call_unknown"
)


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
    #[inline]
    pub fn __unialloc_semantic_scope_push_for_rust_type<T: 'static>(
        _: u64, _: u32, _: u64,
    ) { super::record(0); }
    #[inline]
    pub fn __unialloc_semantic_scope_push_for_rust_type_local<T: 'static>(
        _: u64, _: u32, _: u64,
    ) { super::record(0); }
    #[inline]
    pub fn __unialloc_semantic_scope_push_for_rust_type_hints<T: 'static>(
        _: u64, _: u32, lifetime: u16, _: u16, _: u64,
    ) { super::record(lifetime); }
    #[inline]
    pub fn __unialloc_semantic_scope_push_for_rust_type_hints_local<T: 'static>(
        _: u64, _: u32, lifetime: u16, _: u16, _: u64,
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
fn box_drop_then_may_unwind(flag: bool) {
    let owner = Box::new([12_u8; PAYLOAD_BYTES]);
    drop(owner);
    may_unwind(flag);
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
fn return_generic_box<T>(value: T) -> Box<T> {
    let owner = Box::new(value);
    owner
}

struct Holder(Box<[u8; PAYLOAD_BYTES]>);

#[inline(never)]
fn return_owner_through_aggregate() -> Holder {
    let owner = Box::new([11_u8; PAYLOAD_BYTES]);
    let holder = Holder(owner);
    holder
}

#[inline(never)]
fn return_nonowner_projection_from_pair() -> String {
    let pair = (
        Box::new([13_u8; PAYLOAD_BYTES]),
        String::from("hello"),
    );
    let result = pair.1;
    result
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

#[inline(never)]
fn receiver_owned_reserve_then_release() {
    let mut owner = Vec::<u8>::new();
    owner.reserve(256);
    drop(owner);
}

#[inline(never)]
fn receiver_conditional(mut owner: Vec<u8>, flag: bool) -> Option<Vec<u8>> {
    owner.reserve(384);
    if flag {
        Some(owner)
    } else {
        drop(owner);
        None
    }
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
    box_drop_then_may_unwind(false);
    box_may_unwind_then_forget(false);
    box_in_loop(2);
    drop(return_box());
    drop(return_generic_box([14_u8; 64]));
    drop(return_owner_through_aggregate());
    drop(return_nonowner_projection_from_pair());
    store_box();
    receiver_owned_reserve();
    receiver_owned_reserve_then_release();
    drop(receiver_conditional(Vec::new(), true));
    let observed = unialloc::observed_hints();
    println!("observed_hints={observed}");
    assert_eq!(observed & 0b0011, 0b0010);
    assert_eq!(observed & 0b1100, 0);
}
""",
            encoding="utf-8",
        )

        cls.prior_transport_fixture = cls.tmp / "rust_lifetime_prior_transport.rs"
        cls.prior_transport_fixture.write_text(
            """extern crate unialloc;

#[inline(never)]
fn all_path_local_release() {
    let owner = Box::new([2_u8; 4096]);
    drop(owner);
}

#[inline(never)]
fn returned_owner() -> Box<[u8; 4096]> {
    let owner = Box::new([1_u8; 4096]);
    owner
}

fn main() {
    all_path_local_release();
    drop(returned_owner());
    let observed = unialloc::observed_hints();
    println!("observed_hints={observed}");
    assert_eq!(observed & 0b1100, 0b1100);
}
""",
            encoding="utf-8",
        )

        cls.measured_box_long_fixture = (
            cls.tmp / "rust_lifetime_prior_measured_box_long.rs"
        )
        cls.measured_box_long_fixture.write_text(
            """extern crate unialloc;

#[inline(never)]
fn convex_sized_box() -> Box<[u8; 6_000]> {
    Box::new([1_u8; 6_000])
}

#[inline(never)]
fn lume_roaring_bedrock_sized_box() -> Box<[u8; 8_192]> {
    Box::new([2_u8; 8_192])
}

#[inline(never)]
fn datafusion_sized_box() -> Box<[u8; 16_384]> {
    Box::new([3_u8; 16_384])
}

fn main() {
    drop(convex_sized_box());
    drop(lume_roaring_bedrock_sized_box());
    drop(datafusion_sized_box());
    let observed = unialloc::observed_hints();
    println!("observed_hints={observed}");
    assert_eq!(observed & 0b1000, 0b1000);
}
""",
            encoding="utf-8",
        )

        cls.generic_prior_transport_fixture = (
            cls.tmp / "rust_lifetime_prior_generic_transport.rs"
        )
        cls.generic_prior_transport_fixture.write_text(
            """extern crate unialloc;

#[inline(never)]
fn returned_generic_owner<T: 'static>(value: T) -> Box<T> {
    let owner = Box::new(value);
    owner
}

fn main() {
    drop(returned_generic_owner([3_u8; 96]));
    let observed = unialloc::observed_hints();
    println!("observed_hints={observed}");
    assert_eq!(observed, 0);
}
""",
            encoding="utf-8",
        )

        cls.constant_vec_layout_fixture = cls.tmp / "rust_lifetime_prior_vec_layout.rs"
        cls.constant_vec_layout_fixture.write_text(
            """extern crate unialloc;

const ROWS_PER_BATCH: usize = 3072;
const DIRECT_LONG_MIN_BYTES: usize = 4096;
const DIRECT_LONG_MAX_BYTES: usize = 28032;
const BELOW_DIRECT_LONG_MIN_BYTES: usize = 4095;
const ABOVE_DIRECT_LONG_MAX_BYTES: usize = 28033;
const TANTIVY_SSTABLE_BLOCK_LEN: usize = 4_000;

#[inline(never)]
fn zeroed_u64_column() -> Vec<u64> {
    let mut values = Vec::with_capacity(ROWS_PER_BATCH);
    values.resize(ROWS_PER_BATCH, 0_u64);
    values
}

#[inline(never)]
fn lower_boundary_column() -> Vec<u8> {
    Vec::with_capacity(DIRECT_LONG_MIN_BYTES)
}

#[inline(never)]
fn upper_boundary_column() -> Vec<u8> {
    Vec::with_capacity(DIRECT_LONG_MAX_BYTES)
}

#[inline(never)]
fn below_boundary_column() -> Vec<u8> {
    Vec::with_capacity(BELOW_DIRECT_LONG_MIN_BYTES)
}

#[inline(never)]
fn above_boundary_column() -> Vec<u8> {
    Vec::with_capacity(ABOVE_DIRECT_LONG_MAX_BYTES)
}

#[inline(never)]
fn dynamic_capacity_column(capacity: usize) -> Vec<u8> {
    Vec::with_capacity(capacity)
}

#[inline(never)]
fn tantivy_sstable_const_mul_capacity() -> Vec<u8> {
    Vec::with_capacity(TANTIVY_SSTABLE_BLOCK_LEN * 2)
}

#[inline(never)]
fn single_assignment_const_add_capacity() -> Vec<u8> {
    Vec::with_capacity(TANTIVY_SSTABLE_BLOCK_LEN + 4_096)
}

#[inline(never)]
fn parameter_mul_capacity(capacity: usize) -> Vec<u8> {
    Vec::with_capacity(capacity * 2)
}

#[inline(never)]
fn multiply_reassigned_local(mut replace: bool) -> Vec<u8> {
    let mut capacity = TANTIVY_SSTABLE_BLOCK_LEN;
    if std::hint::black_box(replace) {
        capacity = 4_096;
    }
    replace = !replace;
    std::hint::black_box(replace);
    Vec::with_capacity(capacity * 2)
}

#[inline(never)]
fn multiply_loop_phi(mut iterations: usize) -> Vec<u8> {
    let mut capacity = TANTIVY_SSTABLE_BLOCK_LEN;
    while std::hint::black_box(iterations) > 0 {
        capacity += 1;
        iterations -= 1;
    }
    Vec::with_capacity(capacity * 2)
}

#[inline(never)]
fn tiny_box() -> Box<[u8; 128]> {
    Box::new([0_u8; 128])
}

fn main() {
    drop(zeroed_u64_column());
    drop(lower_boundary_column());
    drop(upper_boundary_column());
    drop(below_boundary_column());
    drop(above_boundary_column());
    drop(dynamic_capacity_column(ROWS_PER_BATCH));
    drop(tantivy_sstable_const_mul_capacity());
    drop(single_assignment_const_add_capacity());
    drop(parameter_mul_capacity(TANTIVY_SSTABLE_BLOCK_LEN));
    drop(multiply_reassigned_local(false));
    drop(multiply_loop_phi(0));
    drop(tiny_box());
    let observed = unialloc::observed_hints();
    println!("observed_hints={observed}");
    assert_eq!(observed & 0b1000, 0b1000);
}
""",
            encoding="utf-8",
        )

        cls.borrowed_vec_reserve_fixture = (
            cls.tmp / "rust_lifetime_prior_borrowed_vec_reserve.rs"
        )
        cls.borrowed_vec_reserve_fixture.write_text(
            """extern crate unialloc;

const TANTIVY_RESERVE_BYTES: usize = 15_360;

#[inline(never)]
fn tantivy_borrowed_reserve(owner: &mut Vec<u8>) {
    owner.reserve_exact(TANTIVY_RESERVE_BYTES);
    std::hint::black_box(owner.capacity());
}

#[inline(never)]
fn local_borrowed_reserve_must_abstain() {
    let mut owner: Vec<u8> = Vec::new();
    let borrowed = &mut owner;
    borrowed.reserve_exact(TANTIVY_RESERVE_BYTES);
    std::hint::black_box(borrowed.capacity());
}

fn main() {
    let mut owner: Vec<u8> = Vec::new();
    tantivy_borrowed_reserve(&mut owner);
    local_borrowed_reserve_must_abstain();
    let observed = unialloc::observed_hints();
    println!("observed_hints={observed}");
    assert_eq!(observed & 0b1000, 0b1000);
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
        rust_prior: bool = False,
        rust_prior_from_env: bool = False,
        actual_rewrite: bool = False,
        global_hint: int | None = None,
        lifetime_profile: Path | None = None,
        fixture: Path | None = None,
        release_optimized: bool = False,
        panic_abort: bool = False,
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
        if rust_prior and not rust_prior_from_env:
            command.append("--unialloc-auto-rust-lifetime-prior")
        if actual_rewrite:
            command.append("--unialloc-actual-semantic-scope-rewrite")
        if global_hint is not None:
            command.extend(["--unialloc-lifetime-hint", str(global_hint)])
        if lifetime_profile is not None:
            command.extend(["--unialloc-lifetime-profile", str(lifetime_profile)])
        command.extend(["--", "--sysroot", str(self.sysroot)])
        if release_optimized:
            command.extend(["-C", "opt-level=3"])
        else:
            command.append("-Zmir-opt-level=0")
        if panic_abort:
            command.extend(["-C", "panic=abort"])
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
        env.pop("UNIALLOC_AUTO_RUST_LIFETIME_PRIOR", None)
        if automatic and automatic_from_env:
            env["UNIALLOC_AUTO_HEAP_LIFETIME_INFERENCE"] = "1"
        if rust_prior and rust_prior_from_env:
            env["UNIALLOC_AUTO_RUST_LIFETIME_PRIOR"] = "1"
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

    def receiver_allocation_row(
        self, audit: dict[str, object], function: str
    ) -> dict[str, object]:
        rows = [
            row
            for row in audit.get("rewrite_candidates", [])  # type: ignore[union-attr]
            if isinstance(row, dict)
            and str(row.get("mir_function") or "").endswith(function)
            and row.get("lowering_kind") == "semantic_scope_enter_exit_rewrite"
            and "reserve" in str(row.get("callee") or "")
        ]
        self.assertEqual(len(rows), 1, rows)
        return rows[0]

    def test_exact_drop_move_forget_and_leak_proofs(self) -> None:
        audit = self.run_pass("heap-inference", automatic=True)
        local = self.assert_hint(
            audit, "exact_local_drop", 0, 0, LOCAL_DROP_BASIS
        )
        moved = self.assert_hint(
            audit, "exact_move_chain_drop", 0, 0, MOVE_DROP_BASIS
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
            environment, "exact_local_drop", 0, 0, LOCAL_DROP_BASIS
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
        self.assert_hint(audit, "box_local_drop", 0, 0, LOCAL_DROP_BASIS)
        self.assert_hint(
            audit, "box_move_chain_drop", 0, 0, MOVE_DROP_BASIS
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

    def test_rust_lifetime_prior_is_separate_opt_in_with_manual_precedence(self) -> None:
        baseline = self.run_pass(
            "rust-prior-disabled",
            fixture=self.release_box_fixture,
            panic_abort=True,
        )
        self.assert_hint(baseline, "box_local_drop", 0, 0, "default_unknown")

        environment = self.run_pass(
            "rust-prior-env",
            rust_prior=True,
            rust_prior_from_env=True,
            fixture=self.release_box_fixture,
            panic_abort=True,
        )
        self.assert_hint(
            environment,
            "box_local_drop",
            1,
            85,
            RUST_PRIOR_LOCAL_RELEASE_SHORT_BASIS,
        )
        compiler_pass = environment["compiler_pass"]
        self.assertTrue(compiler_pass["automatic_rust_lifetime_prior_enabled"])
        self.assertFalse(compiler_pass["automatic_heap_lifetime_inference_enabled"])
        audit_text = (self.tmp / "rust-prior-env.json").read_text(encoding="utf-8")
        self.assertEqual(
            1,
            audit_text.count('"automatic_rust_lifetime_prior_enabled":'),
            "the audit must bind the prior mode with one unambiguous JSON key",
        )

        manual = self.run_pass(
            "rust-prior-manual",
            rust_prior=True,
            global_hint=2,
            fixture=self.release_box_fixture,
            panic_abort=True,
        )
        self.assert_hint(
            manual, "box_local_drop", 2, 100, "manual_global_lifetime_hint"
        )

        seed_row = self.receiver_allocation_row(
            environment, "receiver_owned_reserve_then_release"
        )
        self.assertNotEqual(seed_row["type_id"], 0, seed_row)
        profile = self.tmp / "rust-prior-precedence.profile"
        profile.write_text(
            "\n".join(
                [
                    "unialloc-lifetime-profile-v2",
                    f"{seed_row['callsite']} {seed_row['type_id']} "
                    f"{seed_row['module_id']} long-lived 99",
                    "",
                ]
            ),
            encoding="utf-8",
        )
        profiled = self.run_pass(
            "rust-prior-profile",
            rust_prior=True,
            lifetime_profile=profile,
            fixture=self.release_box_fixture,
            panic_abort=True,
        )
        profiled_row = self.receiver_allocation_row(
            profiled, "receiver_owned_reserve_then_release"
        )
        self.assertEqual(profiled_row["lifetime_hint"], 2, profiled_row)
        self.assertEqual(profiled_row["lifetime_hint_confidence"], 99, profiled_row)
        self.assertEqual(
            profiled_row["lifetime_hint_basis"], "profile_exact_match", profiled_row
        )

    def test_rust_lifetime_prior_uses_owner_live_ranges_and_carriers(self) -> None:
        audit = self.run_pass(
            "rust-prior-ownership",
            rust_prior=True,
            fixture=self.release_box_fixture,
            panic_abort=True,
        )
        self.assert_hint(
            audit,
            "box_local_drop",
            1,
            85,
            RUST_PRIOR_LOCAL_RELEASE_SHORT_BASIS,
        )
        self.assert_hint(
            audit,
            "box_long_work_then_drop",
            0,
            0,
            RUST_PRIOR_OWNER_LIVE_CALL_UNKNOWN_BASIS,
        )
        returned = self.assert_hint(
            audit,
            "return_owner_through_aggregate",
            2,
            70,
            RUST_PRIOR_RETURN_LONG_BASIS,
        )
        self.assertTrue(returned["lifetime_analysis_features"]["return_sink"])
        self.assertGreaterEqual(
            returned["lifetime_analysis_features"]["owner_move_count"], 2
        )
        self.assertTrue(
            returned["lifetime_analysis_features"]["classification_rule_applied"]
        )

        nonowner_projection = self.assert_hint(
            audit,
            "return_nonowner_projection_from_pair",
            0,
            0,
            RUST_PRIOR_OWNER_LIVE_CALL_UNKNOWN_BASIS,
        )
        self.assertFalse(
            nonowner_projection["lifetime_analysis_features"]["return_sink"],
            nonowner_projection,
        )
        self.assertTrue(
            nonowner_projection["lifetime_analysis_features"]["store_sink"],
            nonowner_projection,
        )
        self.assertFalse(
            nonowner_projection["lifetime_analysis_features"][
                "classification_rule_applied"
            ],
            nonowner_projection,
        )

        receiver = self.receiver_allocation_row(
            audit, "receiver_owned_reserve_then_release"
        )
        self.assertEqual(receiver["lifetime_hint"], 0, receiver)
        self.assertEqual(receiver["lifetime_hint_confidence"], 0, receiver)
        self.assertEqual(
            receiver["lifetime_hint_basis"],
            RUST_PRIOR_RECEIVER_LAYOUT_UNKNOWN_BASIS,
            receiver,
        )
        self.assertFalse(
            receiver["lifetime_analysis_features"]["runtime_join_key_complete"],
            receiver,
        )

        generic_return = self.assert_hint(
            audit,
            "return_generic_box",
            0,
            0,
            RUST_PRIOR_RETURN_LAYOUT_UNKNOWN_BASIS,
        )
        generic_features = generic_return["lifetime_analysis_features"]
        self.assertTrue(generic_features["return_sink"], generic_return)
        self.assertFalse(
            generic_features["runtime_join_key_complete"], generic_return
        )
        self.assertEqual(
            generic_features["requested_layout_basis"],
            "dynamic_or_unproven_requested_layout",
            generic_return,
        )

        conditional = self.receiver_allocation_row(audit, "receiver_conditional")
        self.assertEqual(conditional["lifetime_hint"], 0, conditional)
        self.assertEqual(conditional["lifetime_hint_basis"], "default_unknown", conditional)
        conditional_features = conditional["lifetime_analysis_features"]
        self.assertTrue(conditional_features["return_sink"], conditional)
        self.assertTrue(conditional_features["store_sink"], conditional)
        self.assertTrue(conditional_features["conditional_drop_path"], conditional)

    def test_rust_lifetime_prior_abstains_on_owner_live_cleanup_and_pressure(self) -> None:
        audit = self.run_pass(
            "rust-prior-cleanup",
            rust_prior=True,
            fixture=self.release_box_fixture,
            release_optimized=False,
        )
        for function in (
            "box_long_work_then_drop",
            "box_may_unwind_then_drop",
            "box_may_unwind_then_forget",
        ):
            self.assert_hint(
                audit, function, 0, 0, RUST_PRIOR_CLEANUP_UNKNOWN_BASIS
            )

        # Cleanup reached only after the owner was released cannot shorten its
        # lifetime.  The prior must use owner liveness, rather than the mere
        # presence of a later cleanup block, when deciding whether to abstain.
        self.assert_hint(
            audit,
            "box_drop_then_may_unwind",
            1,
            85,
            RUST_PRIOR_LOCAL_RELEASE_SHORT_BASIS,
        )

        receiver = self.receiver_allocation_row(audit, "receiver_owned_reserve")
        self.assertEqual(receiver["lifetime_hint"], 0, receiver)
        self.assertEqual(receiver["lifetime_hint_confidence"], 0, receiver)
        self.assertEqual(
            receiver["lifetime_hint_basis"],
            RUST_PRIOR_CLEANUP_UNKNOWN_BASIS,
            receiver,
        )

    def test_rust_lifetime_prior_preserves_exact_process_long_oracle(self) -> None:
        audit = self.run_pass(
            "rust-prior-exact-oracle",
            rust_prior=True,
            fixture=self.release_box_fixture,
            panic_abort=True,
        )
        self.assert_hint(audit, "box_mem_forget", 0xA102, 100, FORGET_BASIS)
        self.assert_hint(audit, "box_leak", 0xA102, 100, LEAK_BASIS)

    def test_rust_lifetime_prior_transports_advisory_hint_in_actual_rewrite(self) -> None:
        audit = self.run_pass(
            "rust-prior-actual-transport",
            rust_prior=True,
            actual_rewrite=True,
            fixture=self.prior_transport_fixture,
            panic_abort=True,
        )
        row = self.assert_hint(
            audit,
            "returned_owner",
            2,
            70,
            RUST_PRIOR_RETURN_LONG_BASIS,
        )
        self.assertTrue(str(row["rewrite_status"]).endswith("_applied"), row)
        short_row = self.assert_hint(
            audit,
            "all_path_local_release",
            1,
            85,
            RUST_PRIOR_LOCAL_RELEASE_SHORT_BASIS,
        )
        self.assertTrue(str(short_row["rewrite_status"]).endswith("_applied"), short_row)
        completed = subprocess.run(
            [str(self.tmp / "rust-prior-actual-transport")],
            cwd=ROOT,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertIn("observed_hints=", completed.stdout)

    def test_measured_box_long_parity_kernel(self) -> None:
        audit = self.run_pass(
            "rust-prior-measured-box-long",
            rust_prior=True,
            actual_rewrite=True,
            fixture=self.measured_box_long_fixture,
            panic_abort=True,
        )

        for function, requested_size in (
            ("convex_sized_box", 6_000),
            ("lume_roaring_bedrock_sized_box", 8_192),
            ("datafusion_sized_box", 16_384),
        ):
            row = self.assert_hint(
                audit,
                function,
                2,
                70,
                RUST_PRIOR_RETURN_LONG_BASIS,
            )
            self.assertEqual(
                row["rewrite_status"],
                "actual_semantic_scope_generic_type_rewrite_applied",
                row,
            )
            self.assertEqual(
                row["replacement_symbol"],
                "__unialloc_semantic_scope_push_for_rust_type_hints",
                row,
            )
            self.assertEqual(
                row["replacement_resolution_status"],
                "resolved_unialloc_semantic_scope_push_for_rust_type_hints_pop",
                row,
            )
            self.assertEqual(
                row["metadata_pairing_contract"],
                "semantic_scope_monomorphized_runtime_type_metadata",
                row,
            )
            features = row["lifetime_analysis_features"]
            self.assertTrue(features["classification_rule_applied"], row)
            self.assertTrue(features["runtime_join_key_complete"], row)
            self.assertEqual(
                features["requested_layout_basis"],
                "exact_box_new_payload_layout",
                row,
            )
            self.assertEqual(
                features["runtime_join_key"]["requested_size_bytes"],
                requested_size,
                row,
            )
            self.assertEqual(
                features["runtime_join_key"]["requested_align_bytes"],
                1,
                row,
            )

        completed = subprocess.run(
            [str(self.tmp / "rust-prior-measured-box-long")],
            cwd=ROOT,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertIn("observed_hints=8", completed.stdout)

    def test_rust_lifetime_prior_abstains_before_unjoinable_generic_transport(
        self,
    ) -> None:
        audit = self.run_pass(
            "rust-prior-generic-layout-abstain",
            rust_prior=True,
            actual_rewrite=True,
            fixture=self.generic_prior_transport_fixture,
            panic_abort=True,
        )
        row = self.assert_hint(
            audit,
            "returned_generic_owner",
            0,
            0,
            RUST_PRIOR_RETURN_LAYOUT_UNKNOWN_BASIS,
        )
        self.assertEqual(
            row["rewrite_status"],
            "actual_semantic_scope_generic_type_rewrite_applied",
            row,
        )
        self.assertFalse(
            row["lifetime_analysis_features"]["runtime_join_key_complete"], row
        )
        completed = subprocess.run(
            [str(self.tmp / "rust-prior-generic-layout-abstain")],
            cwd=ROOT,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertIn("observed_hints=0", completed.stdout)

    def test_direct_long_requires_exact_in_band_vec_or_box_layout(self) -> None:
        audit = self.run_pass(
            "rust-prior-direct-long-layout-gate",
            rust_prior=True,
            actual_rewrite=True,
            fixture=self.constant_vec_layout_fixture,
            panic_abort=True,
        )

        for function, requested_size in (
            ("zeroed_u64_column", 24_576),
            ("lower_boundary_column", 4_096),
            ("upper_boundary_column", 28_032),
        ):
            row = self.assert_hint(
                audit,
                function,
                2,
                70,
                RUST_PRIOR_RETURN_LONG_BASIS,
            )
            features = row["lifetime_analysis_features"]
            self.assertEqual(
                features["requested_layout_basis"],
                "exact_vec_with_capacity_requested_layout",
                row,
            )
            self.assertEqual(
                features["runtime_join_key"]["requested_size_bytes"],
                requested_size,
                row,
            )
            self.assertNotEqual(row["type_id"], 0, row)

        for function, requested_size in (
            ("below_boundary_column", 4_095),
            ("above_boundary_column", 28_033),
        ):
            row = self.assert_hint(
                audit,
                function,
                0,
                0,
                RUST_PRIOR_RETURN_OUT_OF_BAND_LAYOUT_UNKNOWN_BASIS,
            )
            features = row["lifetime_analysis_features"]
            self.assertEqual(
                features["runtime_join_key"]["requested_size_bytes"],
                requested_size,
                row,
            )
            self.assertFalse(features["classification_rule_applied"], row)

        dynamic = self.assert_hint(
            audit,
            "dynamic_capacity_column",
            0,
            0,
            RUST_PRIOR_RETURN_LAYOUT_UNKNOWN_BASIS,
        )
        dynamic_features = dynamic["lifetime_analysis_features"]
        self.assertEqual(
            dynamic_features["requested_layout_basis"],
            "dynamic_or_unproven_requested_layout",
            dynamic,
        )
        self.assertFalse(dynamic_features["runtime_join_key_complete"], dynamic)

        for function, requested_size in (
            ("tantivy_sstable_const_mul_capacity", 8_000),
            ("single_assignment_const_add_capacity", 8_096),
        ):
            row = self.assert_hint(
                audit,
                function,
                2,
                70,
                RUST_PRIOR_RETURN_LONG_BASIS,
            )
            features = row["lifetime_analysis_features"]
            self.assertEqual(
                features["requested_layout_basis"],
                "exact_vec_with_capacity_requested_layout",
                row,
            )
            self.assertEqual(
                features["runtime_join_key"]["requested_size_bytes"],
                requested_size,
                row,
            )

        for function in (
            "parameter_mul_capacity",
            "multiply_reassigned_local",
            "multiply_loop_phi",
        ):
            row = self.assert_hint(
                audit,
                function,
                0,
                0,
                RUST_PRIOR_RETURN_LAYOUT_UNKNOWN_BASIS,
            )
            features = row["lifetime_analysis_features"]
            self.assertEqual(
                features["requested_layout_basis"],
                "dynamic_or_unproven_requested_layout",
                row,
            )
            self.assertFalse(features["runtime_join_key_complete"], row)

        tiny_box = self.assert_hint(
            audit,
            "tiny_box",
            0,
            0,
            RUST_PRIOR_RETURN_OUT_OF_BAND_LAYOUT_UNKNOWN_BASIS,
        )
        tiny_box_features = tiny_box["lifetime_analysis_features"]
        self.assertEqual(
            tiny_box_features["requested_layout_basis"],
            "exact_box_new_payload_layout",
            tiny_box,
        )
        self.assertEqual(
            tiny_box_features["runtime_join_key"]["requested_size_bytes"],
            128,
            tiny_box,
        )

        completed = subprocess.run(
            [str(self.tmp / "rust-prior-direct-long-layout-gate")],
            cwd=ROOT,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertIn("observed_hints=8", completed.stdout)

    def test_borrowed_vec_reserve_uses_runtime_layout_long_gate(self) -> None:
        audit = self.run_pass(
            "rust-prior-borrowed-vec-reserve-long",
            rust_prior=True,
            actual_rewrite=True,
            fixture=self.borrowed_vec_reserve_fixture,
            panic_abort=True,
        )
        reserve_rows = [
            row
            for row in audit["rewrite_candidates"]
            if row.get("lowering_kind") == "semantic_scope_enter_exit_rewrite"
            and "reserve_exact" in str(row.get("callee") or "")
        ]
        positive = [
            row
            for row in reserve_rows
            if str(row.get("mir_function") or "").endswith("tantivy_borrowed_reserve")
        ]
        self.assertEqual(len(positive), 1, positive)
        row = positive[0]
        self.assertEqual(row["lifetime_hint"], 2, row)
        self.assertEqual(row["lifetime_hint_confidence"], 70, row)
        self.assertEqual(
            row["lifetime_hint_basis"],
            RUST_PRIOR_BORROWED_VEC_RESERVE_LONG_BASIS,
            row,
        )
        self.assertNotEqual(row["type_id"], 0, row)
        self.assertTrue(str(row["rewrite_status"]).endswith("_applied"), row)
        features = row["lifetime_analysis_features"]
        self.assertTrue(features["borrowed_vec_reserve_prior_eligible"], row)
        self.assertTrue(features["classification_rule_applied"], row)
        self.assertTrue(features["runtime_layout_captured_by_semantic_scope"], row)
        self.assertEqual(
            features["requested_layout_basis"],
            "exact_borrowed_vec_reserve_runtime_layout",
            row,
        )
        self.assertFalse(features["runtime_join_key_complete"], row)
        self.assertEqual(features["runtime_observation_min_requested_bytes"], 4_096)
        self.assertEqual(features["runtime_observation_max_requested_bytes"], 28_032)

        local = [
            candidate
            for candidate in reserve_rows
            if str(candidate.get("mir_function") or "").endswith(
                "local_borrowed_reserve_must_abstain"
            )
        ]
        self.assertEqual(len(local), 1, local)
        self.assertNotEqual(local[0]["lifetime_hint"], 2, local[0])
        self.assertFalse(
            local[0]["lifetime_analysis_features"][
                "borrowed_vec_reserve_prior_eligible"
            ],
            local[0],
        )

        completed = subprocess.run(
            [str(self.tmp / "rust-prior-borrowed-vec-reserve-long")],
            cwd=ROOT,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertIn("observed_hints=8", completed.stdout)


if __name__ == "__main__":
    unittest.main()
