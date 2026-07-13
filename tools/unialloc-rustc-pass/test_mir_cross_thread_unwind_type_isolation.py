#!/usr/bin/env python3
"""Prove cross-thread panic unwind preserves typed owner isolation."""

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
APP_CRATE = "cross_thread_unwind_type_isolation_app"
RUNTIME_SOURCE = "cross_thread_unwind_type_isolation"
RESERVE_HELPER = "reserve_in_worker"
PRODUCER_SOURCE_HELPER = "make_producer"
PRODUCER_RECOVERY_HELPER = "recover_producer"
CONSUMER_HELPER = "make_consumer"
APPLIED_SCOPE_STATUS = "actual_semantic_scope_enter_exit_rewrite_applied"
TYPE_ISOLATED = 0x1
CROSS_THREAD_RECOVERY = 0x8000


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


def require_success(
    result: subprocess.CompletedProcess[str], command: list[str]
) -> None:
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
name = "cross-thread-unwind-type-isolation-app"
version = "0.1.0"
edition = "2021"

[dependencies]
unialloc = {{ path = {unialloc_path}, features = ["stats", "type_isolation"] }}
''',
        encoding="utf-8",
    )
    (app / "src/main.rs").write_text(
        r'''use std::fmt::Write as _;
use std::mem::{align_of, size_of};
use std::panic::{catch_unwind, AssertUnwindSafe};
use std::sync::atomic::{AtomicUsize, Ordering};
use std::thread;

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

const ELEMENTS: usize = 4;

static PANIC_MAIN_DEPTH: AtomicUsize = AtomicUsize::new(usize::MAX);
static PANIC_OVERFLOW_DEPTH: AtomicUsize = AtomicUsize::new(usize::MAX);
static PANIC_REPRESENTED_DEPTH: AtomicUsize = AtomicUsize::new(usize::MAX);

#[derive(Debug, Eq, PartialEq)]
#[repr(C)]
struct ProducerPayload([u64; 8]);

#[derive(Debug, Eq, PartialEq)]
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
fn make_producer() -> Vec<ProducerPayload> {
    let mut values = Vec::<ProducerPayload>::with_capacity(ELEMENTS);
    for index in 0..ELEMENTS as u64 {
        values.push(producer_payload(0xA110_0000 + index));
    }
    values
}

#[inline(never)]
fn recover_producer() -> Vec<ProducerPayload> {
    Vec::<ProducerPayload>::with_capacity(ELEMENTS)
}

#[inline(never)]
fn make_consumer() -> Vec<ConsumerPayload> {
    Vec::<ConsumerPayload>::with_capacity(ELEMENTS)
}

#[inline(never)]
fn reserve_in_worker(value: &mut Vec<ProducerPayload>) {
    // Capacity overflow is deterministic and callback-free. The actual MIR
    // wrapper must insert a semantic-scope pop on this unwind edge.
    value.reserve(usize::MAX);
}

fn producer_matches(values: &[ProducerPayload]) -> bool {
    values.len() == ELEMENTS
        && values
            .iter()
            .enumerate()
            .all(|(index, value)| *value == producer_payload(0xA110_0000 + index as u64))
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

    // The fixture uses ordinary Vec and thread APIs. It never calls a metadata
    // allocation ABI; semantic identity is available only through the actual
    // RUSTC_WRAPPER rewrite.
    semantic_auto_metadata_disable();
    std::panic::set_hook(Box::new(|_| {
        let depth = semantic_scope_depth_snapshot();
        PANIC_MAIN_DEPTH.store(depth.main_depth, Ordering::SeqCst);
        PANIC_OVERFLOW_DEPTH.store(depth.overflow_depth, Ordering::SeqCst);
        PANIC_REPRESENTED_DEPTH.store(depth.represented_depth, Ordering::SeqCst);
    }));

    let source = make_producer();
    assert!(producer_matches(&source));
    let main_pointer = source.as_ptr() as usize;
    let main_thread = thread::current().id();

    let worker = thread::spawn(move || {
        assert_ne!(thread::current().id(), main_thread);
        let mut source = source;
        let worker_pointer = source.as_ptr() as usize;
        assert_eq!(worker_pointer, main_pointer);
        assert!(producer_matches(&source));

        let initial_depth = semantic_scope_depth_snapshot();
        let caught = catch_unwind(AssertUnwindSafe(|| reserve_in_worker(&mut source)));

        // Exclude caught-payload destruction and thread startup from the
        // measured allocator lifecycle. The source allocation record remains
        // live and must still be recovered on this worker.
        semantic_stats_recording_disable();
        let panic_observed = caught.is_err();
        drop(caught);
        semantic_stats_recording_enable();

        let post_unwind_depth = semantic_scope_depth_snapshot();
        let source_preserved_after_unwind =
            source.as_ptr() as usize == main_pointer && producer_matches(&source);
        assert!(panic_observed);
        assert!(source_preserved_after_unwind);

        let fallback_before = semantic_fallback_attribution_snapshot();
        let validation_before = semantic_metadata_validation_snapshot();
        semantic_stats_reset();

        // Cross-thread drop must retain the Producer allocation in its typed
        // domain. A same-layout Consumer cannot recover it; exact Producer can.
        drop(source);
        let consumer = make_consumer();
        let consumer_pointer = consumer.as_ptr() as usize;
        let consumer_avoided_producer_storage = consumer_pointer != main_pointer;
        assert!(consumer_avoided_producer_storage);

        let recovered = recover_producer();
        let recovered_pointer = recovered.as_ptr() as usize;
        let exact_producer_reuse = recovered_pointer == main_pointer;
        let recovered_distinct_from_live_consumer = recovered_pointer != consumer_pointer;
        assert!(exact_producer_reuse);
        assert!(recovered_distinct_from_live_consumer);

        let stats = semantic_stats_snapshot();
        let fallback = semantic_fallback_attribution_snapshot();
        let validation = semantic_metadata_validation_snapshot();
        let side_cache = type_isolation_side_cache_snapshot();
        let final_depth = semantic_scope_depth_snapshot();
        let mut rows = [SemanticTypeStatsSnapshot::empty(); 32];
        let row_count = semantic_type_stats_snapshot(&mut rows);

        semantic_type_stats_recording_disable();
        semantic_stats_recording_disable();
        drop(recovered);
        drop(consumer);
        let _ = std::panic::take_hook();

        let type_rows = type_rows_json(&rows, row_count);
        println!(
            concat!(
                "{{",
                "\"source\":\"cross_thread_unwind_type_isolation\",",
                "\"elements\":{},",
                "\"main_pointer\":{},",
                "\"worker_pointer\":{},",
                "\"consumer_pointer\":{},",
                "\"recovered_pointer\":{},",
                "\"worker_thread_distinct\":true,",
                "\"panic_observed\":{},",
                "\"source_preserved_after_unwind\":{},",
                "\"consumer_avoided_producer_storage\":{},",
                "\"exact_producer_reuse\":{},",
                "\"recovered_distinct_from_live_consumer\":{},",
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
                "\"raw_dealloc_no_metadata\":{},",
                "\"raw_realloc_no_metadata\":{},",
                "\"recovery_identity_mismatches\":{},",
                "\"side_cache_corrupt_slots\":{},",
                "\"semantic_type_stats_dropped_events\":{},",
                "\"type_rows\":{}",
                "}}"
            ),
            ELEMENTS,
            main_pointer,
            worker_pointer,
            consumer_pointer,
            recovered_pointer,
            panic_observed,
            source_preserved_after_unwind,
            consumer_avoided_producer_storage,
            exact_producer_reuse,
            recovered_distinct_from_live_consumer,
            initial_depth.main_depth,
            initial_depth.overflow_depth,
            initial_depth.represented_depth,
            PANIC_MAIN_DEPTH.load(Ordering::SeqCst),
            PANIC_OVERFLOW_DEPTH.load(Ordering::SeqCst),
            PANIC_REPRESENTED_DEPTH.load(Ordering::SeqCst),
            post_unwind_depth.main_depth,
            post_unwind_depth.overflow_depth,
            post_unwind_depth.represented_depth,
            final_depth.main_depth,
            final_depth.overflow_depth,
            final_depth.represented_depth,
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
            fallback
                .raw_alloc_no_metadata
                .saturating_sub(fallback_before.raw_alloc_no_metadata),
            fallback
                .raw_dealloc_no_metadata
                .saturating_sub(fallback_before.raw_dealloc_no_metadata),
            fallback
                .raw_realloc_no_metadata
                .saturating_sub(fallback_before.raw_realloc_no_metadata),
            validation
                .recovery_identity_mismatches
                .saturating_sub(validation_before.recovery_identity_mismatches),
            side_cache.corrupt_slots,
            stats.semantic_type_stats_dropped_events,
            type_rows,
        );
    });

    worker.join().expect("worker thread completed");
}
''',
        encoding="utf-8",
    )
    return app


def function_matches(row: dict[str, object], function_name: str) -> bool:
    function = str(row.get("mir_function") or "")
    return function == function_name or function.endswith(f"::{function_name}")


def allocation_scope_rows(
    audit: dict[str, object], function_name: str, payload: str
) -> list[dict[str, object]]:
    rows = audit.get("rewrite_candidates")
    assert isinstance(rows, list), rows
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


def require_actual_scope(row: dict[str, object], label: str) -> None:
    assert row.get("lowering_kind") == "semantic_scope_enter_exit_rewrite", (
        label,
        row,
    )
    assert row.get("rewrite_status") == APPLIED_SCOPE_STATUS, (label, row)
    assert str(row.get("replacement_resolution_status") or "").startswith(
        "resolved_unialloc_semantic_scope"
    ), (label, row)
    assert row.get("metadata_pairing_contract") == "semantic_scope_active_metadata", (
        label,
        row,
    )
    assert int(row.get("type_id") or 0) != 0, (label, row)
    assert int(row.get("module_id") or 0) != 0, (label, row)
    assert int(row.get("callsite") or 0) != 0, (label, row)
    assert int(row.get("flags") or 0) & TYPE_ISOLATED, (label, row)
    assert int(row.get("placement_hint") or 0) & CROSS_THREAD_RECOVERY, (label, row)
    assert row.get("cross_thread_recovery_hint") is True, (label, row)
    assert row.get("placement_hint_basis") == "manual_cross_thread_recovery_hint", (
        label,
        row,
    )


def unique_actual_scope(
    rows: list[dict[str, object]], label: str
) -> dict[str, object]:
    assert len(rows) == 1, f"{label} must have exactly one scope, got {rows!r}"
    row = rows[0]
    require_actual_scope(row, label)
    return row


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
    return {
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


def load_runtime(stdout: str) -> dict[str, object]:
    matches: list[dict[str, object]] = []
    for line in stdout.splitlines():
        stripped = line.strip()
        if not stripped.startswith("{"):
            continue
        value = json.loads(stripped)
        if isinstance(value, dict) and value.get("source") == RUNTIME_SOURCE:
            matches.append(value)
    assert len(matches) == 1, (matches, stdout)
    return matches[0]


def validate(audit: dict[str, object], stdout: str) -> dict[str, object]:
    summary = audit.get("summary")
    assert isinstance(summary, dict), summary
    assert summary.get("provider_override_installed") is True, summary
    assert summary.get("body_clone_returned_to_rustc") is True, summary
    assert summary.get("actual_semantic_scope_rewrite_requested") is True, summary
    assert summary.get("actual_semantic_scope_rewrite") is True, summary
    assert int(summary.get("semantic_scope_unwind_pop_inserted_count") or 0) >= 1, summary

    rows = audit.get("rewrite_candidates")
    assert isinstance(rows, list), rows
    reserve_rows = [
        row
        for row in rows
        if isinstance(row, dict)
        and function_matches(row, RESERVE_HELPER)
        and "reserve" in str(row.get("callee") or "")
        and "ProducerPayload"
        in " ".join(
            str(row.get(field) or "")
            for field in ("semantic_object_type", "destination_type", "argument_types")
        )
    ]
    reserve = unique_actual_scope(reserve_rows, "worker Vec::reserve unwind scope")
    assert reserve.get("semantic_scope_unwind_pop_inserted") is True, reserve
    assert "src/main.rs" in str(reserve.get("source_span") or ""), reserve

    source = unique_actual_scope(
        allocation_scope_rows(audit, PRODUCER_SOURCE_HELPER, "ProducerPayload"),
        "main Producer allocation",
    )
    recovery = unique_actual_scope(
        allocation_scope_rows(audit, PRODUCER_RECOVERY_HELPER, "ProducerPayload"),
        "worker exact Producer recovery",
    )
    consumer = unique_actual_scope(
        allocation_scope_rows(audit, CONSUMER_HELPER, "ConsumerPayload"),
        "worker same-layout Consumer allocation",
    )

    producer_type_ids = {
        int(row.get("type_id") or 0) for row in (reserve, source, recovery)
    }
    producer_module_ids = {
        int(row.get("module_id") or 0) for row in (reserve, source, recovery)
    }
    assert len(producer_type_ids) == 1, producer_type_ids
    assert len(producer_module_ids) == 1, producer_module_ids
    producer_type_id = next(iter(producer_type_ids))
    module_id = next(iter(producer_module_ids))
    consumer_type_id = int(consumer.get("type_id") or 0)
    assert int(consumer.get("module_id") or 0) == module_id, consumer
    assert producer_type_id != consumer_type_id, (producer_type_id, consumer_type_id)

    runtime = load_runtime(stdout)
    for field in (
        "worker_thread_distinct",
        "panic_observed",
        "source_preserved_after_unwind",
        "consumer_avoided_producer_storage",
        "exact_producer_reuse",
        "recovered_distinct_from_live_consumer",
    ):
        assert runtime.get(field) is True, (field, runtime)

    main_pointer = int(runtime.get("main_pointer") or 0)
    worker_pointer = int(runtime.get("worker_pointer") or 0)
    consumer_pointer = int(runtime.get("consumer_pointer") or 0)
    recovered_pointer = int(runtime.get("recovered_pointer") or 0)
    assert all(
        pointer != 0
        for pointer in (
            main_pointer,
            worker_pointer,
            consumer_pointer,
            recovered_pointer,
        )
    )
    assert main_pointer == worker_pointer == recovered_pointer, runtime
    assert consumer_pointer != main_pointer, runtime

    for prefix in ("initial", "post_unwind", "final"):
        assert int(runtime.get(f"{prefix}_main_depth") or 0) == 0, runtime
        assert int(runtime.get(f"{prefix}_overflow_depth") or 0) == 0, runtime
        assert int(runtime.get(f"{prefix}_represented_depth") or 0) == 0, runtime
    assert int(runtime.get("panic_main_depth") or 0) == 1, runtime
    assert int(runtime.get("panic_overflow_depth") or 0) == 0, runtime
    assert int(runtime.get("panic_represented_depth") or 0) == 1, runtime

    for field, expected in (
        ("elements", 4),
        ("total_allocations", 2),
        ("typed_allocations", 2),
        ("typed_allocated_bytes", 512),
        ("total_deallocations", 1),
        ("typed_deallocations", 1),
        ("typed_cache_hits", 1),
        ("typed_cache_inserts", 1),
        # The same-layout Consumer correctly misses the Producer-only cache
        # entry and therefore records one typed cache bypass.
        ("typed_cache_bypasses", 1),
        ("fallback_allocations", 0),
        ("fallback_deallocations", 0),
        ("raw_alloc_no_metadata", 0),
        ("raw_dealloc_no_metadata", 0),
        ("raw_realloc_no_metadata", 0),
        ("recovery_identity_mismatches", 0),
        ("side_cache_corrupt_slots", 0),
        ("semantic_type_stats_dropped_events", 0),
    ):
        assert int(runtime.get(field) or 0) == expected, (field, runtime)

    producer_runtime = aggregate_type_rows(
        runtime, type_id=producer_type_id, module_id=module_id
    )
    consumer_runtime = aggregate_type_rows(
        runtime, type_id=consumer_type_id, module_id=module_id
    )
    assert producer_runtime == {
        "allocations": 1,
        "allocated_bytes": 256,
        "deallocations": 1,
        "cache_hits": 1,
        "cache_inserts": 1,
        "cache_bypasses": 0,
    }, producer_runtime
    assert consumer_runtime == {
        "allocations": 1,
        "allocated_bytes": 256,
        "deallocations": 0,
        "cache_hits": 0,
        "cache_inserts": 0,
        "cache_bypasses": 1,
    }, consumer_runtime

    return {
        "reserve_scope": {
            "type_id": producer_type_id,
            "module_id": module_id,
            "callsite": int(reserve.get("callsite") or 0),
            "source_span": reserve.get("source_span"),
            "semantic_scope_unwind_pop_inserted": True,
            "placement_hint_basis": reserve.get("placement_hint_basis"),
        },
        "consumer_type_id": consumer_type_id,
        "scope_depth_transition": {"at_panic": 1, "after_unwind": 0},
        "runtime": runtime,
        "producer_runtime": producer_runtime,
        "consumer_runtime": consumer_runtime,
    }


def runtime_stdout_with_override(
    stdout: str, field: str, value: object
) -> str:
    rewritten: list[str] = []
    matches = 0
    for line in stdout.splitlines():
        replacement = line
        stripped = line.strip()
        if stripped.startswith("{"):
            event = json.loads(stripped)
            if isinstance(event, dict) and event.get("source") == RUNTIME_SOURCE:
                event[field] = value
                replacement = json.dumps(event, sort_keys=True)
                matches += 1
        rewritten.append(replacement)
    assert matches == 1, (matches, stdout)
    return "\n".join(rewritten) + ("\n" if stdout.endswith("\n") else "")


def mutable_reserve_row(audit: dict[str, object]) -> dict[str, object]:
    rows = audit.get("rewrite_candidates")
    assert isinstance(rows, list), rows
    matches = [
        row
        for row in rows
        if isinstance(row, dict)
        and function_matches(row, RESERVE_HELPER)
        and "reserve" in str(row.get("callee") or "")
    ]
    assert len(matches) == 1, matches
    return matches[0]


def assert_validation_rejected(
    label: str, audit: dict[str, object], stdout: str
) -> bool:
    try:
        validate(audit, stdout)
    except AssertionError:
        return True
    raise AssertionError(f"validator accepted tampered {label} evidence")


def validate_negative_controls(
    audit: dict[str, object], stdout: str
) -> dict[str, bool]:
    status_audit = copy.deepcopy(audit)
    mutable_reserve_row(status_audit)["rewrite_status"] = "tampered_not_applied"

    unwind_audit = copy.deepcopy(audit)
    mutable_reserve_row(unwind_audit)["semantic_scope_unwind_pop_inserted"] = False

    bad_depth = runtime_stdout_with_override(stdout, "post_unwind_main_depth", 1)
    bad_reuse = runtime_stdout_with_override(stdout, "exact_producer_reuse", False)
    bad_isolation = runtime_stdout_with_override(
        stdout, "consumer_avoided_producer_storage", False
    )
    duplicate_runtime = (
        stdout.rstrip("\n")
        + "\n"
        + json.dumps(load_runtime(stdout), sort_keys=True)
        + "\n"
    )

    return {
        "tampered_applied_status_rejected": assert_validation_rejected(
            "applied status", status_audit, stdout
        ),
        "missing_unwind_pop_rejected": assert_validation_rejected(
            "unwind pop", unwind_audit, stdout
        ),
        "nonzero_post_unwind_depth_rejected": assert_validation_rejected(
            "post-unwind depth", audit, bad_depth
        ),
        "missing_exact_reuse_rejected": assert_validation_rejected(
            "exact reuse", audit, bad_reuse
        ),
        "same_layout_cross_reuse_rejected": assert_validation_rejected(
            "same-layout cross reuse", audit, bad_isolation
        ),
        "duplicate_runtime_event_rejected": assert_validation_rejected(
            "duplicate runtime event", audit, duplicate_runtime
        ),
    }


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
            prefix="unialloc-cross-thread-unwind-"
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
                "UNIALLOC_LOWERING_PLACEMENT_HINT": str(CROSS_THREAD_RECOVERY),
                "UNIALLOC_RUSTC_SYSROOT": sysroot,
                "CARGO_NET_OFFLINE": "true",
                "CARGO_INCREMENTAL": "0",
                "CARGO_PROFILE_DEV_PANIC": "unwind",
                "CARGO_TARGET_DIR": str(workspace / "target"),
            }
        )
        if persistent_workspace is not None:
            mir_dir = workspace / "mir"
            mir_dir.mkdir()
            existing_rustflags = run_env.get("RUSTFLAGS", "").strip()
            dump_flags = f"-Zdump-mir={RESERVE_HELPER} -Zdump-mir-dir={mir_dir}"
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
        evidence = validate(audit, runtime_run.stdout)
        negative_controls = validate_negative_controls(audit, runtime_run.stdout)

    print(
        json.dumps(
            {
                "source": "mir_cross_thread_unwind_type_isolation_probe",
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
                    "Functional actual-RUSTC_WRAPPER regression for one main-to-worker "
                    "Vec<ProducerPayload> owner and one callback-free Vec::reserve "
                    "capacity-overflow unwind; no universal unwind or thread-escape "
                    "coverage claim.",
                    "Cross-thread recovery placement is forced through "
                    "UNIALLOC_LOWERING_PLACEMENT_HINT; this probe does not claim "
                    "automatic escape inference.",
                    "The generated Rust application uses ordinary Vec, thread, and "
                    "catch_unwind APIs and no manual metadata allocation ABI.",
                    "Worker-side post-panic accounting proves typed cross-thread "
                    "deallocation, same-layout Consumer non-reuse, exact Producer reuse, "
                    "and zero observed fallback/raw/mismatch/corrupt/dropped events in "
                    "the bounded window.",
                    "This is a functional security regression, not a benchmark or "
                    "paper-performance claim.",
                ],
            },
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
