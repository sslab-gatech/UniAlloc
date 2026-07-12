#!/usr/bin/env python3
"""Exercise actual MIR rewriting in a realistic multi-module Rust pipeline."""

from __future__ import annotations

import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import tempfile


ROOT = Path(__file__).resolve().parents[2]
PASS_SOURCE = ROOT / "tools/unialloc-rustc-pass/unialloc-rustc-mir-rewrite-dry-run.rs"
PROBE_NAME = "realistic_multimodule_typeiso_app"
STRING_TYPE = "std::string::String"
VEC_TYPE = "std::vec::Vec<u8, std::alloc::Global>"
BOX_TYPE = "std::boxed::Box<[u8], std::alloc::Global>"
APPLIED_SCOPE_STATUS = "actual_semantic_scope_enter_exit_rewrite_applied"
TRANSFER_STATUS = "actual_semantic_ownership_transfer_rewrite_applied"
TYPE_ISOLATED = 1
RECORD_BYTES = 256
BLOB_BYTES = 512
CROSS_THREAD_RECOVERY = 0x8000


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


def write_fixture(workspace: Path) -> Path:
    app = workspace / PROBE_NAME
    source = app / "src"
    source.mkdir(parents=True)
    (app / "Cargo.toml").write_text(
        f'''[package]
name = "realistic-multimodule-typeiso-app"
version = "0.1.0"
edition = "2021"

[dependencies]
unialloc = {{ path = {json.dumps(str(ROOT / "unialloc"))}, features = ["stats", "type_isolation"] }}
''',
        encoding="utf-8",
    )
    (source / "ingest.rs").write_text(
        f'''use std::hint::black_box;

pub const RECORD_BYTES: usize = {RECORD_BYTES};
pub const BLOB_BYTES: usize = {BLOB_BYTES};

#[inline(never)]
fn record_byte(seed: u8, index: usize) -> u8 {{
    b'a' + ((seed.wrapping_add(index as u8).wrapping_mul(13)) % 26)
}}

#[inline(never)]
fn blob_byte(seed: u8, index: usize) -> u8 {{
    seed.wrapping_add((index as u8).wrapping_mul(17)) ^ (index as u8).rotate_left(3)
}}

#[inline(never)]
pub fn read_record(seed: u8) -> String {{
    let mut record = String::with_capacity(RECORD_BYTES);
    for index in 0..RECORD_BYTES {{
        record.push(record_byte(seed, index) as char);
    }}
    black_box(record)
}}

#[inline(never)]
pub fn load_blob(seed: u8) -> Box<[u8]> {{
    let mut source = [0_u8; BLOB_BYTES];
    for (index, byte) in source.iter_mut().enumerate() {{
        *byte = blob_byte(seed, index);
    }}
    black_box(Box::<[u8]>::from(&source[..]))
}}

#[inline(never)]
pub fn record_matches(value: &[u8], seed: u8) -> bool {{
    value.len() == RECORD_BYTES
        && value
            .iter()
            .enumerate()
            .all(|(index, byte)| *byte == record_byte(seed, index))
}}

#[inline(never)]
pub fn blob_matches(value: &[u8], seed: u8) -> bool {{
    value.len() == BLOB_BYTES
        && value
            .iter()
            .enumerate()
            .all(|(index, byte)| *byte == blob_byte(seed, index))
}}
''',
        encoding="utf-8",
    )
    (source / "transform.rs").write_text(
        '''#[inline(never)]
pub fn normalize_record(record: String) -> Vec<u8> {
    record.into_bytes()
}

#[inline(never)]
pub fn checksum(bytes: &[u8]) -> u64 {
    bytes.iter().fold(0_u64, |sum, byte| {
        sum.wrapping_mul(16777619).wrapping_add(u64::from(*byte))
    })
}
''',
        encoding="utf-8",
    )
    (source / "storage.rs").write_text(
        '''use crate::ingest::{BLOB_BYTES, RECORD_BYTES};

#[inline(never)]
pub fn reserve_record_buffer() -> Vec<u8> {
    Vec::<u8>::with_capacity(RECORD_BYTES)
}

#[inline(never)]
pub fn reserve_blob_buffer() -> Vec<u8> {
    Vec::<u8>::with_capacity(BLOB_BYTES)
}
''',
        encoding="utf-8",
    )
    (source / "handoff.rs").write_text(
        r'''use std::fmt::Write as _;
use std::hint::black_box;
use std::thread;
use unialloc::{
    semantic_fallback_attribution_snapshot, semantic_metadata_validation_snapshot,
    semantic_stats_recording_disable, semantic_stats_reset, semantic_stats_snapshot,
    semantic_type_stats_recording_disable, semantic_type_stats_snapshot,
    type_isolation_side_cache_snapshot, SemanticTypeStatsSnapshot,
};

const BYTES: usize = 768;

#[inline(never)]
fn payload_byte(seed: u8, index: usize) -> u8 {
    seed.wrapping_add((index as u8).wrapping_mul(29)) ^ (index as u8).rotate_left(5)
}

#[inline(never)]
fn local_buffer(seed: u8) -> Vec<u8> {
    let mut value = Vec::<u8>::with_capacity(BYTES);
    for index in 0..BYTES {
        value.push(payload_byte(seed, index));
    }
    value
}

#[inline(never)]
fn auto_cross_buffer(seed: u8) -> Vec<u8> {
    let mut value = Vec::<u8>::with_capacity(BYTES);
    for index in 0..BYTES {
        value.push(payload_byte(seed, index));
    }

    // Keep a spawn-shaped branch in optimized MIR without executing a second
    // thread in the measured window. This exercises the same conservative
    // body/type auto-placement rule as the real escaping object below.
    if black_box(false) {
        let marker = Vec::<u8>::new();
        thread::spawn(move || drop(marker))
            .join()
            .expect("unreachable marker worker");
    }
    value
}

#[inline(never)]
fn payload_matches(value: &[u8], seed: u8) -> bool {
    value.len() == BYTES
        && value
            .iter()
            .enumerate()
            .all(|(index, byte)| *byte == payload_byte(seed, index))
}

fn type_rows_json(rows: &[SemanticTypeStatsSnapshot], row_count: usize) -> String {
    let mut out = String::from("[");
    let mut emitted = 0usize;
    for row in rows.iter().take(std::cmp::min(row_count, rows.len())) {
        if row.allocations == 0 && row.deallocations == 0 {
            continue;
        }
        if emitted != 0 {
            out.push(',');
        }
        emitted += 1;
        let _ = write!(
            out,
            concat!(
                "{{",
                "\"type_id\":{},",
                "\"module_id\":{},",
                "\"callsite\":{},",
                "\"allocations\":{},",
                "\"deallocations\":{},",
                "\"cache_hits\":{},",
                "\"cache_inserts\":{},",
                "\"observed_alloc_size\":{},",
                "\"observed_dealloc_size\":{},",
                "\"policy_flags_seen\":{}",
                "}}"
            ),
            row.type_id,
            row.module_id,
            row.callsite,
            row.allocations,
            row.deallocations,
            row.cache_hits,
            row.cache_inserts,
            row.observed_alloc_size,
            row.observed_dealloc_size,
            row.policy_flags_seen,
        );
    }
    out.push(']');
    out
}

pub fn run_cross_placement_lane() {
    // This allocation and the actual spawn are deliberately in the same MIR
    // body, so the pass must infer cross-thread recovery without a manual hint.
    let mut escaping = Vec::<u8>::with_capacity(BYTES);
    for index in 0..BYTES {
        escaping.push(payload_byte(0x31, index));
    }
    let escaping_pointer = escaping.as_ptr() as usize;

    thread::spawn(move || {
        assert!(payload_matches(&escaping, 0x31));

        // Thread creation itself is outside the diagnostic window. The
        // allocation-side recovery record intentionally survives this reset.
        semantic_stats_reset();
        let fallback_before = semantic_fallback_attribution_snapshot();
        drop(escaping);

        let local = local_buffer(0x53);
        let local_pointer = local.as_ptr() as usize;
        let cross_to_local_non_reuse = local_pointer != escaping_pointer;
        assert!(cross_to_local_non_reuse);
        drop(local);

        let local_again = local_buffer(0x75);
        let local_again_pointer = local_again.as_ptr() as usize;
        let exact_local_reuse = local_again_pointer == local_pointer;
        assert!(exact_local_reuse);

        let cross_again = auto_cross_buffer(0x97);
        let cross_again_pointer = cross_again.as_ptr() as usize;
        let exact_cross_reuse = cross_again_pointer == escaping_pointer;
        assert!(exact_cross_reuse);
        assert!(payload_matches(&cross_again, 0x97));

        drop(cross_again);
        drop(local_again);

        let stats = semantic_stats_snapshot();
        let fallback = semantic_fallback_attribution_snapshot();
        let validation = semantic_metadata_validation_snapshot();
        let side_cache = type_isolation_side_cache_snapshot();
        let mut rows = [SemanticTypeStatsSnapshot::empty(); 16];
        let row_count = semantic_type_stats_snapshot(&mut rows);

        assert_eq!(stats.typed_allocations, 3, "{stats:?}");
        assert_eq!(stats.typed_deallocations, 4, "{stats:?}");
        assert_eq!(stats.typed_cache_hits, 2, "{stats:?}");
        assert_eq!(stats.typed_cache_inserts, 4, "{stats:?}");
        assert_eq!(stats.fallback_allocations, 0, "{stats:?}");
        assert_eq!(stats.fallback_deallocations, 0, "{stats:?}");
        assert_eq!(stats.semantic_type_stats_dropped_events, 0, "{stats:?}");
        assert_eq!(
            fallback
                .raw_alloc_no_metadata
                .saturating_sub(fallback_before.raw_alloc_no_metadata),
            0,
            "{fallback:?}"
        );
        assert_eq!(
            fallback
                .raw_dealloc_no_metadata
                .saturating_sub(fallback_before.raw_dealloc_no_metadata),
            0,
            "{fallback:?}"
        );
        assert_eq!(
            fallback
                .raw_realloc_no_metadata
                .saturating_sub(fallback_before.raw_realloc_no_metadata),
            0,
            "{fallback:?}"
        );
        assert_eq!(validation.recovery_identity_mismatches, 0, "{validation:?}");
        assert_eq!(side_cache.corrupt_slots, 0, "{side_cache:?}");

        semantic_type_stats_recording_disable();
        semantic_stats_recording_disable();
        let type_rows = type_rows_json(&rows, row_count);
        println!(
            concat!(
                "{{",
                "\"source\":\"cross_placement_lane\",",
                "\"bytes\":{},",
                "\"escaping_pointer\":{},",
                "\"local_pointer\":{},",
                "\"local_again_pointer\":{},",
                "\"cross_again_pointer\":{},",
                "\"cross_to_local_non_reuse\":{},",
                "\"exact_local_reuse\":{},",
                "\"exact_cross_reuse\":{},",
                "\"typed_allocations\":{},",
                "\"typed_deallocations\":{},",
                "\"typed_cache_hits\":{},",
                "\"typed_cache_inserts\":{},",
                "\"fallback_allocations\":{},",
                "\"fallback_deallocations\":{},",
                "\"raw_alloc_no_metadata\":{},",
                "\"raw_dealloc_no_metadata\":{},",
                "\"raw_realloc_no_metadata\":{},",
                "\"recovery_identity_mismatches\":{},",
                "\"side_cache_corrupt_slots\":{},",
                "\"semantic_type_stats_dropped_events\":{},",
                "\"type_rows\":{}",
                "}}"
            ),
            BYTES,
            escaping_pointer,
            local_pointer,
            local_again_pointer,
            cross_again_pointer,
            cross_to_local_non_reuse,
            exact_local_reuse,
            exact_cross_reuse,
            stats.typed_allocations,
            stats.typed_deallocations,
            stats.typed_cache_hits,
            stats.typed_cache_inserts,
            stats.fallback_allocations,
            stats.fallback_deallocations,
            fallback
                .raw_alloc_no_metadata
                .saturating_sub(fallback_before.raw_alloc_no_metadata),
            fallback
                .raw_dealloc_no_metadata
                .saturating_sub(fallback_before.raw_dealloc_no_metadata),
            fallback
                .raw_realloc_no_metadata
                .saturating_sub(fallback_before.raw_realloc_no_metadata),
            validation.recovery_identity_mismatches,
            side_cache.corrupt_slots,
            stats.semantic_type_stats_dropped_events,
            type_rows,
        );
    })
    .join()
    .expect("cross-placement worker should finish");
}
''',
        encoding="utf-8",
    )
    (source / "main.rs").write_text(
        r'''mod handoff;
mod ingest;
mod storage;
mod transform;

use std::fmt::Write as _;
use unialloc::{
    semantic_auto_metadata_disable, semantic_fallback_attribution_snapshot,
    semantic_metadata_validation_snapshot, semantic_ownership_transfer_snapshot,
    semantic_stats_recording_disable, semantic_stats_reset, semantic_stats_snapshot,
    semantic_type_stats_recording_disable, semantic_type_stats_snapshot,
    type_isolation_side_cache_snapshot, SemanticTypeStatsSnapshot, UniAlloc,
};

#[global_allocator]
static ALLOCATOR: UniAlloc = UniAlloc;

fn type_rows_json(rows: &[SemanticTypeStatsSnapshot], row_count: usize) -> String {
    let mut out = String::from("[");
    let mut emitted = 0usize;
    for row in rows.iter().take(std::cmp::min(row_count, rows.len())) {
        if row.allocations == 0 && row.deallocations == 0 {
            continue;
        }
        if emitted != 0 {
            out.push(',');
        }
        emitted += 1;
        let _ = write!(
            out,
            concat!(
                "{{",
                "\"type_id\":{},",
                "\"module_id\":{},",
                "\"callsite\":{},",
                "\"allocations\":{},",
                "\"deallocations\":{},",
                "\"cache_hits\":{},",
                "\"cache_inserts\":{},",
                "\"observed_alloc_size\":{},",
                "\"observed_dealloc_size\":{},",
                "\"policy_flags_seen\":{}",
                "}}"
            ),
            row.type_id,
            row.module_id,
            row.callsite,
            row.allocations,
            row.deallocations,
            row.cache_hits,
            row.cache_inserts,
            row.observed_alloc_size,
            row.observed_dealloc_size,
            row.policy_flags_seen,
        );
    }
    out.push(']');
    out
}

fn main() {
    // The application uses ordinary String/Vec/Box APIs across three source
    // modules. It provides no allocation metadata; identity must come from the
    // actual rustc MIR rewrite path.
    semantic_auto_metadata_disable();
    handoff::run_cross_placement_lane();
    semantic_stats_reset();
    let transfer_before = semantic_ownership_transfer_snapshot();

    let record = ingest::read_record(0x31);
    assert_eq!(record.len(), ingest::RECORD_BYTES);
    let record_pointer = record.as_ptr() as usize;
    let normalized = transform::normalize_record(record);
    let normalized_pointer = normalized.as_ptr() as usize;
    assert_eq!(normalized_pointer, record_pointer);
    assert!(ingest::record_matches(&normalized, 0x31));
    assert_ne!(transform::checksum(&normalized), 0);
    drop(normalized);

    let wrong_record = ingest::read_record(0x53);
    let wrong_record_pointer = wrong_record.as_ptr() as usize;
    let wrong_record_non_reuse = wrong_record_pointer != normalized_pointer;
    assert!(wrong_record_non_reuse);
    drop(wrong_record);

    let exact_record_buffer = storage::reserve_record_buffer();
    let exact_record_buffer_pointer = exact_record_buffer.as_ptr() as usize;
    let exact_record_vec_reuse = exact_record_buffer_pointer == normalized_pointer;
    assert!(exact_record_vec_reuse);

    let blob = ingest::load_blob(0x75);
    assert!(ingest::blob_matches(&blob, 0x75));
    let blob_pointer = blob.as_ptr() as usize;
    assert_ne!(transform::checksum(&blob), 0);
    drop(blob);

    let wrong_blob_buffer = storage::reserve_blob_buffer();
    let wrong_blob_buffer_pointer = wrong_blob_buffer.as_ptr() as usize;
    let wrong_blob_vec_non_reuse = wrong_blob_buffer_pointer != blob_pointer;
    assert!(wrong_blob_vec_non_reuse);

    let exact_blob = ingest::load_blob(0x97);
    let exact_blob_pointer = exact_blob.as_ptr() as usize;
    let exact_blob_reuse = exact_blob_pointer == blob_pointer;
    assert!(exact_blob_reuse);
    assert!(ingest::blob_matches(&exact_blob, 0x97));

    drop(exact_blob);
    drop(wrong_blob_buffer);
    drop(exact_record_buffer);

    let transfer_after = semantic_ownership_transfer_snapshot();
    let stats = semantic_stats_snapshot();
    let fallback = semantic_fallback_attribution_snapshot();
    let validation = semantic_metadata_validation_snapshot();
    let side_cache = type_isolation_side_cache_snapshot();
    let mut rows = [SemanticTypeStatsSnapshot::empty(); 32];
    let row_count = semantic_type_stats_snapshot(&mut rows);
    semantic_type_stats_recording_disable();
    semantic_stats_recording_disable();
    let type_rows = type_rows_json(&rows, row_count);

    println!(
        concat!(
            "{{",
            "\"source\":\"realistic_multimodule_typeiso_app\",",
            "\"record_bytes\":{},",
            "\"blob_bytes\":{},",
            "\"record_pointer\":{},",
            "\"normalized_pointer\":{},",
            "\"wrong_record_pointer\":{},",
            "\"exact_record_buffer_pointer\":{},",
            "\"blob_pointer\":{},",
            "\"wrong_blob_buffer_pointer\":{},",
            "\"exact_blob_pointer\":{},",
            "\"record_transfer_pointer_preserved\":{},",
            "\"wrong_record_non_reuse\":{},",
            "\"exact_record_vec_reuse\":{},",
            "\"wrong_blob_vec_non_reuse\":{},",
            "\"exact_blob_reuse\":{},",
            "\"transfer_attempted\":{},",
            "\"transfer_applied\":{},",
            "\"transfer_rejected\":{},",
            "\"fallback_allocations\":{},",
            "\"fallback_deallocations\":{},",
            "\"raw_alloc_no_metadata\":{},",
            "\"raw_dealloc_no_metadata\":{},",
            "\"raw_realloc_no_metadata\":{},",
            "\"recovery_identity_mismatches\":{},",
            "\"side_cache_corrupt_slots\":{},",
            "\"semantic_type_stats_dropped_events\":{},",
            "\"type_rows\":{}",
            "}}"
        ),
        ingest::RECORD_BYTES,
        ingest::BLOB_BYTES,
        record_pointer,
        normalized_pointer,
        wrong_record_pointer,
        exact_record_buffer_pointer,
        blob_pointer,
        wrong_blob_buffer_pointer,
        exact_blob_pointer,
        record_pointer == normalized_pointer,
        wrong_record_non_reuse,
        exact_record_vec_reuse,
        wrong_blob_vec_non_reuse,
        exact_blob_reuse,
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
        stats.semantic_type_stats_dropped_events,
        type_rows,
    );
}
''',
        encoding="utf-8",
    )
    return app


def function_matches(row: dict[str, object], suffix: str) -> bool:
    function = str(row.get("mir_function") or "")
    return function == suffix or function.endswith(f"::{suffix}")


def applied_scope_rows(
    audit: dict[str, object], function_suffix: str, semantic_type: str
) -> list[dict[str, object]]:
    rows = audit.get("rewrite_candidates")
    assert isinstance(rows, list), rows
    return [
        row
        for row in rows
        if isinstance(row, dict)
        and function_matches(row, function_suffix)
        and row.get("lowering_kind") == "semantic_scope_enter_exit_rewrite"
        and row.get("rewrite_status") == APPLIED_SCOPE_STATUS
        and row.get("semantic_object_type") == semantic_type
    ]


def one_scope_row(
    audit: dict[str, object], function_suffix: str, semantic_type: str, callee_hint: str
) -> dict[str, object]:
    rows = [
        row
        for row in applied_scope_rows(audit, function_suffix, semantic_type)
        if callee_hint in str(row.get("callee") or "")
    ]
    assert len(rows) == 1, (function_suffix, semantic_type, callee_hint, rows)
    return rows[0]


def parse_runtime(stdout: str, source: str = PROBE_NAME) -> dict[str, object]:
    for line in stdout.splitlines():
        if not line.startswith("{"):
            continue
        value = json.loads(line)
        if isinstance(value, dict) and value.get("source") == source:
            return value
    raise AssertionError(f"missing {source} JSON event in stdout:\n{stdout}")


def preview_type_id(transfer: dict[str, object], label: str) -> int:
    match = re.search(rf"\b{label}=(\d+)\b", str(transfer.get("replacement_preview") or ""))
    assert match is not None, transfer
    return int(match.group(1))


def validate(audit: dict[str, object], stdout: str) -> dict[str, object]:
    summary = audit.get("summary")
    assert isinstance(summary, dict), summary
    for field in (
        "provider_override_installed",
        "body_clone_returned_to_rustc",
        "actual_semantic_scope_rewrite_requested",
        "actual_semantic_scope_rewrite",
    ):
        assert summary.get(field) is True, (field, summary)

    record_string = one_scope_row(audit, "ingest::read_record", STRING_TYPE, "with_capacity")
    record_vec = one_scope_row(
        audit, "storage::reserve_record_buffer", VEC_TYPE, "with_capacity"
    )
    blob_vec = one_scope_row(
        audit, "storage::reserve_blob_buffer", VEC_TYPE, "with_capacity"
    )
    blob_box = one_scope_row(audit, "ingest::load_blob", BOX_TYPE, "from")
    scope_rows = [record_string, record_vec, blob_vec, blob_box]

    module_id = int(record_string.get("module_id") or 0)
    assert module_id != 0, record_string
    assert {int(row.get("module_id") or 0) for row in scope_rows} == {module_id}, scope_rows
    callsites = {int(row.get("callsite") or 0) for row in scope_rows}
    assert len(callsites) == 4 and 0 not in callsites, scope_rows

    string_type_id = int(record_string.get("type_id") or 0)
    vec_type_id = int(record_vec.get("type_id") or 0)
    box_type_id = int(blob_box.get("type_id") or 0)
    assert string_type_id != 0 and vec_type_id != 0 and box_type_id != 0, scope_rows
    assert len({string_type_id, vec_type_id, box_type_id}) == 3, scope_rows
    assert int(blob_vec.get("type_id") or 0) == vec_type_id, blob_vec

    records = audit.get("rewrite_candidates")
    assert isinstance(records, list), records
    transfer_rows = [
        row
        for row in records
        if isinstance(row, dict)
        and function_matches(row, "transform::normalize_record")
        and row.get("lowering_kind") == "semantic_ownership_transfer_rewrite"
    ]
    assert len(transfer_rows) == 1, transfer_rows
    transfer = transfer_rows[0]
    assert transfer.get("rewrite_status") == TRANSFER_STATUS, transfer
    assert transfer.get("replacement_symbol") == "__unialloc_semantic_string_into_bytes", transfer
    assert transfer.get("semantic_object_type") == VEC_TYPE, transfer
    assert transfer.get("argument_types") == [STRING_TYPE], transfer
    assert int(transfer.get("flags") or 0) & TYPE_ISOLATED, transfer
    assert int(transfer.get("module_id") or 0) == module_id, transfer
    assert int(transfer.get("callsite") or 0) not in callsites, transfer
    assert preview_type_id(transfer, "old_type_id") == string_type_id, transfer
    assert preview_type_id(transfer, "new_type_id") == vec_type_id, transfer
    assert int(summary.get("semantic_ownership_transfer_candidate_count") or 0) == 1, summary
    assert int(summary.get("semantic_ownership_transfer_rewrite_applied_count") or 0) == 1, summary

    runtime = parse_runtime(stdout)
    assert int(runtime["record_bytes"]) == RECORD_BYTES, runtime
    assert int(runtime["blob_bytes"]) == BLOB_BYTES, runtime
    for field in (
        "record_transfer_pointer_preserved",
        "wrong_record_non_reuse",
        "exact_record_vec_reuse",
        "wrong_blob_vec_non_reuse",
        "exact_blob_reuse",
    ):
        assert runtime.get(field) is True, (field, runtime)
    assert int(runtime["record_pointer"]) == int(runtime["normalized_pointer"]), runtime
    assert int(runtime["wrong_record_pointer"]) != int(runtime["normalized_pointer"]), runtime
    assert int(runtime["exact_record_buffer_pointer"]) == int(runtime["normalized_pointer"]), runtime
    assert int(runtime["wrong_blob_buffer_pointer"]) != int(runtime["blob_pointer"]), runtime
    assert int(runtime["exact_blob_pointer"]) == int(runtime["blob_pointer"]), runtime
    for field, expected in (
        ("transfer_attempted", 1),
        ("transfer_applied", 1),
        ("transfer_rejected", 0),
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

    runtime_rows = runtime.get("type_rows")
    assert isinstance(runtime_rows, list), runtime_rows
    runtime_identities = {
        (
            int(row.get("type_id") or 0),
            int(row.get("module_id") or 0),
            int(row.get("callsite") or 0),
        )
        for row in runtime_rows
        if isinstance(row, dict)
    }
    for row in scope_rows:
        identity = (
            int(row.get("type_id") or 0),
            int(row.get("module_id") or 0),
            int(row.get("callsite") or 0),
        )
        assert identity in runtime_identities, (identity, runtime_rows, row)

    runtime_by_identity = {
        (
            int(row.get("type_id") or 0),
            int(row.get("module_id") or 0),
            int(row.get("callsite") or 0),
        ): row
        for row in runtime_rows
        if isinstance(row, dict)
    }

    def runtime_row(audit_row: dict[str, object]) -> dict[str, object]:
        identity = (
            int(audit_row.get("type_id") or 0),
            int(audit_row.get("module_id") or 0),
            int(audit_row.get("callsite") or 0),
        )
        row = runtime_by_identity.get(identity)
        assert row is not None, (identity, runtime_rows)
        return row

    assert int(runtime_row(record_string).get("allocations") or 0) == 2, runtime_rows
    assert int(runtime_row(record_string).get("cache_hits") or 0) == 0, runtime_rows
    assert int(runtime_row(record_vec).get("allocations") or 0) == 1, runtime_rows
    assert int(runtime_row(record_vec).get("cache_hits") or 0) == 1, runtime_rows
    assert int(runtime_row(blob_vec).get("allocations") or 0) == 1, runtime_rows
    assert int(runtime_row(blob_vec).get("cache_hits") or 0) == 0, runtime_rows
    assert int(runtime_row(blob_box).get("allocations") or 0) == 2, runtime_rows
    assert int(runtime_row(blob_box).get("cache_hits") or 0) == 1, runtime_rows

    escaped_vec = one_scope_row(
        audit, "handoff::run_cross_placement_lane", VEC_TYPE, "with_capacity"
    )
    local_vec = one_scope_row(audit, "handoff::local_buffer", VEC_TYPE, "with_capacity")
    auto_cross_vec = one_scope_row(
        audit, "handoff::auto_cross_buffer", VEC_TYPE, "with_capacity"
    )
    placement_rows = [escaped_vec, local_vec, auto_cross_vec]
    assert {int(row.get("type_id") or 0) for row in placement_rows} == {vec_type_id}, placement_rows
    assert {int(row.get("module_id") or 0) for row in placement_rows} == {module_id}, placement_rows
    placement_callsites = {int(row.get("callsite") or 0) for row in placement_rows}
    assert len(placement_callsites) == 3 and 0 not in placement_callsites, placement_rows
    for row in (escaped_vec, auto_cross_vec):
        assert int(row.get("placement_hint") or 0) == CROSS_THREAD_RECOVERY, row
        assert row.get("cross_thread_recovery_hint") is True, row
        assert row.get("placement_hint_basis") == "auto_cross_thread_escape", row
    assert int(local_vec.get("placement_hint") or 0) == 0, local_vec
    assert local_vec.get("cross_thread_recovery_hint") is False, local_vec
    assert local_vec.get("placement_hint_basis") == "default", local_vec

    cross_runtime = parse_runtime(stdout, "cross_placement_lane")
    assert int(cross_runtime["bytes"]) == 768, cross_runtime
    for field in (
        "cross_to_local_non_reuse",
        "exact_local_reuse",
        "exact_cross_reuse",
    ):
        assert cross_runtime.get(field) is True, (field, cross_runtime)
    assert int(cross_runtime["escaping_pointer"]) != int(cross_runtime["local_pointer"]), cross_runtime
    assert int(cross_runtime["local_pointer"]) == int(cross_runtime["local_again_pointer"]), cross_runtime
    assert int(cross_runtime["escaping_pointer"]) == int(cross_runtime["cross_again_pointer"]), cross_runtime
    for field, expected in (
        ("typed_allocations", 3),
        ("typed_deallocations", 4),
        ("typed_cache_hits", 2),
        ("typed_cache_inserts", 4),
        ("fallback_allocations", 0),
        ("fallback_deallocations", 0),
        ("raw_alloc_no_metadata", 0),
        ("raw_dealloc_no_metadata", 0),
        ("raw_realloc_no_metadata", 0),
        ("recovery_identity_mismatches", 0),
        ("side_cache_corrupt_slots", 0),
        ("semantic_type_stats_dropped_events", 0),
    ):
        assert int(cross_runtime[field]) == expected, (field, cross_runtime)
    cross_rows = cross_runtime.get("type_rows")
    assert isinstance(cross_rows, list), cross_rows
    cross_runtime_identities = {
        (
            int(row.get("type_id") or 0),
            int(row.get("module_id") or 0),
            int(row.get("callsite") or 0),
        ): row
        for row in cross_rows
        if isinstance(row, dict)
    }
    for row in placement_rows:
        identity = (
            int(row.get("type_id") or 0),
            int(row.get("module_id") or 0),
            int(row.get("callsite") or 0),
        )
        assert identity in cross_runtime_identities, (identity, cross_rows, row)
    assert int(cross_runtime_identities[
        (vec_type_id, module_id, int(escaped_vec["callsite"]))
    ].get("deallocations") or 0) == 1, cross_rows
    assert int(cross_runtime_identities[
        (vec_type_id, module_id, int(local_vec["callsite"]))
    ].get("cache_hits") or 0) == 1, cross_rows
    assert int(cross_runtime_identities[
        (vec_type_id, module_id, int(auto_cross_vec["callsite"]))
    ].get("cache_hits") or 0) == 1, cross_rows

    return {
        "actual_scope_rows": len(scope_rows),
        "ownership_transfer": {
            "candidates": 1,
            "applied": 1,
            "callsite": int(transfer.get("callsite") or 0),
        },
        "compiler_identities": {
            "module_id": module_id,
            "type_ids": {
                "string": string_type_id,
                "vec_u8": vec_type_id,
                "box_slice_u8": box_type_id,
            },
            "allocation_callsites": sorted(callsites),
            "modules": ["handoff", "ingest", "storage", "transform"],
        },
        "automatic_cross_placement": {
            "bit": CROSS_THREAD_RECOVERY,
            "escaped_callsite": int(escaped_vec.get("callsite") or 0),
            "local_callsite": int(local_vec.get("callsite") or 0),
            "exact_cross_probe_callsite": int(auto_cross_vec.get("callsite") or 0),
            "runtime": cross_runtime,
        },
        "runtime": runtime,
    }


def main() -> int:
    toolchain = (ROOT / "rust-toolchain").read_text(encoding="utf-8").strip()
    rustc = shutil.which("rustc") or "rustc"
    cargo = shutil.which("cargo") or "cargo"
    sysroot = subprocess.check_output(
        [rustc, f"+{toolchain}", "--print", "sysroot"], cwd=ROOT, text=True
    ).strip()

    with tempfile.TemporaryDirectory(prefix="unialloc-realistic-multimodule-typeiso-") as raw:
        workspace = Path(raw)
        app = write_fixture(workspace)
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
                "UNIALLOC_LOWERING_AUTO_CROSS_THREAD_HINT": "1",
                "UNIALLOC_LOWERING_POLICY_FLAGS": str(TYPE_ISOLATED),
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
            {
                "source": "mir_realistic_multimodule_type_isolation",
                "validated": True,
                "single_run": True,
                "benchmark": False,
                "boundaries": [
                    "Functional actual-rustc regression for one generated multi-module Cargo application; not arbitrary external-application coverage.",
                    "The address oracle covers String-to-Vec ownership transfer and same-layout Box-slice versus Vec reuse under trusted compiler metadata; it is not a universal memory-safety proof.",
                    "No timing or publication-grade performance claim is made.",
                ],
                "evidence": evidence,
            },
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
