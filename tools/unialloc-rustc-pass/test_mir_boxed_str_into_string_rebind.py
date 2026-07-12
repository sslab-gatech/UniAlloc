#!/usr/bin/env python3
"""Prove Box<str>::into_string transfers compiler-derived cache ownership."""

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
PROBE_NAME = "boxed_str_into_string_rebind_probe"
BOX_FUNCTION = "make_boxed_str"
STRING_FUNCTION = "make_string"
TRANSFER_FUNCTION = "boxed_str_into_string"
BOX_TYPE = "std::boxed::Box<str, std::alloc::Global>"
STRING_TYPE = "std::string::String"
APPLIED_SCOPE_STATUS = "actual_semantic_scope_enter_exit_rewrite_applied"
TRANSFER_LOWERING_KIND = "semantic_ownership_transfer_rewrite"
TRANSFER_STATUS = "actual_semantic_ownership_transfer_rewrite_applied"
TRANSFER_SYMBOL = "__unialloc_semantic_boxed_str_into_string"
TRANSFER_RESOLUTION_STATUS = "resolved_unialloc_semantic_boxed_str_into_string"
TRANSFER_PAIRING = "pointer_preserving_owner_identity_rebind"
TRANSFER_TYPE_ID_BASIS = "rustc_middle_exact_boxed_str_into_string_owner_transfer"
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

#[inline(never)]
fn payload_byte(seed: u8, index: usize) -> u8 {
    b'a' + ((seed.wrapping_add(index as u8).wrapping_mul(13)) % 26)
}

#[inline(never)]
fn make_boxed_str(seed: u8) -> Box<str> {
    let mut payload = [0_u8; BYTES];
    let mut index = 0usize;
    while index < BYTES {
        payload[index] = payload_byte(seed, index);
        index += 1;
    }
    Box::<str>::from(std::str::from_utf8(&payload).expect("ASCII payload"))
}

#[inline(never)]
fn boxed_str_into_string(value: Box<str>) -> String {
    value.into_string()
}

#[inline(never)]
fn make_string(seed: u8) -> String {
    let mut value = String::with_capacity(BYTES);
    let mut index = 0usize;
    while index < BYTES {
        value.push(payload_byte(seed, index) as char);
        index += 1;
    }
    value
}

#[inline(never)]
fn payload_matches(value: &str, seed: u8) -> bool {
    value.len() == BYTES
        && value
            .as_bytes()
            .iter()
            .enumerate()
            .all(|(index, byte)| *byte == payload_byte(seed, index))
}

fn main() {
    // Ordinary Rust only: successful identity transfer must come from the MIR
    // pass rewriting Box<str>::into_string, not hand-written metadata.
    semantic_auto_metadata_disable();
    semantic_stats_reset();
    let transfer_before = semantic_ownership_transfer_snapshot();

    let boxed = make_boxed_str(0x31);
    let boxed_pointer = boxed.as_ptr() as usize;
    assert!(payload_matches(&boxed, 0x31));

    let string = boxed_str_into_string(boxed);
    let string_pointer = string.as_ptr() as usize;
    let payload_preserved = payload_matches(&string, 0x31);
    let pointer_preserved = string_pointer == boxed_pointer;
    let capacity_preserved = string.capacity() == BYTES;
    drop(string);

    let wrong_boxed = make_boxed_str(0x53);
    let wrong_boxed_pointer = wrong_boxed.as_ptr() as usize;
    let source_non_reuse = wrong_boxed_pointer != string_pointer;
    drop(wrong_boxed);

    let same_string = make_string(0x71);
    let same_string_pointer = same_string.as_ptr() as usize;
    let target_reuse = same_string_pointer == string_pointer;
    drop(same_string);

    let transfer_after = semantic_ownership_transfer_snapshot();
    let stats = semantic_stats_snapshot();
    let fallback = semantic_fallback_attribution_snapshot();
    let validation = semantic_metadata_validation_snapshot();
    let side_cache = type_isolation_side_cache_snapshot();
    semantic_stats_recording_disable();

    println!(
        concat!(
            "{{",
            "\"source\":\"boxed_str_into_string_rebind_probe\",",
            "\"bytes\":{},",
            "\"boxed_pointer\":{},",
            "\"string_pointer\":{},",
            "\"wrong_boxed_pointer\":{},",
            "\"same_string_pointer\":{},",
            "\"payload_preserved\":{},",
            "\"pointer_preserved\":{},",
            "\"capacity_preserved\":{},",
            "\"source_non_reuse\":{},",
            "\"target_reuse\":{},",
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
        boxed_pointer,
        string_pointer,
        wrong_boxed_pointer,
        same_string_pointer,
        payload_preserved,
        pointer_preserved,
        capacity_preserved,
        source_non_reuse,
        target_reuse,
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
    assert len(transfer_rows) == 1, (
        f"expected 1 Box<str>::into_string ownership-transfer candidate, got {len(transfer_rows)}",
        summary,
        transfer_rows,
    )
    candidate_count = int(summary.get("semantic_ownership_transfer_candidate_count") or 0)
    applied_count = int(summary.get("semantic_ownership_transfer_rewrite_applied_count") or 0)
    assert candidate_count == 1, summary
    assert applied_count == 1, summary

    transfer = transfer_rows[0]
    assert re.search(
        r"~ alloc(?:\[[^]]+\])?::str::(?:<impl str>|\{impl#\d+\})::into_string\)",
        str(transfer.get("callee") or ""),
    ), transfer
    assert transfer.get("rewrite_status") == TRANSFER_STATUS, transfer
    assert transfer.get("replacement_symbol") == TRANSFER_SYMBOL, transfer
    assert transfer.get("replacement_resolution_status") == TRANSFER_RESOLUTION_STATUS, transfer
    assert transfer.get("metadata_pairing_contract") == TRANSFER_PAIRING, transfer
    assert transfer.get("type_id_basis") == TRANSFER_TYPE_ID_BASIS, transfer
    assert transfer.get("semantic_object_type") == STRING_TYPE, transfer
    assert transfer.get("destination_type") == STRING_TYPE, transfer
    assert transfer.get("argument_types") == [BOX_TYPE], transfer
    assert int(transfer.get("type_id") or 0) != 0, transfer
    assert int(transfer.get("module_id") or 0) != 0, transfer
    assert int(transfer.get("callsite") or 0) != 0, transfer
    assert int(transfer.get("flags") or 0) & TYPE_ISOLATED, transfer
    assert transfer.get("semantic_scope_unwind_pop_inserted") is False, transfer

    box_rows = applied_scope_rows(audit, BOX_FUNCTION, BOX_TYPE)
    string_rows = [
        row
        for row in applied_scope_rows(audit, STRING_FUNCTION, STRING_TYPE)
        if "with_capacity" in str(row.get("callee") or "")
    ]
    assert len(box_rows) == 1, box_rows
    assert len(string_rows) == 1, string_rows
    box_type_id = int(box_rows[0].get("type_id") or 0)
    string_type_id = int(string_rows[0].get("type_id") or 0)
    assert box_type_id != 0, box_rows[0]
    assert string_type_id != 0, string_rows[0]
    assert box_type_id != string_type_id, (box_rows[0], string_rows[0])
    assert int(transfer.get("type_id") or 0) == string_type_id, transfer
    preview = str(transfer.get("replacement_preview") or "")
    old_match = re.search(r"\bold_type_id=(\d+)\b", preview)
    new_match = re.search(r"\bnew_type_id=(\d+)\b", preview)
    assert old_match is not None, transfer
    assert new_match is not None, transfer
    assert int(old_match.group(1)) == box_type_id, transfer
    assert int(new_match.group(1)) == string_type_id, transfer

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
        "capacity_preserved",
        "source_non_reuse",
        "target_reuse",
    ):
        assert runtime[field] is True, (field, runtime)
    assert int(runtime["boxed_pointer"]) == int(runtime["string_pointer"]), runtime
    assert int(runtime["wrong_boxed_pointer"]) != int(runtime["string_pointer"]), runtime
    assert int(runtime["same_string_pointer"]) == int(runtime["string_pointer"]), runtime
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
        "transfer_counts": {"candidates": candidate_count, "applied": applied_count},
        "transfer_audit": transfer,
        "compiler_type_ids": {"boxed_str": box_type_id, "string": string_type_id},
        "runtime": runtime,
    }


def main() -> int:
    toolchain = (ROOT / "rust-toolchain").read_text(encoding="utf-8").strip()
    rustc = shutil.which("rustc") or "rustc"
    cargo = shutil.which("cargo") or "cargo"
    sysroot = subprocess.check_output(
        [rustc, f"+{toolchain}", "--print", "sysroot"], cwd=ROOT, text=True
    ).strip()

    with tempfile.TemporaryDirectory(prefix="unialloc-boxed-str-into-string-rebind-") as raw:
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
                "source": "mir_boxed_str_into_string_rebind_probe",
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
