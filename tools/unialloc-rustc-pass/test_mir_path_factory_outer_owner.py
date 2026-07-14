#!/usr/bin/env python3
"""Prove exact borrowed ``Path`` factories use the direct ``PathBuf`` identity."""

from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile


ROOT = Path(__file__).resolve().parents[2]
PASS_SOURCE = ROOT / "tools/unialloc-rustc-pass/unialloc-rustc-mir-rewrite-dry-run.rs"
PROBE_NAME = "path_factory_outer_owner_probe"
TO_PATH_BUF_FUNCTION = "make_path_buf"
JOIN_FUNCTION = "join_paths"
JOIN_STR_FUNCTION = "join_str"
JOIN_OS_STR_FUNCTION = "join_os_str"
BY_VALUE_NEGATIVE_FUNCTION = "by_value_join_negative"
GENERIC_NEGATIVE_FUNCTION = "generic_join_negative"
CUSTOM_NEGATIVE_FUNCTION = "custom_join_negative"
OPAQUE_CALL_NEGATIVE_FUNCTION = "call_opaque_factory_negative"
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
        r'''use std::path::{Path, PathBuf};
use unialloc::{
    semantic_auto_metadata_disable, semantic_fallback_attribution_snapshot,
    semantic_metadata_validation_snapshot, semantic_stats_recording_disable,
    semantic_stats_reset, semantic_stats_snapshot, type_isolation_side_cache_snapshot,
    UniAlloc,
};

#[global_allocator]
static ALLOCATOR: UniAlloc = UniAlloc;

const BASE: &str = "0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef";
const CHILD: &str = "fedcba9876543210fedcba9876543210fedcba9876543210fedcba9876543210";

#[inline(never)]
fn make_path_buf(input: &Path) -> PathBuf {
    input.to_path_buf()
}

#[inline(never)]
fn join_paths(base: &Path, child: &Path) -> PathBuf {
    base.join(child)
}

#[inline(never)]
fn join_str(base: &Path, child: &str) -> PathBuf {
    base.join(child)
}

#[inline(never)]
fn join_os_str(base: &Path, child: &std::ffi::OsStr) -> PathBuf {
    base.join(child)
}

#[inline(never)]
fn by_value_join_negative(base: &Path, child: PathBuf) -> PathBuf {
    base.join(child)
}

#[inline(never)]
fn generic_join_negative<P: AsRef<Path>>(base: &Path, child: P) -> PathBuf {
    base.join(child)
}

struct CustomPath;

impl CustomPath {
    #[inline(never)]
    fn join(&self, _child: &Path) -> PathBuf {
        PathBuf::new()
    }
}

#[inline(never)]
fn custom_join_negative(base: &CustomPath, child: &Path) -> PathBuf {
    base.join(child)
}

#[inline(never)]
fn opaque_path_factory(base: &Path, child: &Path) -> PathBuf {
    base.join(child)
}

#[inline(never)]
fn call_opaque_factory_negative(base: &Path, child: &Path) -> PathBuf {
    opaque_path_factory(base, child)
}

#[inline(never)]
fn opaque_false() -> bool {
    unsafe { std::ptr::read_volatile(&false) }
}

fn main() {
    assert_eq!(BASE.len(), 192);
    if opaque_false() {
        let base = Path::new(BASE);
        let child = Path::new(CHILD);
        drop(join_str(base, CHILD));
        drop(join_os_str(base, child.as_os_str()));
        drop(by_value_join_negative(base, child.to_path_buf()));
        drop(generic_join_negative(base, child));
        drop(custom_join_negative(&CustomPath, child));
        drop(call_opaque_factory_negative(base, child));
    }

    semantic_auto_metadata_disable();
    semantic_stats_reset();

    let first = make_path_buf(Path::new(BASE));
    let first_pointer = first.as_os_str().as_encoded_bytes().as_ptr() as usize;
    let first_type_id = semantic_stats_snapshot().last_type_id;
    drop(first);

    let recovered = make_path_buf(Path::new(BASE));
    let recovered_pointer = recovered.as_os_str().as_encoded_bytes().as_ptr() as usize;
    let recovered_type_id = semantic_stats_snapshot().last_type_id;
    let exact_type_reuse = recovered_pointer == first_pointer;
    drop(recovered);

    let joined = join_paths(Path::new(BASE), Path::new(CHILD));
    assert_eq!(joined.file_name(), Some(std::ffi::OsStr::new(CHILD)));
    let joined_type_id = semantic_stats_snapshot().last_type_id;
    drop(joined);

    let stats = semantic_stats_snapshot();
    let fallback = semantic_fallback_attribution_snapshot();
    let validation = semantic_metadata_validation_snapshot();
    let side_cache = type_isolation_side_cache_snapshot();
    semantic_stats_recording_disable();

    println!(
        concat!(
            "{{",
            "\"source\":\"path_factory_outer_owner_probe\",",
            "\"first_pointer\":{},",
            "\"recovered_pointer\":{},",
            "\"first_type_id\":{},",
            "\"recovered_type_id\":{},",
            "\"joined_type_id\":{},",
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
        recovered_pointer,
        first_type_id,
        recovered_type_id,
        joined_type_id,
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


def assert_unresolved(row: dict[str, object]) -> None:
    assert row.get("rewrite_status") != APPLIED_STATUS, row
    assert row.get("metadata_pairing_contract") == UNRESOLVED_CONTRACT, row


def assert_applied_path_factory(row: dict[str, object]) -> None:
    assert row.get("lowering_kind") == "semantic_scope_enter_exit_rewrite", row
    assert row.get("rewrite_status") == APPLIED_STATUS, row
    assert row.get("semantic_object_type") == "std::path::PathBuf", row
    assert int(row.get("type_id") or 0) != 0, row
    assert row.get("metadata_pairing_contract") == "semantic_scope_active_metadata", row
    assert row.get("semantic_scope_unwind_pop_inserted") is True, row


def validate(audit: dict[str, object], stdout: str) -> dict[str, object]:
    summary = audit.get("summary")
    assert isinstance(summary, dict), summary
    assert summary.get("provider_override_installed") is True, summary
    assert summary.get("body_clone_returned_to_rustc") is True, summary
    assert summary.get("actual_semantic_scope_rewrite_requested") is True, summary
    assert summary.get("actual_semantic_scope_rewrite") is True, summary

    rows = audit.get("rewrite_candidates")
    assert isinstance(rows, list), rows

    to_path_buf_matches = rows_for(rows, TO_PATH_BUF_FUNCTION, "::to_path_buf")
    assert len(to_path_buf_matches) == 1, to_path_buf_matches
    to_path_buf_row = to_path_buf_matches[0]
    assert_applied_path_factory(to_path_buf_row)
    assert len(to_path_buf_row.get("argument_types") or []) == 1, to_path_buf_row
    assert "&'{erased} std::path::Path" in str(
        to_path_buf_row.get("argument_types")
    ), to_path_buf_row

    join_matches = rows_for(rows, JOIN_FUNCTION, "::join")
    assert len(join_matches) == 1, join_matches
    join_row = join_matches[0]
    assert_applied_path_factory(join_row)
    assert len(join_row.get("argument_types") or []) == 2, join_row
    assert all(
        "&'{erased} std::path::Path" in str(argument)
        for argument in join_row.get("argument_types") or []
    ), join_row
    assert int(to_path_buf_row["type_id"]) == int(join_row["type_id"]), (
        to_path_buf_row,
        join_row,
    )

    borrowed_join_rows = {
        "str": rows_for(rows, JOIN_STR_FUNCTION, "::join"),
        "os_str": rows_for(rows, JOIN_OS_STR_FUNCTION, "::join"),
    }
    for name, matches in borrowed_join_rows.items():
        assert len(matches) == 1, (name, matches)
        assert_applied_path_factory(matches[0])
        assert int(matches[0]["type_id"]) == int(join_row["type_id"]), matches[0]

    negative_rows = {
        "by_value": rows_for(rows, BY_VALUE_NEGATIVE_FUNCTION, "::join"),
        "generic": rows_for(rows, GENERIC_NEGATIVE_FUNCTION, "::join"),
        "custom": rows_for(rows, CUSTOM_NEGATIVE_FUNCTION, "::join"),
        "opaque": rows_for(
            rows, OPAQUE_CALL_NEGATIVE_FUNCTION, "opaque_path_factory"
        ),
    }
    for name, matches in negative_rows.items():
        assert len(matches) == 1, (name, matches)
        assert_unresolved(matches[0])

    path_drop_rows = [
        row
        for row in rows
        if isinstance(row, dict)
        and row.get("lowering_kind") == "semantic_scope_drop_rewrite"
        and row.get("rewrite_status") == "actual_semantic_scope_drop_rewrite_applied"
        and row.get("semantic_object_type") == "std::path::PathBuf"
    ]
    assert path_drop_rows, path_drop_rows
    assert all(
        int(row.get("type_id") or 0) == int(join_row["type_id"])
        and row.get("metadata_pairing_contract")
        == "semantic_scope_drop_active_metadata"
        for row in path_drop_rows
    ), path_drop_rows

    runtime = next(
        json.loads(line)
        for line in stdout.splitlines()
        if line.startswith("{") and PROBE_NAME in line
    )
    assert runtime["exact_type_reuse"] is True, runtime
    assert int(runtime["first_pointer"]) == int(runtime["recovered_pointer"]), runtime
    expected_type_id = int(to_path_buf_row["type_id"])
    for field in ("first_type_id", "recovered_type_id", "joined_type_id"):
        assert int(runtime[field]) == expected_type_id, (field, runtime)
    assert int(runtime["typed_allocations"]) == 4, runtime
    assert int(runtime["typed_deallocations"]) == 4, runtime
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
        "to_path_buf": {
            key: to_path_buf_row.get(key)
            for key in ("callee", "semantic_object_type", "type_id", "rewrite_status")
        },
        "join": {
            key: join_row.get(key)
            for key in ("callee", "semantic_object_type", "type_id", "rewrite_status")
        },
        "fail_closed_controls": {
            name: {
                key: matches[0].get(key)
                for key in ("callee", "rewrite_status", "metadata_pairing_contract")
            }
            for name, matches in negative_rows.items()
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

    with tempfile.TemporaryDirectory(prefix="unialloc-path-factory-") as raw:
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
                    "Only exact std Path::to_path_buf and Path::join calls with a PathBuf destination and canonical immutable borrowed Path, OsStr, or str arguments are covered.",
                    "By-value, generic, custom same-name, and opaque helper calls remain audit-only.",
                    "The runtime oracle covers PathBuf metadata continuity, recovery, and the internal join reallocation in one process; it makes no performance claim.",
                ],
            },
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
