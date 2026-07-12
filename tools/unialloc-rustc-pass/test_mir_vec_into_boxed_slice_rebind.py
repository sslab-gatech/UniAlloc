#!/usr/bin/env python3
"""Prove Vec::into_boxed_slice transfers compiler-derived cache ownership."""

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
PROBE_NAME = "vec_into_boxed_slice_rebind_probe"
VEC_FUNCTION = "make_vec_with_capacity"
BOX_FUNCTION = "make_box"
TRANSFER_FUNCTIONS = (
    "vec_into_boxed_slice_exact",
    "vec_into_boxed_slice_spare",
)
VEC_TYPE = "std::vec::Vec<u8, std::alloc::Global>"
BOX_TYPE = "std::boxed::Box<[u8], std::alloc::Global>"
APPLIED_SCOPE_STATUS = "actual_semantic_scope_enter_exit_rewrite_applied"
TRANSFER_LOWERING_KIND = "semantic_ownership_transfer_rewrite"
TRANSFER_STATUS = "actual_semantic_ownership_transfer_rewrite_applied"
TRANSFER_SYMBOL = "__unialloc_semantic_vec_into_boxed_slice"
TRANSFER_RESOLUTION_STATUS = "resolved_unialloc_semantic_vec_into_boxed_slice"
TRANSFER_PAIRING = "shrink_aware_owner_identity_transfer"
TRANSFER_TYPE_ID_BASIS = "rustc_middle_exact_vec_into_boxed_slice_owner_transfer"
TYPE_ISOLATED = 1
BYTES = 256
SPARE_CAPACITY = 512


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
    type_isolation_side_cache_snapshot, SemanticStatsSnapshot, UniAlloc,
};

#[global_allocator]
static ALLOCATOR: UniAlloc = UniAlloc;

const BYTES: usize = 256;
const SPARE_CAPACITY: usize = 512;

#[inline(never)]
fn payload_byte(seed: u8, index: usize) -> u8 {
    seed.wrapping_add((index as u8).wrapping_mul(17)) ^ (index as u8).rotate_left(3)
}

#[inline(never)]
fn make_vec_with_capacity(capacity: usize, seed: u8) -> Vec<u8> {
    let mut value = Vec::<u8>::with_capacity(capacity);
    for index in 0..BYTES {
        value.push(payload_byte(seed, index));
    }
    value
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
fn vec_into_boxed_slice_exact(value: Vec<u8>) -> Box<[u8]> {
    value.into_boxed_slice()
}

#[inline(never)]
fn vec_into_boxed_slice_spare(value: Vec<u8>) -> Box<[u8]> {
    value.into_boxed_slice()
}

#[inline(never)]
fn payload_matches(bytes: &[u8], seed: u8) -> bool {
    bytes.len() == BYTES
        && bytes
            .iter()
            .enumerate()
            .all(|(index, byte)| *byte == payload_byte(seed, index))
}

fn no_allocation_lifecycle_event(
    before: SemanticStatsSnapshot,
    after: SemanticStatsSnapshot,
) -> bool {
    after.total_allocations == before.total_allocations
        && after.typed_allocations == before.typed_allocations
        && after.fallback_allocations == before.fallback_allocations
        && after.total_allocated_bytes == before.total_allocated_bytes
        && after.typed_allocated_bytes == before.typed_allocated_bytes
        && after.fallback_allocated_bytes == before.fallback_allocated_bytes
        && after.total_deallocations == before.total_deallocations
        && after.typed_deallocations == before.typed_deallocations
        && after.fallback_deallocations == before.fallback_deallocations
        && after.typed_cache_hits == before.typed_cache_hits
        && after.typed_cache_inserts == before.typed_cache_inserts
        && after.typed_cache_bypasses == before.typed_cache_bypasses
}

fn main() {
    // This is an ordinary Rust application. The probe deliberately supplies no
    // allocation metadata ABI: successful isolation must come from actual MIR
    // rewriting of Vec::into_boxed_slice on both standard-library paths.
    semantic_auto_metadata_disable();
    semantic_stats_reset();
    let transfer_before = semantic_ownership_transfer_snapshot();

    let exact_vec = make_vec_with_capacity(BYTES, 0x31);
    assert_eq!(exact_vec.len(), BYTES);
    assert_eq!(exact_vec.capacity(), BYTES);
    assert!(payload_matches(&exact_vec, 0x31));
    let exact_vec_pointer = exact_vec.as_ptr() as usize;
    let exact_stats_before = semantic_stats_snapshot();
    let exact_box = vec_into_boxed_slice_exact(exact_vec);
    let exact_stats_after = semantic_stats_snapshot();
    let exact_box_pointer = exact_box.as_ptr() as usize;
    let exact_payload_preserved = payload_matches(&exact_box, 0x31);
    let exact_pointer_preserved = exact_box_pointer == exact_vec_pointer;
    let exact_no_lifecycle = no_allocation_lifecycle_event(exact_stats_before, exact_stats_after);
    assert!(exact_payload_preserved);
    assert!(exact_pointer_preserved);
    assert!(exact_no_lifecycle);
    drop(exact_box);

    let exact_wrong_vec = make_vec_with_capacity(BYTES, 0x53);
    let exact_wrong_vec_pointer = exact_wrong_vec.as_ptr() as usize;
    let exact_same_box = make_box(0x75);
    let exact_same_box_pointer = exact_same_box.as_ptr() as usize;
    let exact_wrong_vec_non_reuse = exact_wrong_vec_pointer != exact_box_pointer;
    let exact_same_box_reuse = exact_same_box_pointer == exact_box_pointer;
    drop(exact_same_box);
    drop(exact_wrong_vec);

    let spare_vec = make_vec_with_capacity(SPARE_CAPACITY, 0x97);
    assert_eq!(spare_vec.len(), BYTES);
    assert_eq!(spare_vec.capacity(), SPARE_CAPACITY);
    assert!(payload_matches(&spare_vec, 0x97));
    let spare_vec_pointer = spare_vec.as_ptr() as usize;
    let spare_box = vec_into_boxed_slice_spare(spare_vec);
    let spare_box_pointer = spare_box.as_ptr() as usize;
    let spare_payload_preserved = payload_matches(&spare_box, 0x97);
    let spare_pointer_moved = spare_box_pointer != spare_vec_pointer;
    assert!(spare_payload_preserved);
    drop(spare_box);

    let spare_wrong_vec = make_vec_with_capacity(BYTES, 0xb9);
    let spare_wrong_vec_pointer = spare_wrong_vec.as_ptr() as usize;
    let spare_same_box = make_box(0xdb);
    let spare_same_box_pointer = spare_same_box.as_ptr() as usize;
    let spare_recovered_vec = make_vec_with_capacity(SPARE_CAPACITY, 0xfd);
    let spare_recovered_vec_pointer = spare_recovered_vec.as_ptr() as usize;
    let spare_wrong_vec_non_reuse = spare_wrong_vec_pointer != spare_box_pointer;
    let spare_same_box_reuse = spare_same_box_pointer == spare_box_pointer;
    let spare_old_vec_reuse = spare_recovered_vec_pointer == spare_vec_pointer;
    drop(spare_recovered_vec);
    drop(spare_same_box);
    drop(spare_wrong_vec);

    let transfer_after = semantic_ownership_transfer_snapshot();
    let stats = semantic_stats_snapshot();
    let fallback = semantic_fallback_attribution_snapshot();
    let validation = semantic_metadata_validation_snapshot();
    let side_cache = type_isolation_side_cache_snapshot();
    semantic_stats_recording_disable();

    println!(
        concat!(
            "{{",
            "\"source\":\"vec_into_boxed_slice_rebind_probe\",",
            "\"bytes\":{},",
            "\"spare_capacity\":{},",
            "\"exact_vec_pointer\":{},",
            "\"exact_box_pointer\":{},",
            "\"exact_wrong_vec_pointer\":{},",
            "\"exact_same_box_pointer\":{},",
            "\"exact_payload_preserved\":{},",
            "\"exact_pointer_preserved\":{},",
            "\"exact_no_lifecycle\":{},",
            "\"exact_wrong_vec_non_reuse\":{},",
            "\"exact_same_box_reuse\":{},",
            "\"spare_vec_pointer\":{},",
            "\"spare_box_pointer\":{},",
            "\"spare_wrong_vec_pointer\":{},",
            "\"spare_same_box_pointer\":{},",
            "\"spare_recovered_vec_pointer\":{},",
            "\"spare_payload_preserved\":{},",
            "\"spare_pointer_moved\":{},",
            "\"spare_wrong_vec_non_reuse\":{},",
            "\"spare_same_box_reuse\":{},",
            "\"spare_old_vec_reuse\":{},",
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
        SPARE_CAPACITY,
        exact_vec_pointer,
        exact_box_pointer,
        exact_wrong_vec_pointer,
        exact_same_box_pointer,
        exact_payload_preserved,
        exact_pointer_preserved,
        exact_no_lifecycle,
        exact_wrong_vec_non_reuse,
        exact_same_box_reuse,
        spare_vec_pointer,
        spare_box_pointer,
        spare_wrong_vec_pointer,
        spare_same_box_pointer,
        spare_recovered_vec_pointer,
        spare_payload_preserved,
        spare_pointer_moved,
        spare_wrong_vec_non_reuse,
        spare_same_box_reuse,
        spare_old_vec_reuse,
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
    reverse_rows = [
        row
        for row in rows
        if isinstance(row, dict)
        and any(function_matches(row, name) for name in TRANSFER_FUNCTIONS)
        and row.get("lowering_kind") == TRANSFER_LOWERING_KIND
    ]
    function_rows = [
        row
        for row in rows
        if isinstance(row, dict)
        and any(function_matches(row, name) for name in TRANSFER_FUNCTIONS)
    ]
    assert len(reverse_rows) == 2, (
        f"expected 2 Vec::into_boxed_slice ownership-transfer candidates, got {len(reverse_rows)}",
        summary,
        reverse_rows,
        function_rows,
    )

    transfer_candidate_count = int(
        summary.get("semantic_ownership_transfer_candidate_count") or 0
    )
    transfer_applied_count = int(
        summary.get("semantic_ownership_transfer_rewrite_applied_count") or 0
    )
    assert transfer_candidate_count == 2, summary
    assert transfer_applied_count == 2, summary

    transfers_by_function: dict[str, dict[str, object]] = {}
    for function_name in TRANSFER_FUNCTIONS:
        matching = [row for row in reverse_rows if function_matches(row, function_name)]
        assert len(matching) == 1, matching
        transfers_by_function[function_name] = matching[0]

    for transfer in transfers_by_function.values():
        assert re.search(
            r"~ alloc(?:\[[^]]+\])?::vec::\{impl#\d+\}::into_boxed_slice\)",
            str(transfer.get("callee") or ""),
        ), transfer
        assert transfer.get("rewrite_status") == TRANSFER_STATUS, transfer
        assert transfer.get("replacement_symbol") == TRANSFER_SYMBOL, transfer
        assert (
            transfer.get("replacement_resolution_status")
            == TRANSFER_RESOLUTION_STATUS
        ), transfer
        assert transfer.get("metadata_pairing_contract") == TRANSFER_PAIRING, transfer
        assert transfer.get("type_id_basis") == TRANSFER_TYPE_ID_BASIS, transfer
        assert transfer.get("semantic_object_type") == BOX_TYPE, transfer
        assert transfer.get("destination_type") == BOX_TYPE, transfer
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
    box_rows = [
        row
        for row in applied_scope_rows(audit, BOX_FUNCTION, BOX_TYPE)
        if "::From::from" in str(row.get("callee") or "")
    ]
    assert len(vec_rows) == 1, vec_rows
    assert len(box_rows) == 1, box_rows
    vec_row = vec_rows[0]
    box_row = box_rows[0]
    assert "with_capacity" in str(vec_row.get("callee") or ""), vec_row
    assert "::From::from" in str(box_row.get("callee") or ""), box_row
    vec_type_id = int(vec_row.get("type_id") or 0)
    box_type_id = int(box_row.get("type_id") or 0)
    assert vec_type_id != 0, vec_row
    assert box_type_id != 0, box_row
    assert vec_type_id != box_type_id, (vec_row, box_row)

    for transfer in transfers_by_function.values():
        assert int(transfer.get("type_id") or 0) == box_type_id, transfer
        preview = str(transfer.get("replacement_preview") or "")
        old_match = re.search(r"\bold_type_id=(\d+)\b", preview)
        new_match = re.search(r"\bnew_type_id=(\d+)\b", preview)
        assert old_match is not None, transfer
        assert new_match is not None, transfer
        assert int(old_match.group(1)) == vec_type_id, transfer
        assert int(new_match.group(1)) == box_type_id, transfer

    generic_transfer_scopes = [
        row
        for row in rows
        if isinstance(row, dict)
        and any(function_matches(row, name) for name in TRANSFER_FUNCTIONS)
        and row.get("lowering_kind") == "semantic_scope_enter_exit_rewrite"
    ]
    assert generic_transfer_scopes == [], generic_transfer_scopes

    runtime = load_runtime(stdout)
    assert int(runtime["bytes"]) == BYTES, runtime
    assert int(runtime["spare_capacity"]) == SPARE_CAPACITY, runtime
    for field in (
        "exact_payload_preserved",
        "exact_pointer_preserved",
        "exact_no_lifecycle",
        "exact_wrong_vec_non_reuse",
        "exact_same_box_reuse",
        "spare_payload_preserved",
        "spare_pointer_moved",
        "spare_wrong_vec_non_reuse",
        "spare_same_box_reuse",
        "spare_old_vec_reuse",
    ):
        assert runtime[field] is True, (field, runtime)
    assert int(runtime["exact_vec_pointer"]) == int(runtime["exact_box_pointer"]), runtime
    assert int(runtime["exact_wrong_vec_pointer"]) != int(
        runtime["exact_box_pointer"]
    ), runtime
    assert int(runtime["exact_same_box_pointer"]) == int(
        runtime["exact_box_pointer"]
    ), runtime
    assert int(runtime["spare_vec_pointer"]) != int(runtime["spare_box_pointer"]), runtime
    assert int(runtime["spare_wrong_vec_pointer"]) != int(
        runtime["spare_box_pointer"]
    ), runtime
    assert int(runtime["spare_same_box_pointer"]) == int(
        runtime["spare_box_pointer"]
    ), runtime
    assert int(runtime["spare_recovered_vec_pointer"]) == int(
        runtime["spare_vec_pointer"]
    ), runtime
    for field, expected in (
        ("transfer_attempted", 2),
        ("transfer_applied", 2),
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
        "transfer_audits": {
            function_name: transfers_by_function[function_name]
            for function_name in TRANSFER_FUNCTIONS
        },
        "compiler_type_ids": {"vec": vec_type_id, "box_slice": box_type_id},
        "runtime": runtime,
    }


def main() -> int:
    toolchain = (ROOT / "rust-toolchain").read_text(encoding="utf-8").strip()
    rustc = shutil.which("rustc") or "rustc"
    cargo = shutil.which("cargo") or "cargo"
    sysroot = subprocess.check_output(
        [rustc, f"+{toolchain}", "--print", "sysroot"], cwd=ROOT, text=True
    ).strip()

    with tempfile.TemporaryDirectory(prefix="unialloc-vec-into-boxed-slice-rebind-") as raw:
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
                "source": "mir_vec_into_boxed_slice_rebind_probe",
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
