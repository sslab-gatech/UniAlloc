#!/usr/bin/env python3
"""Prove cross-thread Box<[u8]>::into_vec transfers compiler cache ownership."""

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
PROBE_NAME = "cross_thread_box_slice_into_vec_rebind_probe"
BOX_FUNCTION = "make_box"
TRANSFER_FUNCTION = "worker_box_into_vec"
VEC_FUNCTION = "make_vec"
BOX_TYPE = "std::boxed::Box<[u8], std::alloc::Global>"
VEC_TYPE = "std::vec::Vec<u8, std::alloc::Global>"
APPLIED_SCOPE_STATUS = "actual_semantic_scope_enter_exit_rewrite_applied"
TRANSFER_LOWERING_KIND = "semantic_ownership_transfer_rewrite"
TRANSFER_STATUS = "actual_semantic_ownership_transfer_rewrite_applied"
TRANSFER_SYMBOL = "__unialloc_semantic_box_slice_into_vec"
TRANSFER_PAIRING = "pointer_preserving_owner_identity_rebind"
TRANSFER_TYPE_ID_BASIS = "rustc_middle_exact_box_slice_into_vec_owner_transfer"
TYPE_ISOLATED = 0x1
CROSS_THREAD_RECOVERY = 0x8000
BYTES = 256


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
    app = workspace / PROBE_NAME
    (app / "src").mkdir(parents=True)
    (app / "Cargo.toml").write_text(
        f'''[package]
name = "{PROBE_NAME.replace('_', '-')}"
version = "0.1.0"
edition = "2021"

[dependencies]
unialloc = {{ path = {json.dumps(str(ROOT / "unialloc"))}, features = ["stats", "type_isolation"] }}
''',
        encoding="utf-8",
    )
    (app / "src/main.rs").write_text(
        r'''use std::thread;
use unialloc::{
    semantic_auto_metadata_disable, semantic_fallback_attribution_snapshot,
    semantic_metadata_validation_snapshot, semantic_ownership_transfer_snapshot,
    semantic_stats_recording_disable, semantic_stats_reset, semantic_stats_snapshot,
    type_isolation_side_cache_snapshot, SemanticStatsSnapshot, UniAlloc,
};

#[global_allocator]
static ALLOCATOR: UniAlloc = UniAlloc;

const BYTES: usize = 256;

#[inline(never)]
fn payload_byte(seed: u8, index: usize) -> u8 {
    seed.wrapping_add((index as u8).wrapping_mul(17)) ^ (index as u8).rotate_left(3)
}

#[inline(never)]
fn make_box(seed: u8) -> Box<[u8]> {
    let mut source = [0_u8; BYTES];
    for (index, byte) in source.iter_mut().enumerate() {
        *byte = payload_byte(seed, index);
    }
    Box::<[u8]>::from(&source[..])
}

#[inline(never)]
fn worker_box_into_vec(value: Box<[u8]>) -> Vec<u8> {
    value.into_vec()
}

#[inline(never)]
fn make_vec() -> Vec<u8> {
    Vec::<u8>::with_capacity(BYTES)
}

#[inline(never)]
fn payload_matches(bytes: &[u8], seed: u8) -> bool {
    bytes.len() == BYTES
        && bytes
            .iter()
            .enumerate()
            .all(|(index, byte)| *byte == payload_byte(seed, index))
}

fn assert_no_allocation_lifecycle_event(
    before: SemanticStatsSnapshot,
    after: SemanticStatsSnapshot,
) {
    assert_eq!(after.total_allocations, before.total_allocations);
    assert_eq!(after.typed_allocations, before.typed_allocations);
    assert_eq!(after.fallback_allocations, before.fallback_allocations);
    assert_eq!(after.total_allocated_bytes, before.total_allocated_bytes);
    assert_eq!(after.typed_allocated_bytes, before.typed_allocated_bytes);
    assert_eq!(after.fallback_allocated_bytes, before.fallback_allocated_bytes);
    assert_eq!(after.total_deallocations, before.total_deallocations);
    assert_eq!(after.typed_deallocations, before.typed_deallocations);
    assert_eq!(after.fallback_deallocations, before.fallback_deallocations);
    assert_eq!(after.typed_cache_hits, before.typed_cache_hits);
    assert_eq!(after.typed_cache_inserts, before.typed_cache_inserts);
    assert_eq!(after.typed_cache_bypasses, before.typed_cache_bypasses);
    assert_eq!(
        after.semantic_type_stats_dropped_events,
        before.semantic_type_stats_dropped_events
    );
}

fn main() {
    // Ordinary Rust application: a Box<[u8]> is produced on one thread, moved
    // through thread::spawn, converted with Box<[T]>::into_vec on the worker,
    // and then tested for exact type-isolated reuse on that worker.
    semantic_auto_metadata_disable();
    semantic_stats_reset();

    let boxed = make_box(0x31);
    assert_eq!(boxed.len(), BYTES);
    assert!(payload_matches(&boxed, 0x31));
    let main_box_pointer = boxed.as_ptr() as usize;
    let main_thread = thread::current().id();

    let worker = thread::spawn(move || {
        assert_ne!(thread::current().id(), main_thread);
        let worker_thread_distinct = true;
        let box_pointer = boxed.as_ptr() as usize;
        assert_eq!(box_pointer, main_box_pointer);
        assert!(payload_matches(&boxed, 0x31));

        // Reset after thread creation so this probe measures only the
        // transferred owner, same-thread controls, and typed reuse evidence.
        semantic_stats_reset();
        let transfer_before = semantic_ownership_transfer_snapshot();
        let fallback_before = semantic_fallback_attribution_snapshot();
        let validation_before = semantic_metadata_validation_snapshot();
        let before_transfer_stats = semantic_stats_snapshot();
        let converted = worker_box_into_vec(boxed);
        let after_transfer_stats = semantic_stats_snapshot();
        let fallback_after = semantic_fallback_attribution_snapshot();
        let validation_after = semantic_metadata_validation_snapshot();
        let converted_pointer = converted.as_ptr() as usize;
        let payload_preserved = payload_matches(&converted, 0x31);
        assert_eq!(converted_pointer, main_box_pointer);
        assert_eq!(converted.len(), BYTES);
        assert_eq!(converted.capacity(), BYTES);
        assert!(payload_preserved);
        assert_no_allocation_lifecycle_event(before_transfer_stats, after_transfer_stats);
        assert_eq!(fallback_after, fallback_before);
        assert_eq!(
            validation_after.recovery_identity_mismatches,
            validation_before.recovery_identity_mismatches
        );

        drop(converted);
        let after_converted_drop = semantic_stats_snapshot();
        assert_eq!(
            after_converted_drop
                .typed_deallocations
                .saturating_sub(after_transfer_stats.typed_deallocations),
            1
        );
        assert_eq!(
            after_converted_drop
                .typed_cache_inserts
                .saturating_sub(after_transfer_stats.typed_cache_inserts),
            1
        );

        let wrong_box = make_box(0x73);
        let wrong_box_pointer = wrong_box.as_ptr() as usize;
        assert_ne!(wrong_box_pointer, converted_pointer);
        assert!(payload_matches(&wrong_box, 0x73));
        drop(wrong_box);

        let recovered_vec = make_vec();
        let recovered_vec_pointer = recovered_vec.as_ptr() as usize;
        assert_eq!(recovered_vec.capacity(), BYTES);
        assert_eq!(recovered_vec_pointer, converted_pointer);
        drop(recovered_vec);

        let transfer_after = semantic_ownership_transfer_snapshot();
        let stats = semantic_stats_snapshot();
        let fallback = semantic_fallback_attribution_snapshot();
        let validation = semantic_metadata_validation_snapshot();
        let side_cache = type_isolation_side_cache_snapshot();
        semantic_stats_recording_disable();

        assert_eq!(
            transfer_after.attempted.saturating_sub(transfer_before.attempted),
            1
        );
        assert_eq!(
            transfer_after.applied.saturating_sub(transfer_before.applied),
            1
        );
        assert_eq!(
            transfer_after.rejected.saturating_sub(transfer_before.rejected),
            0
        );
        assert_eq!(stats.fallback_allocations, 0, "{stats:?}");
        assert_eq!(stats.fallback_deallocations, 0, "{stats:?}");
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
        assert_eq!(stats.semantic_type_stats_dropped_events, 0, "{stats:?}");

        println!(
            concat!(
                "{{",
                "\"source\":\"cross_thread_box_slice_into_vec_rebind_probe\",",
                "\"bytes\":{},",
                "\"main_box_pointer\":{},",
                "\"worker_box_pointer\":{},",
                "\"converted_pointer\":{},",
                "\"wrong_box_pointer\":{},",
                "\"recovered_vec_pointer\":{},",
                "\"worker_thread_distinct\":{},",
                "\"pointer_preserved\":{},",
                "\"payload_preserved\":{},",
                "\"wrong_box_non_reuse\":{},",
                "\"same_vec_reuse\":{},",
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
                "\"semantic_type_stats_dropped_events\":{}",
                "}}"
            ),
            BYTES,
            main_box_pointer,
            box_pointer,
            converted_pointer,
            wrong_box_pointer,
            recovered_vec_pointer,
            worker_thread_distinct,
            box_pointer == converted_pointer,
            payload_preserved,
            wrong_box_pointer != converted_pointer,
            recovered_vec_pointer == converted_pointer,
            transfer_after.attempted.saturating_sub(transfer_before.attempted),
            transfer_after.applied.saturating_sub(transfer_before.applied),
            transfer_after.rejected.saturating_sub(transfer_before.rejected),
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


def applied_scope_rows(
    audit: dict[str, object], function_name: str, semantic_type: str
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
        and row.get("semantic_object_type") == semantic_type
    ]


def load_runtime(stdout: str) -> dict[str, object]:
    for line in stdout.splitlines():
        if not line.startswith("{"):
            continue
        value = json.loads(line)
        if isinstance(value, dict) and value.get("source") == PROBE_NAME:
            return value
    raise AssertionError(f"missing {PROBE_NAME} JSON event in stdout:\n{stdout}")


def validate(audit: dict[str, object], stdout: str) -> dict[str, object]:
    summary = audit.get("summary")
    assert isinstance(summary, dict), summary
    assert summary.get("provider_override_installed") is True, summary
    assert summary.get("body_clone_returned_to_rustc") is True, summary
    assert summary.get("actual_semantic_scope_rewrite_requested") is True, summary
    assert summary.get("actual_semantic_scope_rewrite") is True, summary

    rows = audit.get("rewrite_candidates")
    assert isinstance(rows, list), rows
    transfer_rows = [
        row
        for row in rows
        if isinstance(row, dict)
        and function_matches(row, TRANSFER_FUNCTION)
        and row.get("lowering_kind") == TRANSFER_LOWERING_KIND
    ]
    assert len(transfer_rows) == 1, transfer_rows
    transfer = transfer_rows[0]
    assert re.search(
        r"~ alloc(?:\[[^]]+\])?::slice::\{impl#0\}::into_vec\)",
        str(transfer.get("callee") or ""),
    ), transfer
    assert transfer.get("rewrite_status") == TRANSFER_STATUS, transfer
    assert transfer.get("replacement_symbol") == TRANSFER_SYMBOL, transfer
    assert (
        transfer.get("replacement_resolution_status")
        == "resolved_unialloc_semantic_box_slice_into_vec"
    ), transfer
    assert transfer.get("metadata_pairing_contract") == TRANSFER_PAIRING, transfer
    assert transfer.get("type_id_basis") == TRANSFER_TYPE_ID_BASIS, transfer
    assert transfer.get("semantic_object_type") == VEC_TYPE, transfer
    assert transfer.get("destination_type") == VEC_TYPE, transfer
    assert transfer.get("argument_types") == [BOX_TYPE], transfer
    assert int(transfer.get("type_id") or 0) != 0, transfer
    assert int(transfer.get("module_id") or 0) != 0, transfer
    assert int(transfer.get("callsite") or 0) != 0, transfer
    assert int(transfer.get("flags") or 0) & TYPE_ISOLATED, transfer
    assert transfer.get("semantic_scope_unwind_pop_inserted") is False, transfer

    transfer_candidate_count = int(
        summary.get("semantic_ownership_transfer_candidate_count") or 0
    )
    transfer_applied_count = int(
        summary.get("semantic_ownership_transfer_rewrite_applied_count") or 0
    )
    assert transfer_candidate_count == 1, summary
    assert transfer_applied_count == 1, summary

    box_rows = [
        row
        for row in applied_scope_rows(audit, BOX_FUNCTION, BOX_TYPE)
        if "::From::from" in str(row.get("callee") or "")
    ]
    vec_rows = [
        row
        for row in applied_scope_rows(audit, VEC_FUNCTION, VEC_TYPE)
        if "with_capacity" in str(row.get("callee") or "")
    ]
    assert len(box_rows) == 1, box_rows
    assert len(vec_rows) == 1, vec_rows
    box_row = box_rows[0]
    vec_row = vec_rows[0]
    box_type_id = int(box_row.get("type_id") or 0)
    vec_type_id = int(vec_row.get("type_id") or 0)
    assert box_type_id != 0, box_row
    assert vec_type_id != 0, vec_row
    assert box_type_id != vec_type_id, (box_row, vec_row)
    for row in (box_row, vec_row):
        assert int(row.get("flags") or 0) & TYPE_ISOLATED, row
        assert int(row.get("placement_hint") or 0) & CROSS_THREAD_RECOVERY, row
        assert row.get("cross_thread_recovery_hint") is True, row
    assert int(transfer.get("type_id") or 0) == vec_type_id, transfer
    preview = str(transfer.get("replacement_preview") or "")
    old_match = re.search(r"\bold_type_id=(\d+)\b", preview)
    new_match = re.search(r"\bnew_type_id=(\d+)\b", preview)
    assert old_match is not None, transfer
    assert new_match is not None, transfer
    assert int(old_match.group(1)) == box_type_id, transfer
    assert int(new_match.group(1)) == vec_type_id, transfer

    generic_transfer_scopes = [
        row
        for row in rows
        if isinstance(row, dict)
        and function_matches(row, TRANSFER_FUNCTION)
        and row.get("lowering_kind") == "semantic_scope_enter_exit_rewrite"
    ]
    assert generic_transfer_scopes == [], generic_transfer_scopes

    runtime = load_runtime(stdout)
    assert int(runtime["bytes"]) == BYTES, runtime
    for field in (
        "worker_thread_distinct",
        "pointer_preserved",
        "payload_preserved",
        "wrong_box_non_reuse",
        "same_vec_reuse",
    ):
        assert runtime[field] is True, (field, runtime)
    assert int(runtime["main_box_pointer"]) == int(runtime["worker_box_pointer"]), runtime
    assert int(runtime["worker_box_pointer"]) == int(runtime["converted_pointer"]), runtime
    assert int(runtime["wrong_box_pointer"]) != int(runtime["converted_pointer"]), runtime
    assert int(runtime["recovered_vec_pointer"]) == int(runtime["converted_pointer"]), runtime
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

    return {
        "transfer_counts": {
            "candidates": transfer_candidate_count,
            "applied": transfer_applied_count,
        },
        "transfer_audit": {
            key: transfer.get(key)
            for key in (
                "mir_function",
                "callee",
                "destination_type",
                "argument_types",
                "semantic_object_type",
                "type_id",
                "module_id",
                "callsite",
                "lowering_kind",
                "rewrite_status",
                "replacement_symbol",
                "replacement_resolution_status",
                "metadata_pairing_contract",
                "type_id_basis",
                "placement_hint",
                "cross_thread_recovery_hint",
                "replacement_preview",
            )
        },
        "compiler_type_ids": {"box": box_type_id, "vec": vec_type_id},
        "runtime": runtime,
    }


def main() -> int:
    toolchain = (ROOT / "rust-toolchain").read_text(encoding="utf-8").strip()
    rustc = shutil.which("rustc") or "rustc"
    cargo = shutil.which("cargo") or "cargo"
    sysroot = subprocess.check_output(
        [rustc, f"+{toolchain}", "--print", "sysroot"], cwd=ROOT, text=True
    ).strip()

    with tempfile.TemporaryDirectory(
        prefix="unialloc-cross-thread-box-slice-into-vec-rebind-"
    ) as raw:
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
                "UNIALLOC_LOWERING_AUTO_CROSS_THREAD_HINT": "1",
                "UNIALLOC_LOWERING_POLICY_FLAGS": str(TYPE_ISOLATED),
                "UNIALLOC_LOWERING_PLACEMENT_HINT": str(CROSS_THREAD_RECOVERY),
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
                "source": "mir_cross_thread_box_slice_into_vec_rebind_probe",
                "validated": True,
                "single_run": True,
                "benchmark": False,
                "evidence": evidence,
            },
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
