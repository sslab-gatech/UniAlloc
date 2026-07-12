#!/usr/bin/env python3
"""Prove CString::into_bytes_with_nul transfers compiler-derived cache ownership."""

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
PROBE_NAME = "cstring_into_bytes_with_nul_rebind_probe"
CSTRING_FUNCTION = "make_cstring"
VEC_FUNCTION = "make_vec"
TRANSFER_FUNCTION = "cstring_into_bytes_with_nul"
CSTRING_TYPE = "std::ffi::CString"
VEC_TYPE = "std::vec::Vec<u8, std::alloc::Global>"
APPLIED_SCOPE_STATUS = "actual_semantic_scope_enter_exit_rewrite_applied"
TRANSFER_LOWERING_KIND = "semantic_ownership_transfer_rewrite"
TRANSFER_STATUS = "actual_semantic_ownership_transfer_rewrite_applied"
TRANSFER_SYMBOL = "__unialloc_semantic_cstring_into_bytes_with_nul"
TRANSFER_RESOLUTION_STATUS = "resolved_unialloc_semantic_cstring_into_bytes_with_nul"
TRANSFER_PAIRING = "pointer_preserving_owner_identity_rebind"
TRANSFER_TYPE_ID_BASIS = "rustc_middle_exact_cstring_into_bytes_with_nul_owner_transfer"
TYPE_ISOLATED = 1
BYTES = 257
ALLOCATION_BYTES = BYTES + 1


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
        r'''use std::ffi::CString;
use unialloc::{
    semantic_auto_metadata_disable, semantic_fallback_attribution_snapshot,
    semantic_metadata_validation_snapshot, semantic_ownership_transfer_snapshot,
    semantic_stats_recording_disable, semantic_stats_reset, semantic_stats_snapshot,
    type_isolation_side_cache_snapshot, UniAlloc,
};

#[global_allocator]
static ALLOCATOR: UniAlloc = UniAlloc;

const BYTES: usize = 257;
const ALLOCATION_BYTES: usize = BYTES + 1;

#[inline(never)]
fn payload_byte(seed: u8, index: usize) -> u8 {
    1 + seed.wrapping_add(index as u8).wrapping_mul(13) % 254
}

#[inline(never)]
fn make_cstring(seed: u8) -> CString {
    let mut payload = [0_u8; BYTES];
    for (index, byte) in payload.iter_mut().enumerate() {
        *byte = payload_byte(seed, index);
    }
    CString::new(&payload[..]).expect("generated payload has no nul bytes")
}

#[inline(never)]
fn cstring_into_bytes_with_nul(value: CString) -> Vec<u8> {
    value.into_bytes_with_nul()
}

#[inline(never)]
fn make_vec() -> Vec<u8> {
    Vec::<u8>::with_capacity(ALLOCATION_BYTES)
}

#[inline(never)]
fn payload_matches(bytes: &[u8], seed: u8) -> bool {
    bytes.len() == ALLOCATION_BYTES
        && bytes[BYTES] == 0
        && bytes[..BYTES]
            .iter()
            .enumerate()
            .all(|(index, byte)| *byte == payload_byte(seed, index))
}

fn main() {
    // Ordinary Rust application: successful type isolation must come from the
    // compiler pass rewriting CString::into_bytes_with_nul, not hand-written metadata.
    semantic_auto_metadata_disable();
    semantic_stats_reset();
    let transfer_before = semantic_ownership_transfer_snapshot();

    let cstring = make_cstring(0x31);
    assert_eq!(cstring.as_bytes().len(), BYTES);
    assert_eq!(cstring.as_bytes_with_nul().len(), ALLOCATION_BYTES);
    let cstring_pointer = cstring.as_ptr() as usize;

    let bytes = cstring_into_bytes_with_nul(cstring);
    let bytes_pointer = bytes.as_ptr() as usize;
    let payload_preserved = payload_matches(&bytes, 0x31);
    let pointer_preserved = bytes_pointer == cstring_pointer;
    let capacity_preserved = bytes.capacity() == ALLOCATION_BYTES;
    drop(bytes);

    let wrong_cstring = make_cstring(0x53);
    let wrong_cstring_pointer = wrong_cstring.as_ptr() as usize;
    let wrong_cstring_non_reuse = wrong_cstring_pointer != bytes_pointer;
    drop(wrong_cstring);

    let same_vec = make_vec();
    let same_vec_pointer = same_vec.as_ptr() as usize;
    let same_vec_reuse = same_vec_pointer == bytes_pointer;
    drop(same_vec);

    let transfer_after = semantic_ownership_transfer_snapshot();
    let stats = semantic_stats_snapshot();
    let fallback = semantic_fallback_attribution_snapshot();
    let validation = semantic_metadata_validation_snapshot();
    let side_cache = type_isolation_side_cache_snapshot();
    semantic_stats_recording_disable();

    println!(
        concat!(
            "{{",
            "\"source\":\"cstring_into_bytes_with_nul_rebind_probe\",",
            "\"bytes\":{},",
            "\"cstring_pointer\":{},",
            "\"bytes_pointer\":{},",
            "\"wrong_cstring_pointer\":{},",
            "\"same_vec_pointer\":{},",
            "\"payload_preserved\":{},",
            "\"pointer_preserved\":{},",
            "\"capacity_preserved\":{},",
            "\"wrong_cstring_non_reuse\":{},",
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
        ALLOCATION_BYTES,
        cstring_pointer,
        bytes_pointer,
        wrong_cstring_pointer,
        same_vec_pointer,
        payload_preserved,
        pointer_preserved,
        capacity_preserved,
        wrong_cstring_non_reuse,
        same_vec_reuse,
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
    );
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
        f"expected 1 CString::into_bytes_with_nul ownership-transfer candidate, got {len(transfer_rows)}",
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
        r"~ alloc(?:\[[^]]+\])?::ffi::c_str::(?:CString|\{impl#\d+\})::into_bytes_with_nul\)",
        str(transfer.get("callee") or ""),
    ), transfer
    assert transfer.get("rewrite_status") == TRANSFER_STATUS, transfer
    assert transfer.get("replacement_symbol") == TRANSFER_SYMBOL, transfer
    assert transfer.get("replacement_resolution_status") == TRANSFER_RESOLUTION_STATUS, transfer
    assert transfer.get("metadata_pairing_contract") == TRANSFER_PAIRING, transfer
    assert transfer.get("type_id_basis") == TRANSFER_TYPE_ID_BASIS, transfer
    assert transfer.get("semantic_object_type") == VEC_TYPE, transfer
    assert transfer.get("destination_type") == VEC_TYPE, transfer
    assert transfer.get("argument_types") == [CSTRING_TYPE], transfer
    assert int(transfer.get("type_id") or 0) != 0, transfer
    assert int(transfer.get("module_id") or 0) != 0, transfer
    assert int(transfer.get("callsite") or 0) != 0, transfer
    assert int(transfer.get("flags") or 0) & TYPE_ISOLATED, transfer
    assert transfer.get("semantic_scope_unwind_pop_inserted") is False, transfer

    cstring_rows = [
        row
        for row in applied_scope_rows(audit, "main", CSTRING_TYPE)
        if CSTRING_FUNCTION in str(row.get("callee") or "")
    ]
    vec_rows = [
        row
        for row in applied_scope_rows(audit, VEC_FUNCTION, VEC_TYPE)
        if "with_capacity" in str(row.get("callee") or "")
    ]
    assert len(cstring_rows) == 2, cstring_rows
    assert len(vec_rows) == 1, vec_rows
    cstring_type_id = int(cstring_rows[0].get("type_id") or 0)
    assert all(int(row.get("type_id") or 0) == cstring_type_id for row in cstring_rows), (
        cstring_rows
    )
    vec_type_id = int(vec_rows[0].get("type_id") or 0)
    assert cstring_type_id != 0, cstring_rows[0]
    assert vec_type_id != 0, vec_rows[0]
    assert cstring_type_id != vec_type_id, (cstring_rows[0], vec_rows[0])

    assert int(transfer.get("type_id") or 0) == vec_type_id, transfer
    preview = str(transfer.get("replacement_preview") or "")
    old_match = re.search(r"\bold_type_id=(\d+)\b", preview)
    new_match = re.search(r"\bnew_type_id=(\d+)\b", preview)
    assert old_match is not None, transfer
    assert new_match is not None, transfer
    assert int(old_match.group(1)) == cstring_type_id, transfer
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
    assert int(runtime["bytes"]) == ALLOCATION_BYTES, runtime
    for field in (
        "payload_preserved",
        "pointer_preserved",
        "capacity_preserved",
        "wrong_cstring_non_reuse",
        "same_vec_reuse",
    ):
        assert runtime[field] is True, (field, runtime)
    assert int(runtime["cstring_pointer"]) == int(runtime["bytes_pointer"]), runtime
    assert int(runtime["wrong_cstring_pointer"]) != int(runtime["bytes_pointer"]), runtime
    assert int(runtime["same_vec_pointer"]) == int(runtime["bytes_pointer"]), runtime
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
        "transfer_audit": transfer,
        "compiler_type_ids": {"cstring": cstring_type_id, "vec": vec_type_id},
        "runtime": runtime,
    }


def main() -> int:
    toolchain = (ROOT / "rust-toolchain").read_text(encoding="utf-8").strip()
    rustc = shutil.which("rustc") or "rustc"
    cargo = shutil.which("cargo") or "cargo"
    sysroot = subprocess.check_output(
        [rustc, f"+{toolchain}", "--print", "sysroot"], cwd=ROOT, text=True
    ).strip()

    with tempfile.TemporaryDirectory(prefix="unialloc-cstring-into-bytes-with-nul-rebind-") as raw:
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
                "source": "mir_cstring_into_bytes_with_nul_rebind_probe",
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
