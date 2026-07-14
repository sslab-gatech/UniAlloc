#!/usr/bin/env python3
"""Prove exact ``std::fs::DirEntry::path`` scopes use ``PathBuf`` identity."""

from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile


ROOT = Path(__file__).resolve().parents[2]
PASS_SOURCE = ROOT / "tools/unialloc-rustc-pass/unialloc-rustc-mir-rewrite-dry-run.rs"
PROBE_NAME = "dir_entry_path_outer_owner_probe"
FACTORY_FUNCTION = "make_entry_path"
CUSTOM_NEGATIVE_FUNCTION = "custom_path_negative"
OPAQUE_NEGATIVE_FUNCTION = "call_opaque_factory_negative"
APPLIED_STATUS = "actual_semantic_scope_enter_exit_rewrite_applied"
UNRESOLVED_CONTRACT = "audit_only_unresolved_heap_object_type"


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
    shutil.copy2(ROOT / "Cargo.lock", app / "Cargo.lock")
    (app / "src/main.rs").write_text(
        r'''use std::fs::{self, DirEntry};
use std::path::PathBuf;
use unialloc::{
    semantic_auto_metadata_disable, semantic_fallback_attribution_snapshot,
    semantic_metadata_validation_snapshot, semantic_stats_recording_disable,
    semantic_stats_reset, semantic_stats_snapshot, type_isolation_side_cache_snapshot,
    UniAlloc,
};

#[global_allocator]
static ALLOCATOR: UniAlloc = UniAlloc;

const FILE_NAME: &str = "0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef.txt";

#[inline(never)]
fn make_entry_path(entry: &DirEntry) -> PathBuf {
    entry.path()
}

struct CustomEntry;

impl CustomEntry {
    #[inline(never)]
    fn path(&self) -> PathBuf {
        PathBuf::new()
    }
}

#[inline(never)]
fn custom_path_negative(entry: &CustomEntry) -> PathBuf {
    entry.path()
}

#[inline(never)]
fn opaque_path_factory(entry: &DirEntry) -> PathBuf {
    entry.path()
}

#[inline(never)]
fn call_opaque_factory_negative(entry: &DirEntry) -> PathBuf {
    opaque_path_factory(entry)
}

#[inline(never)]
fn opaque_false() -> bool {
    unsafe { std::ptr::read_volatile(&false) }
}

fn prepare_entry() -> (PathBuf, DirEntry) {
    let root = std::env::temp_dir().join(format!(
        "unialloc-dir-entry-path-{}-{}",
        std::process::id(),
        std::thread::current().name().unwrap_or("main")
    ));
    let _ = fs::remove_dir_all(&root);
    fs::create_dir_all(&root).unwrap();
    fs::write(root.join(FILE_NAME), []).unwrap();
    let entry = fs::read_dir(&root).unwrap().next().unwrap().unwrap();
    (root, entry)
}

fn main() {
    let (root, entry) = prepare_entry();
    let expected = root.join(FILE_NAME);
    if opaque_false() {
        drop(custom_path_negative(&CustomEntry));
        drop(call_opaque_factory_negative(&entry));
    }

    semantic_auto_metadata_disable();
    semantic_stats_reset();

    let path = make_entry_path(&entry);
    assert_eq!(path, expected);
    let path_type_id = semantic_stats_snapshot().last_type_id;
    drop(path);

    let stats = semantic_stats_snapshot();
    let fallback = semantic_fallback_attribution_snapshot();
    let validation = semantic_metadata_validation_snapshot();
    let side_cache = type_isolation_side_cache_snapshot();
    semantic_stats_recording_disable();

    drop(entry);
    fs::remove_dir_all(&root).unwrap();

    println!(
        concat!(
            "{{",
            "\"source\":\"dir_entry_path_outer_owner_probe\",",
            "\"path_type_id\":{},",
            "\"typed_allocations\":{},",
            "\"typed_deallocations\":{},",
            "\"fallback_allocations\":{},",
            "\"fallback_deallocations\":{},",
            "\"raw_alloc_no_metadata\":{},",
            "\"raw_dealloc_no_metadata\":{},",
            "\"raw_realloc_no_metadata\":{},",
            "\"recovery_identity_mismatches\":{},",
            "\"side_cache_corrupt_slots\":{}",
            "}}"
        ),
        path_type_id,
        stats.typed_allocations,
        stats.typed_deallocations,
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


def assert_unresolved(row: dict[str, object]) -> None:
    assert row.get("rewrite_status") != APPLIED_STATUS, row
    assert row.get("metadata_pairing_contract") == UNRESOLVED_CONTRACT, row


def validate(audit: dict[str, object], stdout: str) -> dict[str, object]:
    rows = audit.get("rewrite_candidates")
    assert isinstance(rows, list), rows

    matches = rows_for(rows, FACTORY_FUNCTION, "::fs::{impl#")
    matches = [row for row in matches if "::path" in str(row.get("callee") or "")]
    assert len(matches) == 1, matches
    factory = matches[0]
    assert factory.get("lowering_kind") == "semantic_scope_enter_exit_rewrite", factory
    assert factory.get("rewrite_status") == APPLIED_STATUS, factory
    assert factory.get("semantic_object_type") == "std::path::PathBuf", factory
    assert int(factory.get("type_id") or 0) != 0, factory
    assert factory.get("metadata_pairing_contract") == "semantic_scope_active_metadata", factory
    assert factory.get("semantic_scope_unwind_pop_inserted") is True, factory
    assert factory.get("destination_type") == "std::path::PathBuf", factory
    assert factory.get("argument_types") == ["&'{erased} std::fs::DirEntry"], factory

    custom = rows_for(rows, CUSTOM_NEGATIVE_FUNCTION, "::path")
    assert len(custom) == 1, custom
    assert_unresolved(custom[0])

    opaque = rows_for(rows, OPAQUE_NEGATIVE_FUNCTION, "opaque_path_factory")
    assert len(opaque) == 1, opaque
    assert_unresolved(opaque[0])

    path_drop_rows = [
        row
        for row in rows
        if isinstance(row, dict)
        and row.get("lowering_kind") == "semantic_scope_drop_rewrite"
        and row.get("rewrite_status") == "actual_semantic_scope_drop_rewrite_applied"
        and row.get("semantic_object_type") == "std::path::PathBuf"
    ]
    assert path_drop_rows, path_drop_rows
    assert any(
        int(row.get("type_id") or 0) == int(factory["type_id"])
        and row.get("metadata_pairing_contract") == "semantic_scope_drop_active_metadata"
        for row in path_drop_rows
    ), path_drop_rows

    runtime = next(
        json.loads(line)
        for line in stdout.splitlines()
        if line.startswith("{") and PROBE_NAME in line
    )
    assert int(runtime["path_type_id"]) == int(factory["type_id"]), runtime
    assert int(runtime["typed_allocations"]) == 3, runtime
    assert int(runtime["typed_deallocations"]) == 3, runtime
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
        "factory": {
            key: factory.get(key)
            for key in (
                "callee",
                "argument_types",
                "destination_type",
                "semantic_object_type",
                "type_id",
                "rewrite_status",
            )
        },
        "fail_closed_controls": {
            "custom": {
                key: custom[0].get(key)
                for key in ("callee", "rewrite_status", "metadata_pairing_contract")
            },
            "opaque": {
                key: opaque[0].get(key)
                for key in ("callee", "rewrite_status", "metadata_pairing_contract")
            },
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

    with tempfile.TemporaryDirectory(prefix="unialloc-dir-entry-path-") as raw:
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
                "GLIBC_TUNABLES": "glibc.pthread.rseq=0",
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
                    "Only the exact std DirEntry::path method with a canonical immutable DirEntry receiver and PathBuf destination is covered.",
                    "Custom same-name and opaque helper calls remain audit-only.",
                    "The runtime oracle covers the internal PathBuf allocation and reallocation plus matching drop metadata in one process.",
                ],
            },
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
