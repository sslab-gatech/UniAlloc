#!/usr/bin/env python3
"""Prove exact byte-copy ``ToOwned::to_owned`` scopes use outer identity."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile


ROOT = Path(__file__).resolve().parents[2]
PASS_SOURCE = ROOT / "tools/unialloc-rustc-pass/unialloc-rustc-mir-rewrite-dry-run.rs"
PROBE_NAME = "str_to_owned_outer_owner_probe"
STRING_FUNCTION = "make_string"
SLICE_FUNCTION = "slice_to_owned"
WRONG_VEC_FUNCTION = "make_wrong_vec"
CALLBACK_SLICE_NEGATIVE_FUNCTION = "callback_slice_to_owned_negative"
CUSTOM_NEGATIVE_FUNCTION = "custom_to_owned_negative"
GENERIC_NEGATIVE_FUNCTION = "generic_to_owned_negative"
APPLIED_STATUS = "actual_semantic_scope_enter_exit_rewrite_applied"
UNRESOLVED_CONTRACT = "audit_only_unresolved_heap_object_type"
AMBIGUOUS_CONTRACT = "audit_only_ambiguous_heap_object_type"


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
        r'''use unialloc::{
    semantic_auto_metadata_disable, semantic_fallback_attribution_snapshot,
    semantic_metadata_validation_snapshot, semantic_stats_recording_disable,
    semantic_stats_reset, semantic_stats_snapshot, type_isolation_side_cache_snapshot,
    UniAlloc,
};

#[global_allocator]
static ALLOCATOR: UniAlloc = UniAlloc;

const PAYLOAD: &str = "0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef";

#[inline(never)]
fn make_string(input: &str) -> String {
    input.to_owned()
}

#[inline(never)]
fn make_wrong_vec(capacity: usize) -> Vec<i8> {
    Vec::with_capacity(capacity)
}

#[inline(never)]
fn slice_to_owned(input: &[u8]) -> Vec<u8> {
    input.to_owned()
}

#[inline(never)]
fn callback_slice_to_owned_negative(input: &[String]) -> Vec<String> {
    input.to_owned()
}

struct Custom;

impl Custom {
    #[inline(never)]
    fn to_owned(&self) -> String {
        String::from("custom")
    }
}

#[inline(never)]
fn custom_to_owned_negative(input: &Custom) -> String {
    input.to_owned()
}

#[inline(never)]
fn generic_to_owned_negative<T: ToOwned + ?Sized>(input: &T) -> T::Owned {
    input.to_owned()
}

#[inline(never)]
fn opaque_false() -> bool {
    unsafe { std::ptr::read_volatile(&false) }
}

fn main() {
    assert_eq!(PAYLOAD.len(), 192);
    if opaque_false() {
        let callback_values = [String::new()];
        assert_eq!(callback_slice_to_owned_negative(&callback_values).len(), 1);
        assert_eq!(custom_to_owned_negative(&Custom), "custom");
        assert_eq!(generic_to_owned_negative(PAYLOAD), PAYLOAD);
    }
    semantic_auto_metadata_disable();
    semantic_stats_reset();

    let first = make_string(PAYLOAD);
    assert_eq!(first, PAYLOAD);
    assert_eq!(first.capacity(), PAYLOAD.len());
    let first_pointer = first.as_ptr() as usize;
    let first_type_id = semantic_stats_snapshot().last_type_id;
    drop(first);

    let first_slice = slice_to_owned(PAYLOAD.as_bytes());
    assert_eq!(first_slice, PAYLOAD.as_bytes());
    assert_eq!(first_slice.capacity(), PAYLOAD.len());
    let first_slice_pointer = first_slice.as_ptr() as usize;
    let first_slice_type_id = semantic_stats_snapshot().last_type_id;
    drop(first_slice);

    let wrong_vec = make_wrong_vec(PAYLOAD.len());
    assert!(wrong_vec.is_empty());
    assert!(wrong_vec.capacity() >= PAYLOAD.len());
    let wrong_vec_pointer = wrong_vec.as_ptr() as usize;
    let wrong_vec_type_id = semantic_stats_snapshot().last_type_id;
    drop(wrong_vec);

    let recovered_slice = slice_to_owned(PAYLOAD.as_bytes());
    assert_eq!(recovered_slice, PAYLOAD.as_bytes());
    let recovered_slice_pointer = recovered_slice.as_ptr() as usize;
    let recovered_slice_type_id = semantic_stats_snapshot().last_type_id;
    drop(recovered_slice);

    let recovered = make_string(PAYLOAD);
    assert_eq!(recovered, PAYLOAD);
    let recovered_pointer = recovered.as_ptr() as usize;
    let recovered_type_id = semantic_stats_snapshot().last_type_id;
    let string_wrong_type_non_reuse = first_slice_pointer != first_pointer;
    let string_exact_type_reuse = recovered_pointer == first_pointer;
    let slice_wrong_vec_non_reuse = wrong_vec_pointer != first_slice_pointer;
    let slice_exact_type_reuse = recovered_slice_pointer == first_slice_pointer;
    drop(recovered);

    let stats = semantic_stats_snapshot();
    let fallback = semantic_fallback_attribution_snapshot();
    let validation = semantic_metadata_validation_snapshot();
    let side_cache = type_isolation_side_cache_snapshot();
    semantic_stats_recording_disable();

    println!(
        concat!(
            "{{",
            "\"source\":\"str_to_owned_outer_owner_probe\",",
            "\"first_pointer\":{},",
            "\"recovered_pointer\":{},",
            "\"first_type_id\":{},",
            "\"recovered_type_id\":{},",
            "\"first_slice_pointer\":{},",
            "\"wrong_vec_pointer\":{},",
            "\"recovered_slice_pointer\":{},",
            "\"first_slice_type_id\":{},",
            "\"wrong_vec_type_id\":{},",
            "\"recovered_slice_type_id\":{},",
            "\"string_wrong_type_non_reuse\":{},",
            "\"string_exact_type_reuse\":{},",
            "\"slice_wrong_vec_non_reuse\":{},",
            "\"slice_exact_type_reuse\":{},",
            "\"typed_allocations\":{},",
            "\"typed_deallocations\":{},",
            "\"typed_cache_hits\":{},",
            "\"fallback_allocations\":{},",
            "\"fallback_deallocations\":{},",
            "\"raw_alloc_no_metadata\":{},",
            "\"raw_dealloc_no_metadata\":{},",
            "\"raw_realloc_no_metadata\":{},",
            "\"recovery_identity_mismatches\":{},",
            "\"side_cache_corrupt_slots\":{}",
            "}}"
        ),
        first_pointer,
        recovered_pointer,
        first_type_id,
        recovered_type_id,
        first_slice_pointer,
        wrong_vec_pointer,
        recovered_slice_pointer,
        first_slice_type_id,
        wrong_vec_type_id,
        recovered_slice_type_id,
        string_wrong_type_non_reuse,
        string_exact_type_reuse,
        slice_wrong_vec_non_reuse,
        slice_exact_type_reuse,
        stats.typed_allocations,
        stats.typed_deallocations,
        stats.typed_cache_hits,
        stats.fallback_allocations,
        stats.fallback_deallocations,
        fallback.raw_alloc_no_metadata,
        fallback.raw_dealloc_no_metadata,
        fallback.raw_realloc_no_metadata,
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


def rows_for(
    rows: list[object], function_name: str, callee_marker: str
) -> list[dict[str, object]]:
    return [
        row
        for row in rows
        if isinstance(row, dict)
        and function_matches(row, function_name)
        and callee_marker in str(row.get("callee") or "")
    ]


def trait_to_owned_rows(
    rows: list[object], function_name: str
) -> list[dict[str, object]]:
    return [
        row
        for row in rows
        if isinstance(row, dict)
        and function_matches(row, function_name)
        and "borrow::ToOwned" in str(row.get("callee") or "")
        and "::to_owned" in str(row.get("callee") or "")
    ]


def assert_unresolved(row: dict[str, object]) -> None:
    assert row.get("rewrite_status") != APPLIED_STATUS, row
    assert row.get("metadata_pairing_contract") == UNRESOLVED_CONTRACT, row


def assert_fail_closed(row: dict[str, object]) -> None:
    assert row.get("rewrite_status") != APPLIED_STATUS, row
    assert row.get("metadata_pairing_contract") in {
        UNRESOLVED_CONTRACT,
        AMBIGUOUS_CONTRACT,
    }, row


def validate(audit: dict[str, object], stdout: str) -> dict[str, object]:
    summary = audit.get("summary")
    assert isinstance(summary, dict), summary
    assert summary.get("provider_override_installed") is True, summary
    assert summary.get("body_clone_returned_to_rustc") is True, summary
    assert summary.get("actual_semantic_scope_rewrite_requested") is True, summary
    assert summary.get("actual_semantic_scope_rewrite") is True, summary

    rows = audit.get("rewrite_candidates")
    assert isinstance(rows, list), rows
    string_matches = trait_to_owned_rows(rows, STRING_FUNCTION)
    assert len(string_matches) == 1, [
        row
        for row in rows
        if isinstance(row, dict) and function_matches(row, STRING_FUNCTION)
    ]
    string_row = string_matches[0]
    assert string_row.get("lowering_kind") == "semantic_scope_enter_exit_rewrite", string_row
    assert string_row.get("rewrite_status") == APPLIED_STATUS, string_row
    assert "String" in str(string_row.get("semantic_object_type") or ""), string_row
    assert "&str" in json.dumps(string_row.get("argument_types") or []).replace(
        "'{erased} ", ""
    ), string_row
    assert int(string_row.get("type_id") or 0) != 0, string_row
    assert string_row.get("metadata_pairing_contract") == "semantic_scope_active_metadata", string_row

    slice_matches = trait_to_owned_rows(rows, SLICE_FUNCTION)
    assert len(slice_matches) == 1, slice_matches
    slice_row = slice_matches[0]
    assert slice_row.get("lowering_kind") == "semantic_scope_enter_exit_rewrite", slice_row
    assert slice_row.get("rewrite_status") == APPLIED_STATUS, slice_row
    assert "Vec<u8" in str(slice_row.get("semantic_object_type") or ""), slice_row
    assert "[u8]" in json.dumps(slice_row.get("argument_types") or []), slice_row
    assert int(slice_row.get("type_id") or 0) != 0, slice_row
    assert slice_row.get("metadata_pairing_contract") == "semantic_scope_active_metadata", slice_row
    assert int(string_row["type_id"]) != int(slice_row["type_id"]), (
        string_row,
        slice_row,
    )

    wrong_vec_matches = rows_for(rows, WRONG_VEC_FUNCTION, "with_capacity")
    assert len(wrong_vec_matches) == 1, wrong_vec_matches
    wrong_vec_row = wrong_vec_matches[0]
    assert wrong_vec_row.get("rewrite_status") == APPLIED_STATUS, wrong_vec_row
    assert "Vec<i8" in str(wrong_vec_row.get("semantic_object_type") or ""), wrong_vec_row
    assert int(wrong_vec_row.get("type_id") or 0) != 0, wrong_vec_row
    assert int(wrong_vec_row["type_id"]) not in {
        int(string_row["type_id"]),
        int(slice_row["type_id"]),
    }, (string_row, slice_row, wrong_vec_row)

    callback_slice_matches = trait_to_owned_rows(
        rows, CALLBACK_SLICE_NEGATIVE_FUNCTION
    )
    assert len(callback_slice_matches) == 1, callback_slice_matches
    assert_fail_closed(callback_slice_matches[0])

    generic_matches = trait_to_owned_rows(rows, GENERIC_NEGATIVE_FUNCTION)
    assert len(generic_matches) == 1, generic_matches
    assert_unresolved(generic_matches[0])

    custom_matches = rows_for(rows, CUSTOM_NEGATIVE_FUNCTION, "::to_owned")
    assert len(custom_matches) == 1, [
        row
        for row in rows
        if isinstance(row, dict) and function_matches(row, CUSTOM_NEGATIVE_FUNCTION)
    ]
    assert_unresolved(custom_matches[0])

    runtime = next(
        json.loads(line)
        for line in stdout.splitlines()
        if line.startswith("{") and PROBE_NAME in line
    )
    assert runtime["string_wrong_type_non_reuse"] is True, runtime
    assert runtime["string_exact_type_reuse"] is True, runtime
    assert runtime["slice_wrong_vec_non_reuse"] is True, runtime
    assert runtime["slice_exact_type_reuse"] is True, runtime
    assert int(runtime["first_pointer"]) != int(runtime["first_slice_pointer"]), runtime
    assert int(runtime["first_pointer"]) == int(runtime["recovered_pointer"]), runtime
    assert int(runtime["first_slice_pointer"]) != int(runtime["wrong_vec_pointer"]), runtime
    assert int(runtime["first_slice_pointer"]) == int(
        runtime["recovered_slice_pointer"]
    ), runtime
    assert int(runtime["first_type_id"]) == int(string_row["type_id"]), runtime
    assert int(runtime["recovered_type_id"]) == int(string_row["type_id"]), runtime
    assert int(runtime["first_slice_type_id"]) == int(slice_row["type_id"]), runtime
    assert int(runtime["wrong_vec_type_id"]) == int(wrong_vec_row["type_id"]), runtime
    assert int(runtime["recovered_slice_type_id"]) == int(slice_row["type_id"]), runtime
    assert int(runtime["typed_allocations"]) == 5, runtime
    assert int(runtime["typed_deallocations"]) == 5, runtime
    assert int(runtime["typed_cache_hits"]) == 2, runtime
    for field in (
        "fallback_allocations",
        "fallback_deallocations",
        "raw_alloc_no_metadata",
        "raw_dealloc_no_metadata",
        "raw_realloc_no_metadata",
        "recovery_identity_mismatches",
        "side_cache_corrupt_slots",
    ):
        assert int(runtime[field]) == 0, (field, runtime)

    return {
        "str_to_owned": {
            key: string_row.get(key)
            for key in ("callee", "semantic_object_type", "type_id", "rewrite_status")
        },
        "slice_to_owned": {
            key: slice_row.get(key)
            for key in ("callee", "semantic_object_type", "type_id", "rewrite_status")
        },
        "wrong_vec_control": {
            key: wrong_vec_row.get(key)
            for key in ("callee", "semantic_object_type", "type_id", "rewrite_status")
        },
        "fail_closed_controls": {
            name: {
                key: row.get(key)
                for key in ("callee", "rewrite_status", "metadata_pairing_contract")
            }
            for name, row in (
                ("callback_slice", callback_slice_matches[0]),
                ("generic", generic_matches[0]),
                ("custom", custom_matches[0]),
            )
        },
        "runtime": runtime,
    }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--toolchain")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    toolchain = args.toolchain or (ROOT / "rust-toolchain").read_text(
        encoding="utf-8"
    ).strip()
    rustc = shutil.which("rustc") or "rustc"
    cargo = shutil.which("cargo") or "cargo"
    sysroot = subprocess.check_output(
        [rustc, f"+{toolchain}", "--print", "sysroot"], cwd=ROOT, text=True
    ).strip()

    with tempfile.TemporaryDirectory(prefix="unialloc-str-to-owned-") as raw:
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
                    "Exact alloc-owned ToOwned::to_owned is scoped only for String <- &str and Global-backed Vec<u8> <- &[u8]; callback-bearing slices, generic calls, custom same-name methods, and arbitrary factories remain fail closed.",
                    "The address oracle covers same-layout String/Vec<u8>/Vec<i8> allocations and exact typed-cache reuse in one process, not universal ToOwned safety or performance.",
                ],
            },
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
