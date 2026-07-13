#!/usr/bin/env python3
"""Prove exact ``alloc::vec::from_elem::<u8>`` scopes use outer identity."""

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
PROBE_NAME = "vec_from_elem_u8_outer_owner_probe"
EXACT_FUNCTION = "make_bytes"
WRONG_TYPE_FUNCTION = "make_wrong_string"
OTHER_ELEMENT_FUNCTION = "other_element_from_elem"
CUSTOM_CLONE_FUNCTION = "custom_clone_from_elem"
GENERIC_FUNCTION = "generic_from_elem"
CUSTOM_SAME_NAME_FUNCTION = "custom_same_name"
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

const BYTES: usize = 256;

#[inline(never)]
fn make_bytes() -> Vec<u8> {
    vec![0u8; BYTES]
}

#[inline(never)]
fn make_wrong_string() -> String {
    String::with_capacity(BYTES)
}

#[inline(never)]
fn other_element_from_elem() -> Vec<i8> {
    vec![0i8; BYTES]
}

struct AllocatingClone(u8);

impl Clone for AllocatingClone {
    #[inline(never)]
    fn clone(&self) -> Self {
        let nested = String::with_capacity(32);
        drop(nested);
        Self(self.0)
    }
}

#[inline(never)]
fn custom_clone_from_elem() -> Vec<AllocatingClone> {
    vec![AllocatingClone(1); 2]
}

#[inline(never)]
fn generic_from_elem<T: Clone>(value: T, count: usize) -> Vec<T> {
    vec![value; count]
}

mod custom_vec {
    #[inline(never)]
    pub fn from_elem(value: u8, count: usize) -> Vec<u8> {
        let mut result = Vec::with_capacity(count);
        result.resize(count, value);
        result
    }
}

#[inline(never)]
fn custom_same_name() -> Vec<u8> {
    custom_vec::from_elem(0, BYTES)
}

#[inline(never)]
fn opaque_false() -> bool {
    unsafe { std::ptr::read_volatile(&false) }
}

fn main() {
    if opaque_false() {
        assert_eq!(other_element_from_elem().len(), BYTES);
        assert_eq!(custom_clone_from_elem().len(), 2);
        assert_eq!(generic_from_elem(0u8, BYTES).len(), BYTES);
        assert_eq!(custom_same_name().len(), BYTES);
    }

    semantic_auto_metadata_disable();
    semantic_stats_reset();

    let first = make_bytes();
    assert_eq!(first.len(), BYTES);
    assert_eq!(first.capacity(), BYTES);
    assert!(first.iter().all(|byte| *byte == 0));
    let first_pointer = first.as_ptr() as usize;
    let first_type_id = semantic_stats_snapshot().last_type_id;
    drop(first);

    let wrong = make_wrong_string();
    assert!(wrong.is_empty());
    assert!(wrong.capacity() >= BYTES);
    let wrong_pointer = wrong.as_ptr() as usize;
    let wrong_type_id = semantic_stats_snapshot().last_type_id;
    drop(wrong);

    let recovered = make_bytes();
    assert_eq!(recovered.len(), BYTES);
    assert_eq!(recovered.capacity(), BYTES);
    assert!(recovered.iter().all(|byte| *byte == 0));
    let recovered_pointer = recovered.as_ptr() as usize;
    let recovered_type_id = semantic_stats_snapshot().last_type_id;
    let wrong_type_non_reuse = wrong_pointer != first_pointer;
    let same_type_reuse = recovered_pointer == first_pointer;
    drop(recovered);

    let stats = semantic_stats_snapshot();
    let fallback = semantic_fallback_attribution_snapshot();
    let validation = semantic_metadata_validation_snapshot();
    let side_cache = type_isolation_side_cache_snapshot();
    semantic_stats_recording_disable();

    println!(
        concat!(
            "{{",
            "\"source\":\"vec_from_elem_u8_outer_owner_probe\",",
            "\"first_pointer\":{},",
            "\"wrong_pointer\":{},",
            "\"recovered_pointer\":{},",
            "\"first_type_id\":{},",
            "\"wrong_type_id\":{},",
            "\"recovered_type_id\":{},",
            "\"wrong_type_non_reuse\":{},",
            "\"same_type_reuse\":{},",
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
        same_type_reuse,
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

    exact_matches = rows_for(rows, EXACT_FUNCTION, "vec::from_elem")
    assert len(exact_matches) == 1, [
        row
        for row in rows
        if isinstance(row, dict) and function_matches(row, EXACT_FUNCTION)
    ]
    exact_row = exact_matches[0]
    assert exact_row.get("lowering_kind") == "semantic_scope_enter_exit_rewrite", exact_row
    assert exact_row.get("rewrite_status") == APPLIED_STATUS, exact_row
    assert "Vec<u8" in str(exact_row.get("semantic_object_type") or ""), exact_row
    argument_types = json.dumps(exact_row.get("argument_types") or [])
    assert "u8" in argument_types and "usize" in argument_types, exact_row
    assert int(exact_row.get("type_id") or 0) != 0, exact_row
    assert exact_row.get("metadata_pairing_contract") == "semantic_scope_active_metadata", exact_row

    wrong_matches = rows_for(rows, WRONG_TYPE_FUNCTION, "with_capacity")
    assert len(wrong_matches) == 1, wrong_matches
    wrong_row = wrong_matches[0]
    assert wrong_row.get("rewrite_status") == APPLIED_STATUS, wrong_row
    assert "String" in str(wrong_row.get("semantic_object_type") or ""), wrong_row
    assert int(wrong_row.get("type_id") or 0) != 0, wrong_row
    assert int(wrong_row["type_id"]) != int(exact_row["type_id"]), (exact_row, wrong_row)

    other_element_matches = rows_for(rows, OTHER_ELEMENT_FUNCTION, "vec::from_elem")
    assert len(other_element_matches) == 1, other_element_matches
    assert_fail_closed(other_element_matches[0])

    custom_clone_matches = rows_for(rows, CUSTOM_CLONE_FUNCTION, "vec::from_elem")
    assert len(custom_clone_matches) == 1, custom_clone_matches
    assert_fail_closed(custom_clone_matches[0])

    generic_matches = rows_for(rows, GENERIC_FUNCTION, "vec::from_elem")
    assert len(generic_matches) == 1, generic_matches
    assert_fail_closed(generic_matches[0])

    custom_matches = rows_for(rows, CUSTOM_SAME_NAME_FUNCTION, "custom_vec::from_elem")
    assert len(custom_matches) == 1, custom_matches
    assert_fail_closed(custom_matches[0])

    runtime = next(
        json.loads(line)
        for line in stdout.splitlines()
        if line.startswith("{") and PROBE_NAME in line
    )
    assert runtime["wrong_type_non_reuse"] is True, runtime
    assert runtime["same_type_reuse"] is True, runtime
    assert int(runtime["first_pointer"]) != int(runtime["wrong_pointer"]), runtime
    assert int(runtime["first_pointer"]) == int(runtime["recovered_pointer"]), runtime
    assert int(runtime["first_type_id"]) == int(exact_row["type_id"]), runtime
    assert int(runtime["wrong_type_id"]) == int(wrong_row["type_id"]), runtime
    assert int(runtime["recovered_type_id"]) == int(exact_row["type_id"]), runtime
    assert int(runtime["first_type_id"]) != int(runtime["wrong_type_id"]), runtime
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
        "exact_u8_from_elem": {
            key: exact_row.get(key)
            for key in ("callee", "semantic_object_type", "type_id", "rewrite_status")
        },
        "wrong_type_control": {
            key: wrong_row.get(key)
            for key in ("callee", "semantic_object_type", "type_id", "rewrite_status")
        },
        "fail_closed_controls": {
            name: {
                key: row.get(key)
                for key in ("callee", "rewrite_status", "metadata_pairing_contract")
            }
            for name, row in (
                ("other_element", other_element_matches[0]),
                ("custom_clone", custom_clone_matches[0]),
                ("generic", generic_matches[0]),
                ("custom_same_name", custom_matches[0]),
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

    with tempfile.TemporaryDirectory(prefix="unialloc-vec-from-elem-u8-") as raw:
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
                    "Only exact alloc-owned vec::from_elem with concrete u8, usize arguments and a Global-backed Vec<u8> destination is scoped; generic T, custom Clone, other element types, and custom same-name functions remain fail closed.",
                    "The address oracle covers one same-layout Vec<u8>/String cache sequence in one process; it is type-isolation correctness evidence, not whole-application coverage or performance evidence.",
                ],
            },
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
