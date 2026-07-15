#!/usr/bin/env python3
"""Prove generic Box owners use concrete monomorphized runtime identities."""

from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile


ROOT = Path(__file__).resolve().parents[2]
PASS_SOURCE = ROOT / "tools/unialloc-rustc-pass/unialloc-rustc-mir-rewrite-dry-run.rs"
TOOLCHAIN = "nightly-2026-06-11"
TYPE_ISOLATED = 1


def run(command: list[str], *, cwd: Path, env: dict[str, str], timeout: int = 300) -> str:
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


def write_probe(workspace: Path) -> Path:
    dependency = workspace / "generic-box-owner"
    payload_v1 = workspace / "duplicate-payload-v1"
    payload_v2 = workspace / "duplicate-payload-v2"
    app = workspace / "generic-box-probe"
    (dependency / "src").mkdir(parents=True)
    (payload_v1 / "src").mkdir(parents=True)
    (payload_v2 / "src").mkdir(parents=True)
    (app / "src").mkdir(parents=True)
    unialloc = json.dumps(str(ROOT / "unialloc"))
    spin_candidates = sorted(
        (Path.home() / ".cargo/registry/src").glob("*/spin-0.9.0")
    )
    assert spin_candidates, "spin 0.9.0 must be present in the Cargo source cache"
    spin = json.dumps(str(spin_candidates[-1]))
    (dependency / "Cargo.toml").write_text(
        f'''[package]
name = "generic-box-owner"
version = "0.0.0"
edition = "2021"

[dependencies]
payload-v1 = {{ package = "duplicate-payload", path = {json.dumps(str(payload_v1))} }}
unialloc = {{ path = {unialloc}, features = ["stats", "type_isolation"] }}

[patch.crates-io]
spin = {{ path = {spin} }}
''',
        encoding="utf-8",
    )
    (dependency / "src/lib.rs").write_text(
        r'''extern crate unialloc as _;

use payload_v1::Payload as CanonicalPayload;
use std::mem::MaybeUninit;

pub struct Node<K, V> {
    pub key: MaybeUninit<K>,
    pub value: MaybeUninit<V>,
    pub previous: *mut (),
    pub next: *mut (),
}

impl<K: Copy, V: Copy> Clone for Node<K, V> {
    fn clone(&self) -> Self {
        Self {
            key: self.key,
            value: self.value,
            previous: self.previous,
            next: self.next,
        }
    }
}

#[derive(Clone)]
pub struct BoxPair<T, U>(pub Box<T>, pub Box<U>);

#[inline(never)]
pub fn clone_distinct_box_pair<T: Clone, U: Clone>(
    pair: &BoxPair<T, U>,
) -> BoxPair<T, U> {
    pair.clone()
}

#[inline(never)]
pub fn clone_same_box_pair<T: Clone>(pair: &BoxPair<T, T>) -> BoxPair<T, T> {
    pair.clone()
}

#[derive(Clone)]
pub struct BoxVec<T: Clone>(pub Box<T>, pub Vec<T>);

#[inline(never)]
pub fn clone_box_vec<T: Clone>(value: &BoxVec<T>) -> BoxVec<T> {
    value.clone()
}

pub struct DropPayload(pub String);

impl Drop for DropPayload {
    fn drop(&mut self) {}
}

pub struct LocalDropPayload(pub u64);

impl Drop for LocalDropPayload {
    fn drop(&mut self) {}
}

#[inline(never)]
pub fn drop_local_box_at_scope_end() {
    let _payload = Box::new(LocalDropPayload(17));
}

#[inline(never)]
pub fn drop_string_box_normal() {
    drop(Box::new(String::from("normal-string-box")));
}

#[inline(never)]
pub fn drop_custom_box_normal() {
    drop(Box::new(DropPayload(String::from("normal-custom-box"))));
}

#[inline(never)]
pub fn drop_custom_box_unwind() {
    let _payload = Box::new(DropPayload(String::from("unwind-custom-box")));
    panic!("forced custom Box cleanup");
}

#[inline(never)]
pub fn allocate<K, V>(key: K, value: V) -> Box<Node<K, V>> {
    Box::new(Node {
        key: MaybeUninit::new(key),
        value: MaybeUninit::new(value),
        previous: std::ptr::null_mut(),
        next: std::ptr::null_mut(),
    })
}

#[inline(never)]
pub fn release<K, V>(mut node: Box<Node<K, V>>) -> V {
    unsafe {
        std::ptr::drop_in_place(node.key.as_mut_ptr());
        let value = node.value.assume_init_read();
        drop(node);
        value
    }
}

#[inline(never)]
pub fn allocate_concrete(
    key: CanonicalPayload,
    value: CanonicalPayload,
) -> Box<Node<CanonicalPayload, CanonicalPayload>> {
    Box::new(Node {
        key: MaybeUninit::new(key),
        value: MaybeUninit::new(value),
        previous: std::ptr::null_mut(),
        next: std::ptr::null_mut(),
    })
}

#[inline(never)]
pub fn clone_concrete(
    node: &Box<Node<CanonicalPayload, CanonicalPayload>>,
) -> Box<Node<CanonicalPayload, CanonicalPayload>> {
    node.clone()
}

#[inline(never)]
pub fn clone_optional_concrete(
    node: &Option<Box<Node<CanonicalPayload, CanonicalPayload>>>,
) -> Option<Box<Node<CanonicalPayload, CanonicalPayload>>> {
    node.clone()
}

#[inline(never)]
pub fn clone_result_concrete(
    node: &Result<Box<Node<CanonicalPayload, CanonicalPayload>>, u8>,
) -> Result<Box<Node<CanonicalPayload, CanonicalPayload>>, u8> {
    node.clone()
}

#[inline(never)]
pub fn release_concrete(
    mut node: Box<Node<CanonicalPayload, CanonicalPayload>>,
) -> CanonicalPayload {
    unsafe {
        std::ptr::drop_in_place(node.key.as_mut_ptr());
        let value = node.value.assume_init_read();
        drop(node);
        value
    }
}
''',
        encoding="utf-8",
    )
    duplicate_payload_source = '''#[derive(Clone, Copy)]
#[repr(C)]
pub struct Payload(pub [u64; 2]);
'''
    for crate, version in ((payload_v1, "0.1.0"), (payload_v2, "0.2.0")):
        (crate / "Cargo.toml").write_text(
            f'''[package]
name = "duplicate-payload"
version = "{version}"
edition = "2021"
''',
            encoding="utf-8",
        )
        (crate / "src/lib.rs").write_text(duplicate_payload_source, encoding="utf-8")
    (app / "Cargo.toml").write_text(
        f'''[package]
name = "generic-box-probe"
version = "0.0.0"
edition = "2021"

[dependencies]
generic-box-owner = {{ path = {json.dumps(str(dependency))} }}
payload-v1 = {{ package = "duplicate-payload", path = {json.dumps(str(payload_v1))} }}
payload-v2 = {{ package = "duplicate-payload", path = {json.dumps(str(payload_v2))} }}
unialloc = {{ path = {unialloc}, features = ["stats", "type_isolation"] }}

[patch.crates-io]
spin = {{ path = {spin} }}
''',
        encoding="utf-8",
    )
    (app / "src/main.rs").write_text(
        r'''use generic_box_owner::{
    allocate, allocate_concrete, clone_concrete, drop_custom_box_normal,
    drop_custom_box_unwind, drop_local_box_at_scope_end, drop_string_box_normal, release,
    release_concrete, Node,
};
use payload_v1::Payload as PayloadV1;
use payload_v2::Payload as PayloadV2;
use unialloc::{
    semantic_metadata_validation_snapshot, semantic_stats_recording_disable,
    semantic_stats_recording_enable, semantic_stats_reset, semantic_stats_snapshot,
    semantic_type_id, UniAlloc,
};

#[global_allocator]
static ALLOCATOR: UniAlloc = UniAlloc;

fn main() {
    assert_eq!(std::mem::size_of::<Node<PayloadV1, PayloadV1>>(), 48);
    assert_eq!(std::mem::size_of::<Node<PayloadV2, PayloadV2>>(), 48);
    assert_eq!(std::any::type_name::<PayloadV1>(), std::any::type_name::<PayloadV2>());
    let previous_hook = std::panic::take_hook();
    std::panic::set_hook(Box::new(|_| {}));
    let validation_before_dropful_boxes = semantic_metadata_validation_snapshot();
    drop_string_box_normal();
    drop_custom_box_normal();
    let unwind_observed = std::panic::catch_unwind(drop_custom_box_unwind).is_err();
    let validation_after_dropful_boxes = semantic_metadata_validation_snapshot();
    std::panic::set_hook(previous_hook);
    let dropful_box_recovery_mismatches = validation_after_dropful_boxes
        .recovery_identity_mismatches
        .saturating_sub(validation_before_dropful_boxes.recovery_identity_mismatches);
    assert!(unwind_observed);
    assert_eq!(dropful_box_recovery_mismatches, 0);

    // Warm the exact Box cache, then isolate one allocation/reclaim pair whose
    // dropful payload requires allocation-side authenticated recovery.
    drop_local_box_at_scope_end();
    semantic_stats_reset();
    semantic_stats_recording_enable();
    let local_box_validation_before = semantic_metadata_validation_snapshot();
    drop_local_box_at_scope_end();
    let local_box_stats = semantic_stats_snapshot();
    let local_box_validation_after = semantic_metadata_validation_snapshot();
    semantic_stats_recording_disable();
    let local_box_recovery_mismatches = local_box_validation_after
        .recovery_identity_mismatches
        .saturating_sub(local_box_validation_before.recovery_identity_mismatches);
    assert_eq!(local_box_stats.typed_allocations, 1);
    assert_eq!(local_box_stats.typed_deallocations, 1);
    assert_eq!(local_box_stats.fallback_deallocations, 0);
    assert_eq!(local_box_recovery_mismatches, 0);

    semantic_stats_reset();
    semantic_stats_recording_enable();

    let first = allocate(PayloadV1([1, 2]), PayloadV1([3, 4]));
    let first_address = (&*first as *const Node<PayloadV1, PayloadV1>) as usize;
    drop(release(first));
    let wrong_identity_after_first =
        semantic_stats_snapshot().typed_cache_wrong_identity_denials;

    let concrete = allocate_concrete(PayloadV1([5, 6]), PayloadV1([7, 8]));
    let concrete_address = (&*concrete as *const Node<PayloadV1, PayloadV1>) as usize;
    let wrong_identity_after_concrete =
        semantic_stats_snapshot().typed_cache_wrong_identity_denials;

    drop(release_concrete(concrete));

    let recovered = allocate(PayloadV1([9, 10]), PayloadV1([11, 12]));
    let recovered_address = (&*recovered as *const Node<PayloadV1, PayloadV1>) as usize;
    let wrong_identity_after_recovered =
        semantic_stats_snapshot().typed_cache_wrong_identity_denials;
    drop(release(recovered));

    let distinct = allocate(PayloadV2([13, 14]), PayloadV2([15, 16]));
    let distinct_address = (&*distinct as *const Node<PayloadV2, PayloadV2>) as usize;
    let wrong_identity_after_distinct =
        semantic_stats_snapshot().typed_cache_wrong_identity_denials;
    drop(release(distinct));

    let payload_v1_type_id = semantic_type_id::<Box<Node<PayloadV1, PayloadV1>>>();
    let payload_v2_type_id = semantic_type_id::<Box<Node<PayloadV2, PayloadV2>>>();
    let stats = semantic_stats_snapshot();
    semantic_stats_recording_disable();

    println!(
        concat!(
            "{{\"source\":\"generic_box_probe\",",
            "\"first_address\":{},\"concrete_address\":{},",
            "\"recovered_address\":{},",
            "\"distinct_address\":{},",
            "\"payload_type_names_equal\":true,",
            "\"payload_v1_type_id\":{},\"payload_v2_type_id\":{},",
            "\"unwind_observed\":{},\"dropful_box_recovery_mismatches\":{},",
            "\"local_box_typed_allocations\":{},",
            "\"local_box_typed_deallocations\":{},",
            "\"local_box_fallback_deallocations\":{},",
            "\"local_box_recovery_mismatches\":{},",
            "\"wrong_identity_after_first\":{},",
            "\"wrong_identity_after_concrete\":{},",
            "\"wrong_identity_after_recovered\":{},",
            "\"wrong_identity_after_distinct\":{},",
            "\"typed_allocations\":{},\"typed_deallocations\":{},",
            "\"typed_cache_hits\":{},\"typed_cache_inserts\":{},",
            "\"wrong_identity_denials\":{},",
            "\"fallback_allocations\":{},\"fallback_deallocations\":{}}}"
        ),
        first_address,
        concrete_address,
        recovered_address,
        distinct_address,
        payload_v1_type_id,
        payload_v2_type_id,
        unwind_observed,
        dropful_box_recovery_mismatches,
        local_box_stats.typed_allocations,
        local_box_stats.typed_deallocations,
        local_box_stats.fallback_deallocations,
        local_box_recovery_mismatches,
        wrong_identity_after_first,
        wrong_identity_after_concrete,
        wrong_identity_after_recovered,
        wrong_identity_after_distinct,
        stats.typed_allocations,
        stats.typed_deallocations,
        stats.typed_cache_hits,
        stats.typed_cache_inserts,
        stats.typed_cache_wrong_identity_denials,
        stats.fallback_allocations,
        stats.fallback_deallocations,
    );
}
''',
        encoding="utf-8",
    )
    return app


def load_event(stdout: str) -> dict[str, object]:
    for line in stdout.splitlines():
        if line.startswith('{'):
            value = json.loads(line)
            if value.get("source") == "generic_box_probe":
                return value
    raise AssertionError(f"missing probe JSON:\n{stdout}")


def function_matches(row: dict[str, object], suffix: str) -> bool:
    function = str(row.get("mir_function") or "")
    return function == suffix or function.endswith(f"::{suffix}")


def validate(
    dependency_audit: dict[str, object],
    event: dict[str, object],
) -> None:
    rows = dependency_audit.get("rewrite_candidates")
    assert isinstance(rows, list), rows
    allocation_rows = [
        row
        for row in rows
        if isinstance(row, dict)
        and function_matches(row, "allocate")
        and "boxed" in str(row.get("callee") or "")
        and "::new" in str(row.get("callee") or "")
        and "Node<K" in str(row.get("callee") or "")
        and row.get("rewrite_status")
        == "actual_semantic_scope_generic_type_rewrite_applied"
    ]
    reclaim_rows = [
        row
        for row in rows
        if isinstance(row, dict)
        and function_matches(row, "release")
        and row.get("rewrite_status")
        in {
            "actual_semantic_scope_generic_type_rewrite_applied",
            "actual_semantic_scope_drop_generic_type_rewrite_applied",
        }
    ]
    assert len(allocation_rows) == 1, allocation_rows
    assert reclaim_rows, reclaim_rows
    for row in allocation_rows + reclaim_rows:
        assert int(row.get("type_id") or 0) == 0, row
        assert (
            row.get("type_id_basis") == "monomorphized_compiler_type_id_runtime"
        ), row
        assert "push_for_rust_type" in str(row.get("replacement_symbol") or ""), row
        semantic_type = str(row.get("semantic_object_type") or "")
        assert "Box<Node<K" in semantic_type and "V" in semantic_type, row

    concrete_allocation_rows = [
        row
        for row in rows
        if isinstance(row, dict)
        and function_matches(row, "allocate_concrete")
        and "boxed" in str(row.get("callee") or "")
        and "::new" in str(row.get("callee") or "")
        and row.get("rewrite_status")
        == "actual_semantic_scope_generic_type_rewrite_applied"
    ]
    concrete_reclaim_rows = [
        row
        for row in rows
        if isinstance(row, dict)
        and function_matches(row, "release_concrete")
        and row.get("rewrite_status")
        in {
            "actual_semantic_scope_generic_type_rewrite_applied",
            "actual_semantic_scope_drop_generic_type_rewrite_applied",
        }
    ]
    assert len(concrete_allocation_rows) == 1, concrete_allocation_rows
    assert concrete_reclaim_rows, concrete_reclaim_rows
    clone_audit_rows = [
        row
        for row in rows
        if isinstance(row, dict)
        and any(
            function_matches(row, suffix)
            for suffix in (
                "clone_concrete",
                "clone_optional_concrete",
                "clone_result_concrete",
                "clone_distinct_box_pair",
                "clone_same_box_pair",
                "clone_box_vec",
            )
        )
        and "Clone" in str(row.get("callee") or "")
    ]
    assert len(clone_audit_rows) == 6, clone_audit_rows
    for row in clone_audit_rows:
        assert (
            row.get("rewrite_status")
            == "semantic_scope_rewrite_skipped_unresolved_heap_object_type"
        ), row
        assert (
            row.get("replacement_resolution_status")
            == "rustc_middle_heap_object_type_not_solved"
        ), row
    for row in (
        concrete_allocation_rows
        + concrete_reclaim_rows
    ):
        assert int(row.get("type_id") or 0) == 0, row
        assert (
            row.get("type_id_basis") == "monomorphized_compiler_type_id_runtime"
        ), row
        assert "push_for_rust_type" in str(row.get("replacement_symbol") or ""), row

    dropful_box_drop_rows = [
        row
        for row in rows
        if isinstance(row, dict)
        and any(
            function_matches(row, suffix)
            for suffix in (
                "drop_string_box_normal",
                "drop_custom_box_normal",
                "drop_custom_box_unwind",
                "drop_local_box_at_scope_end",
            )
        )
        and row.get("lowering_kind")
        == "semantic_scope_drop_exact_box_recovery_skipped"
    ]
    # Explicit normal-path `drop(Box<_>)` lowers through the recovery-backed
    # std::mem::drop call. The unwind case exposes the Box Drop terminator that
    # must remain unwrapped; runtime validation above covers both normal cases.
    assert dropful_box_drop_rows, dropful_box_drop_rows
    assert any(
        function_matches(row, "drop_custom_box_unwind")
        for row in dropful_box_drop_rows
    ), dropful_box_drop_rows
    for row in dropful_box_drop_rows:
        assert (
            row.get("rewrite_status")
            == "semantic_scope_drop_rewrite_skipped_exact_box_recovery"
        ), row
        assert row.get("replacement_symbol") == "authenticated_allocation_recovery_record", row

    local_box_allocation_rows = [
        row
        for row in rows
        if isinstance(row, dict)
        and function_matches(row, "drop_local_box_at_scope_end")
        and "boxed" in str(row.get("callee") or "")
        and "::new" in str(row.get("callee") or "")
        and row.get("rewrite_status")
        == "actual_semantic_scope_generic_type_rewrite_applied"
    ]
    assert len(local_box_allocation_rows) == 1, local_box_allocation_rows
    local_box_allocation = local_box_allocation_rows[0]
    assert (
        local_box_allocation.get("replacement_symbol")
        == "__unialloc_semantic_scope_push_for_rust_type"
    ), local_box_allocation
    assert (
        local_box_allocation.get("metadata_pairing_contract")
        == "semantic_scope_monomorphized_runtime_type_metadata"
    ), local_box_allocation

    local_box_drop_rows = [
        row
        for row in dropful_box_drop_rows
        if function_matches(row, "drop_local_box_at_scope_end")
    ]
    assert len(local_box_drop_rows) == 1, local_box_drop_rows

    payload_v1_type_id = int(event["payload_v1_type_id"])
    payload_v2_type_id = int(event["payload_v2_type_id"])
    assert event["payload_type_names_equal"] is True, event
    assert event["unwind_observed"] is True, event
    assert int(event["dropful_box_recovery_mismatches"]) == 0, event
    assert int(event["local_box_typed_allocations"]) == 1, event
    assert int(event["local_box_typed_deallocations"]) == 1, event
    assert int(event["local_box_fallback_deallocations"]) == 0, event
    assert int(event["local_box_recovery_mismatches"]) == 0, event
    assert payload_v1_type_id != 0 and payload_v2_type_id != 0
    assert payload_v1_type_id != payload_v2_type_id, event
    assert int(event["concrete_address"]) == int(event["first_address"]), event
    assert int(event["recovered_address"]) == int(event["concrete_address"]), event
    assert int(event["distinct_address"]) != int(event["concrete_address"]), event
    assert int(event["wrong_identity_after_first"]) == 0, event
    assert int(event["wrong_identity_after_concrete"]) == 0, event
    assert int(event["wrong_identity_after_recovered"]) == 0, event
    assert int(event["wrong_identity_after_distinct"]) >= 1, event
    assert int(event["typed_allocations"]) == 4, event
    assert int(event["typed_deallocations"]) == 4, event
    assert int(event["typed_cache_hits"]) >= 2, event
    assert int(event["typed_cache_inserts"]) == 4, event
    assert int(event["wrong_identity_denials"]) >= 1, event
    assert int(event["fallback_allocations"]) == 0, event
    assert int(event["fallback_deallocations"]) == 0, event


def main() -> int:
    rustc = shutil.which("rustc") or "rustc"
    cargo = shutil.which("cargo") or "cargo"
    sysroot = subprocess.check_output(
        [rustc, f"+{TOOLCHAIN}", "--print", "sysroot"], text=True
    ).strip()
    with tempfile.TemporaryDirectory(prefix="unialloc-generic-box-mono-") as raw:
        workspace = Path(raw)
        app = write_probe(workspace)
        pass_binary = workspace / "unialloc-rustc-mir-rewrite-dry-run"
        env = os.environ.copy()
        env["RUSTC_BOOTSTRAP"] = "1"
        run(
            [rustc, f"+{TOOLCHAIN}", "--cfg", "unialloc_rustc_current", str(PASS_SOURCE), "-o", str(pass_binary)],
            cwd=ROOT,
            env=env,
        )
        audits = workspace / "audits"
        audits.mkdir()
        env.update(
            {
                "RUSTC_WRAPPER": str(pass_binary),
                "UNIALLOC_RUSTC_TARGET_CRATES": "generic-box-owner,generic-box-probe",
                "UNIALLOC_REWRITE_AUDIT_DIR": str(audits),
                "UNIALLOC_CONTINUE_COMPILATION": "1",
                "UNIALLOC_ACTUAL_MIR_REWRITE": "1",
                "UNIALLOC_ACTUAL_SEMANTIC_SCOPE_REWRITE": "1",
                "UNIALLOC_DIRECT_LOCAL_SIZE_ALIGN_WITH_SEMANTIC_DROP": "1",
                "UNIALLOC_LOWERING_POLICY_FLAGS": str(TYPE_ISOLATED),
                "UNIALLOC_RUSTC_SYSROOT": sysroot,
                "LD_LIBRARY_PATH": f"{sysroot}/lib",
                "CARGO_NET_OFFLINE": "true",
                "CARGO_INCREMENTAL": "0",
                "CARGO_TARGET_DIR": str(workspace / "target"),
            }
        )
        stdout = run(
            [cargo, f"+{TOOLCHAIN}", "run", "--quiet", "--manifest-path", str(app / "Cargo.toml")],
            cwd=workspace,
            env=env,
        )
        dependency_audits = sorted(audits.glob("generic_box_owner-*.json"))
        assert len(dependency_audits) == 1, list(audits.glob("*.json"))
        dependency_audit = json.loads(
            dependency_audits[0].read_text(encoding="utf-8")
        )
        validate(dependency_audit, load_event(stdout))
    print(json.dumps({"source": "generic_box_monomorphized_identity", "validated": True}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
