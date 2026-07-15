#!/usr/bin/env python3
"""Prove Err-side Result<u8, Vec<T>>::clone stays audit-only and falls back."""

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
APP_CRATE = "result_err_clone_type_isolation_app"
RUNTIME_SOURCE = "result_err_clone_type_isolation"
RESULT_CLONE_HELPER = "clone_err_result"
SOURCE_HELPER = "make_live_source"
PRODUCER_SEED_HELPER = "seed_producer_buffer"
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
name = "result-err-clone-type-isolation-app"
version = "0.1.0"
edition = "2021"

[dependencies]
unialloc = {{ path = {unialloc_path}, features = ["stats", "type_isolation"] }}
''',
        encoding="utf-8",
    )
    shutil.copy2(ROOT / "Cargo.lock", app / "Cargo.lock")
    (app / "src/main.rs").write_text(
        r'''use std::fmt::Write as _;
use std::mem::{align_of, size_of};

use unialloc::{
    semantic_auto_metadata_disable, semantic_fallback_attribution_snapshot,
    semantic_metadata_validation_snapshot, semantic_scope_depth_snapshot,
    semantic_stats_recording_disable, semantic_stats_reset, semantic_stats_snapshot,
    semantic_type_stats_recording_disable, semantic_type_stats_snapshot,
    type_isolation_side_cache_snapshot, SemanticTypeStatsSnapshot, UniAlloc,
};

#[global_allocator]
static ALLOCATOR: UniAlloc = UniAlloc;

#[derive(Clone, Debug, Eq, PartialEq)]
#[repr(C)]
struct ProducerPayload([u64; 8]);

#[derive(Clone, Debug, Eq, PartialEq)]
#[repr(C)]
struct ConsumerPayload([u64; 8]);

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
fn make_live_source() -> Result<u8, Vec<ProducerPayload>> {
    let mut values = Vec::<ProducerPayload>::with_capacity(4);
    for index in 0..4_u64 {
        values.push(producer_payload(0x5100_0000 + index));
    }
    Err(values)
}

#[inline(never)]
fn seed_producer_buffer() -> usize {
    let mut values = Vec::<ProducerPayload>::with_capacity(4);
    for index in 0..4_u64 {
        values.push(producer_payload(0x5200_0000 + index));
    }
    let pointer = values.as_ptr() as usize;
    assert_ne!(pointer, 0);
    drop(values);
    pointer
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
fn clone_err_result(
    source: &Result<u8, Vec<ProducerPayload>>,
) -> Result<u8, Vec<ProducerPayload>> {
    // Ordinary Rust Clone: no manual semantic scope or metadata allocation ABI.
    <Result<u8, Vec<ProducerPayload>> as Clone>::clone(source)
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

    semantic_auto_metadata_disable();
    let initial_depth = semantic_scope_depth_snapshot();

    // Keep the source live, then publish one Producer and one same-layout
    // Consumer buffer into their distinct typed-cache identity domains.
    let source = make_live_source();
    let source_values = source.as_ref().err().expect("Err source");
    let source_pointer = source_values.as_ptr() as usize;
    let source_len = source_values.len();
    let producer_seed_pointer = seed_producer_buffer();
    let consumer_seed_pointer = seed_consumer_buffer();

    semantic_stats_reset();
    let cloned = clone_err_result(&source);
    let cloned_values = cloned.as_ref().err().expect("Err clone");
    let cloned_pointer = cloned_values.as_ptr() as usize;
    let cloned_len = cloned_values.len();
    let cloned_matches_source = cloned_values == source_values;

    // Recover Consumer only after Result::clone. Its entry must have remained
    // unavailable to the Producer clone even though size and alignment match.
    let consumer_recovery = recover_consumer_buffer();
    let consumer_recovery_pointer = consumer_recovery.as_ptr() as usize;

    let allocation_stats = semantic_stats_snapshot();
    let allocation_fallback = semantic_fallback_attribution_snapshot();
    let allocation_validation = semantic_metadata_validation_snapshot();
    let allocation_side_cache = type_isolation_side_cache_snapshot();
    let pre_cleanup_depth = semantic_scope_depth_snapshot();
    let mut rows = [SemanticTypeStatsSnapshot::empty(); 32];
    let row_count = semantic_type_stats_snapshot(&mut rows);

    // Validate ownership closure in a separate counter window. All three live
    // owners must execute ordinary Rust Drop before the event is emitted.
    semantic_stats_reset();
    drop(source);
    drop(cloned);
    drop(consumer_recovery);
    let cleanup_stats = semantic_stats_snapshot();
    let cleanup_fallback = semantic_fallback_attribution_snapshot();
    let cleanup_validation = semantic_metadata_validation_snapshot();
    let cleanup_side_cache = type_isolation_side_cache_snapshot();
    let final_depth = semantic_scope_depth_snapshot();
    let mut cleanup_rows = [SemanticTypeStatsSnapshot::empty(); 32];
    let cleanup_row_count = semantic_type_stats_snapshot(&mut cleanup_rows);

    semantic_type_stats_recording_disable();
    semantic_stats_recording_disable();
    let type_rows = type_rows_json(&rows, row_count);
    let cleanup_type_rows = type_rows_json(&cleanup_rows, cleanup_row_count);
    println!(
        concat!(
            "{{",
            "\"source\":\"result_err_clone_type_isolation\",",
            "\"result_variant\":\"Err\",",
            "\"source_len\":{},",
            "\"cloned_len\":{},",
            "\"cloned_matches_source\":{},",
            "\"initial_main_depth\":{},",
            "\"initial_overflow_depth\":{},",
            "\"initial_represented_depth\":{},",
            "\"pre_cleanup_main_depth\":{},",
            "\"pre_cleanup_overflow_depth\":{},",
            "\"pre_cleanup_represented_depth\":{},",
            "\"final_main_depth\":{},",
            "\"final_overflow_depth\":{},",
            "\"final_represented_depth\":{},",
            "\"source_pointer\":{},",
            "\"producer_seed_pointer\":{},",
            "\"consumer_seed_pointer\":{},",
            "\"cloned_pointer\":{},",
            "\"consumer_recovery_pointer\":{},",
            "\"producer_exact_reuse\":{},",
            "\"consumer_exact_reuse\":{},",
            "\"clone_avoided_consumer_storage\":{},",
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
            "\"type_rows\":{},",
            "\"cleanup_total_allocations\":{},",
            "\"cleanup_typed_allocations\":{},",
            "\"cleanup_typed_allocated_bytes\":{},",
            "\"cleanup_total_deallocations\":{},",
            "\"cleanup_typed_deallocations\":{},",
            "\"cleanup_typed_cache_hits\":{},",
            "\"cleanup_typed_cache_inserts\":{},",
            "\"cleanup_typed_cache_bypasses\":{},",
            "\"cleanup_fallback_allocations\":{},",
            "\"cleanup_fallback_deallocations\":{},",
            "\"cleanup_raw_alloc_no_metadata\":{},",
            "\"cleanup_raw_alloc_no_metadata_bytes\":{},",
            "\"cleanup_raw_dealloc_no_metadata\":{},",
            "\"cleanup_raw_realloc_no_metadata\":{},",
            "\"cleanup_raw_realloc_no_metadata_bytes\":{},",
            "\"cleanup_raw_realloc_moved_dealloc_no_metadata\":{},",
            "\"cleanup_realloc_recorded_old_metadata_new_allocations\":{},",
            "\"cleanup_realloc_recorded_old_metadata_new_allocation_bytes\":{},",
            "\"cleanup_recovery_identity_matches\":{},",
            "\"cleanup_recovery_identity_mismatches\":{},",
            "\"cleanup_side_cache_corrupt_slots\":{},",
            "\"cleanup_semantic_type_stats_dropped_events\":{},",
            "\"cleanup_type_rows\":{}",
            "}}"
        ),
        source_len,
        cloned_len,
        cloned_matches_source,
        initial_depth.main_depth,
        initial_depth.overflow_depth,
        initial_depth.represented_depth,
        pre_cleanup_depth.main_depth,
        pre_cleanup_depth.overflow_depth,
        pre_cleanup_depth.represented_depth,
        final_depth.main_depth,
        final_depth.overflow_depth,
        final_depth.represented_depth,
        source_pointer,
        producer_seed_pointer,
        consumer_seed_pointer,
        cloned_pointer,
        consumer_recovery_pointer,
        cloned_pointer == producer_seed_pointer,
        consumer_recovery_pointer == consumer_seed_pointer,
        cloned_pointer != consumer_seed_pointer,
        allocation_stats.total_allocations,
        allocation_stats.typed_allocations,
        allocation_stats.typed_allocated_bytes,
        allocation_stats.total_deallocations,
        allocation_stats.typed_deallocations,
        allocation_stats.typed_cache_hits,
        allocation_stats.typed_cache_inserts,
        allocation_stats.typed_cache_bypasses,
        allocation_stats.fallback_allocations,
        allocation_stats.fallback_deallocations,
        allocation_fallback.raw_alloc_no_metadata,
        allocation_fallback.raw_alloc_no_metadata_bytes,
        allocation_fallback.raw_dealloc_no_metadata,
        allocation_fallback.raw_realloc_no_metadata,
        allocation_fallback.raw_realloc_no_metadata_bytes,
        allocation_fallback.raw_realloc_moved_dealloc_no_metadata,
        allocation_fallback.realloc_recorded_old_metadata_new_allocations,
        allocation_fallback.realloc_recorded_old_metadata_new_allocation_bytes,
        allocation_validation.recovery_identity_matches,
        allocation_validation.recovery_identity_mismatches,
        allocation_side_cache.corrupt_slots,
        allocation_stats.semantic_type_stats_dropped_events,
        type_rows,
        cleanup_stats.total_allocations,
        cleanup_stats.typed_allocations,
        cleanup_stats.typed_allocated_bytes,
        cleanup_stats.total_deallocations,
        cleanup_stats.typed_deallocations,
        cleanup_stats.typed_cache_hits,
        cleanup_stats.typed_cache_inserts,
        cleanup_stats.typed_cache_bypasses,
        cleanup_stats.fallback_allocations,
        cleanup_stats.fallback_deallocations,
        cleanup_fallback.raw_alloc_no_metadata,
        cleanup_fallback.raw_alloc_no_metadata_bytes,
        cleanup_fallback.raw_dealloc_no_metadata,
        cleanup_fallback.raw_realloc_no_metadata,
        cleanup_fallback.raw_realloc_no_metadata_bytes,
        cleanup_fallback.raw_realloc_moved_dealloc_no_metadata,
        cleanup_fallback.realloc_recorded_old_metadata_new_allocations,
        cleanup_fallback.realloc_recorded_old_metadata_new_allocation_bytes,
        cleanup_validation.recovery_identity_matches,
        cleanup_validation.recovery_identity_mismatches,
        cleanup_side_cache.corrupt_slots,
        cleanup_stats.semantic_type_stats_dropped_events,
        cleanup_type_rows,
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


def result_clone_rows(audit: dict[str, object]) -> list[dict[str, object]]:
    rows = audit.get("rewrite_candidates") or []
    return [
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


def validate_audit(audit: dict[str, object]) -> dict[str, object]:
    summary = audit.get("summary") or {}
    assert isinstance(summary, dict)
    assert summary.get("provider_override_installed") is True
    assert summary.get("body_clone_returned_to_rustc") is True
    assert summary.get("actual_semantic_scope_rewrite") is True

    result_row = unique_audit_only_clone(
        result_clone_rows(audit), "Err-side Result Clone"
    )
    assert "src/main.rs" in str(result_row.get("source_span") or ""), result_row

    producer_rows = [
        unique_scope(
            allocation_scope_rows(audit, function_name, "Vec<ProducerPayload"),
            f"{function_name} Producer allocation",
        )
        for function_name in (SOURCE_HELPER, PRODUCER_SEED_HELPER)
    ]
    consumer_rows = [
        unique_scope(
            allocation_scope_rows(audit, function_name, "Vec<ConsumerPayload"),
            f"{function_name} Consumer allocation",
        )
        for function_name in (CONSUMER_SEED_HELPER, CONSUMER_RECOVERY_HELPER)
    ]

    producer_type_ids = {int(row.get("type_id") or 0) for row in producer_rows}
    producer_module_ids = {int(row.get("module_id") or 0) for row in producer_rows}
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
    assert result_callsite != 0, result_row

    return {
        "producer_type_id": producer_type_id,
        "consumer_type_id": consumer_type_id,
        "module_id": producer_module_id,
        "result_clone_callsite": result_callsite,
        "result_clone_source_span": result_row.get("source_span"),
        "result_clone_basic_block": result_row.get("basic_block"),
        "result_clone_rewrite_status": result_row.get("rewrite_status"),
        "result_clone_audit_only": True,
        "zero_result_clone_rewrites": True,
        "producer_control_scopes": len(producer_rows),
        "consumer_control_scopes": len(consumer_rows),
    }


def aggregate_type_rows(
    runtime: dict[str, object], *, type_id: int, module_id: int, rows_field: str
) -> dict[str, int]:
    rows = runtime.get(rows_field)
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
    totals["observed_alloc_size"] = max(
        int(row.get("observed_alloc_size") or 0) for row in matching
    )
    totals["observed_alloc_align"] = max(
        int(row.get("observed_alloc_align") or 0) for row in matching
    )
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
    assert runtime.get("result_variant") == "Err", runtime
    assert int(runtime.get("source_len") or 0) == 4, runtime
    assert int(runtime.get("cloned_len") or 0) == 4, runtime
    assert runtime.get("cloned_matches_source") is True, runtime

    for prefix in ("initial", "pre_cleanup", "final"):
        assert int(runtime.get(f"{prefix}_main_depth") or 0) == 0, runtime
        assert int(runtime.get(f"{prefix}_overflow_depth") or 0) == 0, runtime
        assert int(runtime.get(f"{prefix}_represented_depth") or 0) == 0, runtime

    source_pointer = int(runtime.get("source_pointer") or 0)
    producer_seed = int(runtime.get("producer_seed_pointer") or 0)
    consumer_seed = int(runtime.get("consumer_seed_pointer") or 0)
    cloned_pointer = int(runtime.get("cloned_pointer") or 0)
    consumer_recovery = int(runtime.get("consumer_recovery_pointer") or 0)
    assert all(
        pointer != 0
        for pointer in (
            source_pointer,
            producer_seed,
            consumer_seed,
            cloned_pointer,
            consumer_recovery,
        )
    )
    assert len({source_pointer, producer_seed, consumer_seed}) == 3
    assert runtime.get("producer_exact_reuse") is False
    assert cloned_pointer != producer_seed
    assert runtime.get("consumer_exact_reuse") is True
    assert consumer_recovery == consumer_seed
    assert runtime.get("clone_avoided_consumer_storage") is True
    assert cloned_pointer != consumer_seed

    for field, expected in (
        ("total_allocations", 2),
        ("typed_allocations", 1),
        ("typed_allocated_bytes", 256),
        ("total_deallocations", 0),
        ("typed_deallocations", 0),
        ("typed_cache_hits", 1),
        ("typed_cache_inserts", 0),
        ("typed_cache_bypasses", 0),
        ("fallback_allocations", 1),
        ("fallback_deallocations", 0),
        ("raw_alloc_no_metadata", 1),
        ("raw_alloc_no_metadata_bytes", 256),
        ("raw_dealloc_no_metadata", 0),
        ("raw_realloc_no_metadata", 0),
        ("raw_realloc_no_metadata_bytes", 0),
        ("raw_realloc_moved_dealloc_no_metadata", 0),
        ("realloc_recorded_old_metadata_new_allocations", 0),
        ("realloc_recorded_old_metadata_new_allocation_bytes", 0),
        # Typed-cache hits do not traverse the side-table recovery path.
        ("recovery_identity_matches", 0),
        ("recovery_identity_mismatches", 0),
        ("side_cache_corrupt_slots", 0),
        ("semantic_type_stats_dropped_events", 0),
    ):
        assert int(runtime.get(field) or 0) == expected, (field, runtime)

    module_id = int(audit_evidence["module_id"])
    producer_type_id = int(audit_evidence["producer_type_id"])
    allocation_rows = runtime.get("type_rows")
    assert isinstance(allocation_rows, list), runtime
    assert not [
        row
        for row in allocation_rows
        if isinstance(row, dict)
        and int(row.get("type_id") or 0) == producer_type_id
        and int(row.get("module_id") or 0) == module_id
    ], allocation_rows
    consumer = aggregate_type_rows(
        runtime,
        type_id=int(audit_evidence["consumer_type_id"]),
        module_id=module_id,
        rows_field="type_rows",
    )
    expected_row = {
        "allocations": 1,
        "allocated_bytes": 256,
        "deallocations": 0,
        "cache_hits": 1,
        "cache_inserts": 0,
        "cache_bypasses": 0,
        "observed_alloc_size": 256,
        "observed_alloc_align": 8,
        "observed_dealloc_size": 0,
        "observed_dealloc_align": 0,
    }
    assert consumer == expected_row, consumer

    for field, expected in (
        ("cleanup_total_allocations", 0),
        ("cleanup_typed_allocations", 0),
        ("cleanup_typed_allocated_bytes", 0),
        ("cleanup_total_deallocations", 3),
        ("cleanup_typed_deallocations", 2),
        ("cleanup_typed_cache_hits", 0),
        ("cleanup_typed_cache_inserts", 2),
        ("cleanup_typed_cache_bypasses", 0),
        ("cleanup_fallback_allocations", 0),
        ("cleanup_fallback_deallocations", 1),
        ("cleanup_raw_alloc_no_metadata", 0),
        ("cleanup_raw_alloc_no_metadata_bytes", 0),
        ("cleanup_raw_dealloc_no_metadata", 1),
        ("cleanup_raw_realloc_no_metadata", 0),
        ("cleanup_raw_realloc_no_metadata_bytes", 0),
        ("cleanup_raw_realloc_moved_dealloc_no_metadata", 0),
        ("cleanup_realloc_recorded_old_metadata_new_allocations", 0),
        ("cleanup_realloc_recorded_old_metadata_new_allocation_bytes", 0),
        ("cleanup_recovery_identity_matches", 0),
        ("cleanup_recovery_identity_mismatches", 0),
        ("cleanup_side_cache_corrupt_slots", 0),
        ("cleanup_semantic_type_stats_dropped_events", 0),
    ):
        assert int(runtime.get(field) or 0) == expected, (field, runtime)

    cleanup_producer = aggregate_type_rows(
        runtime,
        type_id=producer_type_id,
        module_id=module_id,
        rows_field="cleanup_type_rows",
    )
    cleanup_consumer = aggregate_type_rows(
        runtime,
        type_id=int(audit_evidence["consumer_type_id"]),
        module_id=module_id,
        rows_field="cleanup_type_rows",
    )
    assert cleanup_producer == {
        "allocations": 0,
        "allocated_bytes": 0,
        "deallocations": 1,
        "cache_hits": 0,
        "cache_inserts": 1,
        "cache_bypasses": 0,
        "observed_alloc_size": 0,
        "observed_alloc_align": 0,
        "observed_dealloc_size": 256,
        "observed_dealloc_align": 8,
    }, cleanup_producer
    assert cleanup_consumer == {
        "allocations": 0,
        "allocated_bytes": 0,
        "deallocations": 1,
        "cache_hits": 0,
        "cache_inserts": 1,
        "cache_bypasses": 0,
        "observed_alloc_size": 0,
        "observed_alloc_align": 0,
        "observed_dealloc_size": 256,
        "observed_dealloc_align": 8,
    }, cleanup_consumer
    return {
        "result_variant": "Err",
        "source_and_clone_lengths": [4, 4],
        "producer_allocation_rows": 0,
        "consumer_runtime": consumer,
        "cleanup_producer_runtime": cleanup_producer,
        "cleanup_consumer_runtime": cleanup_consumer,
        "normal_drop_cleanup": {
            "typed_deallocations": 2,
            "typed_cache_inserts": 2,
            "fallback_deallocations": 1,
        },
        "result_clone_used_fallback": True,
        "producer_typed_entry_preserved": True,
        "consumer_same_layout_non_reuse": True,
        "fallback_raw_mismatch_corrupt_dropped": 0,
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
    broad_clone_rewritten = copy.deepcopy(audit)
    mutated_rows = result_clone_rows(broad_clone_rewritten)
    assert len(mutated_rows) == 1, mutated_rows
    mutated_rows[0]["rewrite_status"] = "semantic_scope_enter_exit_rewrite_planned"
    mutated_rows[0]["lowering_kind"] = "semantic_scope_enter_exit_rewrite"

    wrong_type_reuse = copy.deepcopy(runtime)
    wrong_type_reuse["cloned_pointer"] = wrong_type_reuse["consumer_seed_pointer"]
    wrong_type_reuse["producer_exact_reuse"] = False
    wrong_type_reuse["clone_avoided_consumer_storage"] = False

    missing_fallback = copy.deepcopy(runtime)
    missing_fallback["fallback_allocations"] = 0

    missing_cleanup = copy.deepcopy(runtime)
    missing_cleanup["cleanup_typed_deallocations"] = 1

    return {
        "broad_clone_rewrite_rejected": expect_rejected(
            broad_clone_rewritten, runtime, "applied Result Clone scope"
        ),
        "same_layout_wrong_type_reuse_rejected": expect_rejected(
            audit, wrong_type_reuse, "same-layout Consumer reuse"
        ),
        "missing_fallback_event_rejected": expect_rejected(
            audit, missing_fallback, "missing fallback allocation"
        ),
        "missing_normal_drop_cleanup_rejected": expect_rejected(
            audit, missing_cleanup, "missing typed deallocation"
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
            prefix="unialloc-result-err-clone-"
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
                "CARGO_TARGET_DIR": str(workspace / "target"),
            }
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
                "source": "mir_result_err_clone_type_isolation_probe",
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
                    "Functional actual-RUSTC_WRAPPER regression for one ordinary Err(Result<u8, Vec<ProducerPayload>>) audit-only Clone path; no universal Result/Clone coverage claim.",
                    "The generated Rust application uses ordinary Result::clone and Vec APIs and no manual metadata allocation ABI.",
                    "The broad Result Clone remains an exact unresolved audit-only row with zero applied scope rewrites.",
                    "Runtime counters prove one raw fallback allocation/deallocation while protected Producer and same-layout Consumer typed-cache entries remain unavailable to the fallback path.",
                    "Identity mismatches, corrupt slots, and dropped type-stat counters remain zero; no benchmark or performance claim is made.",
                ],
            },
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
