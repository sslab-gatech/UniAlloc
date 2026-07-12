#!/usr/bin/env python3
"""Prove Vec::into_iter transfers compiler-derived live owner identity."""

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
PROBE_NAME = "vec_into_iter_rebind_probe"
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
TYPE_ISOLATED = 1
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
        r'''use unialloc::{
    semantic_auto_metadata_disable, semantic_fallback_attribution_snapshot,
    semantic_metadata_validation_snapshot, semantic_ownership_transfer_snapshot,
    semantic_stats_recording_disable, semantic_stats_reset, semantic_stats_snapshot,
    type_isolation_side_cache_snapshot, UniAlloc,
};

#[global_allocator]
static ALLOCATOR: UniAlloc = UniAlloc;

const BYTES: usize = 256;

#[derive(Clone, Copy)]
struct TransferEvidence {
    vec_pointer: usize,
    iter_pointer: usize,
    first_clone_pointer: usize,
    second_clone_pointer: usize,
    payload_preserved: bool,
    pointer_preserved: bool,
    clone_allocations_distinct_from_live_source: bool,
    same_intoiter_reuse: bool,
}

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

#[inline(never)]
fn transfer_clone_and_implicit_drop(seed: u8) -> TransferEvidence {
    let value = make_vec(seed);
    assert_eq!(value.len(), BYTES);
    assert_eq!(value.capacity(), BYTES);
    assert!(payload_matches(&value, seed));
    let vec_pointer = value.as_ptr() as usize;

    // Ordinary Rust only: the compiler must rewrite this ownership transfer.
    let iter = vec_into_iter(value);
    let iter_pointer = iter.as_slice().as_ptr() as usize;
    let payload_preserved = payload_matches(iter.as_slice(), seed);
    let pointer_preserved = iter_pointer == vec_pointer;

    // IntoIter::clone is an ordinary target-identity allocation site. Its
    // first result is released into the IntoIter cache and the second clone
    // must recover that exact storage without aliasing the still-live source.
    let first_clone = clone_iter(&iter);
    assert!(payload_matches(first_clone.as_slice(), seed));
    let first_clone_pointer = first_clone.as_slice().as_ptr() as usize;
    drop(first_clone);

    let second_clone = clone_iter(&iter);
    assert!(payload_matches(second_clone.as_slice(), seed));
    let second_clone_pointer = second_clone.as_slice().as_ptr() as usize;
    let same_intoiter_reuse = second_clone_pointer == first_clone_pointer;
    let clone_allocations_distinct_from_live_source =
        first_clone_pointer != iter_pointer && second_clone_pointer != iter_pointer;
    drop(second_clone);

    let evidence = TransferEvidence {
        vec_pointer,
        iter_pointer,
        first_clone_pointer,
        second_clone_pointer,
        payload_preserved,
        pointer_preserved,
        clone_allocations_distinct_from_live_source,
        same_intoiter_reuse,
    };

    // `iter` is intentionally dropped implicitly here. The compiler-emitted
    // Drop scope selects IntoIter identity; a stale Vec recovery record would
    // therefore be observable as one recovery-identity mismatch.
    evidence
}

fn main() {
    semantic_auto_metadata_disable();
    semantic_stats_reset();
    let transfer_before = semantic_ownership_transfer_snapshot();

    let evidence = transfer_clone_and_implicit_drop(0x31);

    // The transferred storage and both clone buffers are now target-owned.
    // A new Vec with the same layout must not reuse any of them.
    let wrong_vec = make_vec(0x73);
    let wrong_vec_pointer = wrong_vec.as_ptr() as usize;
    let wrong_vec_non_reuse = wrong_vec_pointer != evidence.iter_pointer
        && wrong_vec_pointer != evidence.first_clone_pointer
        && wrong_vec_pointer != evidence.second_clone_pointer;
    assert!(payload_matches(&wrong_vec, 0x73));

    let transfer_after = semantic_ownership_transfer_snapshot();
    let stats = semantic_stats_snapshot();
    let fallback = semantic_fallback_attribution_snapshot();
    let validation = semantic_metadata_validation_snapshot();
    let side_cache = type_isolation_side_cache_snapshot();
    semantic_stats_recording_disable();

    println!(
        concat!(
            "{{",
            "\"source\":\"vec_into_iter_rebind_probe\",",
            "\"bytes\":{},",
            "\"vec_pointer\":{},",
            "\"iter_pointer\":{},",
            "\"first_clone_pointer\":{},",
            "\"second_clone_pointer\":{},",
            "\"wrong_vec_pointer\":{},",
            "\"payload_preserved\":{},",
            "\"pointer_preserved\":{},",
            "\"clone_allocations_distinct_from_live_source\":{},",
            "\"wrong_vec_non_reuse\":{},",
            "\"same_intoiter_reuse\":{},",
            "\"transfer_attempted\":{},",
            "\"transfer_applied\":{},",
            "\"transfer_rejected\":{},",
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
        evidence.vec_pointer,
        evidence.iter_pointer,
        evidence.first_clone_pointer,
        evidence.second_clone_pointer,
        wrong_vec_pointer,
        evidence.payload_preserved,
        evidence.pointer_preserved,
        evidence.clone_allocations_distinct_from_live_source,
        wrong_vec_non_reuse,
        evidence.same_intoiter_reuse,
        transfer_after.attempted.saturating_sub(transfer_before.attempted),
        transfer_after.applied.saturating_sub(transfer_before.applied),
        transfer_after.rejected.saturating_sub(transfer_before.rejected),
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
    );

    drop(wrong_vec);
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
    assert transfer.get("replacement_resolution_status") == TRANSFER_RESOLUTION_STATUS, transfer
    assert transfer.get("metadata_pairing_contract") == TRANSFER_PAIRING, transfer
    assert transfer.get("type_id_basis") == TRANSFER_TYPE_ID_BASIS, transfer
    assert transfer.get("semantic_object_type") == INTO_ITER_TYPE, transfer
    assert transfer.get("destination_type") == INTO_ITER_TYPE, transfer
    assert transfer.get("argument_types") == [VEC_TYPE], transfer
    assert int(transfer.get("type_id") or 0) != 0, transfer
    assert int(transfer.get("module_id") or 0) != 0, transfer
    assert int(transfer.get("callsite") or 0) != 0, transfer
    assert int(transfer.get("flags") or 0) & TYPE_ISOLATED, transfer
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
        "payload_preserved",
        "pointer_preserved",
        "clone_allocations_distinct_from_live_source",
        "wrong_vec_non_reuse",
        "same_intoiter_reuse",
    ):
        assert runtime[field] is True, (field, runtime)
    assert int(runtime["vec_pointer"]) == int(runtime["iter_pointer"]), runtime
    assert int(runtime["wrong_vec_pointer"]) != int(runtime["iter_pointer"]), runtime
    assert int(runtime["first_clone_pointer"]) == int(
        runtime["second_clone_pointer"]
    ), runtime
    assert int(runtime["first_clone_pointer"]) != int(runtime["iter_pointer"]), runtime
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
    assert int(runtime["typed_cache_hits"]) >= 1, runtime
    assert int(runtime["typed_cache_inserts"]) >= 1, runtime

    return {
        "transfer_counts": {
            "candidates": transfer_candidate_count,
            "applied": transfer_applied_count,
        },
        "transfer_audit": transfer,
        "compiler_type_ids": {"vec": vec_type_id, "into_iter": into_iter_type_id},
        "runtime": runtime,
    }


def main() -> int:
    toolchain = (ROOT / "rust-toolchain").read_text(encoding="utf-8").strip()
    rustc = shutil.which("rustc") or "rustc"
    cargo = shutil.which("cargo") or "cargo"
    sysroot = subprocess.check_output(
        [rustc, f"+{toolchain}", "--print", "sysroot"], cwd=ROOT, text=True
    ).strip()

    with tempfile.TemporaryDirectory(prefix="unialloc-vec-into-iter-rebind-") as raw:
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
                "source": "mir_vec_into_iter_rebind_probe",
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
