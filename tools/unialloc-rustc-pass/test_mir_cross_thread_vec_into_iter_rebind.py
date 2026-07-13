#!/usr/bin/env python3
"""Prove cross-thread Vec::into_iter transfer preserves isolated owner identity."""

from __future__ import annotations

import copy
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import tempfile


ROOT = Path(__file__).resolve().parents[2]
PASS_SOURCE = ROOT / "tools/unialloc-rustc-pass/unialloc-rustc-mir-rewrite-dry-run.rs"
PROBE_NAME = "cross_thread_vec_into_iter_rebind_probe"
VEC_FUNCTION = "make_vec"
TRANSFER_FUNCTION = "vec_into_iter"
CLONE_FUNCTION = "clone_iter"
VEC_TYPE = "std::vec::Vec<u8, std::alloc::Global>"
INTO_ITER_TYPE = "std::vec::IntoIter<u8, std::alloc::Global>"
APPLIED_SCOPE_STATUS = "actual_semantic_scope_enter_exit_rewrite_applied"
TRANSFER_LOWERING_KIND = "semantic_ownership_transfer_rewrite"
TRANSFER_STATUS = "actual_semantic_ownership_transfer_rewrite_applied"
TRANSFER_SYMBOL = "__unialloc_semantic_vec_into_iter"
TRANSFER_RESOLUTION_STATUS = "resolved_unialloc_semantic_vec_into_iter"
TRANSFER_PAIRING = "pointer_preserving_owner_identity_rebind"
TRANSFER_TYPE_ID_BASIS = "rustc_middle_exact_vec_into_iter_owner_transfer"
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
    seed.wrapping_add((index as u8).wrapping_mul(29))
}

#[inline(never)]
fn make_vec(seed: u8) -> Vec<u8> {
    let mut value = Vec::with_capacity(BYTES);
    let mut index = 0usize;
    while index < BYTES {
        value.push(payload_byte(seed, index));
        index += 1;
    }
    value
}

#[inline(never)]
fn vec_into_iter(value: Vec<u8>) -> std::vec::IntoIter<u8> {
    value.into_iter()
}

#[inline(never)]
fn clone_iter(value: &std::vec::IntoIter<u8>) -> std::vec::IntoIter<u8> {
    value.clone()
}

#[inline(never)]
fn payload_matches(bytes: &[u8], seed: u8) -> bool {
    if bytes.len() != BYTES {
        return false;
    }
    let mut index = 0usize;
    while index < BYTES {
        if bytes[index] != payload_byte(seed, index) {
            return false;
        }
        index += 1;
    }
    true
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
    semantic_auto_metadata_disable();
    semantic_stats_reset();

    // Ordinary Rust only: the Vec owner is allocated on main, moved through
    // thread::spawn, and converted to IntoIter on a distinct worker.
    let value = make_vec(0x31);
    assert!(payload_matches(&value, 0x31));
    let main_vec_pointer = value.as_ptr() as usize;
    let main_thread = thread::current().id();

    let worker = thread::spawn(move || {
        assert_ne!(thread::current().id(), main_thread);
        let worker_vec_pointer = value.as_ptr() as usize;
        assert_eq!(worker_vec_pointer, main_vec_pointer);
        assert!(payload_matches(&value, 0x31));

        // Exclude thread startup from the measured lifecycle without retiring
        // the process-visible allocation record carried from main.
        semantic_stats_reset();
        let transfer_before = semantic_ownership_transfer_snapshot();
        let fallback_before = semantic_fallback_attribution_snapshot();
        let validation_before = semantic_metadata_validation_snapshot();

        let before_first_transfer = semantic_stats_snapshot();
        let first_iter = vec_into_iter(value);
        let after_first_transfer = semantic_stats_snapshot();
        let first_iter_pointer = first_iter.as_slice().as_ptr() as usize;
        let pointer_preserved = first_iter_pointer == main_vec_pointer;
        let payload_preserved = payload_matches(first_iter.as_slice(), 0x31);
        assert!(pointer_preserved);
        assert!(payload_preserved);
        assert_no_allocation_lifecycle_event(before_first_transfer, after_first_transfer);
        assert_eq!(
            semantic_metadata_validation_snapshot().recovery_identity_mismatches,
            validation_before.recovery_identity_mismatches
        );

        drop(first_iter);
        let after_first_drop = semantic_stats_snapshot();
        assert_eq!(
            after_first_drop
                .typed_deallocations
                .saturating_sub(after_first_transfer.typed_deallocations),
            1
        );
        assert_eq!(
            after_first_drop
                .typed_cache_inserts
                .saturating_sub(after_first_transfer.typed_cache_inserts),
            1
        );

        // The transferred buffer now belongs to IntoIter. A same-layout Vec
        // must not recover it, even though both operations happen on the worker.
        let wrong_vec = make_vec(0x73);
        let wrong_vec_pointer = wrong_vec.as_ptr() as usize;
        let wrong_vec_non_reuse = wrong_vec_pointer != first_iter_pointer;
        assert!(wrong_vec_non_reuse);
        assert!(payload_matches(&wrong_vec, 0x73));

        // Keep an independent source live while IntoIter::clone requests target
        // storage. Exact target identity must recover the transferred buffer,
        // never alias the live seed Vec/IntoIter allocation.
        let seed_vec = make_vec(0x55);
        let seed_vec_pointer = seed_vec.as_ptr() as usize;
        let seed_vec_non_reuse = seed_vec_pointer != first_iter_pointer;
        assert!(seed_vec_non_reuse);
        assert_ne!(seed_vec_pointer, wrong_vec_pointer);
        let seed_iter = vec_into_iter(seed_vec);
        let seed_iter_pointer = seed_iter.as_slice().as_ptr() as usize;
        assert_eq!(seed_iter_pointer, seed_vec_pointer);
        assert!(payload_matches(seed_iter.as_slice(), 0x55));

        let recovered_iter = clone_iter(&seed_iter);
        let recovered_iter_pointer = recovered_iter.as_slice().as_ptr() as usize;
        let exact_intoiter_reuse = recovered_iter_pointer == first_iter_pointer;
        let recovered_distinct_from_live_seed = recovered_iter_pointer != seed_iter_pointer;
        assert!(exact_intoiter_reuse);
        assert!(recovered_distinct_from_live_seed);
        assert!(payload_matches(recovered_iter.as_slice(), 0x55));

        drop(recovered_iter);
        drop(seed_iter);
        drop(wrong_vec);

        let transfer_after = semantic_ownership_transfer_snapshot();
        let stats = semantic_stats_snapshot();
        let fallback = semantic_fallback_attribution_snapshot();
        let validation = semantic_metadata_validation_snapshot();
        let side_cache = type_isolation_side_cache_snapshot();
        semantic_stats_recording_disable();

        let transfer_attempted = transfer_after
            .attempted
            .saturating_sub(transfer_before.attempted);
        let transfer_applied = transfer_after.applied.saturating_sub(transfer_before.applied);
        let transfer_rejected = transfer_after
            .rejected
            .saturating_sub(transfer_before.rejected);
        assert_eq!(transfer_attempted, 2);
        assert_eq!(transfer_applied, 2);
        assert_eq!(transfer_rejected, 0);
        assert_eq!(stats.typed_allocations, 3, "{stats:?}");
        assert_eq!(stats.typed_deallocations, 4, "{stats:?}");
        assert_eq!(stats.typed_cache_hits, 1, "{stats:?}");
        assert_eq!(stats.typed_cache_inserts, 4, "{stats:?}");
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
        assert_eq!(
            validation.recovery_identity_mismatches,
            validation_before.recovery_identity_mismatches,
            "{validation:?}"
        );
        assert_eq!(side_cache.corrupt_slots, 0, "{side_cache:?}");
        assert_eq!(stats.semantic_type_stats_dropped_events, 0, "{stats:?}");

        println!(
            concat!(
                "{{",
                "\"source\":\"cross_thread_vec_into_iter_rebind_probe\",",
                "\"bytes\":{},",
                "\"main_vec_pointer\":{},",
                "\"worker_vec_pointer\":{},",
                "\"first_iter_pointer\":{},",
                "\"wrong_vec_pointer\":{},",
                "\"seed_vec_pointer\":{},",
                "\"seed_iter_pointer\":{},",
                "\"recovered_iter_pointer\":{},",
                "\"worker_thread_distinct\":true,",
                "\"pointer_preserved\":{},",
                "\"payload_preserved\":{},",
                "\"wrong_vec_non_reuse\":{},",
                "\"seed_vec_non_reuse\":{},",
                "\"exact_intoiter_reuse\":{},",
                "\"recovered_distinct_from_live_seed\":{},",
                "\"transfer_attempted\":{},",
                "\"transfer_applied\":{},",
                "\"transfer_rejected\":{},",
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
                "\"semantic_type_stats_dropped_events\":{}",
                "}}"
            ),
            BYTES,
            main_vec_pointer,
            worker_vec_pointer,
            first_iter_pointer,
            wrong_vec_pointer,
            seed_vec_pointer,
            seed_iter_pointer,
            recovered_iter_pointer,
            pointer_preserved,
            payload_preserved,
            wrong_vec_non_reuse,
            seed_vec_non_reuse,
            exact_intoiter_reuse,
            recovered_distinct_from_live_seed,
            transfer_attempted,
            transfer_applied,
            transfer_rejected,
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
            validation
                .recovery_identity_mismatches
                .saturating_sub(validation_before.recovery_identity_mismatches),
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
    matches: list[dict[str, object]] = []
    for line in stdout.splitlines():
        if not line.startswith("{"):
            continue
        value = json.loads(line)
        if isinstance(value, dict) and value.get("source") == PROBE_NAME:
            matches.append(value)
    assert len(matches) == 1, (
        f"expected exactly one {PROBE_NAME} JSON event, got {len(matches)}",
        matches,
        stdout,
    )
    return matches[0]


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
    function_rows = [
        row
        for row in rows
        if isinstance(row, dict) and function_matches(row, TRANSFER_FUNCTION)
    ]
    assert len(transfer_rows) == 1, (
        f"expected 1 Vec::into_iter ownership-transfer candidate, got {len(transfer_rows)}",
        summary,
        transfer_rows,
        function_rows,
    )

    transfer_candidate_count = int(
        summary.get("semantic_ownership_transfer_candidate_count") or 0
    )
    transfer_applied_count = int(
        summary.get("semantic_ownership_transfer_rewrite_applied_count") or 0
    )
    assert transfer_candidate_count == 1, summary
    assert transfer_applied_count == 1, summary

    transfer = transfer_rows[0]
    assert re.search(
        r"~ core(?:\[[^]]+\])?::iter::traits::collect::IntoIterator::into_iter\)",
        str(transfer.get("callee") or ""),
    ), transfer
    assert transfer.get("rewrite_status") == TRANSFER_STATUS, transfer
    assert transfer.get("replacement_symbol") == TRANSFER_SYMBOL, transfer
    assert (
        transfer.get("replacement_resolution_status") == TRANSFER_RESOLUTION_STATUS
    ), transfer
    assert transfer.get("metadata_pairing_contract") == TRANSFER_PAIRING, transfer
    assert transfer.get("type_id_basis") == TRANSFER_TYPE_ID_BASIS, transfer
    assert transfer.get("semantic_object_type") == INTO_ITER_TYPE, transfer
    assert transfer.get("destination_type") == INTO_ITER_TYPE, transfer
    assert transfer.get("argument_types") == [VEC_TYPE], transfer
    assert int(transfer.get("type_id") or 0) != 0, transfer
    assert int(transfer.get("module_id") or 0) != 0, transfer
    assert int(transfer.get("callsite") or 0) != 0, transfer
    assert int(transfer.get("flags") or 0) & TYPE_ISOLATED, transfer
    assert int(transfer.get("placement_hint") or 0) & CROSS_THREAD_RECOVERY, transfer
    assert transfer.get("placement_hint_basis") == "manual_placement_hint", transfer
    assert transfer.get("semantic_scope_unwind_pop_inserted") is False, transfer

    vec_rows = [
        row
        for row in applied_scope_rows(audit, VEC_FUNCTION, VEC_TYPE)
        if "with_capacity" in str(row.get("callee") or "")
    ]
    clone_rows = [
        row
        for row in applied_scope_rows(audit, CLONE_FUNCTION, INTO_ITER_TYPE)
        if "Clone::clone" in str(row.get("callee") or "")
    ]
    assert len(vec_rows) == 1, vec_rows
    assert len(clone_rows) == 1, clone_rows
    vec_type_id = int(vec_rows[0].get("type_id") or 0)
    into_iter_type_id = int(clone_rows[0].get("type_id") or 0)
    assert vec_type_id != 0, vec_rows[0]
    assert into_iter_type_id != 0, clone_rows[0]
    assert vec_type_id != into_iter_type_id, (vec_rows[0], clone_rows[0])
    for row in (vec_rows[0], clone_rows[0]):
        assert int(row.get("flags") or 0) & TYPE_ISOLATED, row
        assert int(row.get("placement_hint") or 0) & CROSS_THREAD_RECOVERY, row
        assert row.get("cross_thread_recovery_hint") is True, row
        assert (
            row.get("placement_hint_basis") == "manual_cross_thread_recovery_hint"
        ), row

    assert int(transfer.get("type_id") or 0) == into_iter_type_id, transfer
    preview = str(transfer.get("replacement_preview") or "")
    old_match = re.search(r"\bold_type_id=(\d+)\b", preview)
    new_match = re.search(r"\bnew_type_id=(\d+)\b", preview)
    assert old_match is not None, transfer
    assert new_match is not None, transfer
    assert int(old_match.group(1)) == vec_type_id, transfer
    assert int(new_match.group(1)) == into_iter_type_id, transfer

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
        "payload_preserved",
        "pointer_preserved",
        "wrong_vec_non_reuse",
        "seed_vec_non_reuse",
        "exact_intoiter_reuse",
        "recovered_distinct_from_live_seed",
    ):
        assert runtime[field] is True, (field, runtime)
    assert int(runtime["main_vec_pointer"]) == int(runtime["worker_vec_pointer"]), runtime
    assert int(runtime["worker_vec_pointer"]) == int(
        runtime["first_iter_pointer"]
    ), runtime
    assert int(runtime["wrong_vec_pointer"]) != int(
        runtime["first_iter_pointer"]
    ), runtime
    assert int(runtime["seed_vec_pointer"]) == int(runtime["seed_iter_pointer"]), runtime
    assert int(runtime["seed_vec_pointer"]) != int(
        runtime["first_iter_pointer"]
    ), runtime
    assert int(runtime["recovered_iter_pointer"]) == int(
        runtime["first_iter_pointer"]
    ), runtime
    assert int(runtime["recovered_iter_pointer"]) != int(
        runtime["seed_iter_pointer"]
    ), runtime
    for field, expected in (
        ("transfer_attempted", 2),
        ("transfer_applied", 2),
        ("transfer_rejected", 0),
        ("typed_allocations", 3),
        ("typed_deallocations", 4),
        ("typed_cache_hits", 1),
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
        assert int(runtime[field]) == expected, (field, runtime)

    return {
        "transfer_counts": {
            "candidates": transfer_candidate_count,
            "applied": transfer_applied_count,
        },
        "transfer_audit": transfer,
        "compiler_type_ids": {"vec": vec_type_id, "into_iter": into_iter_type_id},
        "runtime": runtime,
    }


def runtime_stdout_with_override(
    stdout: str, field: str, value: object
) -> str:
    rewritten: list[str] = []
    match_count = 0
    for line in stdout.splitlines():
        replacement = line
        if line.startswith("{"):
            event = json.loads(line)
            if isinstance(event, dict) and event.get("source") == PROBE_NAME:
                event[field] = value
                replacement = json.dumps(event, sort_keys=True)
                match_count += 1
        rewritten.append(replacement)
    assert match_count == 1, (match_count, stdout)
    trailing_newline = "\n" if stdout.endswith("\n") else ""
    return "\n".join(rewritten) + trailing_newline


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
    def mutable_transfer_row(candidate_audit: dict[str, object]) -> dict[str, object]:
        rows = candidate_audit.get("rewrite_candidates")
        assert isinstance(rows, list), rows
        matches = [
            row
            for row in rows
            if isinstance(row, dict)
            and function_matches(row, TRANSFER_FUNCTION)
            and row.get("lowering_kind") == TRANSFER_LOWERING_KIND
        ]
        assert len(matches) == 1, matches
        return matches[0]

    status_audit = copy.deepcopy(audit)
    mutable_transfer_row(status_audit)["rewrite_status"] = "tampered_not_applied"

    type_id_audit = copy.deepcopy(audit)
    mutable_transfer_row(type_id_audit)["type_id"] = 0

    placement_audit = copy.deepcopy(audit)
    mutable_transfer_row(placement_audit)["placement_hint_basis"] = (
        "auto_cross_thread_escape"
    )

    runtime_reuse_stdout = runtime_stdout_with_override(
        stdout, "exact_intoiter_reuse", False
    )
    duplicate_runtime_stdout = (
        stdout.rstrip("\n")
        + "\n"
        + json.dumps(load_runtime(stdout), sort_keys=True)
        + "\n"
    )

    return {
        "tampered_transfer_status_rejected": assert_validation_rejected(
            "transfer status", status_audit, stdout
        ),
        "tampered_transfer_type_id_rejected": assert_validation_rejected(
            "transfer type id", type_id_audit, stdout
        ),
        "tampered_manual_placement_basis_rejected": assert_validation_rejected(
            "manual placement basis", placement_audit, stdout
        ),
        "tampered_runtime_exact_reuse_rejected": assert_validation_rejected(
            "runtime exact reuse", audit, runtime_reuse_stdout
        ),
        "duplicate_runtime_event_rejected": assert_validation_rejected(
            "duplicate runtime event", audit, duplicate_runtime_stdout
        ),
    }


def main() -> int:
    toolchain = (ROOT / "rust-toolchain").read_text(encoding="utf-8").strip()
    rustc = shutil.which("rustc") or "rustc"
    cargo = shutil.which("cargo") or "cargo"
    sysroot = subprocess.check_output(
        [rustc, f"+{toolchain}", "--print", "sysroot"], cwd=ROOT, text=True
    ).strip()

    with tempfile.TemporaryDirectory(prefix="unialloc-cross-thread-vec-into-iter-rebind-") as raw:
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
        evidence["validator_negative_controls"] = validate_negative_controls(
            audit, stdout
        )

    print(
        json.dumps(
            {
                "source": "mir_cross_thread_vec_into_iter_rebind_probe",
                "validated": True,
                "single_run": True,
                "benchmark": False,
                "evidence": evidence,
                "boundaries": [
                    "Cross-thread recovery placement is forced manually through UNIALLOC_LOWERING_PLACEMENT_HINT; this probe does not establish automatic escape inference.",
                    "Worker-side semantic statistics are reset after thread startup and after the original allocation, so 3 measured allocations and 4 measured deallocations include the pre-window allocation's in-window drop and do not indicate a leak.",
                    "This is a bounded functional security regression, not a benchmark, universal type-isolation proof, or paper-performance result.",
                ],
            },
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
