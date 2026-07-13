#!/usr/bin/env python3
"""Prove generic Vec<T> scopes fail closed while concrete same-layout Vec scopes isolate."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile


ROOT = Path(__file__).resolve().parents[2]
PASS_SOURCE = ROOT / "tools/unialloc-rustc-pass/unialloc-rustc-mir-rewrite-dry-run.rs"
PROBE_NAME = "generic_vec_type_isolation_probe"
GENERIC_FUNCTION = "generic_roundtrip"
PRODUCER_FUNCTION = "make_producer"
CONSUMER_FUNCTION = "make_consumer"
APPLIED_SCOPE_STATUS = "actual_semantic_scope_enter_exit_rewrite_applied"
TYPE_ISOLATED = 0x1
OBJECTS = 4
CAPACITY = 8
PAYLOAD_BYTES = 32
ALLOCATION_BYTES = CAPACITY * PAYLOAD_BYTES


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


def write_probe(workspace: Path, toolchain: str) -> Path:
    app = workspace / PROBE_NAME
    (app / "src").mkdir(parents=True)
    features = ["stats", "type_isolation"]
    feature_text = ", ".join(json.dumps(feature) for feature in features)
    app_features = '[features]\nfixed_heap = ["unialloc/fixed_heap"]\n\n'
    legacy_lock_pin = ""
    if toolchain.lstrip("+").strip() == "nightly-2022-07-01":
        # UniAlloc already depends on lock_api through spin. Pin that existing
        # transitive dependency so the transient fixture remains Rust-1.64 compatible.
        legacy_lock_pin = 'lock_api = "=0.4.3"\n'
    (app / "Cargo.toml").write_text(
        f'''[package]
name = "{PROBE_NAME.replace('_', '-')}"
version = "0.1.0"
edition = "2021"

{app_features}[dependencies]
{legacy_lock_pin}unialloc = {{ path = {json.dumps(str(ROOT / "unialloc"))}, features = [{feature_text}] }}
''',
        encoding="utf-8",
    )
    fixed_support = ROOT / "unialloc/src/bin_support/fixed_heap_probe_global.rs"
    (app / "src/main.rs").write_text(
        f'''#![cfg_attr(feature = "fixed_heap", allow(dead_code, unused_imports))]

use std::fmt::Write as _;
use std::mem::{{align_of, size_of}};
use unialloc::{{
    semantic_auto_metadata_disable, semantic_fallback_attribution_snapshot,
    semantic_metadata_validation_snapshot, semantic_stats_recording_disable,
    semantic_stats_reset, semantic_stats_snapshot, semantic_type_stats_recording_disable,
    semantic_type_stats_snapshot, type_isolation_side_cache_snapshot,
    SemanticTypeStatsSnapshot, UniAlloc,
}};

#[cfg(feature = "fixed_heap")]
#[path = {json.dumps(str(fixed_support))}]
mod fixed_heap_probe_global;

#[cfg(feature = "fixed_heap")]
#[global_allocator]
static ALLOCATOR: fixed_heap_probe_global::FixedHeapProbeAllocator =
    fixed_heap_probe_global::FixedHeapProbeAllocator;

#[cfg(not(feature = "fixed_heap"))]
#[global_allocator]
static ALLOCATOR: UniAlloc = UniAlloc;

const OBJECTS: usize = {OBJECTS};
const CAPACITY: usize = {CAPACITY};

#[repr(C)]
struct Producer([u64; 4]);

#[repr(C)]
struct Consumer([u64; 4]);

#[inline(never)]
fn producer_payload(seed: u64, index: usize) -> Producer {{
    Producer([
        seed.wrapping_add(index as u64),
        seed ^ 0x1111_1111_1111_1111 ^ index as u64,
        seed ^ 0x2222_2222_2222_2222 ^ index as u64,
        seed ^ 0x3333_3333_3333_3333 ^ index as u64,
    ])
}}

#[inline(never)]
fn consumer_payload(seed: u64, index: usize) -> Consumer {{
    Consumer([
        seed.wrapping_add(index as u64),
        seed ^ 0xAAAA_AAAA_AAAA_AAAA ^ index as u64,
        seed ^ 0xBBBB_BBBB_BBBB_BBBB ^ index as u64,
        seed ^ 0xCCCC_CCCC_CCCC_CCCC ^ index as u64,
    ])
}}

#[inline(never)]
fn generic_roundtrip<T>(payload: T) -> usize {{
    let mut value = Vec::<T>::with_capacity(CAPACITY);
    value.push(payload);
    let address = value.as_ptr() as usize;
    drop(value);
    address
}}

#[inline(never)]
fn make_producer(seed: u64) -> Vec<Producer> {{
    let mut value = Vec::<Producer>::with_capacity(CAPACITY);
    for index in 0..CAPACITY {{
        value.push(producer_payload(seed, index));
    }}
    value
}}

#[inline(never)]
fn make_consumer(seed: u64) -> Vec<Consumer> {{
    let mut value = Vec::<Consumer>::with_capacity(CAPACITY);
    for index in 0..CAPACITY {{
        value.push(consumer_payload(seed, index));
    }}
    value
}}

#[inline(never)]
fn producer_matches(value: &Vec<Producer>, seed: u64) -> bool {{
    value.len() == CAPACITY
        && value.iter().enumerate().all(|(index, item)| {{
            let expected = producer_payload(seed, index);
            item.0 == expected.0
        }})
}}

#[inline(never)]
fn consumer_matches(value: &Vec<Consumer>, seed: u64) -> bool {{
    value.len() == CAPACITY
        && value.iter().enumerate().all(|(index, item)| {{
            let expected = consumer_payload(seed, index);
            item.0 == expected.0
        }})
}}

#[inline(never)]
fn producer_address(value: &Vec<Producer>) -> usize {{
    value.first().expect("non-empty Producer Vec") as *const Producer as usize
}}

#[inline(never)]
fn consumer_address(value: &Vec<Consumer>) -> usize {{
    value.first().expect("non-empty Consumer Vec") as *const Consumer as usize
}}

fn addresses_unique(values: &[usize; OBJECTS]) -> bool {{
    for index in 0..values.len() {{
        if values[..index].contains(&values[index]) {{
            return false;
        }}
    }}
    true
}}

fn addresses_json(values: &[usize; OBJECTS]) -> String {{
    let mut out = String::from("[");
    for (index, value) in values.iter().enumerate() {{
        if index != 0 {{
            out.push(',');
        }}
        let _ = write!(out, "{{}}", value);
    }}
    out.push(']');
    out
}}

fn type_rows_json(rows: &[SemanticTypeStatsSnapshot], row_count: usize) -> String {{
    let mut out = String::from("[");
    let mut emitted = 0usize;
    for row in rows.iter().take(std::cmp::min(row_count, rows.len())) {{
        if row.allocations == 0 && row.deallocations == 0 {{
            continue;
        }}
        if emitted != 0 {{
            out.push(',');
        }}
        emitted += 1;
        let _ = write!(
            out,
            concat!(
                "{{{{",
                "\\\"type_id\\\":{{}},",
                "\\\"module_id\\\":{{}},",
                "\\\"callsite\\\":{{}},",
                "\\\"allocations\\\":{{}},",
                "\\\"allocated_bytes\\\":{{}},",
                "\\\"deallocations\\\":{{}},",
                "\\\"cache_hits\\\":{{}},",
                "\\\"cache_inserts\\\":{{}},",
                "\\\"cache_bypasses\\\":{{}},",
                "\\\"observed_alloc_size\\\":{{}},",
                "\\\"observed_alloc_align\\\":{{}},",
                "\\\"observed_dealloc_size\\\":{{}},",
                "\\\"observed_dealloc_align\\\":{{}},",
                "\\\"policy_flags_seen\\\":{{}}",
                "}}}}"
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
    }}
    out.push(']');
    out
}}

fn main() {{
    #[cfg(feature = "fixed_heap")]
    fixed_heap_probe_global::ensure_initialized_for_probe();

    assert_eq!(size_of::<Producer>(), size_of::<Consumer>());
    assert_eq!(align_of::<Producer>(), align_of::<Consumer>());
    assert_eq!(size_of::<Producer>(), {PAYLOAD_BYTES});

    // Ordinary Rust application: there are no manual metadata ABI calls.
    // Generic MIR must stay audit-only until a concrete type identity is
    // available. The concrete controls below prove actual MIR scopes still
    // isolate two same-layout payload types.
    semantic_auto_metadata_disable();
    semantic_stats_reset();

    let generic_addresses = [
        generic_roundtrip(producer_payload(0xA110_8000, 0)),
        generic_roundtrip(producer_payload(0xA110_8001, 1)),
        generic_roundtrip(producer_payload(0xA110_8002, 2)),
        generic_roundtrip(producer_payload(0xA110_8003, 3)),
        generic_roundtrip(consumer_payload(0xBE70_8000, 0)),
        generic_roundtrip(consumer_payload(0xBE70_8001, 1)),
        generic_roundtrip(consumer_payload(0xBE70_8002, 2)),
        generic_roundtrip(consumer_payload(0xBE70_8003, 3)),
    ];
    assert!(generic_addresses.iter().all(|address| *address != 0));
    let generic_stats = semantic_stats_snapshot();
    let generic_fallback = semantic_fallback_attribution_snapshot();
    let generic_validation = semantic_metadata_validation_snapshot();
    let generic_side_cache = type_isolation_side_cache_snapshot();

    // Keep the exact-control counters independent from the generic fallback
    // interval. Cache contents intentionally remain live: only accounting is
    // reset, matching an ordinary single-process application.
    semantic_stats_reset();

    let a0 = make_producer(0xA110_0000);
    let a1 = make_producer(0xA110_1000);
    let a2 = make_producer(0xA110_2000);
    let a3 = make_producer(0xA110_3000);
    assert!(producer_matches(&a0, 0xA110_0000));
    assert!(producer_matches(&a1, 0xA110_1000));
    assert!(producer_matches(&a2, 0xA110_2000));
    assert!(producer_matches(&a3, 0xA110_3000));
    let producer_addresses = [
        producer_address(&a0), producer_address(&a1), producer_address(&a2), producer_address(&a3),
    ];
    assert!(addresses_unique(&producer_addresses));
    drop(a0);
    drop(a1);
    drop(a2);
    drop(a3);

    let b0 = make_consumer(0xBE70_0000);
    let b1 = make_consumer(0xBE70_1000);
    let b2 = make_consumer(0xBE70_2000);
    let b3 = make_consumer(0xBE70_3000);
    assert!(consumer_matches(&b0, 0xBE70_0000));
    assert!(consumer_matches(&b1, 0xBE70_1000));
    assert!(consumer_matches(&b2, 0xBE70_2000));
    assert!(consumer_matches(&b3, 0xBE70_3000));
    let consumer_addresses = [
        consumer_address(&b0), consumer_address(&b1), consumer_address(&b2), consumer_address(&b3),
    ];
    let wrong_type_reuse_blocked = producer_addresses
        .iter()
        .all(|address| !consumer_addresses.contains(address));
    assert!(wrong_type_reuse_blocked, "same-layout Consumer Vec reused Producer storage");
    drop(b0);
    drop(b1);
    drop(b2);
    drop(b3);

    let r0 = make_producer(0xA110_4000);
    let r1 = make_producer(0xA110_5000);
    let r2 = make_producer(0xA110_6000);
    let r3 = make_producer(0xA110_7000);
    assert!(producer_matches(&r0, 0xA110_4000));
    assert!(producer_matches(&r1, 0xA110_5000));
    assert!(producer_matches(&r2, 0xA110_6000));
    assert!(producer_matches(&r3, 0xA110_7000));
    let recovered_producer_addresses = [
        producer_address(&r0), producer_address(&r1), producer_address(&r2), producer_address(&r3),
    ];
    let same_type_reuse_complete = recovered_producer_addresses
        .iter()
        .all(|address| producer_addresses.contains(address));
    assert!(same_type_reuse_complete, "Producer Vec did not recover its own typed storage");
    drop(r0);
    drop(r1);
    drop(r2);
    drop(r3);

    let stats = semantic_stats_snapshot();
    let fallback = semantic_fallback_attribution_snapshot();
    let validation = semantic_metadata_validation_snapshot();
    let side_cache = type_isolation_side_cache_snapshot();
    let mut type_rows = [SemanticTypeStatsSnapshot::empty(); 64];
    let type_row_count = semantic_type_stats_snapshot(&mut type_rows);
    semantic_type_stats_recording_disable();
    semantic_stats_recording_disable();

    let address_json = addresses_json(&producer_addresses);
    let consumer_json = addresses_json(&consumer_addresses);
    let recovered_json = addresses_json(&recovered_producer_addresses);
    let type_json = type_rows_json(&type_rows, type_row_count);
    println!(
        concat!(
            "{{{{",
            "\\\"source\\\":\\\"generic_vec_type_isolation_probe\\\",",
            "\\\"objects\\\":{{}},",
            "\\\"capacity\\\":{{}},",
            "\\\"payload_bytes\\\":{{}},",
            "\\\"allocation_bytes\\\":{{}},",
            "\\\"generic_typed_allocations\\\":{{}},",
            "\\\"generic_typed_deallocations\\\":{{}},",
            "\\\"generic_typed_cache_hits\\\":{{}},",
            "\\\"generic_typed_cache_inserts\\\":{{}},",
            "\\\"generic_fallback_allocations\\\":{{}},",
            "\\\"generic_fallback_deallocations\\\":{{}},",
            "\\\"generic_raw_alloc_no_metadata\\\":{{}},",
            "\\\"generic_raw_alloc_no_metadata_bytes\\\":{{}},",
            "\\\"generic_raw_dealloc_no_metadata\\\":{{}},",
            "\\\"generic_raw_realloc_no_metadata\\\":{{}},",
            "\\\"generic_recovery_identity_mismatches\\\":{{}},",
            "\\\"generic_side_cache_corrupt_slots\\\":{{}},",
            "\\\"generic_semantic_type_stats_dropped_events\\\":{{}},",
            "\\\"producer_addresses\\\":{{}},",
            "\\\"consumer_addresses\\\":{{}},",
            "\\\"recovered_producer_addresses\\\":{{}},",
            "\\\"wrong_type_reuse_blocked\\\":{{}},",
            "\\\"same_type_reuse_complete\\\":{{}},",
            "\\\"typed_allocations\\\":{{}},",
            "\\\"typed_deallocations\\\":{{}},",
            "\\\"typed_cache_hits\\\":{{}},",
            "\\\"typed_cache_inserts\\\":{{}},",
            "\\\"fallback_allocations\\\":{{}},",
            "\\\"fallback_deallocations\\\":{{}},",
            "\\\"raw_alloc_no_metadata\\\":{{}},",
            "\\\"raw_dealloc_no_metadata\\\":{{}},",
            "\\\"raw_realloc_no_metadata\\\":{{}},",
            "\\\"recovery_identity_mismatches\\\":{{}},",
            "\\\"side_cache_corrupt_slots\\\":{{}},",
            "\\\"semantic_type_stats_dropped_events\\\":{{}},",
            "\\\"type_rows\\\":{{}}",
            "}}}}"
        ),
        OBJECTS,
        CAPACITY,
        size_of::<Producer>(),
        CAPACITY * size_of::<Producer>(),
        generic_stats.typed_allocations,
        generic_stats.typed_deallocations,
        generic_stats.typed_cache_hits,
        generic_stats.typed_cache_inserts,
        generic_stats.fallback_allocations,
        generic_stats.fallback_deallocations,
        generic_fallback.raw_alloc_no_metadata,
        generic_fallback.raw_alloc_no_metadata_bytes,
        generic_fallback.raw_dealloc_no_metadata,
        generic_fallback.raw_realloc_no_metadata,
        generic_validation.recovery_identity_mismatches,
        generic_side_cache.corrupt_slots,
        generic_stats.semantic_type_stats_dropped_events,
        address_json,
        consumer_json,
        recovered_json,
        wrong_type_reuse_blocked,
        same_type_reuse_complete,
        stats.typed_allocations,
        stats.typed_deallocations,
        stats.typed_cache_hits,
        stats.typed_cache_inserts,
        stats.fallback_allocations,
        stats.fallback_deallocations,
        fallback.raw_alloc_no_metadata,
        fallback.raw_dealloc_no_metadata,
        fallback.raw_realloc_no_metadata,
        validation.recovery_identity_mismatches,
        side_cache.corrupt_slots,
        stats.semantic_type_stats_dropped_events,
        type_json,
    );
}}
''',
        encoding="utf-8",
    )
    return app


def function_matches(row: dict[str, object], function_name: str) -> bool:
    function = str(row.get("mir_function") or "")
    return function == function_name or function.endswith(f"::{function_name}")


def applied_capacity_rows(
    audit: dict[str, object], function_name: str, payload_marker: str
) -> list[dict[str, object]]:
    rows = audit.get("rewrite_candidates")
    assert isinstance(rows, list), rows
    return [
        row
        for row in rows
        if isinstance(row, dict)
        and function_matches(row, function_name)
        and row.get("lowering_kind") == "semantic_scope_enter_exit_rewrite"
        and row.get("rewrite_status") == APPLIED_SCOPE_STATUS
        and "Vec<" in str(row.get("semantic_object_type") or "")
        and payload_marker in str(row.get("semantic_object_type") or "")
        and "with_capacity" in str(row.get("callee") or "")
    ]


def load_runtime(stdout: str) -> dict[str, object]:
    for line in stdout.splitlines():
        if not line.startswith("{"):
            continue
        value = json.loads(line)
        if isinstance(value, dict) and value.get("source") == PROBE_NAME:
            return value
    raise AssertionError(f"missing {PROBE_NAME} JSON event in stdout:\n{stdout}")


def aggregate_runtime_type(
    rows: list[object], type_id: int
) -> dict[str, object]:
    selected = [
        row
        for row in rows
        if isinstance(row, dict) and int(row.get("type_id") or 0) == type_id
    ]
    assert selected, f"runtime type rows missing compiler type_id {type_id}"
    return {
        "allocations": sum(int(row.get("allocations") or 0) for row in selected),
        "allocated_bytes": sum(int(row.get("allocated_bytes") or 0) for row in selected),
        "deallocations": sum(int(row.get("deallocations") or 0) for row in selected),
        "cache_hits": sum(int(row.get("cache_hits") or 0) for row in selected),
        "cache_inserts": sum(int(row.get("cache_inserts") or 0) for row in selected),
        "cache_bypasses": sum(int(row.get("cache_bypasses") or 0) for row in selected),
        "observed_alloc_sizes": sorted(
            {
                int(row.get("observed_alloc_size") or 0)
                for row in selected
                if int(row.get("observed_alloc_size") or 0) != 0
            }
        ),
        "observed_dealloc_sizes": sorted(
            {
                int(row.get("observed_dealloc_size") or 0)
                for row in selected
                if int(row.get("observed_dealloc_size") or 0) != 0
            }
        ),
        "policy_flags_seen": _or_flags(selected),
    }


def _or_flags(rows: list[dict[str, object]]) -> int:
    flags = 0
    for row in rows:
        flags |= int(row.get("policy_flags_seen") or 0)
    return flags


def validate_generic_fail_closed(audit: dict[str, object]) -> dict[str, object]:
    rows = audit.get("rewrite_candidates")
    assert isinstance(rows, list), rows
    generic_rows = [
        row
        for row in rows
        if isinstance(row, dict) and function_matches(row, GENERIC_FUNCTION)
    ]
    assert generic_rows, "actual optimized_mir audit omitted generic_roundtrip"

    lowered = [
        row
        for row in generic_rows
        if "applied" in str(row.get("rewrite_status") or "")
        or "planned" in str(row.get("rewrite_status") or "")
    ]
    assert not lowered, (
        "generic MIR must not receive a callsite-shared typed scope before "
        "monomorphized type evidence exists",
        lowered,
    )

    capacity_rows = [
        row
        for row in generic_rows
        if "with_capacity" in str(row.get("callee") or "")
        and "Vec<T" in str(row.get("destination_type") or "")
    ]
    assert len(capacity_rows) == 1, capacity_rows
    capacity = capacity_rows[0]
    assert (
        capacity.get("lowering_kind")
        == "semantic_scope_unsolved_heap_object_candidate"
    ), capacity
    assert (
        capacity.get("rewrite_status")
        == "semantic_scope_rewrite_skipped_unresolved_heap_object_type"
    ), capacity
    assert (
        capacity.get("replacement_resolution_status")
        == "rustc_middle_heap_object_type_not_solved"
    ), capacity
    assert (
        capacity.get("metadata_pairing_contract")
        == "audit_only_unresolved_heap_object_type"
    ), capacity
    assert capacity.get("semantic_object_type") == "<unknown-heap-object-type>", capacity
    assert (
        capacity.get("type_id_basis")
        == "rustc_middle_type_solver_failed_callsite_fallback"
    ), capacity
    assert int(capacity.get("callsite") or 0) != 0, capacity

    generic_drop_skips = [
        row
        for row in generic_rows
        if row.get("lowering_kind")
        == "semantic_scope_drop_generic_type_parameter_skipped"
    ]
    for row in generic_drop_skips:
        assert (
            row.get("rewrite_status")
            == "semantic_scope_drop_rewrite_skipped_generic_type_parameter"
        ), row
        assert row.get("metadata_pairing_contract") == "audit_only_generic_drop_type", row

    return {
        "generic_scope_mode": "audit_only_unresolved",
        "generic_row_count": len(generic_rows),
        "generic_drop_skip_count": len(generic_drop_skips),
        "with_capacity_row": capacity,
    }


def validate(audit: dict[str, object], stdout: str) -> dict[str, object]:
    summary = audit.get("summary")
    assert isinstance(summary, dict), summary
    assert summary.get("provider_override_installed") is True, summary
    assert summary.get("body_clone_returned_to_rustc") is True, summary
    assert summary.get("actual_semantic_scope_rewrite_requested") is True, summary
    assert summary.get("actual_semantic_scope_rewrite") is True, summary
    assert int(summary.get("semantic_scope_rewrite_applied_count") or 0) >= 2, summary
    generic_audit = validate_generic_fail_closed(audit)

    producer_rows = applied_capacity_rows(audit, PRODUCER_FUNCTION, "Producer")
    consumer_rows = applied_capacity_rows(audit, CONSUMER_FUNCTION, "Consumer")
    assert len(producer_rows) == 1, producer_rows
    assert len(consumer_rows) == 1, consumer_rows
    producer_row = producer_rows[0]
    consumer_row = consumer_rows[0]
    producer_type_id = int(producer_row.get("type_id") or 0)
    consumer_type_id = int(consumer_row.get("type_id") or 0)
    assert producer_type_id != 0, producer_row
    assert consumer_type_id != 0, consumer_row
    assert producer_type_id != consumer_type_id, (producer_row, consumer_row)
    assert int(producer_row.get("module_id") or 0) != 0, producer_row
    assert int(producer_row.get("module_id") or 0) == int(consumer_row.get("module_id") or 0), (
        producer_row,
        consumer_row,
    )
    for row in (producer_row, consumer_row):
        assert int(row.get("callsite") or 0) != 0, row
        assert int(row.get("flags") or 0) & TYPE_ISOLATED, row
        assert isinstance(row.get("semantic_scope_unwind_pop_inserted"), bool), row

    runtime = load_runtime(stdout)
    assert int(runtime["objects"]) == OBJECTS, runtime
    assert int(runtime["capacity"]) == CAPACITY, runtime
    assert int(runtime["payload_bytes"]) == PAYLOAD_BYTES, runtime
    assert int(runtime["allocation_bytes"]) == ALLOCATION_BYTES, runtime
    for field, expected in (
        ("generic_typed_allocations", 0),
        ("generic_typed_deallocations", 0),
        ("generic_typed_cache_hits", 0),
        ("generic_typed_cache_inserts", 0),
        ("generic_fallback_allocations", OBJECTS * 2),
        ("generic_fallback_deallocations", OBJECTS * 2),
        ("generic_raw_alloc_no_metadata", OBJECTS * 2),
        ("generic_raw_alloc_no_metadata_bytes", ALLOCATION_BYTES * OBJECTS * 2),
        ("generic_raw_dealloc_no_metadata", OBJECTS * 2),
        ("generic_raw_realloc_no_metadata", 0),
        ("generic_recovery_identity_mismatches", 0),
        ("generic_side_cache_corrupt_slots", 0),
        ("generic_semantic_type_stats_dropped_events", 0),
    ):
        assert int(runtime[field]) == expected, (field, runtime)
    assert runtime["wrong_type_reuse_blocked"] is True, runtime
    assert runtime["same_type_reuse_complete"] is True, runtime
    producer_addresses = [int(value) for value in runtime["producer_addresses"]]
    consumer_addresses = [int(value) for value in runtime["consumer_addresses"]]
    recovered_addresses = [
        int(value) for value in runtime["recovered_producer_addresses"]
    ]
    assert len(producer_addresses) == len(consumer_addresses) == len(recovered_addresses) == OBJECTS
    assert len(set(producer_addresses)) == OBJECTS, runtime
    assert len(set(consumer_addresses)) == OBJECTS, runtime
    assert set(producer_addresses).isdisjoint(consumer_addresses), runtime
    assert set(recovered_addresses) == set(producer_addresses), runtime

    for field, expected in (
        ("typed_allocations", OBJECTS * 3),
        ("typed_deallocations", OBJECTS * 3),
        ("typed_cache_hits", OBJECTS),
        ("typed_cache_inserts", OBJECTS * 3),
        ("fallback_allocations", 0),
        ("fallback_deallocations", 0),
        ("raw_alloc_no_metadata", 0),
        ("raw_dealloc_no_metadata", 0),
        ("raw_realloc_no_metadata", 0),
        ("recovery_identity_mismatches", 0),
        ("side_cache_corrupt_slots", 0),
        ("semantic_type_stats_dropped_events", 0),
    ):
        assert int(runtime[field]) == expected, (field, runtime)

    type_rows = runtime.get("type_rows")
    assert isinstance(type_rows, list), runtime
    producer_totals = aggregate_runtime_type(type_rows, producer_type_id)
    consumer_totals = aggregate_runtime_type(type_rows, consumer_type_id)
    expected_producer = {
        "allocations": OBJECTS * 2,
        "allocated_bytes": ALLOCATION_BYTES * OBJECTS * 2,
        "deallocations": OBJECTS * 2,
        "cache_hits": OBJECTS,
        "cache_inserts": OBJECTS * 2,
        "cache_bypasses": OBJECTS,
        "observed_alloc_sizes": [ALLOCATION_BYTES],
        "observed_dealloc_sizes": [ALLOCATION_BYTES],
    }
    expected_consumer = {
        "allocations": OBJECTS,
        "allocated_bytes": ALLOCATION_BYTES * OBJECTS,
        "deallocations": OBJECTS,
        "cache_hits": 0,
        "cache_inserts": OBJECTS,
        "cache_bypasses": OBJECTS,
        "observed_alloc_sizes": [ALLOCATION_BYTES],
        "observed_dealloc_sizes": [ALLOCATION_BYTES],
    }
    for field, expected in expected_producer.items():
        assert producer_totals[field] == expected, (field, producer_totals)
    for field, expected in expected_consumer.items():
        assert consumer_totals[field] == expected, (field, consumer_totals)
    assert int(producer_totals["policy_flags_seen"]) & TYPE_ISOLATED, producer_totals
    assert int(consumer_totals["policy_flags_seen"]) & TYPE_ISOLATED, consumer_totals

    return {
        "generic_fail_closed": generic_audit,
        "compiler_type_ids": {"producer_vec": producer_type_id, "consumer_vec": consumer_type_id},
        "module_id": int(producer_row["module_id"]),
        "actual_scope_rows": {
            "producer_with_capacity": producer_row,
            "consumer_with_capacity": consumer_row,
        },
        "runtime_type_totals": {"producer_vec": producer_totals, "consumer_vec": consumer_totals},
        "runtime": runtime,
    }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--fixed-heap", action="store_true")
    parser.add_argument("--toolchain")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    toolchain = args.toolchain or (ROOT / "rust-toolchain").read_text(
        encoding="utf-8"
    ).strip()
    rustc = shutil.which("rustc") or "rustc"
    cargo = shutil.which("cargo") or "cargo"
    sysroot = subprocess.check_output(
        [rustc, f"+{toolchain}", "--print", "sysroot"], cwd=ROOT, text=True
    ).strip()

    with tempfile.TemporaryDirectory(prefix="unialloc-generic-vec-type-isolation-") as raw:
        workspace = Path(raw)
        app = write_probe(workspace, toolchain)
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
                "UNIALLOC_LOWERING_POLICY_FLAGS": str(TYPE_ISOLATED),
                "UNIALLOC_RUSTC_SYSROOT": sysroot,
                "CARGO_NET_OFFLINE": "true",
                "CARGO_INCREMENTAL": "0",
                "CARGO_TARGET_DIR": str(workspace / "target"),
            }
        )
        command = [
            cargo,
            f"+{toolchain}",
            "run",
            "--quiet",
            "--manifest-path",
            str(app / "Cargo.toml"),
        ]
        if args.fixed_heap:
            command.extend(["--features", "fixed_heap"])
        stdout = run(command, cwd=workspace, env=run_env)
        audit_paths = sorted(rewrites.glob("*.json"))
        assert len(audit_paths) == 1, audit_paths
        audit = json.loads(audit_paths[0].read_text(encoding="utf-8"))
        evidence = validate(audit, stdout)

    print(
        json.dumps(
            {
                "source": "mir_generic_vec_type_isolation_probe",
                "validated": True,
                "fixed_heap": args.fixed_heap,
                "toolchain": toolchain,
                "single_run": True,
                "benchmark": False,
                "boundaries": [
                    "Functional compiler-pass and allocator type-isolation regression only; no benchmark or performance claim.",
                    "Generic Vec<T> allocation and Drop remain audit-only/raw fallback; this is a safety proof, not positive generic type-isolation coverage.",
                    "Concrete same-layout Vec<Producer>/Vec<Consumer> controls prove distinct compiler type identities and exact typed-cache isolation on one host invocation, not universal Vec coverage.",
                ],
                "evidence": evidence,
            },
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
