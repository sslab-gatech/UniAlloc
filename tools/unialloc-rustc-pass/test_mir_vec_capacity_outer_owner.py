#!/usr/bin/env python3
"""Prove capacity-only Vec scopes use the direct outer Vec identity."""

from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile


ROOT = Path(__file__).resolve().parents[2]
PASS_SOURCE = ROOT / "tools/unialloc-rustc-pass/unialloc-rustc-mir-rewrite-dry-run.rs"
PROBE_NAME = "vec_capacity_outer_owner_probe"
STRING_RESERVE_FUNCTION = "reserve_strings"
NESTED_VEC_RESERVE_FUNCTION = "reserve_nested_vecs"
NESTED_VEC_CONSTRUCTOR_FUNCTION = "construct_nested_vecs"
RESIZE_FUNCTION = "resize_strings"
APPLIED_STATUS = "actual_semantic_scope_enter_exit_rewrite_applied"
CALLBACK_STATUS = "semantic_scope_rewrite_skipped_callback_capable_receiver"


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
    spin_candidates = sorted((Path.home() / ".cargo/registry/src").glob("*/spin-0.9.0"))
    assert spin_candidates, "spin 0.9.0 must be present in the Cargo source cache"
    (app / "Cargo.toml").write_text(
        f'''[package]
name = "{PROBE_NAME.replace('_', '-')}"
version = "0.1.0"
edition = "2021"

[dependencies]
unialloc = {{ path = {json.dumps(str(ROOT / "unialloc"))}, features = ["stats", "type_isolation"] }}

[patch.crates-io]
spin = {{ path = {json.dumps(str(spin_candidates[-1]))} }}
''',
        encoding="utf-8",
    )
    (app / "src/main.rs").write_text(
        r'''use std::mem::{align_of, size_of};
use unialloc::{
    semantic_auto_metadata_disable, semantic_metadata_validation_snapshot,
    semantic_stats_recording_disable, semantic_stats_reset, semantic_stats_snapshot,
    type_isolation_side_cache_snapshot, UniAlloc,
};

#[global_allocator]
static ALLOCATOR: UniAlloc = UniAlloc;

const CAPACITY: usize = 4;

#[inline(never)]
fn reserve_strings(values: &mut Vec<String>) {
    values.reserve_exact(CAPACITY);
}

#[inline(never)]
fn reserve_nested_vecs(values: &mut Vec<Vec<u8>>) {
    values.reserve_exact(CAPACITY);
}

#[inline(never)]
fn construct_nested_vecs() -> Vec<Vec<u8>> {
    Vec::with_capacity(CAPACITY)
}

#[inline(never)]
fn resize_strings(values: &mut Vec<String>) {
    values.resize(2, String::new());
}

fn main() {
    assert_eq!(size_of::<String>(), size_of::<Vec<u8>>());
    assert_eq!(align_of::<String>(), align_of::<Vec<u8>>());

    // Keep the negative control live in MIR without letting it perturb the
    // deterministic allocator/cache sequence below.
    if std::hint::black_box(false) {
        let mut negative = Vec::<String>::new();
        resize_strings(&mut negative);
        std::hint::black_box(negative);
    }

    semantic_auto_metadata_disable();
    semantic_stats_reset();

    let before_first = semantic_stats_snapshot();
    let mut first = Vec::<String>::new();
    reserve_strings(&mut first);
    assert_eq!(first.capacity(), CAPACITY);
    let first_buffer = first.as_ptr() as usize;
    let after_first = semantic_stats_snapshot();
    let string_type_id = after_first.last_type_id;
    drop(first);
    let after_first_drop = semantic_stats_snapshot();

    let mut wrong = construct_nested_vecs();
    reserve_nested_vecs(&mut wrong);
    assert_eq!(wrong.capacity(), CAPACITY);
    let wrong_buffer = wrong.as_ptr() as usize;
    let after_wrong = semantic_stats_snapshot();
    let nested_vec_type_id = after_wrong.last_type_id;
    drop(wrong);
    let after_wrong_drop = semantic_stats_snapshot();

    let mut recovered = Vec::<String>::new();
    reserve_strings(&mut recovered);
    assert_eq!(recovered.capacity(), CAPACITY);
    let recovered_buffer = recovered.as_ptr() as usize;
    let after_recovered = semantic_stats_snapshot();
    drop(recovered);
    let after_recovered_drop = semantic_stats_snapshot();

    let validation = semantic_metadata_validation_snapshot();
    let side_cache = type_isolation_side_cache_snapshot();
    semantic_stats_recording_disable();

    println!(
        concat!(
            "{{",
            "\"source\":\"vec_capacity_outer_owner_probe\",",
            "\"element_layout_bytes\":{},",
            "\"element_layout_align\":{},",
            "\"string_type_id\":{},",
            "\"nested_vec_type_id\":{},",
            "\"first_buffer\":{},",
            "\"wrong_buffer\":{},",
            "\"recovered_buffer\":{},",
            "\"wrong_type_non_reuse\":{},",
            "\"same_type_recovery\":{},",
            "\"first_typed_allocations\":{},",
            "\"first_drop_typed_deallocations\":{},",
            "\"first_drop_cache_inserts\":{},",
            "\"wrong_typed_allocations\":{},",
            "\"wrong_drop_typed_deallocations\":{},",
            "\"wrong_drop_cache_inserts\":{},",
            "\"recovered_typed_allocations\":{},",
            "\"recovered_cache_hits\":{},",
            "\"recovered_drop_typed_deallocations\":{},",
            "\"fallback_allocations\":{},",
            "\"fallback_deallocations\":{},",
            "\"recovery_identity_mismatches\":{},",
            "\"side_cache_corrupt_slots\":{}",
            "}}"
        ),
        size_of::<String>(),
        align_of::<String>(),
        string_type_id,
        nested_vec_type_id,
        first_buffer,
        wrong_buffer,
        recovered_buffer,
        wrong_buffer != first_buffer,
        recovered_buffer == first_buffer,
        after_first.typed_allocations.saturating_sub(before_first.typed_allocations),
        after_first_drop.typed_deallocations.saturating_sub(after_first.typed_deallocations),
        after_first_drop.typed_cache_inserts.saturating_sub(after_first.typed_cache_inserts),
        after_wrong.typed_allocations.saturating_sub(after_first_drop.typed_allocations),
        after_wrong_drop.typed_deallocations.saturating_sub(after_wrong.typed_deallocations),
        after_wrong_drop.typed_cache_inserts.saturating_sub(after_wrong.typed_cache_inserts),
        after_recovered.typed_allocations.saturating_sub(after_wrong_drop.typed_allocations),
        after_recovered.typed_cache_hits.saturating_sub(after_wrong_drop.typed_cache_hits),
        after_recovered_drop.typed_deallocations.saturating_sub(after_recovered.typed_deallocations),
        after_recovered_drop.fallback_allocations,
        after_recovered_drop.fallback_deallocations,
        validation.recovery_identity_mismatches,
        side_cache.corrupt_slots,
    );
}
''',
        encoding="utf-8",
    )
    return app


def function_matches(row: dict[str, object], function_name: str) -> bool:
    function = str(row.get("mir_function") or "")
    return function == function_name or function.endswith(f"::{function_name}")


def function_method_rows(
    audit: dict[str, object], function_name: str, method: str
) -> list[dict[str, object]]:
    rows = audit.get("rewrite_candidates")
    assert isinstance(rows, list), rows
    return [
        row
        for row in rows
        if isinstance(row, dict)
        and function_matches(row, function_name)
        and method in str(row.get("callee") or "")
    ]


def validate(audit: dict[str, object], stdout: str) -> dict[str, object]:
    summary = audit.get("summary")
    assert isinstance(summary, dict), summary
    assert summary.get("provider_override_installed") is True, summary
    assert summary.get("body_clone_returned_to_rustc") is True, summary
    assert summary.get("actual_semantic_scope_rewrite_requested") is True, summary
    assert summary.get("actual_semantic_scope_rewrite") is True, summary

    string_rows = function_method_rows(
        audit, STRING_RESERVE_FUNCTION, "reserve_exact"
    )
    nested_rows = function_method_rows(
        audit, NESTED_VEC_RESERVE_FUNCTION, "reserve_exact"
    )
    constructor_rows = function_method_rows(
        audit, NESTED_VEC_CONSTRUCTOR_FUNCTION, "with_capacity"
    )
    assert len(string_rows) == 1, string_rows
    assert len(nested_rows) == 1, nested_rows
    assert len(constructor_rows) == 1, constructor_rows
    string_row = string_rows[0]
    nested_row = nested_rows[0]
    constructor_row = constructor_rows[0]
    for row, outer, nested in (
        (string_row, "Vec<std::string::String", "std::string::String"),
        (nested_row, "Vec<std::vec::Vec<u8", "Vec<u8"),
    ):
        assert row.get("lowering_kind") == "semantic_scope_enter_exit_rewrite", row
        assert row.get("rewrite_status") == APPLIED_STATUS, row
        assert outer in str(row.get("semantic_object_type") or ""), row
        assert nested in json.dumps(row.get("argument_types") or []), row
        assert row.get("metadata_pairing_contract") == "semantic_scope_active_metadata", row

    assert (
        constructor_row.get("lowering_kind") == "semantic_scope_enter_exit_rewrite"
    ), constructor_row
    assert (
        constructor_row.get("rewrite_status")
        == "actual_semantic_scope_generic_type_rewrite_applied"
    ), constructor_row
    assert "Vec<std::vec::Vec<u8" in str(
        constructor_row.get("semantic_object_type") or ""
    ), constructor_row
    assert (
        constructor_row.get("metadata_pairing_contract")
        == "semantic_scope_monomorphized_runtime_type_metadata"
    ), constructor_row
    assert constructor_row.get("type_id_basis") == (
        "monomorphized_compiler_type_id_runtime"
    ), constructor_row
    assert constructor_row.get("replacement_symbol") == (
        "__unialloc_semantic_scope_push_for_rust_type"
    ), constructor_row

    string_type_id = int(string_row.get("type_id") or 0)
    nested_type_id = int(nested_row.get("type_id") or 0)
    assert string_type_id != 0, string_row
    assert nested_type_id != 0, nested_row
    assert string_type_id != nested_type_id, (string_row, nested_row)
    assert int(constructor_row.get("type_id") or 0) == 0, constructor_row

    resize_rows = function_method_rows(audit, RESIZE_FUNCTION, "resize")
    assert len(resize_rows) == 1, resize_rows
    resize = resize_rows[0]
    assert resize.get("lowering_kind") == "semantic_scope_callback_capable_receiver_skipped", resize
    assert resize.get("rewrite_status") == CALLBACK_STATUS, resize
    assert (
        resize.get("replacement_resolution_status")
        == "exact_receiver_call_callback_capable_not_lowered"
    ), resize
    assert resize.get("metadata_pairing_contract") == "audit_only_callback_capable_receiver", resize
    resize_text = json.dumps(resize, sort_keys=True)
    assert "resize" in resize_text, resize

    runtime = next(
        json.loads(line)
        for line in stdout.splitlines()
        if line.startswith("{") and PROBE_NAME in line
    )
    assert int(runtime["string_type_id"]) == string_type_id, runtime
    assert int(runtime["nested_vec_type_id"]) == nested_type_id, runtime
    element_layout_bytes = int(runtime["element_layout_bytes"])
    element_layout_align = int(runtime["element_layout_align"])
    assert element_layout_bytes > 0, runtime
    assert element_layout_align > 0, runtime
    assert element_layout_bytes % element_layout_align == 0, runtime
    assert runtime["wrong_type_non_reuse"] is True, runtime
    assert runtime["same_type_recovery"] is True, runtime
    assert int(runtime["first_typed_allocations"]) == 1, runtime
    assert int(runtime["first_drop_typed_deallocations"]) == 1, runtime
    assert int(runtime["first_drop_cache_inserts"]) == 1, runtime
    assert int(runtime["wrong_typed_allocations"]) == 1, runtime
    assert int(runtime["wrong_drop_typed_deallocations"]) == 1, runtime
    assert int(runtime["wrong_drop_cache_inserts"]) == 1, runtime
    assert int(runtime["recovered_typed_allocations"]) == 1, runtime
    assert int(runtime["recovered_cache_hits"]) == 1, runtime
    assert int(runtime["recovered_drop_typed_deallocations"]) == 1, runtime
    assert int(runtime["fallback_allocations"]) == 0, runtime
    assert int(runtime["fallback_deallocations"]) == 0, runtime
    assert int(runtime["recovery_identity_mismatches"]) == 0, runtime
    assert int(runtime["side_cache_corrupt_slots"]) == 0, runtime

    return {
        "string_reserve": {
            key: string_row.get(key)
            for key in (
                "callee",
                "semantic_object_type",
                "type_id",
                "rewrite_status",
            )
        },
        "nested_vec_reserve": {
            key: nested_row.get(key)
            for key in (
                "callee",
                "semantic_object_type",
                "type_id",
                "rewrite_status",
            )
        },
        "nested_vec_constructor": {
            key: constructor_row.get(key)
            for key in (
                "callee",
                "semantic_object_type",
                "type_id",
                "rewrite_status",
            )
        },
        "resize_negative_control": {
            key: resize.get(key)
            for key in (
                "callee",
                "semantic_object_type",
                "rewrite_status",
                "replacement_resolution_status",
            )
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

    with tempfile.TemporaryDirectory(prefix="unialloc-vec-capacity-owner-") as raw:
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
                "source": "mir_vec_capacity_outer_owner_probe",
                "validated": True,
                "evidence": evidence,
            },
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
