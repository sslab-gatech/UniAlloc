#!/usr/bin/env python3
"""Prove Vec::<u8>::from(String) transfers compiler-derived cache ownership."""

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
PROBE_NAME = "vec_from_string_rebind_probe"
STRING_FUNCTION = "make_string"
VEC_FUNCTION = "make_vec"
TRANSFER_FUNCTION = "vec_from_string"
REF_CONTROL_FUNCTION = "vec_from_string_ref"
GENERIC_CONTROL_FUNCTION = "generic_into_vec"
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
# Keep the transient probe resolvable by nightly-2022-07-01; UniAlloc's loose
# lock_api requirement would otherwise select a release requiring rustc 1.71.
lock_api = "=0.4.3"
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
fn make_string(seed: u8) -> String {
    let mut value = String::with_capacity(BYTES);
    for index in 0..BYTES {
        value.push(payload_byte(seed, index) as char);
    }
    value
}

#[inline(never)]
fn vec_from_string(value: String) -> Vec<u8> {
    Vec::<u8>::from(value)
}

#[inline(never)]
fn vec_from_string_ref(value: &String) -> Vec<u8> {
    Vec::<u8>::from(value.as_bytes())
}

#[inline(never)]
fn generic_into_vec<T: Into<Vec<u8>>>(value: T) -> Vec<u8> {
    value.into()
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

fn main() {
    // Ordinary Rust application: successful type isolation must come from the
    // compiler pass rewriting exact Vec::<u8>::from(String), not hand-written metadata.
    semantic_auto_metadata_disable();
    semantic_stats_reset();
    let transfer_before = semantic_ownership_transfer_snapshot();

    let string = make_string(0x31);
    assert_eq!(string.len(), BYTES);
    assert_eq!(string.capacity(), BYTES);
    let string_pointer = string.as_ptr() as usize;

    let bytes = vec_from_string(string);
    let bytes_pointer = bytes.as_ptr() as usize;
    let payload_preserved = payload_matches(&bytes, 0x31);
    let pointer_preserved = bytes_pointer == string_pointer;
    let capacity_preserved = bytes.capacity() == BYTES;
    drop(bytes);

    let wrong_string = make_string(0x53);
    let wrong_string_pointer = wrong_string.as_ptr() as usize;
    let wrong_string_non_reuse = wrong_string_pointer != bytes_pointer;
    drop(wrong_string);

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

    // Keep the exact-From negative controls in codegen without allowing their
    // intentionally unsupported conversions to affect the measured transfer.
    let ref_source = String::from("reference-control");
    assert_eq!(
        vec_from_string_ref(&ref_source),
        b"reference-control".to_vec()
    );
    let generic_control = generic_into_vec(String::from("generic-control"));
    assert_eq!(generic_control, b"generic-control".to_vec());

    println!(
        concat!(
            "{{",
            "\"source\":\"vec_from_string_rebind_probe\",",
            "\"bytes\":{},",
            "\"string_pointer\":{},",
            "\"bytes_pointer\":{},",
            "\"wrong_string_pointer\":{},",
            "\"same_vec_pointer\":{},",
            "\"payload_preserved\":{},",
            "\"pointer_preserved\":{},",
            "\"capacity_preserved\":{},",
            "\"wrong_string_non_reuse\":{},",
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
        string_pointer,
        bytes_pointer,
        wrong_string_pointer,
        same_vec_pointer,
        payload_preserved,
        pointer_preserved,
        capacity_preserved,
        wrong_string_non_reuse,
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
    audit: dict[str, object], function_name: str, semantic_type: set[str]
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
        and row.get("semantic_object_type") in semantic_type
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
        f"expected 1 Vec::<u8>::from(String) ownership-transfer candidate, got {len(transfer_rows)}",
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
    callee = str(transfer.get("callee") or "")
    current_exact_from = re.search(
        r"~ core(?:\[[^]]+\])?::convert::From::from\)", callee
    ) and re.search(
        r"\[std::vec::Vec<u8, std::alloc::Global>, std::string::String\]\)\)$",
        callee,
    )
    legacy_exact_from = re.search(
        r"fn\(std::string::String\) -> std::vec::Vec<u8> "
        r"\{<std::vec::Vec<u8> as std::convert::From<std::string::String>>::from\}\)$",
        callee,
    )
    assert current_exact_from or legacy_exact_from, transfer
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
    assert int(transfer.get("flags") or 0) & TYPE_ISOLATED, transfer
    assert transfer.get("semantic_scope_unwind_pop_inserted") is False, transfer

    string_rows = [
        row
        for row in applied_scope_rows(audit, STRING_FUNCTION, {STRING_TYPE})
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
    fail_closed_controls: dict[str, list[str]] = {}
    for function_name in (REF_CONTROL_FUNCTION, GENERIC_CONTROL_FUNCTION):
        control_rows = [
            row
            for row in rows
            if isinstance(row, dict) and function_matches(row, function_name)
        ]
        assert control_rows, f"missing fail-closed control audit for {function_name}"
        control_transfers = [
            row
            for row in control_rows
            if row.get("lowering_kind") == TRANSFER_LOWERING_KIND
        ]
        assert control_transfers == [], control_transfers
        fail_closed_controls[function_name] = sorted(
            {str(row.get("rewrite_status") or "") for row in control_rows}
        )

    runtime = load_runtime(stdout)
    assert int(runtime["bytes"]) == BYTES, runtime
    for field in (
        "payload_preserved",
        "pointer_preserved",
        "capacity_preserved",
        "wrong_string_non_reuse",
        "same_vec_reuse",
    ):
        assert runtime[field] is True, (field, runtime)
    assert int(runtime["string_pointer"]) == int(runtime["bytes_pointer"]), runtime
    assert int(runtime["wrong_string_pointer"]) != int(runtime["bytes_pointer"]), runtime
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
        "compiler_type_ids": {"string": string_type_id, "vec": vec_type_id},
        "fail_closed_controls": fail_closed_controls,
        "runtime": runtime,
    }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--toolchain")
    return parser.parse_args()


def main() -> int:
    if not __debug__:
        raise SystemExit("do not run this assertion-based validator with python -O")
    args = parse_args()
    toolchain = args.toolchain or (ROOT / "rust-toolchain").read_text(
        encoding="utf-8"
    ).strip()
    rustc = shutil.which("rustc") or "rustc"
    cargo = shutil.which("cargo") or "cargo"
    sysroot = subprocess.check_output(
        [rustc, f"+{toolchain}", "--print", "sysroot"], cwd=ROOT, text=True
    ).strip()

    with tempfile.TemporaryDirectory(prefix="unialloc-vec-from-string-rebind-") as raw:
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
                "source": "mir_vec_from_string_rebind_probe",
                "validated": True,
                "single_run": True,
                "benchmark": False,
                "toolchain": toolchain,
                "evidence": evidence,
            },
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
