#!/usr/bin/env python3
"""Fail closed when a current-rustc custom ADT hides another heap owner."""

from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile


ROOT = Path(__file__).resolve().parents[2]
PASS_SOURCE = ROOT / "tools/unialloc-rustc-pass/unialloc-rustc-mir-rewrite-dry-run.rs"
PROBE_NAME = "hidden_adt_owner_probe"
FACTORY_FUNCTION = "assemble"
BORROWED_FACTORY_FUNCTION = "wrap_borrowed"
PHANTOM_FACTORY_FUNCTION = "make_phantom"
RAW_FACTORY_FUNCTION = "make_raw_hidden"
AMBIGUOUS_STATUS = "semantic_scope_rewrite_skipped_ambiguous_heap_object_type"
UNRESOLVED_STATUS = "semantic_scope_rewrite_skipped_unresolved_heap_object_type"
MULTI_OWNER_DROP_STATUS = "semantic_scope_drop_rewrite_skipped_multiple_heap_owners"
APPLIED_OR_PLANNED_SCOPE_STATUSES = {
    "actual_semantic_scope_enter_exit_rewrite_applied",
    "semantic_scope_enter_exit_rewrite_planned",
    "actual_semantic_scope_drop_rewrite_applied",
    "semantic_scope_drop_rewrite_planned",
}


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
        r'''use std::marker::PhantomData;
use unialloc::{semantic_metadata_validation_snapshot, semantic_stats_reset, UniAlloc};

#[global_allocator]
static ALLOCATOR: UniAlloc = UniAlloc;

struct Hidden<T> {
    visible: T,
    hidden: String,
}

struct Borrowed<'a, T> {
    value: &'a T,
}

struct Phantom<T> {
    marker: PhantomData<T>,
}

struct RawHidden<T> {
    raw: *mut T,
    hidden: String,
}

#[inline(never)]
fn make_vec() -> Vec<u8> {
    Vec::with_capacity(32)
}

#[inline(never)]
fn make_string() -> String {
    String::with_capacity(32)
}

#[inline(never)]
fn assemble(visible: Vec<u8>, hidden: String) -> Hidden<Vec<u8>> {
    Hidden { visible, hidden }
}

#[inline(never)]
fn wrap_borrowed(value: &Vec<u8>) -> Borrowed<'_, Vec<u8>> {
    Borrowed { value }
}

#[inline(never)]
fn make_phantom() -> Phantom<Vec<u8>> {
    Phantom { marker: PhantomData }
}

#[inline(never)]
fn make_raw_hidden() -> RawHidden<Vec<u8>> {
    RawHidden {
        raw: std::ptr::null_mut(),
        hidden: String::with_capacity(16),
    }
}

fn main() {
    semantic_stats_reset();
    let (visible_capacity, hidden_capacity) = {
        let value = assemble(make_vec(), make_string());
        (value.visible.capacity(), value.hidden.capacity())
    };
    assert_eq!(visible_capacity, 32);
    assert_eq!(hidden_capacity, 32);

    let borrowed_source = Vec::<u8>::new();
    let borrowed = wrap_borrowed(&borrowed_source);
    assert!(std::ptr::eq(borrowed.value, &borrowed_source));
    let phantom = make_phantom();
    std::hint::black_box(phantom);
    let raw_hidden_capacity = {
        let raw_hidden = make_raw_hidden();
        assert!(raw_hidden.raw.is_null());
        raw_hidden.hidden.capacity()
    };
    assert_eq!(raw_hidden_capacity, 16);

    let validation = semantic_metadata_validation_snapshot();
    println!(
        "{{\"source\":\"hidden_adt_owner_probe\",\"visible_capacity\":{},\"hidden_capacity\":{},\"raw_hidden_capacity\":{},\"recovery_identity_mismatches\":{},\"conventional_result_correct\":true}}",
        visible_capacity,
        hidden_capacity,
        raw_hidden_capacity,
        validation.recovery_identity_mismatches,
    );
}
''',
        encoding="utf-8",
    )
    return app


def probe_function_matches(row: dict[str, object], suffix: str) -> bool:
    function = str(row.get("mir_function") or "")
    return function == suffix or function.endswith(f"::{suffix}")


def validate(audit: dict[str, object], stdout: str) -> dict[str, object]:
    summary = audit.get("summary")
    assert isinstance(summary, dict), summary
    assert summary.get("provider_override_installed") is True, summary
    assert summary.get("body_clone_returned_to_rustc") is True, summary

    candidates = audit.get("rewrite_candidates")
    assert isinstance(candidates, list), candidates
    rows = [row for row in candidates if isinstance(row, dict)]

    factory_rows = [
        row
        for row in rows
        if probe_function_matches(row, "main")
        and FACTORY_FUNCTION in str(row.get("callee") or "")
    ]
    assert len(factory_rows) == 1, factory_rows
    factory = factory_rows[0]
    assert factory.get("lowering_kind") == "semantic_scope_unsolved_heap_object_candidate", factory
    assert factory.get("rewrite_status") == AMBIGUOUS_STATUS, factory
    assert (
        factory.get("replacement_resolution_status")
        == "rustc_middle_multiple_heap_object_types_not_lowered"
    ), factory
    assert factory.get("metadata_pairing_contract") == "audit_only_ambiguous_heap_object_type", factory
    assert factory.get("semantic_scope_unwind_pop_inserted") is False, factory
    factory_text = json.dumps(factory, sort_keys=True)
    for owner in ("std::vec::Vec<u8", "std::string::String"):
        assert owner in factory_text, f"factory audit omitted {owner}: {factory!r}"

    structural_fail_closed: dict[str, dict[str, object]] = {}
    for function_name in (
        BORROWED_FACTORY_FUNCTION,
        PHANTOM_FACTORY_FUNCTION,
        RAW_FACTORY_FUNCTION,
    ):
        matching = [
            row
            for row in rows
            if probe_function_matches(row, "main")
            and function_name in str(row.get("callee") or "")
        ]
        assert len(matching) == 1, (function_name, matching)
        row = matching[0]
        assert row.get("lowering_kind") == "semantic_scope_unsolved_heap_object_candidate", row
        assert row.get("rewrite_status") == UNRESOLVED_STATUS, row
        assert row.get("replacement_resolution_status") == "rustc_middle_heap_object_type_not_solved", row
        assert row.get("metadata_pairing_contract") == "audit_only_unresolved_heap_object_type", row
        assert row.get("semantic_scope_unwind_pop_inserted") is False, row
        structural_fail_closed[function_name] = row

    borrowed_text = json.dumps(structural_fail_closed[BORROWED_FACTORY_FUNCTION], sort_keys=True)
    assert "&'" in borrowed_text and "Vec<u8" in borrowed_text, borrowed_text
    phantom_text = json.dumps(structural_fail_closed[PHANTOM_FACTORY_FUNCTION], sort_keys=True)
    assert "Phantom<" in phantom_text and "Vec<u8" in phantom_text, phantom_text
    raw_text = json.dumps(structural_fail_closed[RAW_FACTORY_FUNCTION], sort_keys=True)
    assert "RawHidden<" in raw_text and "Vec<u8" in raw_text, raw_text

    hidden_drop_rows = [
        row
        for row in rows
        if probe_function_matches(row, "main")
        and row.get("callee") == "TerminatorKind::Drop"
        and (
            str(row.get("destination_type") or "").startswith("Hidden<")
            or "::Hidden<" in str(row.get("destination_type") or "")
        )
    ]
    assert hidden_drop_rows, "expected at least one Hidden<T> Drop audit row"
    for drop in hidden_drop_rows:
        assert (
            drop.get("lowering_kind")
            == "semantic_scope_drop_multiple_heap_owners_skipped"
        ), drop
        assert drop.get("rewrite_status") == MULTI_OWNER_DROP_STATUS, drop
        assert (
            drop.get("replacement_resolution_status")
            == "rustc_middle_drop_multiple_heap_owners_not_lowered"
        ), drop
        assert (
            drop.get("metadata_pairing_contract")
            == "audit_only_multiple_heap_owner_drop_type"
        ), drop
        drop_text = json.dumps(drop, sort_keys=True)
        for owner in ("std::vec::Vec<u8", "std::string::String"):
            assert owner in drop_text, f"drop audit omitted {owner}: {drop!r}"

    raw_hidden_drop_rows = [
        row
        for row in rows
        if probe_function_matches(row, "main")
        and row.get("callee") == "TerminatorKind::Drop"
        and (
            str(row.get("destination_type") or "").startswith("RawHidden<")
            or "::RawHidden<" in str(row.get("destination_type") or "")
        )
    ]
    assert raw_hidden_drop_rows, "expected at least one RawHidden<T> Drop audit row"
    for drop in raw_hidden_drop_rows:
        assert drop.get("lowering_kind") == "semantic_scope_drop_unsolved_heap_object_candidate", drop
        assert drop.get("rewrite_status") == "semantic_scope_drop_rewrite_skipped_unresolved_heap_object_type", drop
        assert drop.get("replacement_resolution_status") == "rustc_middle_drop_heap_object_type_not_solved", drop
        assert drop.get("metadata_pairing_contract") == "audit_only_unresolved_drop_type", drop

    relevant_rows = (
        factory_rows
        + hidden_drop_rows
        + raw_hidden_drop_rows
        + list(structural_fail_closed.values())
    )
    applied_or_planned = [
        row
        for row in relevant_rows
        if row.get("rewrite_status") in APPLIED_OR_PLANNED_SCOPE_STATUSES
    ]
    assert not applied_or_planned, applied_or_planned

    runtime = next(
        json.loads(line)
        for line in stdout.splitlines()
        if line.startswith("{") and PROBE_NAME in line
    )
    assert runtime == {
        "source": PROBE_NAME,
        "visible_capacity": 32,
        "hidden_capacity": 32,
        "raw_hidden_capacity": 16,
        "recovery_identity_mismatches": 0,
        "conventional_result_correct": True,
    }, runtime

    return {
        "factory": {
            key: factory.get(key)
            for key in (
                "destination_type",
                "semantic_object_type",
                "lowering_kind",
                "rewrite_status",
                "replacement_resolution_status",
            )
        },
        "hidden_drop_count": len(hidden_drop_rows),
        "hidden_drop_statuses": sorted(
            {str(row.get("rewrite_status")) for row in hidden_drop_rows}
        ),
        "raw_hidden_drop_count": len(raw_hidden_drop_rows),
        "raw_hidden_drop_statuses": sorted(
            {str(row.get("rewrite_status")) for row in raw_hidden_drop_rows}
        ),
        "structural_fail_closed": {
            function_name: {
                key: row.get(key)
                for key in (
                    "destination_type",
                    "lowering_kind",
                    "rewrite_status",
                    "replacement_resolution_status",
                )
            }
            for function_name, row in structural_fail_closed.items()
        },
        "runtime": runtime,
    }


def main() -> int:
    toolchain = (ROOT / "rust-toolchain").read_text(encoding="utf-8").strip()
    rustc = shutil.which("rustc") or "rustc"
    cargo = shutil.which("cargo") or "cargo"
    sysroot = subprocess.check_output(
        [rustc, f"+{toolchain}", "--print", "sysroot"], cwd=ROOT, text=True
    ).strip()

    with tempfile.TemporaryDirectory(prefix="unialloc-hidden-adt-owner-") as raw:
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
                "source": "hidden_adt_owner_fail_closed_probe",
                "validated": True,
                "evidence": evidence,
            },
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
