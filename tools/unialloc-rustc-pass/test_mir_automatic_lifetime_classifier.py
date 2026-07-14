#!/usr/bin/env python3
"""Process-level tests for the conservative MIR lifetime classifier."""

from __future__ import annotations

import json
import os
from pathlib import Path
import platform
import shutil
import subprocess
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[2]
PASS_SOURCE = ROOT / "tools/unialloc-rustc-pass/unialloc-rustc-mir-rewrite-dry-run.rs"
TOOLCHAIN = (ROOT / "rust-toolchain").read_text(encoding="utf-8").strip()

SHORT_BASIS = "automatic_exact_local_drop_before_phase_boundary"
LONG_BASIS = "automatic_exact_local_drop_after_phase_boundary"
ESCAPE_UNKNOWN_BASIS = "automatic_nonlinear_control_flow_unknown"
ALIAS_UNKNOWN_BASIS = "automatic_alias_or_escape_unknown"
CALL_UNKNOWN_BASIS = "automatic_intervening_call_may_advance_epoch_unknown"
CLEANUP_UNKNOWN_BASIS = "automatic_cleanup_before_boundary_unknown"
UNSUPPORTED_UNKNOWN_BASIS = "automatic_unsupported_site_unknown"
EFFECTFUL_DROP_UNKNOWN_BASIS = "automatic_effectful_drop_glue_unknown"


class MirAutomaticLifetimeClassifierTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls._tmp = tempfile.TemporaryDirectory(
            prefix="unialloc-auto-lifetime-", dir="/tmp" if Path("/tmp").is_dir() else None
        )
        cls.tmp = Path(cls._tmp.name)
        cls.rustc = shutil.which("rustc") or "rustc"
        cls.sysroot = Path(
            subprocess.check_output(
                [cls.rustc, f"+{TOOLCHAIN}", "--print", "sysroot"], cwd=ROOT, text=True
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
    pub mod lifetime_hugepage {
        #[inline(never)]
        pub fn lifetime_hugepage_advance_epoch() -> usize {
            std::sync::atomic::compiler_fence(std::sync::atomic::Ordering::SeqCst);
            1
        }
    }
}
pub use alloc_api::lifetime_hugepage::lifetime_hugepage_advance_epoch;
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

        cls.fixture = cls.tmp / "automatic_lifetime_probe.rs"
        cls.fixture.write_text(
            """extern crate unialloc;

#[inline(never)]
fn short_site() {
    let _local = Vec::<u64>::with_capacity(16);
}

#[inline(never)]
fn long_site() {
    let _survivor = Vec::<u64>::with_capacity(32);
    let _ = unialloc::lifetime_hugepage_advance_epoch();
}

#[inline(never)]
fn long_read_only_site() {
    let survivor = Vec::<u64>::with_capacity(48);
    let _ = unialloc::lifetime_hugepage_advance_epoch();
    let _observed_len = survivor.len();
}

#[inline(never)]
fn escaping_site() -> Vec<u64> {
    Vec::<u64>::with_capacity(64)
}

#[inline(never)]
fn lifetime_hugepage_advance_epoch() {}

#[inline(never)]
fn lookalike_is_not_a_boundary() {
    let _local = Vec::<u64>::with_capacity(128);
    lifetime_hugepage_advance_epoch();
}

#[inline(never)]
fn hidden_real_boundary() {
    let _ = unialloc::lifetime_hugepage_advance_epoch();
}

#[inline(never)]
fn hidden_boundary_abstains() {
    let _local = Vec::<u64>::with_capacity(256);
    hidden_real_boundary();
}

#[inline(never)]
fn may_panic(flag: bool) {
    if flag {
        panic!("classifier cleanup edge");
    }
}

#[inline(never)]
fn cleanup_before_boundary_abstains(flag: bool) {
    let _local = Vec::<u64>::with_capacity(512);
    may_panic(flag);
    let _ = unialloc::lifetime_hugepage_advance_epoch();
}

#[inline(never)]
fn receiver_temporary_is_not_the_owner() {
    let mut owner = Vec::<u64>::with_capacity(4);
    owner.push(1);
    let temporary = owner.drain(..);
    drop(temporary);
    drop(owner);
}

#[inline(never)]
fn mutable_borrow_abstains() {
    let mut owner = Vec::<u64>::with_capacity(8);
    owner.push(1);
    let _ = unialloc::lifetime_hugepage_advance_epoch();
    drop(owner);
}

struct AdvancesEpochOnDrop;

impl Drop for AdvancesEpochOnDrop {
    fn drop(&mut self) {
        let _ = unialloc::lifetime_hugepage_advance_epoch();
    }
}

#[inline(never)]
fn effectful_drop_glue_abstains() {
    let _owner = Box::new(AdvancesEpochOnDrop);
}

#[inline(never)]
fn intervening_guard_drop_abstains() {
    let _owner = Vec::<u64>::with_capacity(24);
    // Rust drops locals in reverse declaration order, so this guard's custom
    // Drop runs before `_owner` is implicitly dropped at the function exit.
    let _guard = AdvancesEpochOnDrop;
}

#[inline(never)]
fn cleanup_assert_abstains(should_unwind: bool) {
    let _owner = Vec::<u64>::with_capacity(28);
    let values = [7_u8; 1];
    let _observed = values[should_unwind as usize];
    // The successful bounds-Assert path reaches an otherwise-Ephemeral exact
    // local Drop. Its unwind edge remains opaque because application panic
    // handling can advance the process epoch before cleanup releases `_owner`.
}

#[cfg(target_arch = "x86_64")]
#[inline(never)]
fn inline_asm_abstains() {
    let _owner = Vec::<u64>::with_capacity(36);
    unsafe {
        core::arch::asm!("nop", options(nomem, nostack, preserves_flags));
    }
}

#[inline(never)]
fn nested_multi_owner_uses_recovery() {
    let _owner = Box::new(Vec::<u8>::with_capacity(4));
}

fn main() {
    short_site();
    long_site();
    long_read_only_site();
    drop(escaping_site());
    lookalike_is_not_a_boundary();
    hidden_boundary_abstains();
    cleanup_before_boundary_abstains(false);
    receiver_temporary_is_not_the_owner();
    mutable_borrow_abstains();
    effectful_drop_glue_abstains();
    intervening_guard_drop_abstains();
    cleanup_assert_abstains(false);
    #[cfg(target_arch = "x86_64")]
    inline_asm_abstains();
    nested_multi_owner_uses_recovery();
    let observed = unialloc::observed_hints();
    println!("observed_hints={observed}");
    assert_eq!(observed & ((1u64 << 1) | (1u64 << 2)), 6);
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
        actual_semantic_rewrite: bool = False,
        profile: Path | None = None,
        global_hint: int | None = None,
        direct_local: bool = False,
        direct_local_metadata: bool = False,
        fixture: Path | None = None,
    ) -> dict[str, object]:
        audit = self.tmp / f"{label}.json"
        command = [
            str(self.driver),
            "--unialloc-rewrite-audit-out",
            str(audit),
            "--unialloc-continue-compilation",
        ]
        if automatic and not automatic_from_env:
            command.append("--unialloc-auto-lifetime-classifier")
        if actual_semantic_rewrite:
            command.append("--unialloc-actual-semantic-scope-rewrite")
        if profile is not None:
            command.extend(["--unialloc-lifetime-profile", str(profile)])
        if global_hint is not None:
            command.extend(["--unialloc-lifetime-hint", str(global_hint)])
        if direct_local or direct_local_metadata:
            command.append("--unialloc-direct-local-metadata-abi")
        if direct_local:
            command.append("--unialloc-direct-local-size-align-with-semantic-drop")
        command.extend(
            [
                "--",
                "--sysroot",
                str(self.sysroot),
                "--edition=2021",
                "--extern",
                f"unialloc={self.stub_rlib}",
                "-o",
                str(self.tmp / label),
                str(fixture or self.fixture),
            ]
        )
        env = os.environ.copy()
        env.pop("UNIALLOC_AUTO_LIFETIME_CLASSIFIER", None)
        if automatic and automatic_from_env:
            env["UNIALLOC_AUTO_LIFETIME_CLASSIFIER"] = "1"
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

    def semantic_allocation_rows(
        self,
        audit: dict[str, object],
        function: str,
        *,
        callee_marker: str | None = None,
    ) -> list[dict[str, object]]:
        return [
            row
            for row in audit.get("rewrite_candidates", [])  # type: ignore[union-attr]
            if isinstance(row, dict)
            and str(row.get("mir_function") or "").endswith(function)
            and row.get("lowering_kind") == "semantic_scope_enter_exit_rewrite"
            and (
                callee_marker is None
                or callee_marker in str(row.get("callee") or "")
            )
        ]

    def allocation_row_for_callee(
        self,
        audit: dict[str, object],
        function: str,
        callee_marker: str,
    ) -> dict[str, object]:
        rows = self.semantic_allocation_rows(
            audit, function, callee_marker=callee_marker
        )
        self.assertEqual(len(rows), 1, rows)
        return rows[0]

    def allocation_row(self, audit: dict[str, object], function: str) -> dict[str, object]:
        return self.allocation_row_for_callee(audit, function, "with_capacity")

    def matching_drop_rows(
        self, audit: dict[str, object], allocation: dict[str, object]
    ) -> list[dict[str, object]]:
        return [
            row
            for row in audit.get("rewrite_candidates", [])  # type: ignore[union-attr]
            if isinstance(row, dict)
            and row.get("mir_function") == allocation.get("mir_function")
            and row.get("semantic_object_type") == allocation.get("semantic_object_type")
            and row.get("destination_place") == allocation.get("destination_place")
            and row.get("lowering_kind") == "semantic_scope_drop_rewrite"
        ]

    def receiver_drain_row(self, audit: dict[str, object]) -> dict[str, object]:
        rows = [
            row
            for row in audit.get("rewrite_candidates", [])  # type: ignore[union-attr]
            if isinstance(row, dict)
            and str(row.get("mir_function") or "").endswith(
                "receiver_temporary_is_not_the_owner"
            )
            and row.get("lowering_kind") == "semantic_scope_enter_exit_rewrite"
            and "::drain" in str(row.get("callee") or "")
        ]
        self.assertEqual(len(rows), 1, rows)
        return rows[0]

    def multi_owner_drop_rows(
        self, audit: dict[str, object], function: str
    ) -> list[dict[str, object]]:
        return [
            row
            for row in audit.get("rewrite_candidates", [])  # type: ignore[union-attr]
            if isinstance(row, dict)
            and str(row.get("mir_function") or "").endswith(function)
            and row.get("lowering_kind")
            == "semantic_scope_drop_multiple_heap_owners_skipped"
        ]

    def direct_layout_rows(
        self, audit: dict[str, object], function: str
    ) -> list[dict[str, object]]:
        return [
            row
            for row in audit.get("rewrite_candidates", [])  # type: ignore[union-attr]
            if isinstance(row, dict)
            and str(row.get("mir_function") or "").endswith(function)
            and row.get("lowering_kind") == "direct_allocator_call_rewrite"
        ]

    def direct_layout_rows_by_operation(
        self, audit: dict[str, object], function: str
    ) -> dict[str, dict[str, object]]:
        rows_by_operation: dict[str, dict[str, object]] = {}
        for row in self.direct_layout_rows(audit, function):
            symbol = str(row.get("replacement_symbol") or "")
            operation = next(
                (
                    candidate
                    for candidate in ("alloc", "dealloc")
                    if f"__unialloc_{candidate}_layout_with_metadata" in symbol
                ),
                None,
            )
            self.assertIsNotNone(operation, row)
            assert operation is not None
            self.assertNotIn(operation, rows_by_operation, row)
            rows_by_operation[operation] = row
        self.assertEqual(set(rows_by_operation), {"alloc", "dealloc"}, rows_by_operation)
        return rows_by_operation

    def assert_class(
        self,
        audit: dict[str, object],
        function: str,
        *,
        hint: int,
        confidence: int,
        basis: str,
        paired_drop: bool,
    ) -> dict[str, object]:
        allocation = self.allocation_row(audit, function)
        self.assertEqual(allocation["lifetime_hint"], hint, allocation)
        self.assertEqual(allocation["lifetime_hint_confidence"], confidence, allocation)
        self.assertEqual(allocation["lifetime_hint_basis"], basis, allocation)
        drops = self.matching_drop_rows(audit, allocation)
        if paired_drop:
            self.assertTrue(drops, allocation)
            for drop in drops:
                self.assertEqual(drop["lifetime_hint"], hint, drop)
                self.assertEqual(drop["lifetime_hint_confidence"], confidence, drop)
                self.assertEqual(drop["lifetime_hint_basis"], basis, drop)
        return allocation

    def assert_recovery_hint_scope(self, row: dict[str, object]) -> None:
        self.assertEqual(
            row.get("replacement_symbol"),
            "__unialloc_semantic_scope_push_hints",
            row,
        )
        self.assertFalse(str(row.get("replacement_symbol") or "").endswith("_local"), row)

    def test_exact_linear_drop_classifies_around_exact_phase_boundary(self) -> None:
        audit = self.run_pass("automatic", automatic=True)
        self.assert_class(
            audit,
            "short_site",
            hint=1,
            confidence=100,
            basis=SHORT_BASIS,
            paired_drop=True,
        )
        self.assert_class(
            audit,
            "long_site",
            hint=2,
            confidence=100,
            basis=LONG_BASIS,
            paired_drop=True,
        )
        self.assert_class(
            audit,
            "long_read_only_site",
            hint=2,
            confidence=100,
            basis=LONG_BASIS,
            paired_drop=True,
        )
        self.assert_class(
            audit,
            "escaping_site",
            hint=0,
            confidence=0,
            basis=ESCAPE_UNKNOWN_BASIS,
            paired_drop=False,
        )
        self.assert_class(
            audit,
            "lookalike_is_not_a_boundary",
            hint=0,
            confidence=0,
            basis=CALL_UNKNOWN_BASIS,
            paired_drop=True,
        )
        self.assert_class(
            audit,
            "hidden_boundary_abstains",
            hint=0,
            confidence=0,
            basis=CALL_UNKNOWN_BASIS,
            paired_drop=True,
        )
        self.assert_class(
            audit,
            "cleanup_before_boundary_abstains",
            hint=0,
            confidence=0,
            basis=CLEANUP_UNKNOWN_BASIS,
            paired_drop=True,
        )
        receiver_drain = self.receiver_drain_row(audit)
        self.assertEqual(receiver_drain["lifetime_hint"], 0, receiver_drain)
        self.assertEqual(receiver_drain["lifetime_hint_confidence"], 0, receiver_drain)
        self.assertEqual(
            receiver_drain["lifetime_hint_basis"],
            UNSUPPORTED_UNKNOWN_BASIS,
            receiver_drain,
        )
        self.assert_class(
            audit,
            "mutable_borrow_abstains",
            hint=0,
            confidence=0,
            basis=ALIAS_UNKNOWN_BASIS,
            paired_drop=True,
        )

        compiler_pass = audit["compiler_pass"]
        self.assertTrue(compiler_pass["automatic_lifetime_classifier_enabled"])
        self.assertGreaterEqual(compiler_pass["automatic_lifetime_ephemeral_count"], 2)
        self.assertGreaterEqual(compiler_pass["automatic_lifetime_long_lived_count"], 2)
        self.assertGreaterEqual(compiler_pass["automatic_lifetime_unknown_count"], 1)
        self.assertEqual(
            compiler_pass["automatic_lifetime_ephemeral_allocation_site_count"], 1
        )
        self.assertEqual(
            compiler_pass["automatic_lifetime_long_lived_allocation_site_count"], 2
        )
        self.assertGreaterEqual(
            compiler_pass["automatic_lifetime_unknown_allocation_site_count"], 4
        )
        self.assertEqual(
            compiler_pass["automatic_lifetime_classifier_precedence"],
            "exact_profile>manual_global>automatic>Unknown",
        )

    def test_actual_rewrite_executes_short_and_long_hint_abis(self) -> None:
        audit = self.run_pass(
            "automatic-actual",
            automatic=True,
            actual_semantic_rewrite=True,
        )
        completed = subprocess.run(
            [str(self.tmp / "automatic-actual")],
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
            and row.get("lifetime_hint_basis") in {SHORT_BASIS, LONG_BASIS}
        ]
        self.assertTrue(applied)
        self.assertTrue(
            all(str(row.get("rewrite_status") or "").endswith("_applied") for row in applied),
            applied,
        )
        self.assertEqual({int(row["lifetime_hint"]) for row in applied}, {1, 2})

    def test_effectful_payload_drop_glue_abstains_from_short_classification(self) -> None:
        audit = self.run_pass("effectful-drop-glue", automatic=True)
        allocation = self.allocation_row_for_callee(
            audit, "effectful_drop_glue_abstains", "::new"
        )
        self.assertEqual(allocation["lifetime_hint"], 0, allocation)
        self.assertEqual(allocation["lifetime_hint_confidence"], 0, allocation)
        self.assertEqual(
            allocation["lifetime_hint_basis"],
            EFFECTFUL_DROP_UNKNOWN_BASIS,
            allocation,
        )
        drops = self.matching_drop_rows(audit, allocation)
        self.assertTrue(drops, allocation)
        for drop in drops:
            self.assertEqual(drop["lifetime_hint"], 0, drop)
            self.assertEqual(drop["lifetime_hint_confidence"], 0, drop)
            self.assertEqual(
                drop["lifetime_hint_basis"], EFFECTFUL_DROP_UNKNOWN_BASIS, drop
            )

    def test_implicit_effectful_guard_drop_before_owner_abstains(self) -> None:
        audit = self.run_pass("intervening-effectful-guard-drop", automatic=True)
        allocation = self.assert_class(
            audit,
            "intervening_guard_drop_abstains",
            hint=0,
            confidence=0,
            basis=CALL_UNKNOWN_BASIS,
            paired_drop=True,
        )
        self.assertIn("Vec<u64", str(allocation.get("semantic_object_type") or ""))

    def test_pre_boundary_assert_cleanup_abstains_from_short_classification(
        self,
    ) -> None:
        audit = self.run_pass("pre-boundary-cleanup-drop-glue", automatic=True)
        allocation = self.assert_class(
            audit,
            "cleanup_assert_abstains",
            hint=0,
            confidence=0,
            basis=CLEANUP_UNKNOWN_BASIS,
            paired_drop=True,
        )
        self.assertIn("Vec<u64", str(allocation.get("semantic_object_type") or ""))

    @unittest.skipUnless(
        platform.machine().lower() in {"x86_64", "amd64"},
        "inline-assembly regression uses x86_64 nop syntax",
    )
    def test_inline_asm_between_allocation_and_drop_abstains(self) -> None:
        audit = self.run_pass("inline-asm-abstains", automatic=True)
        allocation = self.assert_class(
            audit,
            "inline_asm_abstains",
            hint=0,
            confidence=0,
            basis=CALL_UNKNOWN_BASIS,
            paired_drop=True,
        )
        self.assertIn("Vec<u64", str(allocation.get("semantic_object_type") or ""))

    def test_nested_multi_owner_drop_forces_recovery_abi(self) -> None:
        audit = self.run_pass(
            "nested-multi-owner-direct-local",
            automatic=True,
            direct_local=True,
        )
        allocation = self.allocation_row_for_callee(
            audit, "nested_multi_owner_uses_recovery", "::new"
        )
        self.assertIn("Box<", str(allocation.get("semantic_object_type") or ""))
        self.assert_recovery_hint_scope(allocation)

        skipped_drops = self.multi_owner_drop_rows(
            audit, "nested_multi_owner_uses_recovery"
        )
        self.assertTrue(skipped_drops, allocation)
        for drop in skipped_drops:
            self.assertEqual(
                drop.get("rewrite_status"),
                "semantic_scope_drop_rewrite_skipped_multiple_heap_owners",
                drop,
            )
            self.assertEqual(
                drop.get("replacement_resolution_status"),
                "rustc_middle_drop_multiple_heap_owners_not_lowered",
                drop,
            )
            self.assertFalse(
                str(drop.get("replacement_symbol") or "").endswith("_local"), drop
            )

    def test_partial_profile_forces_recovery_abi_for_all_pair_members(self) -> None:
        seed = self.run_pass("partial-profile-seed", automatic=True)
        seed_allocation = self.allocation_row(seed, "short_site")
        self.assertTrue(self.matching_drop_rows(seed, seed_allocation))
        profile = self.tmp / "partial-allocation-only.profile"
        profile.write_text(
            "\n".join(
                [
                    "unialloc-lifetime-profile-v2",
                    f"{seed_allocation['callsite']} {seed_allocation['type_id']} "
                    f"{seed_allocation['module_id']} long-lived 93",
                    "",
                ]
            ),
            encoding="utf-8",
        )

        audit = self.run_pass(
            "partial-profile-direct-local",
            automatic=True,
            profile=profile,
            direct_local=True,
        )
        allocation = self.allocation_row(audit, "short_site")
        self.assertEqual(allocation["lifetime_hint"], 2, allocation)
        self.assertEqual(allocation["lifetime_hint_confidence"], 93, allocation)
        self.assertEqual(allocation["lifetime_hint_basis"], "profile_exact_match", allocation)
        self.assert_recovery_hint_scope(allocation)

        drops = self.matching_drop_rows(audit, allocation)
        self.assertTrue(drops, allocation)
        for drop in drops:
            self.assertEqual(drop["lifetime_hint"], 0, drop)
            self.assertEqual(drop["lifetime_hint_confidence"], 0, drop)
            self.assertEqual(drop["lifetime_hint_basis"], "profile_missing_entry", drop)
            self.assert_recovery_hint_scope(drop)

    def test_partial_profile_forces_direct_layout_pair_to_recovery_abis(self) -> None:
        fixture = self.tmp / "direct_layout_partial_profile.rs"
        fixture.write_text(
            """use std::alloc::{alloc, dealloc, Layout};

#[inline(never)]
unsafe fn direct_layout_pair() {
    let layout = Layout::new::<[u64; 8]>();
    let pointer = alloc(layout);
    if !pointer.is_null() {
        dealloc(pointer, layout);
    }
}

fn main() {
    unsafe { direct_layout_pair(); }
}
""",
            encoding="utf-8",
        )
        seed = self.run_pass("direct-layout-profile-seed", fixture=fixture)
        seed_rows = self.direct_layout_rows_by_operation(seed, "direct_layout_pair")
        seed_allocation = seed_rows["alloc"]

        profile = self.tmp / "direct-layout-allocation-only.profile"
        profile.write_text(
            "\n".join(
                [
                    "unialloc-lifetime-profile-v2",
                    f"{seed_allocation['callsite']} {seed_allocation['type_id']} "
                    f"{seed_allocation['module_id']} long-lived 93",
                    "",
                ]
            ),
            encoding="utf-8",
        )
        audit = self.run_pass(
            "direct-layout-partial-profile",
            profile=profile,
            direct_local_metadata=True,
            fixture=fixture,
        )
        rows = self.direct_layout_rows_by_operation(audit, "direct_layout_pair")
        allocation = rows["alloc"]
        deallocation = rows["dealloc"]

        self.assertEqual(allocation["callsite"], seed_allocation["callsite"])
        self.assertEqual(allocation["lifetime_hint"], 2, allocation)
        self.assertEqual(allocation["lifetime_hint_confidence"], 93, allocation)
        self.assertEqual(allocation["lifetime_hint_basis"], "profile_exact_match", allocation)
        self.assertEqual(
            allocation["replacement_symbol"],
            "__unialloc_alloc_layout_with_metadata_hints",
            allocation,
        )

        self.assertEqual(deallocation["callsite"], seed_rows["dealloc"]["callsite"])
        self.assertEqual(deallocation["lifetime_hint"], 0, deallocation)
        self.assertEqual(deallocation["lifetime_hint_confidence"], 0, deallocation)
        self.assertEqual(
            deallocation["lifetime_hint_basis"], "profile_missing_entry", deallocation
        )
        self.assertEqual(
            deallocation["replacement_symbol"],
            "__unialloc_dealloc_layout_with_metadata_hints",
            deallocation,
        )
        for row in rows.values():
            self.assertFalse(
                str(row.get("replacement_symbol") or "").endswith("_local"), row
            )

    def test_classifier_is_opt_in_and_explicit_sources_take_precedence(self) -> None:
        baseline = self.run_pass("baseline")
        baseline_short = self.allocation_row(baseline, "short_site")
        self.assertEqual(baseline_short["lifetime_hint"], 0)
        self.assertEqual(baseline_short["lifetime_hint_basis"], "default_unknown")

        automatic = self.run_pass("profile-seed", automatic=True)
        automatic_short = self.allocation_row(automatic, "short_site")
        automatic_short_drops = self.matching_drop_rows(automatic, automatic_short)
        self.assertTrue(automatic_short_drops)
        profile = self.tmp / "override.profile"
        profile.write_text(
            "\n".join(
                [
                    "unialloc-lifetime-profile-v2",
                    f"{automatic_short['callsite']} {automatic_short['type_id']} "
                    f"{automatic_short['module_id']} long-lived 93",
                    *[
                        f"{drop['callsite']} {drop['type_id']} {drop['module_id']} long-lived 93"
                        for drop in automatic_short_drops
                    ],
                    "",
                ]
            ),
            encoding="utf-8",
        )
        profiled = self.run_pass("profile-override", automatic=True, profile=profile)
        profiled_short = self.allocation_row(profiled, "short_site")
        self.assertEqual(profiled_short["lifetime_hint"], 2)
        self.assertEqual(profiled_short["lifetime_hint_confidence"], 93)
        self.assertEqual(profiled_short["lifetime_hint_basis"], "profile_exact_match")
        for drop in self.matching_drop_rows(profiled, profiled_short):
            self.assertEqual(drop["lifetime_hint"], 2)
            self.assertEqual(drop["lifetime_hint_basis"], "profile_exact_match")
        profiled_long = self.allocation_row(profiled, "long_site")
        self.assertEqual(profiled_long["lifetime_hint"], 0)
        self.assertEqual(profiled_long["lifetime_hint_basis"], "profile_missing_entry")

        manual = self.run_pass("manual-override", automatic=True, global_hint=2)
        manual_short = self.allocation_row(manual, "short_site")
        self.assertEqual(manual_short["lifetime_hint"], 2)
        self.assertEqual(manual_short["lifetime_hint_basis"], "manual_global_lifetime_hint")

        environment = self.run_pass(
            "environment-enable", automatic=True, automatic_from_env=True
        )
        environment_short = self.allocation_row(environment, "short_site")
        self.assertEqual(environment_short["lifetime_hint"], 1)
        self.assertEqual(environment_short["lifetime_hint_basis"], SHORT_BASIS)


if __name__ == "__main__":
    unittest.main()
