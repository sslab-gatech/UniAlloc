#!/usr/bin/env python3
"""Prove partial Result<Vec<T>, E>::clone unwind stays audit-only and falls back."""

from __future__ import annotations

import argparse
import contextlib
import copy
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile


ROOT = Path(__file__).resolve().parents[2]
DEFAULT_PASS_SOURCE = (
    ROOT / "tools/unialloc-rustc-pass/unialloc-rustc-mir-rewrite-dry-run.rs"
)
APP_CRATE = "result_clone_partial_unwind_type_isolation_app"
RUNTIME_SOURCE = "result_clone_partial_unwind_type_isolation"
RESULT_CLONE_HELPER = "clone_result_with_partial_panic"
PRODUCER_SEED_HELPER = "seed_producer_buffer"
PRODUCER_RECOVERY_HELPER = "recover_producer_buffer"
CONSUMER_SEED_HELPER = "seed_consumer_buffer"
CONSUMER_RECOVERY_HELPER = "recover_consumer_buffer"
TYPE_ISOLATED = 0x1


def run(
    command: list[str], *, cwd: Path, env: dict[str, str], timeout: int = 300
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        command,
        cwd=cwd,
        env=env,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=timeout,
        check=False,
    )


def require_success(result: subprocess.CompletedProcess[str], command: list[str]) -> None:
    if result.returncode != 0:
        raise AssertionError(
            f"command failed ({result.returncode}): {' '.join(command)}\n"
            f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"
        )


def current_rustc_cfg(toolchain: str) -> list[str]:
    normalized = toolchain.lstrip("+").strip()
    if normalized == "nightly" or normalized.startswith(("nightly-2025", "nightly-2026")):
        return ["--cfg", "unialloc_rustc_current"]
    return []


def write_fixture(workspace: Path) -> Path:
    app = workspace / "application"
    (app / "src").mkdir(parents=True)
    unialloc_path = json.dumps(str(ROOT / "unialloc"))
    (app / "Cargo.toml").write_text(
        f'''[package]
name = "result-clone-partial-unwind-type-isolation-app"
version = "0.1.0"
edition = "2021"

[dependencies]
unialloc = {{ path = {unialloc_path}, features = ["stats", "type_isolation"] }}
''',
        encoding="utf-8",
    )
    shutil.copy2(ROOT / "Cargo.lock", app / "Cargo.lock")
    (app / "src/main.rs").write_text(
        r'''use std::any::Any;
use std::fmt::Write as _;
use std::mem::{align_of, size_of};
use std::panic::{catch_unwind, AssertUnwindSafe};
use std::sync::atomic::{AtomicUsize, Ordering};
use std::sync::Mutex;

use unialloc::{
    semantic_auto_metadata_disable, semantic_fallback_attribution_snapshot,
    semantic_metadata_validation_snapshot, semantic_scope_depth_snapshot,
    semantic_stats_recording_disable, semantic_stats_recording_enable,
    semantic_stats_reset, semantic_stats_snapshot,
    semantic_type_stats_recording_disable, semantic_type_stats_snapshot,
    type_isolation_side_cache_snapshot, SemanticTypeStatsSnapshot, UniAlloc,
};

#[global_allocator]
static ALLOCATOR: UniAlloc = UniAlloc;

const PANIC_AT_CLONE_ATTEMPT: usize = 2;

static CLONE_ATTEMPTS: AtomicUsize = AtomicUsize::new(0);
static SUCCESSFUL_CLONES: AtomicUsize = AtomicUsize::new(0);
static CLEANUP_WITNESS_DROPS: AtomicUsize = AtomicUsize::new(0);
static PARTIAL_CLEANUP_ARMED: AtomicUsize = AtomicUsize::new(0);
static WITNESS_TYPED_DEALLOCATIONS: AtomicUsize = AtomicUsize::new(0);
static WITNESS_CACHE_INSERTS: AtomicUsize = AtomicUsize::new(0);
static PANIC_MAIN_DEPTH: AtomicUsize = AtomicUsize::new(0);
static PANIC_OVERFLOW_DEPTH: AtomicUsize = AtomicUsize::new(0);
static PANIC_REPRESENTED_DEPTH: AtomicUsize = AtomicUsize::new(0);
static PANIC_PAYLOAD: Mutex<Option<Box<dyn Any + Send>>> = Mutex::new(None);

#[derive(Debug, Eq, PartialEq)]
#[repr(C)]
struct ProducerPayload([u64; 8]);

#[derive(Debug, Eq, PartialEq)]
#[repr(C)]
struct ConsumerPayload([u64; 8]);

struct ResultCloneCleanupWitness;

impl Drop for ProducerPayload {
    fn drop(&mut self) {
        // Keep unwind runtime and payload transport outside accounting. The
        // first already-cloned element re-enables stats immediately before
        // Vec releases its partial buffer.
        if PARTIAL_CLEANUP_ARMED
            .compare_exchange(1, 0, Ordering::SeqCst, Ordering::SeqCst)
            .is_ok()
        {
            semantic_stats_recording_enable();
        }
    }
}

impl Drop for ResultCloneCleanupWitness {
    fn drop(&mut self) {
        let stats = semantic_stats_snapshot();
        WITNESS_TYPED_DEALLOCATIONS.store(stats.typed_deallocations, Ordering::SeqCst);
        WITNESS_CACHE_INSERTS.store(stats.typed_cache_inserts, Ordering::SeqCst);
        semantic_stats_recording_disable();
        CLEANUP_WITNESS_DROPS.fetch_add(1, Ordering::SeqCst);
    }
}

impl Clone for ProducerPayload {
    fn clone(&self) -> Self {
        let attempt = CLONE_ATTEMPTS.fetch_add(1, Ordering::SeqCst);
        if attempt == PANIC_AT_CLONE_ATTEMPT {
            let depth = semantic_scope_depth_snapshot();
            PANIC_MAIN_DEPTH.store(depth.main_depth, Ordering::SeqCst);
            PANIC_OVERFLOW_DEPTH.store(depth.overflow_depth, Ordering::SeqCst);
            PANIC_REPRESENTED_DEPTH.store(depth.represented_depth, Ordering::SeqCst);
            let payload = PANIC_PAYLOAD
                .lock()
                .expect("panic payload mutex")
                .take()
                .expect("preallocated panic payload");
            PARTIAL_CLEANUP_ARMED.store(1, Ordering::SeqCst);
            semantic_stats_recording_disable();
            std::panic::resume_unwind(payload);
        }
        SUCCESSFUL_CLONES.fetch_add(1, Ordering::SeqCst);
        Self(self.0)
    }
}

#[inline(never)]
fn producer_payload(seed: u64) -> ProducerPayload {
    ProducerPayload([
        seed,
        seed ^ 0x1111_1111_1111_1111,
        seed ^ 0x2222_2222_2222_2222,
        seed ^ 0x3333_3333_3333_3333,
        seed ^ 0x4444_4444_4444_4444,
        seed ^ 0x5555_5555_5555_5555,
        seed ^ 0x6666_6666_6666_6666,
        seed ^ 0x7777_7777_7777_7777,
    ])
}

#[inline(never)]
fn seed_producer_buffer() -> usize {
    let mut values = Vec::<ProducerPayload>::with_capacity(4);
    for index in 0..4_u64 {
        values.push(producer_payload(0x5100_0000 + index));
    }
    let pointer = values.as_ptr() as usize;
    assert_ne!(pointer, 0);
    drop(values);
    pointer
}

#[inline(never)]
fn recover_producer_buffer() -> Vec<ProducerPayload> {
    Vec::<ProducerPayload>::with_capacity(4)
}

#[inline(never)]
fn seed_consumer_buffer() -> usize {
    let mut values = Vec::<ConsumerPayload>::with_capacity(4);
    for index in 0..4_u64 {
        values.push(ConsumerPayload(producer_payload(0xC050_0000 + index).0));
    }
    let pointer = values.as_ptr() as usize;
    assert_ne!(pointer, 0);
    drop(values);
    pointer
}

#[inline(never)]
fn recover_consumer_buffer() -> Vec<ConsumerPayload> {
    Vec::<ConsumerPayload>::with_capacity(4)
}

#[inline(never)]
fn clone_result_with_partial_panic(
    source: &Result<Vec<ProducerPayload>, u8>,
) -> Result<Vec<ProducerPayload>, u8> {
    // Keep one ordinary Rust cleanup edge in this frame. Broad Result Clone
    // stays audit-only, so both its allocation and partial-unwind release use
    // the fallback path; no manual scope or metadata ABI is used.
    let _cleanup_witness = ResultCloneCleanupWitness;
    <Result<Vec<ProducerPayload>, u8> as Clone>::clone(source)
}

fn type_rows_json(rows: &[SemanticTypeStatsSnapshot], row_count: usize) -> String {
    let mut output = String::from("[");
    let mut emitted = 0usize;
    for row in rows.iter().take(std::cmp::min(row_count, rows.len())) {
        if row.allocations == 0 && row.deallocations == 0 {
            continue;
        }
        if emitted != 0 {
            output.push(',');
        }
        emitted += 1;
        let _ = write!(
            output,
            concat!(
                "{{",
                "\"type_id\":{},",
                "\"module_id\":{},",
                "\"callsite\":{},",
                "\"allocations\":{},",
                "\"allocated_bytes\":{},",
                "\"deallocations\":{},",
                "\"cache_hits\":{},",
                "\"cache_inserts\":{},",
                "\"cache_bypasses\":{},",
                "\"observed_alloc_size\":{},",
                "\"observed_alloc_align\":{},",
                "\"observed_dealloc_size\":{},",
                "\"observed_dealloc_align\":{},",
                "\"policy_flags_seen\":{}",
                "}}"
            ),
            row.type_id,
            row.module_id,
            row.callsite,
            row.allocations,
            row.allocated_bytes,
            row.deallocations,
            row.cache_hits,
            row.cache_inserts,
            row.cache_bypasses,
            row.observed_alloc_size,
            row.observed_alloc_align,
            row.observed_dealloc_size,
            row.observed_dealloc_align,
            row.policy_flags_seen,
        );
    }
    output.push(']');
    output
}

fn main() {
    assert_eq!(size_of::<ProducerPayload>(), 64);
    assert_eq!(align_of::<ProducerPayload>(), 8);
    assert_eq!(size_of::<ConsumerPayload>(), size_of::<ProducerPayload>());
    assert_eq!(align_of::<ConsumerPayload>(), align_of::<ProducerPayload>());

    // The generated application uses ordinary Result::clone and Vec APIs. It
    // never calls a metadata allocation ABI. The actual RUSTC_WRAPPER must
    // leave the broad Result Clone audit-only while retaining exact Vec
    // controls around it.
    semantic_auto_metadata_disable();
    *PANIC_PAYLOAD.lock().expect("panic payload mutex") =
        Some(Box::new("partial Result<Vec<ProducerPayload>, u8>::clone probe"));
    let initial_depth = semantic_scope_depth_snapshot();

    let mut source_values = Vec::<ProducerPayload>::with_capacity(4);
    for index in 0..4_u64 {
        source_values.push(producer_payload(0x5000_0000 + index));
    }
    let source_pointer = source_values.as_ptr() as usize;
    let source = Ok::<Vec<ProducerPayload>, u8>(source_values);

    let producer_seed_pointer = seed_producer_buffer();
    let consumer_seed_pointer = seed_consumer_buffer();

    semantic_stats_reset();
    let caught = catch_unwind(AssertUnwindSafe(|| {
        let unexpected = clone_result_with_partial_panic(&source);
        std::mem::forget(unexpected);
    }));
    // Exclude destruction of std's caught panic payload. The partial Vec
    // buffer was already destroyed while the clone frame unwound with stats
    // re-enabled by ProducerPayload::drop and disabled by the cleanup witness.
    semantic_stats_recording_disable();
    let panic_observed = caught.is_err();
    drop(caught);
    semantic_stats_recording_enable();
    let source_len = source.as_ref().map(Vec::len).unwrap_or(0);

    let post_unwind_depth = semantic_scope_depth_snapshot();
    let panic_main_depth = PANIC_MAIN_DEPTH.load(Ordering::SeqCst);
    let panic_overflow_depth = PANIC_OVERFLOW_DEPTH.load(Ordering::SeqCst);
    let panic_represented_depth = PANIC_REPRESENTED_DEPTH.load(Ordering::SeqCst);

    // Recover Consumer first. Same-layout storage must remain in its own
    // identity domain even though the Producer buffer was just unwound.
    let consumer_recovery = recover_consumer_buffer();
    let consumer_recovery_pointer = consumer_recovery.as_ptr() as usize;

    let producer_recovery = recover_producer_buffer();
    let producer_recovery_pointer = producer_recovery.as_ptr() as usize;
    let producer_exact_reuse_after_unwind =
        producer_recovery_pointer == producer_seed_pointer;
    let consumer_exact_reuse = consumer_recovery_pointer == consumer_seed_pointer;
    let consumer_avoided_producer_storage =
        consumer_recovery_pointer != producer_seed_pointer;

    let stats = semantic_stats_snapshot();
    let fallback = semantic_fallback_attribution_snapshot();
    let validation = semantic_metadata_validation_snapshot();
    let side_cache = type_isolation_side_cache_snapshot();
    let final_depth = semantic_scope_depth_snapshot();
    let mut rows = [SemanticTypeStatsSnapshot::empty(); 32];
    let row_count = semantic_type_stats_snapshot(&mut rows);

    semantic_type_stats_recording_disable();
    semantic_stats_recording_disable();
    std::mem::forget(source);
    std::mem::forget(consumer_recovery);
    std::mem::forget(producer_recovery);
    let type_rows = type_rows_json(&rows, row_count);
    println!(
        concat!(
            "{{",
            "\"source\":\"result_clone_partial_unwind_type_isolation\",",
            "\"panic_observed\":{},",
            "\"clone_attempts\":{},",
            "\"successful_clones\":{},",
            "\"cleanup_witness_drops\":{},",
            "\"witness_typed_deallocations\":{},",
            "\"witness_cache_inserts\":{},",
            "\"source_len\":{},",
            "\"initial_main_depth\":{},",
            "\"initial_overflow_depth\":{},",
            "\"initial_represented_depth\":{},",
            "\"panic_main_depth\":{},",
            "\"panic_overflow_depth\":{},",
            "\"panic_represented_depth\":{},",
            "\"post_unwind_main_depth\":{},",
            "\"post_unwind_overflow_depth\":{},",
            "\"post_unwind_represented_depth\":{},",
            "\"final_main_depth\":{},",
            "\"final_overflow_depth\":{},",
            "\"final_represented_depth\":{},",
            "\"source_pointer\":{},",
            "\"producer_seed_pointer\":{},",
            "\"consumer_seed_pointer\":{},",
            "\"producer_recovery_pointer\":{},",
            "\"consumer_recovery_pointer\":{},",
            "\"producer_exact_reuse_after_unwind\":{},",
            "\"consumer_exact_reuse\":{},",
            "\"consumer_avoided_producer_storage\":{},",
            "\"total_allocations\":{},",
            "\"typed_allocations\":{},",
            "\"typed_allocated_bytes\":{},",
            "\"total_deallocations\":{},",
            "\"typed_deallocations\":{},",
            "\"typed_cache_hits\":{},",
            "\"typed_cache_inserts\":{},",
            "\"typed_cache_bypasses\":{},",
            "\"fallback_allocations\":{},",
            "\"fallback_deallocations\":{},",
            "\"raw_alloc_no_metadata\":{},",
            "\"raw_alloc_no_metadata_bytes\":{},",
            "\"raw_dealloc_no_metadata\":{},",
            "\"raw_realloc_no_metadata\":{},",
            "\"raw_realloc_no_metadata_bytes\":{},",
            "\"raw_realloc_moved_dealloc_no_metadata\":{},",
            "\"realloc_recorded_old_metadata_new_allocations\":{},",
            "\"realloc_recorded_old_metadata_new_allocation_bytes\":{},",
            "\"recovery_identity_matches\":{},",
            "\"recovery_identity_mismatches\":{},",
            "\"side_cache_corrupt_slots\":{},",
            "\"semantic_type_stats_dropped_events\":{},",
            "\"type_rows\":{}",
            "}}"
        ),
        panic_observed,
        CLONE_ATTEMPTS.load(Ordering::SeqCst),
        SUCCESSFUL_CLONES.load(Ordering::SeqCst),
        CLEANUP_WITNESS_DROPS.load(Ordering::SeqCst),
        WITNESS_TYPED_DEALLOCATIONS.load(Ordering::SeqCst),
        WITNESS_CACHE_INSERTS.load(Ordering::SeqCst),
        source_len,
        initial_depth.main_depth,
        initial_depth.overflow_depth,
        initial_depth.represented_depth,
        panic_main_depth,
        panic_overflow_depth,
        panic_represented_depth,
        post_unwind_depth.main_depth,
        post_unwind_depth.overflow_depth,
        post_unwind_depth.represented_depth,
        final_depth.main_depth,
        final_depth.overflow_depth,
        final_depth.represented_depth,
        source_pointer,
        producer_seed_pointer,
        consumer_seed_pointer,
        producer_recovery_pointer,
        consumer_recovery_pointer,
        producer_exact_reuse_after_unwind,
        consumer_exact_reuse,
        consumer_avoided_producer_storage,
        stats.total_allocations,
        stats.typed_allocations,
        stats.typed_allocated_bytes,
        stats.total_deallocations,
        stats.typed_deallocations,
        stats.typed_cache_hits,
        stats.typed_cache_inserts,
        stats.typed_cache_bypasses,
        stats.fallback_allocations,
        stats.fallback_deallocations,
        fallback.raw_alloc_no_metadata,
        fallback.raw_alloc_no_metadata_bytes,
        fallback.raw_dealloc_no_metadata,
        fallback.raw_realloc_no_metadata,
        fallback.raw_realloc_no_metadata_bytes,
        fallback.raw_realloc_moved_dealloc_no_metadata,
        fallback.realloc_recorded_old_metadata_new_allocations,
        fallback.realloc_recorded_old_metadata_new_allocation_bytes,
        validation.recovery_identity_matches,
        validation.recovery_identity_mismatches,
        side_cache.corrupt_slots,
        stats.semantic_type_stats_dropped_events,
        type_rows,
    );
}
''',
        encoding="utf-8",
    )
    return app


def function_matches(row: dict[str, object], function_name: str) -> bool:
    mir_function = str(row.get("mir_function") or "")
    return mir_function == function_name or mir_function.endswith(f"::{function_name}")


def actual_scope(row: dict[str, object], label: str) -> None:
    assert row.get("lowering_kind") == "semantic_scope_enter_exit_rewrite", (
        label,
        row,
    )
    assert row.get("rewrite_status") == "actual_semantic_scope_enter_exit_rewrite_applied", (
        label,
        row,
    )
    assert str(row.get("replacement_resolution_status") or "").startswith(
        "resolved_unialloc_semantic_scope"
    ), (label, row)
    assert row.get("metadata_pairing_contract") == "semantic_scope_active_metadata", (
        label,
        row,
    )
    assert int(row.get("flags") or 0) & TYPE_ISOLATED, (label, row)


def allocation_scope_rows(
    audit: dict[str, object], function_name: str, payload: str
) -> list[dict[str, object]]:
    rows = audit.get("rewrite_candidates") or []
    return [
        row
        for row in rows
        if isinstance(row, dict)
        and function_matches(row, function_name)
        and "with_capacity" in str(row.get("callee") or "")
        and payload
        in " ".join(
            str(row.get(field) or "")
            for field in ("semantic_object_type", "destination_type")
        )
    ]


def unique_scope(rows: list[dict[str, object]], label: str) -> dict[str, object]:
    assert len(rows) == 1, f"{label} must have exactly one scope, got {rows!r}"
    row = rows[0]
    actual_scope(row, label)
    assert int(row.get("type_id") or 0) != 0, (label, row)
    assert int(row.get("module_id") or 0) != 0, (label, row)
    return row


def unique_audit_only_clone(
    rows: list[dict[str, object]], label: str
) -> dict[str, object]:
    assert len(rows) == 1, f"{label} must have exactly one audit row, got {rows!r}"
    row = rows[0]
    assert row.get("lowering_kind") == "semantic_scope_unsolved_heap_object_candidate", (
        label,
        row,
    )
    assert row.get("rewrite_status") == (
        "semantic_scope_rewrite_skipped_unresolved_heap_object_type"
    ), (label, row)
    assert row.get("replacement_resolution_status") == (
        "rustc_middle_heap_object_type_not_solved"
    ), (label, row)
    assert row.get("metadata_pairing_contract") == (
        "audit_only_unresolved_heap_object_type"
    ), (label, row)
    assert row.get("semantic_object_type") == "<unknown-heap-object-type>", (label, row)
    assert row.get("semantic_scope_unwind_pop_inserted") is False, (label, row)
    return row


def validate_audit(audit: dict[str, object]) -> dict[str, object]:
    summary = audit.get("summary") or {}
    assert isinstance(summary, dict)
    assert summary.get("provider_override_installed") is True
    assert summary.get("body_clone_returned_to_rustc") is True
    assert summary.get("actual_semantic_scope_rewrite") is True
    assert int(summary.get("semantic_scope_rewrite_applied_count") or 0) > 0

    rows = audit.get("rewrite_candidates") or []
    result_rows = [
        row
        for row in rows
        if isinstance(row, dict)
        and function_matches(row, RESULT_CLONE_HELPER)
        and "Clone" in str(row.get("callee") or "")
        and "clone" in str(row.get("callee") or "")
        and "Result" in str(row.get("destination_type") or "")
        and "ProducerPayload" in str(row.get("destination_type") or "")
        and "u8" in str(row.get("destination_type") or "")
    ]
    result_row = unique_audit_only_clone(result_rows, "partial Result Clone")
    assert "src/main.rs" in str(result_row.get("source_span") or ""), result_row

    source_row = unique_scope(
        allocation_scope_rows(audit, "main", "Vec<ProducerPayload"),
        "live source Producer allocation",
    )

    producer_rows = [
        unique_scope(
            allocation_scope_rows(audit, function_name, "Vec<ProducerPayload"),
            f"{function_name} Producer allocation",
        )
        for function_name in (PRODUCER_SEED_HELPER, PRODUCER_RECOVERY_HELPER)
    ]
    consumer_rows = [
        unique_scope(
            allocation_scope_rows(audit, function_name, "Vec<ConsumerPayload"),
            f"{function_name} Consumer allocation",
        )
        for function_name in (CONSUMER_SEED_HELPER, CONSUMER_RECOVERY_HELPER)
    ]

    producer_type_ids = {
        int(row.get("type_id") or 0)
        for row in (source_row, *producer_rows)
    }
    producer_module_ids = {
        int(row.get("module_id") or 0)
        for row in (source_row, *producer_rows)
    }
    consumer_type_ids = {int(row.get("type_id") or 0) for row in consumer_rows}
    consumer_module_ids = {int(row.get("module_id") or 0) for row in consumer_rows}
    assert len(producer_type_ids) == 1, producer_type_ids
    assert len(producer_module_ids) == 1, producer_module_ids
    assert len(consumer_type_ids) == 1, consumer_type_ids
    assert len(consumer_module_ids) == 1, consumer_module_ids
    producer_type_id = next(iter(producer_type_ids))
    producer_module_id = next(iter(producer_module_ids))
    consumer_type_id = next(iter(consumer_type_ids))
    consumer_module_id = next(iter(consumer_module_ids))
    assert producer_type_id != consumer_type_id
    assert producer_module_id == consumer_module_id
    result_callsite = int(result_row.get("callsite") or 0)
    source_callsite = int(source_row.get("callsite") or 0)
    assert result_callsite != 0 and source_callsite != 0
    assert result_callsite != source_callsite

    return {
        "producer_type_id": producer_type_id,
        "consumer_type_id": consumer_type_id,
        "module_id": producer_module_id,
        "result_clone_callsite": result_callsite,
        "live_source_allocation_callsite": source_callsite,
        "result_clone_source_span": result_row.get("source_span"),
        "result_clone_basic_block": result_row.get("basic_block"),
        "result_clone_rewrite_status": result_row.get("rewrite_status"),
        "result_clone_audit_only": True,
        "result_clone_unwind_pop_inserted": False,
        "zero_result_clone_rewrites": True,
        "producer_control_scopes": len(producer_rows),
        "consumer_control_scopes": len(consumer_rows),
    }


def aggregate_type_rows(
    runtime: dict[str, object], *, type_id: int, module_id: int
) -> dict[str, int]:
    rows = runtime.get("type_rows")
    assert isinstance(rows, list), runtime
    matching = [
        row
        for row in rows
        if isinstance(row, dict)
        and int(row.get("type_id") or 0) == type_id
        and int(row.get("module_id") or 0) == module_id
    ]
    assert matching, (type_id, module_id, rows)
    assert all(int(row.get("policy_flags_seen") or 0) & TYPE_ISOLATED for row in matching)
    totals = {
        field: sum(int(row.get(field) or 0) for row in matching)
        for field in (
            "allocations",
            "allocated_bytes",
            "deallocations",
            "cache_hits",
            "cache_inserts",
            "cache_bypasses",
        )
    }
    totals["observed_dealloc_size"] = max(
        int(row.get("observed_dealloc_size") or 0) for row in matching
    )
    totals["observed_dealloc_align"] = max(
        int(row.get("observed_dealloc_align") or 0) for row in matching
    )
    return totals


def validate_runtime(
    runtime: dict[str, object], audit_evidence: dict[str, object]
) -> dict[str, object]:
    assert runtime.get("source") == RUNTIME_SOURCE, runtime
    assert runtime.get("panic_observed") is True, "partial clone panic was not observed"
    assert int(runtime.get("clone_attempts") or 0) == 3, runtime
    assert int(runtime.get("successful_clones") or 0) == 2, runtime
    assert int(runtime.get("cleanup_witness_drops") or 0) == 1, runtime
    assert int(runtime.get("witness_typed_deallocations") or 0) == 0, runtime
    assert int(runtime.get("witness_cache_inserts") or 0) == 0, runtime
    assert int(runtime.get("source_len") or 0) == 4, runtime

    for prefix in ("initial", "panic", "post_unwind", "final"):
        assert int(runtime.get(f"{prefix}_main_depth") or 0) == 0, runtime
        assert int(runtime.get(f"{prefix}_overflow_depth") or 0) == 0, runtime
        assert int(runtime.get(f"{prefix}_represented_depth") or 0) == 0, runtime

    source_pointer = int(runtime.get("source_pointer") or 0)
    producer_seed = int(runtime.get("producer_seed_pointer") or 0)
    consumer_seed = int(runtime.get("consumer_seed_pointer") or 0)
    producer_recovery = int(runtime.get("producer_recovery_pointer") or 0)
    consumer_recovery = int(runtime.get("consumer_recovery_pointer") or 0)
    assert all(
        pointer != 0
        for pointer in (
            source_pointer,
            producer_seed,
            consumer_seed,
            producer_recovery,
            consumer_recovery,
        )
    )
    assert len({source_pointer, producer_seed, consumer_seed}) == 3
    assert runtime.get("producer_exact_reuse_after_unwind") is True
    assert producer_recovery == producer_seed
    assert runtime.get("consumer_exact_reuse") is True
    assert consumer_recovery == consumer_seed
    assert runtime.get("consumer_avoided_producer_storage") is True
    assert consumer_recovery != producer_seed

    for field, expected in (
        ("total_allocations", 3),
        ("typed_allocations", 2),
        ("typed_allocated_bytes", 512),
        ("total_deallocations", 1),
        ("typed_deallocations", 0),
        ("typed_cache_hits", 2),
        ("typed_cache_inserts", 0),
        ("typed_cache_bypasses", 0),
        ("fallback_allocations", 1),
        ("fallback_deallocations", 1),
        ("raw_alloc_no_metadata", 1),
        ("raw_alloc_no_metadata_bytes", 256),
        ("raw_dealloc_no_metadata", 1),
        ("raw_realloc_no_metadata", 0),
        ("raw_realloc_no_metadata_bytes", 0),
        ("raw_realloc_moved_dealloc_no_metadata", 0),
        ("realloc_recorded_old_metadata_new_allocations", 0),
        ("realloc_recorded_old_metadata_new_allocation_bytes", 0),
        ("recovery_identity_matches", 0),
        ("recovery_identity_mismatches", 0),
        ("side_cache_corrupt_slots", 0),
        ("semantic_type_stats_dropped_events", 0),
    ):
        assert int(runtime.get(field) or 0) == expected, (field, runtime)

    module_id = int(audit_evidence["module_id"])
    producer = aggregate_type_rows(
        runtime,
        type_id=int(audit_evidence["producer_type_id"]),
        module_id=module_id,
    )
    consumer = aggregate_type_rows(
        runtime,
        type_id=int(audit_evidence["consumer_type_id"]),
        module_id=module_id,
    )
    assert producer == {
        "allocations": 1,
        "allocated_bytes": 256,
        "deallocations": 0,
        "cache_hits": 1,
        "cache_inserts": 0,
        "cache_bypasses": 0,
        "observed_dealloc_size": 0,
        "observed_dealloc_align": 0,
    }, producer
    assert consumer == {
        "allocations": 1,
        "allocated_bytes": 256,
        "deallocations": 0,
        "cache_hits": 1,
        "cache_inserts": 0,
        "cache_bypasses": 0,
        "observed_dealloc_size": 0,
        "observed_dealloc_align": 0,
    }, consumer
    return {
        "scope_depth_transition": {"at_panic": 0, "after_unwind": 0},
        "partial_clone": {"attempts": 3, "completed_elements": 2},
        "cleanup_witness_drops": 1,
        "partial_cleanup_window": {
            "typed_deallocations": 0,
            "cache_inserts": 0,
            "fallback_deallocations": 1,
        },
        "producer_runtime": producer,
        "consumer_runtime": consumer,
        "result_clone_used_fallback": True,
        "result_clone_partial_release_used_fallback": True,
        "producer_typed_entry_preserved": True,
        "producer_exact_reuse_after_unwind": True,
        "consumer_same_layout_non_reuse": True,
    }


def validate(
    audit: dict[str, object], runtime: dict[str, object]
) -> dict[str, object]:
    audit_evidence = validate_audit(audit)
    return {
        "audit": audit_evidence,
        "runtime": validate_runtime(runtime, audit_evidence),
    }


def expect_rejected(
    audit: dict[str, object], runtime: dict[str, object], label: str
) -> str:
    try:
        validate(audit, runtime)
    except AssertionError as error:
        return str(error) or repr(error)
    raise AssertionError(f"validator negative control accepted {label}")


def validator_negative_controls(
    audit: dict[str, object], runtime: dict[str, object]
) -> dict[str, object]:
    non_panicking = copy.deepcopy(runtime)
    non_panicking["panic_observed"] = False

    broad_clone_rewritten = copy.deepcopy(audit)
    rewritten_rows = [
        row
        for row in broad_clone_rewritten.get("rewrite_candidates") or []
        if isinstance(row, dict)
        and function_matches(row, RESULT_CLONE_HELPER)
        and "Clone" in str(row.get("callee") or "")
        and "Result" in str(row.get("destination_type") or "")
    ]
    assert len(rewritten_rows) == 1, rewritten_rows
    rewritten_rows[0]["rewrite_status"] = "semantic_scope_enter_exit_rewrite_planned"
    rewritten_rows[0]["lowering_kind"] = "semantic_scope_enter_exit_rewrite"

    missing_fallback = copy.deepcopy(runtime)
    missing_fallback["fallback_allocations"] = 0

    wrong_depth = copy.deepcopy(runtime)
    wrong_depth["panic_main_depth"] = 1

    return {
        "non_panicking_partial_clone_fixture_rejected": expect_rejected(
            audit, non_panicking, "non-panicking clone"
        ),
        "broad_clone_rewrite_rejected": expect_rejected(
            broad_clone_rewritten, runtime, "applied Result Clone scope"
        ),
        "missing_fallback_event_rejected": expect_rejected(
            audit, missing_fallback, "missing fallback allocation"
        ),
        "forged_scope_depth_rejected": expect_rejected(
            audit, wrong_depth, "forged broad Clone scope depth"
        ),
    }


def load_runtime(stdout: str) -> dict[str, object]:
    events = []
    for line in stdout.splitlines():
        stripped = line.strip()
        if stripped.startswith("{"):
            event = json.loads(stripped)
            if isinstance(event, dict) and event.get("source") == RUNTIME_SOURCE:
                events.append(event)
    assert len(events) == 1, (stdout, events)
    return events[0]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--pass-source", type=Path, default=DEFAULT_PASS_SOURCE)
    parser.add_argument(
        "--toolchain",
        default=(ROOT / "rust-toolchain").read_text(encoding="utf-8").strip(),
    )
    parser.add_argument("--timeout", type=int, default=300)
    parser.add_argument("--output-dir", type=Path)
    return parser.parse_args()


def main() -> int:
    if not __debug__:
        raise SystemExit("do not run this assertion-based validator with python -O")
    args = parse_args()
    pass_source = args.pass_source.resolve()
    toolchain = args.toolchain.lstrip("+").strip()
    assert pass_source.is_file(), pass_source
    rustc = shutil.which("rustc") or "rustc"
    cargo = shutil.which("cargo") or "cargo"
    sysroot = subprocess.check_output(
        [rustc, f"+{toolchain}", "--print", "sysroot"], cwd=ROOT, text=True
    ).strip()

    if args.output_dir:
        persistent_workspace = args.output_dir.resolve()
        persistent_workspace.mkdir(parents=True, exist_ok=True)
        assert not any(persistent_workspace.iterdir()), persistent_workspace
        workspace_context = contextlib.nullcontext(str(persistent_workspace))
    else:
        persistent_workspace = None
        workspace_context = tempfile.TemporaryDirectory(
            prefix="unialloc-result-clone-unwind-"
        )

    with workspace_context as raw:
        workspace = Path(raw)
        app = write_fixture(workspace)
        pass_binary = workspace / "unialloc-rustc-mir-rewrite-dry-run"
        build_env = os.environ.copy()
        build_env["RUSTC_BOOTSTRAP"] = "1"
        build_command = [
            rustc,
            f"+{toolchain}",
            *current_rustc_cfg(toolchain),
            str(pass_source),
            "-o",
            str(pass_binary),
        ]
        build = run(build_command, cwd=ROOT, env=build_env, timeout=args.timeout)
        require_success(build, build_command)

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
                "UNIALLOC_RUSTC_TARGET_CRATES": APP_CRATE,
                "UNIALLOC_REWRITE_AUDIT_DIR": str(rewrites),
                "UNIALLOC_PASS_LOG_DIR": str(logs),
                "UNIALLOC_CONTINUE_COMPILATION": "1",
                "UNIALLOC_ACTUAL_MIR_REWRITE": "1",
                "UNIALLOC_ACTUAL_SEMANTIC_SCOPE_REWRITE": "1",
                "UNIALLOC_LOWERING_POLICY_FLAGS": str(TYPE_ISOLATED),
                "UNIALLOC_RUSTC_SYSROOT": sysroot,
                "CARGO_NET_OFFLINE": "true",
                "CARGO_INCREMENTAL": "0",
                # The repository dev profile aborts. This isolated generated
                # fixture must execute the actual MIR cleanup edge.
                "CARGO_PROFILE_DEV_PANIC": "unwind",
                "CARGO_TARGET_DIR": str(workspace / "target"),
            }
        )
        if persistent_workspace is not None:
            mir_dir = workspace / "mir"
            mir_dir.mkdir()
            existing_rustflags = run_env.get("RUSTFLAGS", "").strip()
            dump_flags = (
                f"-Zdump-mir={RESULT_CLONE_HELPER} "
                f"-Zdump-mir-dir={mir_dir}"
            )
            run_env["RUSTFLAGS"] = " ".join(
                value for value in (existing_rustflags, dump_flags) if value
            )
        run_command = [
            cargo,
            f"+{toolchain}",
            "run",
            "--quiet",
            "--manifest-path",
            str(app / "Cargo.toml"),
        ]
        runtime_run = run(
            run_command, cwd=workspace, env=run_env, timeout=args.timeout
        )
        require_success(runtime_run, run_command)

        audit_paths = sorted(rewrites.glob(f"{APP_CRATE}-*.json"))
        assert len(audit_paths) == 1, audit_paths
        audit = json.loads(audit_paths[0].read_text(encoding="utf-8"))
        runtime = load_runtime(runtime_run.stdout)
        negative_controls = validator_negative_controls(audit, runtime)
        evidence = validate(audit, runtime)

    print(
        json.dumps(
            {
                "source": "mir_result_clone_partial_unwind_type_isolation_probe",
                "validated": True,
                "single_current_toolchain_run": True,
                "benchmark": False,
                "toolchain": toolchain,
                "pass_source": str(pass_source),
                "debug_workspace": (
                    str(persistent_workspace) if persistent_workspace else None
                ),
                "validator_negative_controls": negative_controls,
                "evidence": evidence,
                "boundaries": [
                    "Functional actual-RUSTC_WRAPPER regression for one Ok(Result<Vec<ProducerPayload>, u8>) audit-only partial-clone unwind path; no universal Result/Clone coverage claim.",
                    "The generated Rust application uses ordinary Result::clone and Vec APIs and no manual metadata allocation ABI.",
                    "The broad Result Clone remains one exact unresolved audit-only row with no inserted scope or unwind pop; panic and post-catch scope depths remain zero.",
                    "The partial buffer allocation and unwind release use raw fallback while the exact Producer and Consumer typed-cache entries remain independently recoverable.",
                    "Exact Producer cache reuse and same-layout Consumer non-reuse establish the bounded type-isolation identity oracle; no benchmark or performance claim is made.",
                ],
            },
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
