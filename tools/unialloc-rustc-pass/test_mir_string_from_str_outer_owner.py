#!/usr/bin/env python3
"""Prove exact ``String::from(&str)`` scopes use the direct String identity."""

from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile


ROOT = Path(__file__).resolve().parents[2]
PASS_SOURCE = ROOT / "tools/unialloc-rustc-pass/unialloc-rustc-mir-rewrite-dry-run.rs"
PROBE_NAME = "string_from_str_outer_owner_probe"
STRING_FUNCTION = "make_string"
VEC_FUNCTION = "make_vec"
OWNED_NEGATIVE_FUNCTION = "from_owned_negative"
APPLIED_STATUS = "actual_semantic_scope_enter_exit_rewrite_applied"
BYTES = 192


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
    String::from(input)
}

#[inline(never)]
fn make_vec(capacity: usize) -> Vec<u8> {
    Vec::with_capacity(capacity)
}

#[inline(never)]
fn from_owned_negative(input: String) -> String {
    String::from(input)
}

#[inline(never)]
fn opaque_false() -> bool {
    unsafe { std::ptr::read_volatile(&false) }
}

fn main() {
    assert_eq!(PAYLOAD.len(), 192);
    if opaque_false() {
        let owned = from_owned_negative(String::new());
        assert!(owned.is_empty());
    }
    semantic_auto_metadata_disable();
    semantic_stats_reset();

    let first = make_string(PAYLOAD);
    assert_eq!(first, PAYLOAD);
    assert_eq!(first.capacity(), PAYLOAD.len());
    let first_pointer = first.as_ptr() as usize;
    let first_type_id = semantic_stats_snapshot().last_type_id;
    drop(first);

    let wrong = make_vec(PAYLOAD.len());
    assert!(wrong.is_empty());
    assert!(wrong.capacity() >= PAYLOAD.len());
    let wrong_pointer = wrong.as_ptr() as usize;
    let wrong_type_id = semantic_stats_snapshot().last_type_id;
    drop(wrong);

    let recovered = make_string(PAYLOAD);
    assert_eq!(recovered, PAYLOAD);
    let recovered_pointer = recovered.as_ptr() as usize;
    let recovered_type_id = semantic_stats_snapshot().last_type_id;
    let wrong_type_non_reuse = wrong_pointer != first_pointer;
    let exact_type_reuse = recovered_pointer == first_pointer;
    drop(recovered);

    let stats = semantic_stats_snapshot();
    let fallback = semantic_fallback_attribution_snapshot();
    let validation = semantic_metadata_validation_snapshot();
    let side_cache = type_isolation_side_cache_snapshot();
    semantic_stats_recording_disable();

    println!(
        concat!(
            "{{",
            "\"source\":\"string_from_str_outer_owner_probe\",",
            "\"first_pointer\":{},",
            "\"wrong_pointer\":{},",
            "\"recovered_pointer\":{},",
            "\"first_type_id\":{},",
            "\"wrong_type_id\":{},",
            "\"recovered_type_id\":{},",
            "\"wrong_type_non_reuse\":{},",
            "\"exact_type_reuse\":{},",
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
        wrong_pointer,
        recovered_pointer,
        first_type_id,
        wrong_type_id,
        recovered_type_id,
        wrong_type_non_reuse,
        exact_type_reuse,
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


def string_from_rows(
    rows: list[object], function_name: str = STRING_FUNCTION
) -> list[dict[str, object]]:
    return [
        row
        for row in rows
        if isinstance(row, dict)
        and function_matches(row, function_name)
        and "convert::From" in str(row.get("callee") or "")
        and "::from" in str(row.get("callee") or "")
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
    string_matches = string_from_rows(rows)
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

    vec_matches = rows_for(rows, VEC_FUNCTION, "with_capacity")
    assert len(vec_matches) == 1, vec_matches
    vec_row = vec_matches[0]
    assert vec_row.get("rewrite_status") == APPLIED_STATUS, vec_row
    assert "Vec<u8" in str(vec_row.get("semantic_object_type") or ""), vec_row
    assert int(vec_row.get("type_id") or 0) != 0, vec_row
    assert int(string_row["type_id"]) != int(vec_row["type_id"]), (string_row, vec_row)

    owned_matches = string_from_rows(rows, OWNED_NEGATIVE_FUNCTION)
    assert len(owned_matches) == 1, owned_matches
    owned_row = owned_matches[0]
    assert owned_row.get("rewrite_status") != APPLIED_STATUS, owned_row
    assert owned_row.get("metadata_pairing_contract") == (
        "audit_only_unresolved_heap_object_type"
    ), owned_row

    runtime = next(
        json.loads(line)
        for line in stdout.splitlines()
        if line.startswith("{") and PROBE_NAME in line
    )
    assert runtime["wrong_type_non_reuse"] is True, runtime
    assert runtime["exact_type_reuse"] is True, runtime
    assert int(runtime["first_pointer"]) != int(runtime["wrong_pointer"]), runtime
    assert int(runtime["first_pointer"]) == int(runtime["recovered_pointer"]), runtime
    assert int(runtime["first_type_id"]) == int(string_row["type_id"]), runtime
    assert int(runtime["wrong_type_id"]) == int(vec_row["type_id"]), runtime
    assert int(runtime["recovered_type_id"]) == int(string_row["type_id"]), runtime
    assert int(runtime["typed_allocations"]) == 3, runtime
    assert int(runtime["typed_deallocations"]) == 3, runtime
    assert int(runtime["typed_cache_hits"]) == 1, runtime
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
        "string_from_str": {
            key: string_row.get(key)
            for key in ("callee", "semantic_object_type", "type_id", "rewrite_status")
        },
        "vec_control": {
            key: vec_row.get(key)
            for key in ("callee", "semantic_object_type", "type_id", "rewrite_status")
        },
        "string_from_owned_negative": {
            key: owned_row.get(key)
            for key in ("callee", "rewrite_status", "metadata_pairing_contract")
        },
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

    with tempfile.TemporaryDirectory(prefix="unialloc-string-from-str-") as raw:
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
                    "Exact core From::from monomorphized as String <- &str only; String <- String, Box<str>, Cow<str>, and arbitrary factories are excluded.",
                    "The address oracle covers one same-size String/Vec pair and exact typed-cache reuse in one process, not universal String safety or performance.",
                ],
            },
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
