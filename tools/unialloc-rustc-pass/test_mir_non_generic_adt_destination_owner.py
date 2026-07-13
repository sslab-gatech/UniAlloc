#!/usr/bin/env python3
"""Fail closed on custom ADT returns without exact allocation provenance."""

from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile


ROOT = Path(__file__).resolve().parents[2]
PASS_SOURCE = ROOT / "tools/unialloc-rustc-pass/unialloc-rustc-mir-rewrite-dry-run.rs"
PROBE_NAME = "non_generic_adt_destination_owner_probe"
ALLOCATING_FACTORY_FUNCTION = "allocate_buffer"
UNRELATED_PASSTHROUGH_FUNCTION = "return_existing_with_unrelated_allocation"
PURE_PASSTHROUGH_FUNCTION = "return_existing_without_allocation"
AMBIGUOUS_FUNCTION = "make_ambiguous"
RAW_FUNCTION = "make_raw_marker"
BORROWED_FUNCTION = "make_borrowed"
PHANTOM_FUNCTION = "make_phantom"
APPLIED_STATUS = "actual_semantic_scope_enter_exit_rewrite_applied"


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
lock_api = "=0.4.3"
unialloc = {{ path = {json.dumps(str(ROOT / "unialloc"))}, features = ["stats", "type_isolation"] }}
''',
        encoding="utf-8",
    )
    (app / "src/main.rs").write_text(
        r'''use std::marker::PhantomData;
use unialloc::{
    semantic_fallback_attribution_snapshot, semantic_metadata_validation_snapshot,
    semantic_stats_recording_disable, semantic_stats_reset, semantic_stats_snapshot, UniAlloc,
};

#[global_allocator]
static ALLOCATOR: UniAlloc = UniAlloc;

struct Buffer {
    bytes: Vec<u8>,
}

struct Ambiguous {
    bytes: Vec<u8>,
    label: String,
}

struct RawMarker {
    pointer: *mut Vec<u8>,
}

struct Borrowed<'a> {
    bytes: &'a Vec<u8>,
}

struct Phantom {
    marker: PhantomData<Vec<u8>>,
}

#[inline(never)]
fn allocate_buffer(capacity: usize) -> Buffer {
    Buffer {
        bytes: Vec::with_capacity(capacity),
    }
}

#[inline(never)]
fn return_existing_with_unrelated_allocation(
    existing: Buffer,
    capacity: usize,
) -> Buffer {
    let mut unrelated = String::with_capacity(capacity);
    unrelated.push('x');
    drop(unrelated);
    existing
}

#[inline(never)]
fn return_existing_without_allocation(existing: Buffer, capacity: usize) -> Buffer {
    let _ = capacity;
    existing
}

#[inline(never)]
fn make_ambiguous(capacity: usize) -> Ambiguous {
    Ambiguous {
        bytes: Vec::with_capacity(capacity),
        label: String::with_capacity(capacity),
    }
}

#[inline(never)]
fn make_raw_marker() -> RawMarker {
    RawMarker {
        pointer: std::ptr::null_mut(),
    }
}

#[inline(never)]
fn make_borrowed(bytes: &Vec<u8>) -> Borrowed<'_> {
    Borrowed { bytes }
}

#[inline(never)]
fn make_phantom() -> Phantom {
    Phantom {
        marker: PhantomData,
    }
}

#[inline(never)]
fn opaque_false() -> bool {
    unsafe { std::ptr::read_volatile(&false) }
}

fn main() {
    // Seed both passthrough inputs before the measured interval. Their Vec
    // storage is existing ownership, not an allocation performed by either
    // helper call below.
    let unrelated_existing = Buffer {
        bytes: Vec::with_capacity(64),
    };
    let pure_existing = Buffer {
        bytes: Vec::with_capacity(64),
    };
    semantic_stats_reset();

    let before_unrelated = semantic_metadata_validation_snapshot();
    let unrelated = return_existing_with_unrelated_allocation(unrelated_existing, 32);
    let after_unrelated = semantic_metadata_validation_snapshot();
    let unrelated_mismatch_delta = after_unrelated
        .recovery_identity_mismatches
        .saturating_sub(before_unrelated.recovery_identity_mismatches);
    std::mem::forget(unrelated);

    let before_pure = semantic_metadata_validation_snapshot();
    let pure = return_existing_without_allocation(pure_existing, 32);
    let after_pure = semantic_metadata_validation_snapshot();
    let pure_mismatch_delta = after_pure
        .recovery_identity_mismatches
        .saturating_sub(before_pure.recovery_identity_mismatches);
    std::mem::forget(pure);

    let value = allocate_buffer(64);
    assert_eq!(value.bytes.capacity(), 64);
    let after_allocation = semantic_stats_snapshot();
    drop(value);
    let after_drop = semantic_stats_snapshot();

    if opaque_false() {
        let ambiguous = make_ambiguous(8);
        assert_eq!(ambiguous.bytes.capacity(), 8);
        assert_eq!(ambiguous.label.capacity(), 8);
        drop(ambiguous);

        let raw = make_raw_marker();
        assert!(raw.pointer.is_null());
        let source = Vec::<u8>::new();
        let borrowed = make_borrowed(&source);
        assert!(std::ptr::eq(borrowed.bytes, &source));
        let phantom = make_phantom();
        std::mem::forget(phantom);
    }

    let fallback = semantic_fallback_attribution_snapshot();
    let validation = semantic_metadata_validation_snapshot();
    semantic_stats_recording_disable();
    println!(
        concat!(
            "{{",
            "\"source\":\"non_generic_adt_destination_owner_probe\",",
            "\"allocation_type_id\":{},",
            "\"unrelated_passthrough_mismatch_delta\":{},",
            "\"pure_passthrough_mismatch_delta\":{},",
            "\"typed_allocations\":{},",
            "\"typed_deallocations\":{},",
            "\"fallback_allocations\":{},",
            "\"fallback_deallocations\":{},",
            "\"raw_alloc_no_metadata\":{},",
            "\"raw_dealloc_no_metadata\":{},",
            "\"recovery_identity_mismatches\":{}",
            "}}"
        ),
        after_allocation.last_type_id,
        unrelated_mismatch_delta,
        pure_mismatch_delta,
        after_drop.typed_allocations,
        after_drop.typed_deallocations,
        after_drop.fallback_allocations,
        after_drop.fallback_deallocations,
        fallback.raw_alloc_no_metadata,
        fallback.raw_dealloc_no_metadata,
        validation.recovery_identity_mismatches,
    );
}
''',
        encoding="utf-8",
    )
    return app


def function_matches(row: dict[str, object], function_name: str) -> bool:
    function = str(row.get("mir_function") or "")
    return function == function_name or function.endswith(f"::{function_name}")


def callee_rows(
    rows: list[object], caller: str, callee_fragment: str
) -> list[dict[str, object]]:
    return [
        row
        for row in rows
        if isinstance(row, dict)
        and function_matches(row, caller)
        and callee_fragment in str(row.get("callee") or "")
    ]


def validate(audit: dict[str, object], stdout: str) -> dict[str, object]:
    summary = audit.get("summary")
    assert isinstance(summary, dict), summary
    assert summary.get("provider_override_installed") is True, summary
    assert summary.get("body_clone_returned_to_rustc") is True, summary
    assert summary.get("actual_semantic_scope_rewrite_requested") is True, summary
    assert summary.get("actual_semantic_scope_rewrite") is True, summary

    rows = audit.get("rewrite_candidates")
    assert isinstance(rows, list), rows

    runtime = next(
        json.loads(line)
        for line in stdout.splitlines()
        if line.startswith("{") and PROBE_NAME in line
    )
    # Keep these assertions before the audit assertions so the pre-fix
    # regression reports the concrete runtime safety violation, not merely the
    # over-broad audit classification.
    assert int(runtime["unrelated_passthrough_mismatch_delta"]) == 0, runtime
    assert int(runtime["pure_passthrough_mismatch_delta"]) == 0, runtime

    factory_rows = callee_rows(rows, "main", ALLOCATING_FACTORY_FUNCTION)
    assert len(factory_rows) == 1, factory_rows
    factory = factory_rows[0]
    assert factory.get("lowering_kind") == "semantic_scope_unsolved_heap_object_candidate", factory
    assert factory.get("rewrite_status") == (
        "semantic_scope_rewrite_skipped_unresolved_heap_object_type"
    ), factory
    assert factory.get("metadata_pairing_contract") == (
        "audit_only_unresolved_heap_object_type"
    ), factory
    assert "Buffer" in str(factory.get("destination_type") or ""), factory

    internal_allocation_rows = callee_rows(
        rows, ALLOCATING_FACTORY_FUNCTION, "with_capacity"
    )
    assert len(internal_allocation_rows) == 1, internal_allocation_rows
    internal_allocation = internal_allocation_rows[0]
    assert internal_allocation.get("lowering_kind") == (
        "semantic_scope_enter_exit_rewrite"
    ), internal_allocation
    assert internal_allocation.get("rewrite_status") == APPLIED_STATUS, internal_allocation
    semantic_type = str(internal_allocation.get("semantic_object_type") or "")
    assert semantic_type.startswith("std::vec::Vec<u8"), internal_allocation
    assert int(internal_allocation.get("type_id") or 0) != 0, internal_allocation

    passthrough_rows: dict[str, dict[str, object]] = {}
    for function_name in (
        UNRELATED_PASSTHROUGH_FUNCTION,
        PURE_PASSTHROUGH_FUNCTION,
    ):
        matching = callee_rows(rows, "main", function_name)
        assert len(matching) == 1, (function_name, matching)
        row = matching[0]
        assert row.get("lowering_kind") == (
            "semantic_scope_unsolved_heap_object_candidate"
        ), row
        assert row.get("rewrite_status") == (
            "semantic_scope_rewrite_skipped_unresolved_heap_object_type"
        ), row
        assert row.get("metadata_pairing_contract") == (
            "audit_only_unresolved_heap_object_type"
        ), row
        passthrough_rows[function_name] = row

    drop_rows = [
        row
        for row in rows
        if isinstance(row, dict)
        and function_matches(row, "main")
        and row.get("lowering_kind") == "semantic_scope_drop_rewrite"
        and "Buffer" in str(row.get("destination_type") or "")
    ]
    assert drop_rows, drop_rows
    drop_row = drop_rows[0]
    for row in drop_rows:
        assert row.get("rewrite_status") == "actual_semantic_scope_drop_rewrite_applied", row
        assert row.get("semantic_object_type") == semantic_type, row
        assert int(row.get("type_id") or 0) == int(internal_allocation["type_id"]), row

    ambiguous_rows = callee_rows(rows, "main", AMBIGUOUS_FUNCTION)
    assert len(ambiguous_rows) == 1, ambiguous_rows
    ambiguous = ambiguous_rows[0]
    assert ambiguous.get("rewrite_status") == (
        "semantic_scope_rewrite_skipped_ambiguous_heap_object_type"
    ), ambiguous
    assert ambiguous.get("lowering_kind") == (
        "semantic_scope_unsolved_heap_object_candidate"
    ), ambiguous
    ambiguous_text = json.dumps(ambiguous, sort_keys=True)
    assert "std::vec::Vec<u8" in ambiguous_text, ambiguous
    assert "std::string::String" in ambiguous_text, ambiguous

    raw_rows = callee_rows(rows, "main", RAW_FUNCTION)
    assert len(raw_rows) == 1, raw_rows
    raw = raw_rows[0]
    assert raw.get("rewrite_status") == (
        "semantic_scope_rewrite_skipped_unresolved_heap_object_type"
    ), raw
    assert raw.get("lowering_kind") == "semantic_scope_unsolved_heap_object_candidate", raw

    excluded_statuses: dict[str, list[str]] = {}
    for excluded in (BORROWED_FUNCTION, PHANTOM_FUNCTION):
        excluded_rows = callee_rows(rows, "main", excluded)
        assert all(row.get("rewrite_status") != APPLIED_STATUS for row in excluded_rows), (
            excluded,
            excluded_rows,
        )
        assert all(
            row.get("lowering_kind") == "semantic_scope_unsolved_heap_object_candidate"
            and row.get("rewrite_status")
            == "semantic_scope_rewrite_skipped_unresolved_heap_object_type"
            for row in excluded_rows
        ), (excluded, excluded_rows)
        excluded_statuses[excluded] = [
            str(row.get("rewrite_status")) for row in excluded_rows
        ]

    assert int(runtime["allocation_type_id"]) == int(internal_allocation["type_id"]), runtime
    # Only exact internal constructors are typed: String::with_capacity inside
    # the unrelated passthrough carries String identity, while
    # Vec::with_capacity / Buffer Drop carry the backing Vec identity.  The
    # custom aggregate call itself stays audit-only and cannot misattribute the
    # transient String allocation to the returned Buffer's Vec identity.
    assert int(runtime["typed_allocations"]) == 2, runtime
    assert int(runtime["typed_deallocations"]) == 2, runtime
    for field in (
        "fallback_allocations",
        "fallback_deallocations",
        "raw_alloc_no_metadata",
        "raw_dealloc_no_metadata",
    ):
        assert int(runtime[field]) == 0, (field, runtime)
    assert int(runtime["recovery_identity_mismatches"]) == 0, runtime

    return {
        "factory": {
            key: factory.get(key)
            for key in (
                "callee",
                "destination_type",
                "semantic_object_type",
                "type_id",
                "rewrite_status",
            )
        },
        "internal_allocation": {
            key: internal_allocation.get(key)
            for key in (
                "callee",
                "semantic_object_type",
                "type_id",
                "rewrite_status",
            )
        },
        "passthroughs": {
            function_name: {
                key: row.get(key)
                for key in (
                    "callee",
                    "destination_type",
                    "rewrite_status",
                    "metadata_pairing_contract",
                )
            }
            for function_name, row in passthrough_rows.items()
        },
        "drop": {
            "count": len(drop_rows),
            **{
                key: drop_row.get(key)
                for key in (
                    "destination_type",
                    "semantic_object_type",
                    "type_id",
                    "rewrite_status",
                )
            },
        },
        "ambiguous_status": ambiguous.get("rewrite_status"),
        "raw_status": raw.get("rewrite_status"),
        "borrowed_and_phantom_not_lowered": excluded_statuses,
        "runtime": runtime,
    }


def main() -> int:
    toolchain = os.environ.get("UNIALLOC_RUSTC_TOOLCHAIN") or (
        ROOT / "rust-toolchain"
    ).read_text(encoding="utf-8").strip()
    rustc = shutil.which("rustc") or "rustc"
    cargo = shutil.which("cargo") or "cargo"
    sysroot = subprocess.check_output(
        [rustc, f"+{toolchain}", "--print", "sysroot"], cwd=ROOT, text=True
    ).strip()

    with tempfile.TemporaryDirectory(prefix="unialloc-non-generic-adt-owner-") as raw:
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
                "UNIALLOC_LOWERING_POLICY_FLAGS": "1",
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
                "source": PROBE_NAME,
                "toolchain": toolchain,
                "validated": True,
                "evidence": evidence,
                "boundaries": [
                    "Custom aggregate return calls are discovered but remain audit-only without an exact constructor matcher or sound callee-body allocation proof; the positive Buffer allocation is typed by its exact internal Vec::with_capacity and paired Buffer Drop rewrites instead.",
                    "Returning an existing Buffer, with either an unrelated transient String allocation or no allocation, must not activate a Vec semantic scope and records zero recovery mismatches.",
                    "Multiple supported owners and structurally unresolved raw-pointer fields remain audit-only and fail closed.",
                    "This is actual target-crate MIR rewrite and runtime metadata evidence, not arbitrary-application coverage or performance evidence.",
                ],
            },
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
