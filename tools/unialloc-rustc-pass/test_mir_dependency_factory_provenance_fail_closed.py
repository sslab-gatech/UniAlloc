#!/usr/bin/env python3
"""Fail closed on opaque dependency factories without allocation provenance."""

from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile


ROOT = Path(__file__).resolve().parents[2]
PASS_SOURCE = ROOT / "tools/unialloc-rustc-pass/unialloc-rustc-mir-rewrite-dry-run.rs"
PROBE_NAME = "dependency_factory_provenance_probe"
DIRECT_FACTORY = "opaque_vec"
RESULT_FACTORY = "opaque_result_vec"
POSITIVE_FUNCTION = "exact_vec_with_capacity"
APPLIED_STATUS = "actual_semantic_scope_enter_exit_rewrite_applied"
UNRESOLVED_STATUS = "semantic_scope_rewrite_skipped_unresolved_heap_object_type"


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


def write_probe(workspace: Path) -> None:
    dependency = workspace / "opaque-dependency-factory"
    app = workspace / PROBE_NAME
    (dependency / "src").mkdir(parents=True)
    (app / "src").mkdir(parents=True)
    (dependency / "Cargo.toml").write_text(
        """[package]
name = "opaque-dependency-factory"
version = "0.1.0"
edition = "2021"
""",
        encoding="utf-8",
    )
    (dependency / "src/lib.rs").write_text(
        r'''#[inline(never)]
pub fn opaque_vec() -> Vec<u8> {
    let mut transient = String::with_capacity(64);
    transient.push_str("dependency-only-transient-string");
    assert_eq!(transient.len(), 32);
    drop(transient);
    Vec::new()
}

#[inline(never)]
pub fn opaque_result_vec() -> Result<Vec<u8>, ()> {
    let mut transient = String::with_capacity(96);
    transient.push_str("dependency-only-result-transient-string");
    assert_eq!(transient.len(), 39);
    drop(transient);
    Ok(Vec::new())
}
''',
        encoding="utf-8",
    )
    (app / "Cargo.toml").write_text(
        f'''[package]
name = "{PROBE_NAME.replace('_', '-')}"
version = "0.1.0"
edition = "2021"

[dependencies]
lock_api = "=0.4.3"
opaque-dependency-factory = {{ path = "../opaque-dependency-factory" }}
unialloc = {{ path = {json.dumps(str(ROOT / "unialloc"))}, features = ["stats", "type_isolation"] }}
''',
        encoding="utf-8",
    )
    (app / "src/main.rs").write_text(
        r'''use unialloc::{
    semantic_auto_metadata_disable, semantic_stats_recording_disable,
    semantic_stats_reset, semantic_stats_snapshot, UniAlloc,
};

#[global_allocator]
static ALLOCATOR: UniAlloc = UniAlloc;

#[inline(never)]
fn exact_vec_with_capacity(capacity: usize) -> Vec<u8> {
    Vec::with_capacity(capacity)
}

fn main() {
    semantic_auto_metadata_disable();

    semantic_stats_reset();
    let direct = opaque_dependency_factory::opaque_vec();
    assert!(direct.is_empty());
    assert_eq!(direct.capacity(), 0);
    drop(direct);
    let direct_stats = semantic_stats_snapshot();

    semantic_stats_reset();
    let result = opaque_dependency_factory::opaque_result_vec().unwrap();
    assert!(result.is_empty());
    assert_eq!(result.capacity(), 0);
    drop(result);
    let result_stats = semantic_stats_snapshot();

    semantic_stats_reset();
    let exact = exact_vec_with_capacity(64);
    assert!(exact.capacity() >= 64);
    let exact_after_alloc = semantic_stats_snapshot();
    drop(exact);
    let exact_after_drop = semantic_stats_snapshot();

    semantic_stats_recording_disable();
    println!(
        concat!(
            "{{",
            "\"source\":\"dependency_factory_provenance_probe\",",
            "\"direct_typed_allocations\":{},",
            "\"direct_typed_deallocations\":{},",
            "\"direct_last_type_id\":{},",
            "\"result_typed_allocations\":{},",
            "\"result_typed_deallocations\":{},",
            "\"result_last_type_id\":{},",
            "\"exact_typed_allocations\":{},",
            "\"exact_typed_deallocations\":{},",
            "\"exact_type_id\":{}",
            "}}"
        ),
        direct_stats.typed_allocations,
        direct_stats.typed_deallocations,
        direct_stats.last_type_id,
        result_stats.typed_allocations,
        result_stats.typed_deallocations,
        result_stats.last_type_id,
        exact_after_alloc.typed_allocations,
        exact_after_drop.typed_deallocations,
        exact_after_alloc.last_type_id,
    );
}
''',
        encoding="utf-8",
    )


def function_matches(row: dict[str, object], function_name: str) -> bool:
    function = str(row.get("mir_function") or "")
    return function == function_name or function.endswith(f"::{function_name}")


def call_rows(
    audit: dict[str, object], caller: str, callee_marker: str
) -> list[dict[str, object]]:
    rows = audit.get("rewrite_candidates")
    assert isinstance(rows, list), rows
    return [
        row
        for row in rows
        if isinstance(row, dict)
        and function_matches(row, caller)
        and callee_marker in str(row.get("callee") or "")
    ]


def validate(audit: dict[str, object], stdout: str) -> dict[str, object]:
    summary = audit.get("summary")
    assert isinstance(summary, dict), summary
    assert summary.get("provider_override_installed") is True, summary
    assert summary.get("body_clone_returned_to_rustc") is True, summary
    assert summary.get("actual_semantic_scope_rewrite_requested") is True, summary
    assert summary.get("actual_semantic_scope_rewrite") is True, summary

    dependency_rows: dict[str, dict[str, object]] = {}
    for callee_marker in (DIRECT_FACTORY, RESULT_FACTORY):
        matches = call_rows(audit, "main", callee_marker)
        assert len(matches) == 1, (callee_marker, matches)
        row = matches[0]
        assert row.get("lowering_kind") == (
            "semantic_scope_unsolved_heap_object_candidate"
        ), row
        assert row.get("rewrite_status") == UNRESOLVED_STATUS, row
        assert row.get("replacement_resolution_status") == (
            "rustc_middle_heap_object_type_not_solved"
        ), row
        assert row.get("metadata_pairing_contract") == (
            "audit_only_unresolved_heap_object_type"
        ), row
        assert row.get("semantic_scope_unwind_pop_inserted") is False, row
        assert not [
            candidate
            for candidate in matches
            if candidate.get("rewrite_status") == APPLIED_STATUS
        ], matches
        assert "Vec<u8" in str(row.get("destination_type") or ""), row
        dependency_rows[callee_marker] = row

    positive_matches = call_rows(
        audit, POSITIVE_FUNCTION, "with_capacity"
    )
    assert len(positive_matches) == 1, positive_matches
    positive = positive_matches[0]
    assert positive.get("lowering_kind") == "semantic_scope_enter_exit_rewrite", positive
    assert positive.get("rewrite_status") == APPLIED_STATUS, positive
    assert "Vec<u8" in str(positive.get("semantic_object_type") or ""), positive
    assert int(positive.get("type_id") or 0) != 0, positive

    runtime = next(
        json.loads(line)
        for line in stdout.splitlines()
        if line.startswith("{") and PROBE_NAME in line
    )
    # The dependency returns empty Vec values, so both measured allocations are
    # transient Strings internal to the opaque dependency body. Caller-side
    # return-type scoping would incorrectly count them as typed Vec work.
    for prefix in ("direct", "result"):
        assert int(runtime[f"{prefix}_typed_allocations"]) == 0, runtime
        assert int(runtime[f"{prefix}_typed_deallocations"]) == 0, runtime
        assert int(runtime[f"{prefix}_last_type_id"]) == 0, runtime
    assert int(runtime["exact_typed_allocations"]) == 1, runtime
    assert int(runtime["exact_typed_deallocations"]) == 1, runtime
    assert int(runtime["exact_type_id"]) == int(positive["type_id"]), runtime

    return {
        "dependency_factories": {
            callee: {
                key: row.get(key)
                for key in (
                    "callee",
                    "destination_type",
                    "rewrite_status",
                    "replacement_resolution_status",
                    "metadata_pairing_contract",
                )
            }
            for callee, row in dependency_rows.items()
        },
        "exact_vec_with_capacity_positive": {
            key: positive.get(key)
            for key in (
                "callee",
                "semantic_object_type",
                "type_id",
                "rewrite_status",
            )
        },
        "runtime": runtime,
    }


def main() -> int:
    toolchain = os.environ.get("UNIALLOC_TEST_TOOLCHAIN") or (
        ROOT / "rust-toolchain"
    ).read_text(encoding="utf-8").strip()
    rustc = shutil.which("rustc") or "rustc"
    cargo = shutil.which("cargo") or "cargo"
    sysroot = subprocess.check_output(
        [rustc, f"+{toolchain}", "--print", "sysroot"], cwd=ROOT, text=True
    ).strip()

    with tempfile.TemporaryDirectory(prefix="unialloc-dependency-factory-") as raw:
        workspace = Path(raw)
        write_probe(workspace)
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
                str(workspace / PROBE_NAME / "Cargo.toml"),
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
                "source": "dependency_factory_provenance_fail_closed_probe",
                "validated": True,
                "toolchain": toolchain,
                "evidence": evidence,
            },
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
