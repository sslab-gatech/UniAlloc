#!/usr/bin/env python3
"""Prove cross-thread memory-tagged String::into_bytes transfers cache ownership."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import tempfile


ROOT = Path(__file__).resolve().parents[2]
PASS_SOURCE = ROOT / "tools/unialloc-rustc-pass/unialloc-rustc-mir-rewrite-dry-run.rs"
PROBE_NAME = "cross_thread_memory_tagged_string_into_bytes_rebind_probe"
STRING_FUNCTION = "make_string"
VEC_FUNCTION = "make_vec"
TRANSFER_FUNCTION = "worker_string_into_bytes"
STRING_TYPE = "std::string::String"
VEC_TYPE = "std::vec::Vec<u8, std::alloc::Global>"
VEC_TYPES = {VEC_TYPE, "std::vec::Vec<u8>"}
APPLIED_SCOPE_STATUS = "actual_semantic_scope_enter_exit_rewrite_applied"
TRANSFER_LOWERING_KIND = "semantic_ownership_transfer_rewrite"
TRANSFER_STATUS = "actual_semantic_ownership_transfer_rewrite_applied"
TRANSFER_SYMBOL = "__unialloc_semantic_string_into_bytes"
TRANSFER_RESOLUTION_STATUS = "resolved_unialloc_semantic_string_into_bytes"
TRANSFER_PAIRING = "pointer_preserving_owner_identity_rebind"
TRANSFER_TYPE_ID_BASIS = "rustc_middle_exact_string_into_bytes_owner_transfer"
TYPE_ISOLATED = 1
MEMORY_TAGGING = 1 << 7
POLICY_FLAGS = TYPE_ISOLATED | MEMORY_TAGGING
CROSS_THREAD_RECOVERY = 0x8000
BYTES = 256


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
# Keep the transient probe resolvable by nightly-2022-07-01; UniAlloc's loose
# lock_api requirement would otherwise select a release requiring rustc 1.71.
lock_api = "=0.4.3"
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
    b'a' + ((seed.wrapping_add(index as u8).wrapping_mul(13)) % 26)
}

#[inline(never)]
fn make_string(seed: u8) -> String {
    let mut value = String::with_capacity(BYTES);
    for index in 0..BYTES {
        value.push(payload_byte(seed, index) as char);
    }
    value
}

#[inline(never)]
fn worker_string_into_bytes(value: String) -> Vec<u8> {
    value.into_bytes()
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
    // Ordinary Rust application: a String is allocated on the main thread,
    // moved through thread::spawn, converted with ordinary String::into_bytes on
    // the worker, and then tested for exact memory-tagged type-isolated reuse on
    // that worker. Metadata is compiler-inserted by the rustc pass.
    semantic_auto_metadata_disable();
    semantic_stats_reset();

    let string = make_string(0x31);
    assert_eq!(string.len(), BYTES);
    assert_eq!(string.capacity(), BYTES);
    assert!(payload_matches(string.as_bytes(), 0x31));
    let main_string_pointer = string.as_ptr() as usize;
    let main_thread = thread::current().id();

    let worker = thread::spawn(move || {
        assert_ne!(thread::current().id(), main_thread);
        let worker_thread_distinct = true;
        let worker_string_pointer = string.as_ptr() as usize;
        assert_eq!(worker_string_pointer, main_string_pointer);
        assert!(payload_matches(string.as_bytes(), 0x31));

        // Reset after thread creation so this probe measures only the moved
        // owner transfer, same-worker controls, and typed reuse evidence.
        semantic_stats_reset();
        let transfer_before = semantic_ownership_transfer_snapshot();
        let fallback_before = semantic_fallback_attribution_snapshot();
        let validation_before = semantic_metadata_validation_snapshot();
        let before_transfer_stats = semantic_stats_snapshot();
        let bytes = worker_string_into_bytes(string);
        let after_transfer_stats = semantic_stats_snapshot();
        let fallback_after = semantic_fallback_attribution_snapshot();
        let validation_after = semantic_metadata_validation_snapshot();
        let bytes_pointer = bytes.as_ptr() as usize;
        let payload_preserved = payload_matches(&bytes, 0x31);
        let pointer_preserved = bytes_pointer == worker_string_pointer;
        let capacity_preserved = bytes.capacity() == BYTES;
        assert!(payload_preserved);
        assert!(pointer_preserved);
        assert!(capacity_preserved);
        assert_no_allocation_lifecycle_event(before_transfer_stats, after_transfer_stats);
        assert_eq!(fallback_after, fallback_before);
        assert_eq!(
            validation_after.recovery_identity_mismatches,
            validation_before.recovery_identity_mismatches
        );
        drop(bytes);

        let wrong_string = make_string(0x53);
        let wrong_string_pointer = wrong_string.as_ptr() as usize;
        let wrong_string_non_reuse = wrong_string_pointer != bytes_pointer;
        assert!(wrong_string_non_reuse);
        drop(wrong_string);

        let same_vec = make_vec();
        let same_vec_pointer = same_vec.as_ptr() as usize;
        let same_vec_reuse = same_vec_pointer == bytes_pointer;
        assert!(same_vec_reuse);
        drop(same_vec);

        // Re-acquiring the same cached pointer after its tagged deallocation is
        // a fail-closed public-API proof that the preceding tag record was
        // retired: a stale record makes allocation finishing reject the
        // duplicate record.
        let cleanup_vec = make_vec();
        let cleanup_vec_pointer = cleanup_vec.as_ptr() as usize;
        let tag_record_cleanup_reuse = cleanup_vec_pointer == same_vec_pointer;
        assert!(tag_record_cleanup_reuse);
        drop(cleanup_vec);

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
        assert_eq!(stats.policy_flags_seen & 129, 129, "{stats:?}");
        assert_eq!(stats.semantic_type_stats_dropped_events, 0, "{stats:?}");

        println!(
            concat!(
                "{{",
                "\"source\":\"cross_thread_memory_tagged_string_into_bytes_rebind_probe\",",
                "\"bytes\":{},",
                "\"main_string_pointer\":{},",
                "\"worker_string_pointer\":{},",
                "\"bytes_pointer\":{},",
                "\"wrong_string_pointer\":{},",
                "\"same_vec_pointer\":{},",
                "\"cleanup_vec_pointer\":{},",
                "\"worker_thread_distinct\":{},",
                "\"payload_preserved\":{},",
                "\"pointer_preserved\":{},",
                "\"capacity_preserved\":{},",
                "\"wrong_string_non_reuse\":{},",
                "\"same_vec_reuse\":{},",
                "\"tag_record_cleanup_reuse\":{},",
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
                "\"policy_flags_seen\":{},",
                "\"semantic_type_stats_dropped_events\":{}",
                "}}"
            ),
            BYTES,
            main_string_pointer,
            worker_string_pointer,
            bytes_pointer,
            wrong_string_pointer,
            same_vec_pointer,
            cleanup_vec_pointer,
            worker_thread_distinct,
            payload_preserved,
            pointer_preserved,
            capacity_preserved,
            wrong_string_non_reuse,
            same_vec_reuse,
            tag_record_cleanup_reuse,
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
            stats.policy_flags_seen,
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
    audit: dict[str, object], function_name: str, semantic_types: str | set[str]
) -> list[dict[str, object]]:
    rows = audit.get("rewrite_candidates")
    assert isinstance(rows, list), rows
    accepted_types = {semantic_types} if isinstance(semantic_types, str) else semantic_types
    return [
        row
        for row in rows
        if isinstance(row, dict)
        and function_matches(row, function_name)
        and row.get("lowering_kind") == "semantic_scope_enter_exit_rewrite"
        and row.get("rewrite_status") == APPLIED_SCOPE_STATUS
        and row.get("semantic_object_type") in accepted_types
    ]


def load_runtime(stdout: str) -> dict[str, object]:
    for line in stdout.splitlines():
        if not line.startswith("{"):
            continue
        value = json.loads(line)
        if isinstance(value, dict) and value.get("source") == PROBE_NAME:
            return value
    raise AssertionError(f"missing {PROBE_NAME} JSON event in stdout:\n{stdout}")


def assert_policy_and_cross_thread(row: dict[str, object]) -> None:
    assert int(row.get("flags") or 0) & POLICY_FLAGS == POLICY_FLAGS, row
    assert int(row.get("placement_hint") or 0) & CROSS_THREAD_RECOVERY, row


def validate(audit: dict[str, object], stdout: str) -> dict[str, object]:
    summary = audit.get("summary")
    assert isinstance(summary, dict), summary
    assert summary.get("provider_override_installed") is True, summary
    assert summary.get("body_clone_returned_to_rustc") is True, summary
    assert summary.get("actual_semantic_scope_rewrite_requested") is True, summary
    assert summary.get("actual_semantic_scope_rewrite") is True, summary
    assert int(summary.get("cross_thread_recovery_hint_count") or 0) > 0, summary

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
        f"expected 1 cross-thread String::into_bytes ownership-transfer candidate, got {len(transfer_rows)}",
        summary,
        transfer_rows,
        function_rows,
    )

    transfer_candidate_count = int(summary.get("semantic_ownership_transfer_candidate_count") or 0)
    transfer_applied_count = int(summary.get("semantic_ownership_transfer_rewrite_applied_count") or 0)
    assert transfer_candidate_count == 1, summary
    assert transfer_applied_count == 1, summary

    transfer = transfer_rows[0]
    assert re.search(
        r"(?:~ alloc(?:\[[^]]+\])?::string::(?:String|\{impl#\d+\})::into_bytes|std::string::String::into_bytes)",
        str(transfer.get("callee") or ""),
    ), transfer
    assert transfer.get("rewrite_status") == TRANSFER_STATUS, transfer
    assert transfer.get("replacement_symbol") == TRANSFER_SYMBOL, transfer
    assert transfer.get("replacement_resolution_status") == TRANSFER_RESOLUTION_STATUS, transfer
    assert transfer.get("metadata_pairing_contract") == TRANSFER_PAIRING, transfer
    assert transfer.get("type_id_basis") == TRANSFER_TYPE_ID_BASIS, transfer
    assert transfer.get("semantic_object_type") in VEC_TYPES, transfer
    assert transfer.get("destination_type") in VEC_TYPES, transfer
    assert transfer.get("argument_types") == [STRING_TYPE], transfer
    assert int(transfer.get("type_id") or 0) != 0, transfer
    assert int(transfer.get("module_id") or 0) != 0, transfer
    assert int(transfer.get("callsite") or 0) != 0, transfer
    assert_policy_and_cross_thread(transfer)
    assert transfer.get("semantic_scope_unwind_pop_inserted") is False, transfer

    string_rows = [
        row
        for row in applied_scope_rows(audit, STRING_FUNCTION, STRING_TYPE)
        if "with_capacity" in str(row.get("callee") or "")
    ]
    vec_rows = [
        row
        for row in applied_scope_rows(audit, VEC_FUNCTION, VEC_TYPES)
        if "with_capacity" in str(row.get("callee") or "")
    ]
    assert len(string_rows) == 1, string_rows
    assert len(vec_rows) == 1, vec_rows
    string_type_id = int(string_rows[0].get("type_id") or 0)
    vec_type_id = int(vec_rows[0].get("type_id") or 0)
    assert string_type_id != 0, string_rows[0]
    assert vec_type_id != 0, vec_rows[0]
    assert string_type_id != vec_type_id, (string_rows[0], vec_rows[0])
    for scope in (string_rows[0], vec_rows[0]):
        assert_policy_and_cross_thread(scope)
        assert scope.get("cross_thread_recovery_hint") is True, scope

    assert int(transfer.get("type_id") or 0) == vec_type_id, transfer
    preview = str(transfer.get("replacement_preview") or "")
    old_match = re.search(r"\bold_type_id=(\d+)\b", preview)
    new_match = re.search(r"\bnew_type_id=(\d+)\b", preview)
    assert old_match is not None, transfer
    assert new_match is not None, transfer
    assert int(old_match.group(1)) == string_type_id, transfer
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
        "payload_preserved",
        "pointer_preserved",
        "capacity_preserved",
        "wrong_string_non_reuse",
        "same_vec_reuse",
        "tag_record_cleanup_reuse",
    ):
        assert runtime[field] is True, (field, runtime)
    assert int(runtime["main_string_pointer"]) == int(runtime["worker_string_pointer"]), runtime
    assert int(runtime["worker_string_pointer"]) == int(runtime["bytes_pointer"]), runtime
    assert int(runtime["wrong_string_pointer"]) != int(runtime["bytes_pointer"]), runtime
    assert int(runtime["same_vec_pointer"]) == int(runtime["bytes_pointer"]), runtime
    assert int(runtime["cleanup_vec_pointer"]) == int(runtime["same_vec_pointer"]), runtime
    assert int(runtime["policy_flags_seen"]) & POLICY_FLAGS == POLICY_FLAGS, runtime
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
        "transfer_counts": {"candidates": transfer_candidate_count, "applied": transfer_applied_count},
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
                "flags",
                "placement_hint",
                "cross_thread_recovery_hint",
                "placement_hint_basis",
                "lowering_kind",
                "rewrite_status",
                "replacement_symbol",
                "replacement_resolution_status",
                "metadata_pairing_contract",
                "type_id_basis",
                "replacement_preview",
            )
        },
        "compiler_type_ids": {"string": string_type_id, "vec": vec_type_id},
        "scope_audit": {
            "string": {
                key: string_rows[0].get(key)
                for key in ("type_id", "module_id", "flags", "placement_hint", "cross_thread_recovery_hint")
            },
            "vec": {
                key: vec_rows[0].get(key)
                for key in ("type_id", "module_id", "flags", "placement_hint", "cross_thread_recovery_hint")
            },
        },
        "runtime": runtime,
        "tag_record_cleanup_contract": (
            "same cached pointer allocated again after tagged cross-thread drop; duplicate tag records "
            "fail closed in allocation finishing"
        ),
    }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--toolchain")
    return parser.parse_args()


def main() -> int:
    if not __debug__:
        raise SystemExit("do not run this assertion-based validator with python -O")
    args = parse_args()
    toolchain = args.toolchain or (ROOT / "rust-toolchain").read_text(encoding="utf-8").strip()
    rustc = shutil.which("rustc") or "rustc"
    cargo = shutil.which("cargo") or "cargo"
    sysroot = subprocess.check_output(
        [rustc, f"+{toolchain}", "--print", "sysroot"], cwd=ROOT, text=True
    ).strip()

    with tempfile.TemporaryDirectory(prefix="unialloc-cross-thread-memory-tagged-string-into-bytes-rebind-") as raw:
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
            run_env[variable] = f"{sysroot}/lib" + (os.pathsep + current if current else "")
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
                "UNIALLOC_LOWERING_POLICY_FLAGS": str(POLICY_FLAGS),
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
                "source": "mir_cross_thread_memory_tagged_string_into_bytes_rebind_probe",
                "validated": True,
                "single_run": True,
                "benchmark": False,
                "toolchain": toolchain,
                "policy_flags": POLICY_FLAGS,
                "placement_hint": CROSS_THREAD_RECOVERY,
                "evidence": evidence,
            },
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
