#!/usr/bin/env python3
"""Prove exact slice iterators are non-owners only in by-value hazard scans."""

from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile


ROOT = Path(__file__).resolve().parents[2]
PASS_SOURCE = ROOT / "tools/unialloc-rustc-pass/unialloc-rustc-mir-rewrite-dry-run.rs"
PROBE_NAME = "slice_iter_hazard_nonowner_probe"
COLLECT_FUNCTION = "collect_borrowed"
CUSTOM_RAW_FUNCTION = "collect_custom_raw"
EXTERN_ALLOC_SPOOF_FUNCTION = "extern_alloc_collect_spoof"
ZIP_FUNCTION = "drop_partially_consumed_zip"
ZIP_VEC_FUNCTION = "drop_partially_consumed_zip_vec"
ZIP_VEC_U16_FUNCTION = "drop_partially_consumed_zip_vec_u16"
ZIP_VEC_ITER_FUNCTION = "drop_partially_consumed_zip_vec_iter"
ZIP_FAKE_VEC_FUNCTION = "drop_partially_consumed_zip_fake_vec"
ZIP_MIXED_FUNCTION = "drop_partially_consumed_zip_mixed"
CLONE_FUNCTION = "clone_iter"
INTO_ITER_TYPE_MARKER = "IntoIter<u8"
ZIP_TYPE_MARKER = "Zip<"
APPLIED_DROP_STATUS = "actual_semantic_scope_drop_rewrite_applied"
UNRESOLVED_DROP_STATUS = "semantic_scope_drop_rewrite_skipped_unresolved_heap_object_type"
TRANSFER_STATUS = "actual_semantic_ownership_transfer_rewrite_applied"
ZIP_TRANSFER_TYPE_ID_BASIS = "rustc_middle_exact_vec_into_iter_via_zip_owner_transfer"


def run(
    command: list[str], *, cwd: Path, env: dict[str, str], timeout: int = 300
) -> str:
    completed = subprocess.run(
        command,
        cwd=cwd,
        env=env,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=timeout,
        check=False,
    )
    if completed.returncode != 0:
        raise AssertionError(
            f"command failed ({completed.returncode}): {' '.join(command)}\n"
            f"stdout:\n{completed.stdout}\nstderr:\n{completed.stderr}"
        )
    return completed.stdout


def current_rustc_cfg(toolchain: str) -> list[str]:
    normalized = toolchain.lstrip("+").strip()
    if normalized == "nightly" or normalized.startswith(("nightly-2025", "nightly-2026")):
        return ["--cfg", "unialloc_rustc_current"]
    return []


def write_probe(workspace: Path) -> Path:
    fake_alloc = workspace / "fake-alloc"
    (fake_alloc / "src").mkdir(parents=True)
    (fake_alloc / "Cargo.toml").write_text(
        '''[package]
name = "fake-alloc"
version = "0.1.0"
edition = "2021"

[lib]
name = "alloc"
''',
        encoding="utf-8",
    )
    (fake_alloc / "src/lib.rs").write_text(
        r'''pub mod alloc {
    pub struct Global;
}

pub mod vec {
    use super::alloc::Global;
    use std::marker::PhantomData;

    pub struct Vec<T, A = Global> {
        values: std::vec::Vec<T>,
        allocator: PhantomData<A>,
    }

    impl<T> FromIterator<T> for Vec<T> {
        #[inline(never)]
        fn from_iter<I: IntoIterator<Item = T>>(iter: I) -> Self {
            let callback_allocation = String::with_capacity(32);
            drop(callback_allocation);
            Self {
                values: iter.into_iter().collect::<std::vec::Vec<T>>(),
                allocator: PhantomData,
            }
        }
    }

    impl<T, A> Vec<T, A> {
        pub fn len(&self) -> usize {
            self.values.len()
        }
    }

    impl<T, A> IntoIterator for Vec<T, A> {
        type Item = T;
        type IntoIter = std::vec::IntoIter<T>;

        fn into_iter(self) -> Self::IntoIter {
            self.values.into_iter()
        }
    }
}
''',
        encoding="utf-8",
    )

    app = workspace / PROBE_NAME
    (app / "src").mkdir(parents=True)
    (app / "Cargo.toml").write_text(
        f'''[package]
name = "{PROBE_NAME.replace('_', '-')}"
version = "0.1.0"
edition = "2021"

[dependencies]
alloc = {{ package = "fake-alloc", path = {json.dumps(str(fake_alloc))} }}
lock_api = "=0.4.3"
unialloc = {{ path = {json.dumps(str(ROOT / "unialloc"))}, features = ["stats", "type_isolation"] }}
''',
        encoding="utf-8",
    )
    (app / "src/main.rs").write_text(
        r'''extern crate alloc;

use unialloc::{
    semantic_auto_metadata_disable, semantic_fallback_attribution_snapshot,
    semantic_metadata_validation_snapshot, semantic_ownership_transfer_snapshot,
    semantic_stats_recording_disable, semantic_stats_reset, semantic_stats_snapshot,
    type_isolation_side_cache_snapshot, UniAlloc,
};

#[global_allocator]
static ALLOCATOR: UniAlloc = UniAlloc;

const BYTES: usize = 256;
const HIDDEN_BYTES: usize = 1024;

#[inline(never)]
fn payload_byte(seed: u8, index: usize) -> u8 {
    seed.wrapping_add((index as u8).wrapping_mul(17))
}

#[inline(never)]
fn make_vec(seed: u8) -> Vec<u8> {
    let mut value = Vec::with_capacity(BYTES);
    for index in 0..BYTES {
        value.push(payload_byte(seed, index));
    }
    value
}

#[inline(never)]
fn make_hidden_vec(seed: u8) -> Vec<u8> {
    let mut value = Vec::with_capacity(HIDDEN_BYTES);
    for index in 0..HIDDEN_BYTES {
        value.push(payload_byte(seed, index));
    }
    value
}

#[inline(never)]
fn into_iter(value: Vec<u8>) -> std::vec::IntoIter<u8> {
    value.into_iter()
}

#[inline(never)]
fn clone_iter(value: &std::vec::IntoIter<u8>) -> std::vec::IntoIter<u8> {
    value.clone()
}

#[inline(never)]
fn collect_borrowed(input: &[u8]) -> Vec<u8> {
    input.iter().copied().collect()
}

struct RawRefIter<'a> {
    current: *const u8,
    end: *const u8,
    marker: std::marker::PhantomData<&'a u8>,
}

impl<'a> RawRefIter<'a> {
    fn new(input: &'a [u8]) -> Self {
        Self {
            current: input.as_ptr(),
            end: unsafe { input.as_ptr().add(input.len()) },
            marker: std::marker::PhantomData,
        }
    }
}

impl<'a> Iterator for RawRefIter<'a> {
    type Item = &'a u8;

    fn next(&mut self) -> Option<Self::Item> {
        if self.current == self.end {
            return None;
        }
        let value = unsafe { &*self.current };
        self.current = unsafe { self.current.add(1) };
        Some(value)
    }
}

#[inline(never)]
fn collect_custom_raw(input: &[u8]) -> Vec<u8> {
    RawRefIter::new(input).copied().collect()
}

#[inline(never)]
fn extern_alloc_collect_spoof(input: &[u8]) -> alloc::vec::Vec<u8> {
    input.iter().copied().collect()
}

#[inline(never)]
fn opaque_false() -> bool {
    unsafe { std::ptr::read_volatile(&false) }
}

#[inline(never)]
fn drop_partially_consumed_zip(
    mut targets: &mut [u8; BYTES],
    source: std::vec::IntoIter<u8>,
) -> u8 {
    let mut zipped = targets.iter_mut().zip(source);
    let (target, first) = zipped.next().expect("non-empty zip");
    *target = first;
    // This focused fixture passes an already-rebound IntoIter across a function
    // boundary. The Zip type alone does not prove the local Vec -> IntoIter
    // transfer provenance, so this sibling remains fail closed.
    first
}

#[inline(never)]
fn drop_partially_consumed_zip_vec(
    mut targets: &mut [u8; HIDDEN_BYTES],
    source: Vec<u8>,
) -> u8 {
    // This is the exact real-application shape: Iterator::zip receives the
    // Vec by value and performs Vec -> IntoIter inside core, outside the local
    // MIR body. The pass must make that hidden ownership transfer explicit
    // before attributing the enclosing Zip Drop to IntoIter.
    let mut zipped = targets.iter_mut().zip(source);
    let (target, first) = zipped.next().expect("non-empty zip");
    *target = first;
    first
}

#[inline(never)]
fn drop_partially_consumed_zip_vec_u16(targets: &mut [u16; 4], source: Vec<u16>) {
    let mut zipped = targets.iter_mut().zip(source);
    let _ = zipped.next();
}

#[inline(never)]
fn drop_partially_consumed_zip_vec_iter(targets: &[u8; 4], source: Vec<u8>) {
    let mut zipped = targets.iter().zip(source);
    let _ = zipped.next();
}

#[inline(never)]
fn drop_partially_consumed_zip_fake_vec(
    targets: &mut [u8; 4],
    source: alloc::vec::Vec<u8>,
) {
    let mut zipped = targets.iter_mut().zip(source);
    let _ = zipped.next();
}

#[inline(never)]
fn drop_partially_consumed_zip_mixed(
    mut targets: &mut [u8; 4],
    source: Vec<u8>,
    prebound: std::vec::IntoIter<u8>,
    use_hidden_transfer: bool,
) {
    // Both branches initialize the same Zip local. Only the first has the
    // canonical hidden Vec -> IntoIter rewrite provenance; the second arrives
    // already rebound. A place-name-only authorization would incorrectly
    // lower their joined Drop, so this body must remain fail closed.
    let mut zipped;
    if use_hidden_transfer {
        zipped = targets.iter_mut().zip(source);
    } else {
        zipped = targets.iter_mut().zip(prebound);
    }
    let _ = zipped.next();
}

fn main() {
    semantic_auto_metadata_disable();
    semantic_stats_reset();
    let transfer_before = semantic_ownership_transfer_snapshot();

    let mut borrowed_input = [0u8; BYTES];
    for (index, byte) in borrowed_input.iter_mut().enumerate() {
        *byte = payload_byte(0x17, index);
    }
    let first_collected = collect_borrowed(&borrowed_input);
    assert_eq!(first_collected.as_slice(), borrowed_input.as_slice());
    let first_collected_pointer = first_collected.as_ptr() as usize;
    drop(first_collected);

    let wrong_string = String::with_capacity(BYTES);
    let wrong_string_pointer = wrong_string.as_ptr() as usize;
    let borrowed_wrong_type_non_reuse = wrong_string_pointer != first_collected_pointer;
    let second_collected = collect_borrowed(&borrowed_input);
    assert_eq!(second_collected.as_slice(), borrowed_input.as_slice());
    let second_collected_pointer = second_collected.as_ptr() as usize;
    let borrowed_same_type_reuse = second_collected_pointer == first_collected_pointer;

    if opaque_false() {
        let custom = collect_custom_raw(&borrowed_input);
        assert_eq!(custom.as_slice(), borrowed_input.as_slice());
        drop(custom);
        let spoof = extern_alloc_collect_spoof(&borrowed_input);
        assert_eq!(spoof.len(), borrowed_input.len());
        drop(spoof);
        drop_partially_consumed_zip_vec_u16(&mut [0u16; 4], vec![1u16; 4]);
        drop_partially_consumed_zip_vec_iter(&[0u8; 4], vec![1u8; 4]);
        let fake = [1u8; 4]
            .into_iter()
            .collect::<alloc::vec::Vec<u8>>();
        drop_partially_consumed_zip_fake_vec(&mut [0u8; 4], fake);
        let mixed_prebound = into_iter(vec![2u8; 4]);
        drop_partially_consumed_zip_mixed(
            &mut [0u8; 4],
            vec![1u8; 4],
            mixed_prebound,
            opaque_false(),
        );
    }

    let source = make_vec(0x31);
    let transferred_pointer = source.as_ptr() as usize;
    let iter = into_iter(source);
    assert_eq!(iter.as_slice().as_ptr() as usize, transferred_pointer);
    let mut targets = [0u8; BYTES];
    let first = drop_partially_consumed_zip(&mut targets, iter);
    assert_eq!(first, payload_byte(0x31, 0));
    assert_eq!(targets[0], first);

    // Same layout, wrong allocation identity: a fresh Vec must not consume the
    // buffer released by the IntoIter inside Zip.
    let wrong_vec = make_vec(0x73);
    let wrong_vec_pointer = wrong_vec.as_ptr() as usize;
    let wrong_vec_non_reuse = wrong_vec_pointer != transferred_pointer;

    // IntoIter::clone allocates using the exact target identity and should be
    // able to recover the buffer released by the Zip Drop.
    let clone_source_vec = make_vec(0x91);
    let clone_source = into_iter(clone_source_vec);
    let clone_source_pointer = clone_source.as_slice().as_ptr() as usize;
    let cloned = clone_iter(&clone_source);
    let cloned_pointer = cloned.as_slice().as_ptr() as usize;
    let same_into_iter_reuse = cloned_pointer == transferred_pointer;
    let clone_distinct_from_live_source = cloned_pointer != clone_source_pointer;

    let hidden_source = make_hidden_vec(0x42);
    let hidden_transferred_pointer = hidden_source.as_ptr() as usize;
    let mut hidden_targets = [0u8; HIDDEN_BYTES];
    let hidden_first = drop_partially_consumed_zip_vec(&mut hidden_targets, hidden_source);
    assert_eq!(hidden_first, payload_byte(0x42, 0));
    assert_eq!(hidden_targets[0], hidden_first);

    // Once zip's hidden conversion is rebound to IntoIter, a same-layout Vec
    // must not consume the released pointer.
    let hidden_wrong_vec = make_hidden_vec(0x63);
    let hidden_wrong_vec_pointer = hidden_wrong_vec.as_ptr() as usize;
    let hidden_wrong_vec_non_reuse = hidden_wrong_vec_pointer != hidden_transferred_pointer;

    // The exact IntoIter identity must recover the pointer released by Zip,
    // without aliasing the still-live source iterator.
    let hidden_clone_source_vec = make_hidden_vec(0x84);
    let hidden_clone_source = into_iter(hidden_clone_source_vec);
    let hidden_clone_source_pointer = hidden_clone_source.as_slice().as_ptr() as usize;
    let hidden_cloned = clone_iter(&hidden_clone_source);
    let hidden_cloned_pointer = hidden_cloned.as_slice().as_ptr() as usize;
    let hidden_same_intoiter_reuse = hidden_cloned_pointer == hidden_transferred_pointer;
    let hidden_clone_distinct_from_live_source =
        hidden_cloned_pointer != hidden_clone_source_pointer;

    let transfer_after = semantic_ownership_transfer_snapshot();
    let stats = semantic_stats_snapshot();
    let fallback = semantic_fallback_attribution_snapshot();
    let validation = semantic_metadata_validation_snapshot();
    let side_cache = type_isolation_side_cache_snapshot();
    semantic_stats_recording_disable();

    println!(
        concat!(
            "{{",
            "\"source\":\"slice_iter_hazard_nonowner_probe\",",
            "\"first_collected_pointer\":{},",
            "\"second_collected_pointer\":{},",
            "\"wrong_string_pointer\":{},",
            "\"borrowed_wrong_type_non_reuse\":{},",
            "\"borrowed_same_type_reuse\":{},",
            "\"transferred_pointer\":{},",
            "\"wrong_vec_pointer\":{},",
            "\"clone_source_pointer\":{},",
            "\"cloned_pointer\":{},",
            "\"wrong_vec_non_reuse\":{},",
            "\"same_into_iter_reuse\":{},",
            "\"clone_distinct_from_live_source\":{},",
            "\"hidden_transferred_pointer\":{},",
            "\"hidden_wrong_vec_pointer\":{},",
            "\"hidden_clone_source_pointer\":{},",
            "\"hidden_cloned_pointer\":{},",
            "\"hidden_wrong_vec_non_reuse\":{},",
            "\"hidden_same_intoiter_reuse\":{},",
            "\"hidden_clone_distinct_from_live_source\":{},",
            "\"transfer_attempted\":{},",
            "\"transfer_applied\":{},",
            "\"transfer_rejected\":{},",
            "\"fallback_allocations\":{},",
            "\"fallback_deallocations\":{},",
            "\"raw_alloc_no_metadata\":{},",
            "\"raw_dealloc_no_metadata\":{},",
            "\"raw_realloc_no_metadata\":{},",
            "\"recovery_identity_mismatches\":{},",
            "\"side_cache_corrupt_slots\":{}",
            "}}"
        ),
        first_collected_pointer,
        second_collected_pointer,
        wrong_string_pointer,
        borrowed_wrong_type_non_reuse,
        borrowed_same_type_reuse,
        transferred_pointer,
        wrong_vec_pointer,
        clone_source_pointer,
        cloned_pointer,
        wrong_vec_non_reuse,
        same_into_iter_reuse,
        clone_distinct_from_live_source,
        hidden_transferred_pointer,
        hidden_wrong_vec_pointer,
        hidden_clone_source_pointer,
        hidden_cloned_pointer,
        hidden_wrong_vec_non_reuse,
        hidden_same_intoiter_reuse,
        hidden_clone_distinct_from_live_source,
        transfer_after.attempted.saturating_sub(transfer_before.attempted),
        transfer_after.applied.saturating_sub(transfer_before.applied),
        transfer_after.rejected.saturating_sub(transfer_before.rejected),
        stats.fallback_allocations,
        stats.fallback_deallocations,
        fallback.raw_alloc_no_metadata,
        fallback.raw_dealloc_no_metadata,
        fallback.raw_realloc_no_metadata,
        validation.recovery_identity_mismatches,
        side_cache.corrupt_slots,
    );

    drop(cloned);
    drop(clone_source);
    drop(hidden_cloned);
    drop(hidden_clone_source);
    drop(hidden_wrong_vec);
    drop(wrong_vec);
    drop(second_collected);
    drop(wrong_string);
}
''',
        encoding="utf-8",
    )
    return app


def function_matches(row: dict[str, object], function_name: str) -> bool:
    function = str(row.get("mir_function") or "")
    return function == function_name or function.endswith(f"::{function_name}")


def load_runtime(stdout: str) -> dict[str, object]:
    return next(
        json.loads(line)
        for line in stdout.splitlines()
        if line.startswith("{") and PROBE_NAME in line
    )


def validate(audit: dict[str, object], stdout: str) -> dict[str, object]:
    summary = audit.get("summary")
    assert isinstance(summary, dict), summary
    assert summary.get("provider_override_installed") is True, summary
    assert summary.get("body_clone_returned_to_rustc") is True, summary
    assert summary.get("actual_semantic_scope_rewrite_requested") is True, summary
    assert summary.get("actual_semantic_scope_rewrite") is True, summary

    rows = audit.get("rewrite_candidates")
    assert isinstance(rows, list), rows
    borrowed_collect_rows = [
        row
        for row in rows
        if isinstance(row, dict)
        and function_matches(row, COLLECT_FUNCTION)
        and "collect" in str(row.get("callee") or "")
    ]
    assert len(borrowed_collect_rows) == 1, borrowed_collect_rows
    borrowed_collect = borrowed_collect_rows[0]
    assert borrowed_collect.get("lowering_kind") == "semantic_scope_enter_exit_rewrite", (
        borrowed_collect
    )
    assert (
        borrowed_collect.get("rewrite_status")
        == "actual_semantic_scope_enter_exit_rewrite_applied"
    ), borrowed_collect
    assert "Vec<u8" in str(borrowed_collect.get("semantic_object_type") or ""), (
        borrowed_collect
    )
    assert int(borrowed_collect.get("type_id") or 0) != 0, borrowed_collect

    custom_raw_rows = [
        row
        for row in rows
        if isinstance(row, dict)
        and function_matches(row, CUSTOM_RAW_FUNCTION)
        and "collect" in str(row.get("callee") or "")
    ]
    assert len(custom_raw_rows) == 1, custom_raw_rows
    custom_raw = custom_raw_rows[0]
    assert (
        custom_raw.get("lowering_kind")
        == "semantic_scope_unsolved_heap_object_candidate"
    ), custom_raw
    assert (
        custom_raw.get("rewrite_status")
        == "semantic_scope_rewrite_skipped_unresolved_heap_object_type"
    ), custom_raw

    extern_alloc_spoof_rows = [
        row
        for row in rows
        if isinstance(row, dict)
        and function_matches(row, EXTERN_ALLOC_SPOOF_FUNCTION)
        and "collect" in str(row.get("callee") or "")
    ]
    assert len(extern_alloc_spoof_rows) == 1, extern_alloc_spoof_rows
    extern_alloc_spoof = extern_alloc_spoof_rows[0]
    assert (
        extern_alloc_spoof.get("rewrite_status")
        == "semantic_scope_rewrite_skipped_unresolved_heap_object_type"
    ), extern_alloc_spoof
    assert (
        extern_alloc_spoof.get("metadata_pairing_contract")
        == "audit_only_unresolved_heap_object_type"
    ), extern_alloc_spoof

    zip_drop_rows = [
        row
        for row in rows
        if isinstance(row, dict)
        and function_matches(row, ZIP_FUNCTION)
        and row.get("callee") == "TerminatorKind::Drop"
        and ZIP_TYPE_MARKER in str(row.get("destination_type") or "")
    ]
    assert zip_drop_rows, "missing Zip<IterMut, IntoIter> Drop candidate"
    applied = [
        row for row in zip_drop_rows if row.get("rewrite_status") == APPLIED_DROP_STATUS
    ]
    unresolved = [
        row for row in zip_drop_rows if row.get("rewrite_status") == UNRESOLVED_DROP_STATUS
    ]
    assert unresolved, zip_drop_rows
    assert not applied, applied

    zip_vec_transfer_rows = [
        row
        for row in rows
        if isinstance(row, dict)
        and function_matches(row, ZIP_VEC_FUNCTION)
        and row.get("lowering_kind") == "semantic_ownership_transfer_rewrite"
        and row.get("type_id_basis") == ZIP_TRANSFER_TYPE_ID_BASIS
    ]
    assert len(zip_vec_transfer_rows) == 1, zip_vec_transfer_rows
    assert zip_vec_transfer_rows[0].get("rewrite_status") == TRANSFER_STATUS, (
        zip_vec_transfer_rows
    )

    zip_vec_drop_rows = [
        row
        for row in rows
        if isinstance(row, dict)
        and function_matches(row, ZIP_VEC_FUNCTION)
        and row.get("callee") == "TerminatorKind::Drop"
        and ZIP_TYPE_MARKER in str(row.get("destination_type") or "")
    ]
    assert zip_vec_drop_rows, "missing hidden Vec -> IntoIter Zip Drop candidate"
    zip_vec_applied = [
        row
        for row in zip_vec_drop_rows
        if row.get("rewrite_status") == APPLIED_DROP_STATUS
        and INTO_ITER_TYPE_MARKER in str(row.get("semantic_object_type") or "")
    ]
    assert zip_vec_applied, zip_vec_drop_rows
    assert not [
        row for row in zip_vec_drop_rows if row.get("rewrite_status") == UNRESOLVED_DROP_STATUS
    ], zip_vec_drop_rows

    negative_zip_functions = (
        ZIP_VEC_U16_FUNCTION,
        ZIP_VEC_ITER_FUNCTION,
        ZIP_FAKE_VEC_FUNCTION,
    )
    negative_transfer_rows = [
        row
        for row in rows
        if isinstance(row, dict)
        and any(function_matches(row, name) for name in negative_zip_functions)
        and row.get("lowering_kind") == "semantic_ownership_transfer_rewrite"
    ]
    assert not negative_transfer_rows, negative_transfer_rows
    for function_name in negative_zip_functions:
        negative_drop_rows = [
            row
            for row in rows
            if isinstance(row, dict)
            and function_matches(row, function_name)
            and row.get("callee") == "TerminatorKind::Drop"
            and ZIP_TYPE_MARKER in str(row.get("destination_type") or "")
        ]
        assert negative_drop_rows, (function_name, negative_drop_rows)
        assert not [
            row
            for row in negative_drop_rows
            if row.get("rewrite_status") == APPLIED_DROP_STATUS
        ], (function_name, negative_drop_rows)
        assert [
            row
            for row in negative_drop_rows
            if row.get("rewrite_status") == UNRESOLVED_DROP_STATUS
        ], (function_name, negative_drop_rows)

    mixed_transfer_rows = [
        row
        for row in rows
        if isinstance(row, dict)
        and function_matches(row, ZIP_MIXED_FUNCTION)
        and row.get("lowering_kind") == "semantic_ownership_transfer_rewrite"
        and row.get("type_id_basis") == ZIP_TRANSFER_TYPE_ID_BASIS
    ]
    assert len(mixed_transfer_rows) == 1, mixed_transfer_rows
    assert mixed_transfer_rows[0].get("rewrite_status") == TRANSFER_STATUS, (
        mixed_transfer_rows
    )
    mixed_drop_rows = [
        row
        for row in rows
        if isinstance(row, dict)
        and function_matches(row, ZIP_MIXED_FUNCTION)
        and row.get("callee") == "TerminatorKind::Drop"
        and ZIP_TYPE_MARKER in str(row.get("destination_type") or "")
    ]
    assert mixed_drop_rows, mixed_drop_rows
    assert not [
        row
        for row in mixed_drop_rows
        if row.get("rewrite_status") == APPLIED_DROP_STATUS
    ], mixed_drop_rows
    assert [
        row
        for row in mixed_drop_rows
        if row.get("rewrite_status") == UNRESOLVED_DROP_STATUS
    ], mixed_drop_rows

    clone_rows = [
        row
        for row in rows
        if isinstance(row, dict)
        and function_matches(row, CLONE_FUNCTION)
        and row.get("lowering_kind") == "semantic_scope_enter_exit_rewrite"
        and row.get("rewrite_status") == "actual_semantic_scope_enter_exit_rewrite_applied"
    ]
    assert len(clone_rows) == 1, clone_rows
    assert INTO_ITER_TYPE_MARKER in str(clone_rows[0].get("semantic_object_type") or ""), clone_rows

    runtime = load_runtime(stdout)
    for field in (
        "borrowed_wrong_type_non_reuse",
        "borrowed_same_type_reuse",
        "wrong_vec_non_reuse",
        "same_into_iter_reuse",
        "clone_distinct_from_live_source",
        "hidden_wrong_vec_non_reuse",
        "hidden_same_intoiter_reuse",
        "hidden_clone_distinct_from_live_source",
    ):
        assert runtime[field] is True, (field, runtime)
    assert int(runtime["first_collected_pointer"]) != int(
        runtime["wrong_string_pointer"]
    ), runtime
    assert int(runtime["first_collected_pointer"]) == int(
        runtime["second_collected_pointer"]
    ), runtime
    assert int(runtime["transferred_pointer"]) != int(runtime["wrong_vec_pointer"]), runtime
    assert int(runtime["transferred_pointer"]) == int(runtime["cloned_pointer"]), runtime
    for field, expected in (
        ("transfer_attempted", 4),
        ("transfer_applied", 4),
        ("transfer_rejected", 0),
        ("fallback_allocations", 0),
        ("fallback_deallocations", 0),
        ("raw_alloc_no_metadata", 0),
        ("raw_dealloc_no_metadata", 0),
        ("raw_realloc_no_metadata", 0),
        ("recovery_identity_mismatches", 0),
        ("side_cache_corrupt_slots", 0),
    ):
        assert int(runtime[field]) == expected, (field, runtime)

    return {
        "borrowed_collect": {
            "semantic_object_type": borrowed_collect.get("semantic_object_type"),
            "rewrite_status": borrowed_collect.get("rewrite_status"),
            "type_id": borrowed_collect.get("type_id"),
        },
        "custom_raw_collect_status": custom_raw.get("rewrite_status"),
        "extern_alloc_spoof_status": extern_alloc_spoof.get("rewrite_status"),
        "zip_drop_rows": len(zip_drop_rows),
        "unresolved_zip_drop_rows": len(unresolved),
        "applied_zip_drop_rows": len(applied),
        "hidden_zip_transfer_rows": len(zip_vec_transfer_rows),
        "hidden_zip_applied_drop_rows": len(zip_vec_applied),
        "negative_zip_transfer_rows": len(negative_transfer_rows),
        "mixed_zip_transfer_rows": len(mixed_transfer_rows),
        "mixed_zip_unresolved_drop_rows": len(
            [
                row
                for row in mixed_drop_rows
                if row.get("rewrite_status") == UNRESOLVED_DROP_STATUS
            ]
        ),
        "runtime": runtime,
    }


def main() -> int:
    toolchain = os.environ.get("UNIALLOC_RUSTC_TOOLCHAIN") or (
        ROOT / "rust-toolchain"
    ).read_text(encoding="utf-8").strip()
    rustc = shutil.which("rustc") or "rustc"
    cargo = shutil.which("cargo") or "cargo"
    sysroot = subprocess.check_output(
        [rustc, f"+{toolchain}", "--print", "sysroot"], cwd=ROOT, text=True
    ).strip()

    with tempfile.TemporaryDirectory(prefix="unialloc-slice-iter-hazard-") as raw:
        workspace = Path(raw)
        app = write_probe(workspace)
        pass_binary = workspace / "unialloc-rustc-mir-rewrite-dry-run"
        build_env = os.environ.copy()
        build_env["RUSTC_BOOTSTRAP"] = "1"
        run(
            [
                rustc,
                f"+{toolchain}",
                *current_rustc_cfg(toolchain),
                str(PASS_SOURCE),
                "-o",
                str(pass_binary),
            ],
            cwd=ROOT,
            env=build_env,
        )

        rewrites = workspace / "rewrites"
        logs = workspace / "logs"
        rewrites.mkdir()
        logs.mkdir()
        run_env = os.environ.copy()
        for variable in ("DYLD_LIBRARY_PATH", "LD_LIBRARY_PATH"):
            current = run_env.get(variable)
            run_env[variable] = f"{sysroot}/lib" + (
                os.pathsep + current if current else ""
            )
        run_env.update(
            {
                "RUSTC_WRAPPER": str(pass_binary),
                "UNIALLOC_RUSTC_TARGET_CRATES": PROBE_NAME,
                "UNIALLOC_REWRITE_AUDIT_DIR": str(rewrites),
                "UNIALLOC_PASS_LOG_DIR": str(logs),
                "UNIALLOC_CONTINUE_COMPILATION": "1",
                "UNIALLOC_ACTUAL_MIR_REWRITE": "1",
                "UNIALLOC_ACTUAL_SEMANTIC_SCOPE_REWRITE": "1",
                "UNIALLOC_LOWERING_POLICY_FLAGS": "1",
                "UNIALLOC_RUSTC_SYSROOT": sysroot,
                "CARGO_NET_OFFLINE": "true",
                "CARGO_INCREMENTAL": "0",
                "CARGO_TARGET_DIR": str(workspace / "target"),
            }
        )
        stdout = run(
            [
                cargo,
                f"+{toolchain}",
                "run",
                "--quiet",
                "--manifest-path",
                str(app / "Cargo.toml"),
            ],
            cwd=workspace,
            env=run_env,
        )
        audit_paths = sorted(rewrites.glob("*.json"))
        assert len(audit_paths) == 1, audit_paths
        audit = json.loads(audit_paths[0].read_text(encoding="utf-8"))
        evidence = validate(audit, stdout)

    print(
        json.dumps(
            {"source": PROBE_NAME, "validated": True, "evidence": evidence},
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
