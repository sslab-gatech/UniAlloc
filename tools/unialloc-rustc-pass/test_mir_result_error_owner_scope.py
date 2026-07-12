#!/usr/bin/env python3
"""Reject semantic scopes inferred only from a Result error owner."""

from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile


ROOT = Path(__file__).resolve().parents[2]
PASS_SOURCE = ROOT / "tools/unialloc-rustc-pass/unialloc-rustc-mir-rewrite-dry-run.rs"
PROBE_NAME = "result_error_owner_scope_probe"
ARBITRARY_CALLEE = "arbitrary_status"
FALLIBLE_FACTORY_CALLEE = "ok_only_vec_factory"
CONFLICTING_FACTORY_CALLEE = "conflicting_fallible_vec_factory"
APPLIED_STATUS = "actual_semantic_scope_enter_exit_rewrite_applied"
AMBIGUOUS_STATUS = "semantic_scope_rewrite_skipped_ambiguous_heap_object_type"


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
    semantic_auto_metadata_disable, semantic_metadata_validation_snapshot,
    semantic_stats_reset, semantic_stats_snapshot, type_isolation_side_cache_snapshot,
    UniAlloc,
};

#[global_allocator]
static ALLOCATOR: UniAlloc = UniAlloc;

#[derive(Debug)]
struct ErrorWithBox<T> {
    message: T,
}

type BoxError = ErrorWithBox<Box<str>>;

#[inline(never)]
fn opaque_false() -> bool {
    unsafe { std::ptr::read_volatile(&false) }
}

#[inline(never)]
fn arbitrary_status() -> Result<(), BoxError> {
    let mut bytes = Vec::<u8>::new();
    bytes.reserve_exact(64);
    bytes.extend_from_slice(b"unrelated-internal-allocation");
    assert_eq!(bytes.as_slice(), b"unrelated-internal-allocation");
    drop(bytes);

    if opaque_false() {
        return Err(ErrorWithBox {
            message: Box::<str>::from("not-taken"),
        });
    }
    Ok(())
}

#[inline(never)]
fn ok_only_vec_factory() -> Result<Vec<u8>, u8> {
    let mut bytes = Vec::with_capacity(4);
    bytes.extend_from_slice(&[2, 3, 5, 7]);
    Ok(bytes)
}

#[inline(never)]
fn conflicting_fallible_vec_factory() -> Result<Vec<u8>, BoxError> {
    let mut bytes = Vec::with_capacity(4);
    bytes.extend_from_slice(&[11, 13, 17, 19]);
    if opaque_false() {
        return Err(ErrorWithBox {
            message: Box::<str>::from("not-taken"),
        });
    }
    Ok(bytes)
}

fn main() {
    semantic_auto_metadata_disable();
    semantic_stats_reset();

    let before_arbitrary_stats = semantic_stats_snapshot();
    let before_arbitrary_validation = semantic_metadata_validation_snapshot();
    let status = arbitrary_status();
    let after_arbitrary_stats = semantic_stats_snapshot();
    let after_arbitrary_validation = semantic_metadata_validation_snapshot();
    assert!(status.is_ok());

    let factory_value = ok_only_vec_factory().expect("positive-control factory succeeds");
    assert_eq!(factory_value.as_slice(), [2, 3, 5, 7]);
    drop(factory_value);

    let conflicting_factory_value =
        conflicting_fallible_vec_factory().expect("conflicting factory succeeds");
    assert_eq!(conflicting_factory_value.as_slice(), [11, 13, 17, 19]);
    drop(conflicting_factory_value);

    let final_validation = semantic_metadata_validation_snapshot();
    let side_cache = type_isolation_side_cache_snapshot();
    println!(
        concat!(
            "{{",
            "\"source\":\"result_error_owner_scope_probe\",",
            "\"arbitrary_result_ok\":true,",
            "\"factory_result_correct\":true,",
            "\"conflicting_factory_result_correct\":true,",
            "\"arbitrary_typed_allocations\":{},",
            "\"arbitrary_typed_deallocations\":{},",
            "\"arbitrary_recovery_mismatch_delta\":{},",
            "\"recovery_identity_mismatches\":{},",
            "\"side_cache_corrupt_slots\":{}",
            "}}"
        ),
        after_arbitrary_stats
            .typed_allocations
            .saturating_sub(before_arbitrary_stats.typed_allocations),
        after_arbitrary_stats
            .typed_deallocations
            .saturating_sub(before_arbitrary_stats.typed_deallocations),
        after_arbitrary_validation
            .recovery_identity_mismatches
            .saturating_sub(before_arbitrary_validation.recovery_identity_mismatches),
        final_validation.recovery_identity_mismatches,
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


def call_rows(
    audit: dict[str, object], *, function_name: str, callee_marker: str
) -> list[dict[str, object]]:
    candidates = audit.get("rewrite_candidates")
    assert isinstance(candidates, list), candidates
    return [
        row
        for row in candidates
        if isinstance(row, dict)
        and function_matches(row, function_name)
        and callee_marker in str(row.get("callee") or "")
    ]


def validate(audit: dict[str, object], stdout: str) -> dict[str, object]:
    summary = audit.get("summary")
    assert isinstance(summary, dict), summary
    assert summary.get("provider_override_installed") is True, summary
    assert summary.get("body_clone_returned_to_rustc") is True, summary
    assert summary.get("actual_semantic_scope_rewrite_requested") is True, summary
    assert summary.get("actual_semantic_scope_rewrite") is True, summary

    arbitrary_rows = call_rows(
        audit, function_name="main", callee_marker=ARBITRARY_CALLEE
    )
    assert not arbitrary_rows, (
        "Result<(), ErrorWithBox> must not infer a semantic scope from the Err owner",
        arbitrary_rows,
    )

    reserve_rows = call_rows(
        audit, function_name=ARBITRARY_CALLEE, callee_marker="reserve_exact"
    )
    assert len(reserve_rows) == 1, reserve_rows
    reserve = reserve_rows[0]
    assert reserve.get("lowering_kind") == "semantic_scope_enter_exit_rewrite", reserve
    assert reserve.get("rewrite_status") == APPLIED_STATUS, reserve
    assert "Vec<u8" in str(reserve.get("semantic_object_type") or ""), reserve

    factory_rows = call_rows(
        audit, function_name="main", callee_marker=FALLIBLE_FACTORY_CALLEE
    )
    assert len(factory_rows) == 1, factory_rows
    factory = factory_rows[0]
    assert factory.get("lowering_kind") == "semantic_scope_enter_exit_rewrite", factory
    assert factory.get("rewrite_status") == APPLIED_STATUS, factory
    assert "Vec<u8" in str(factory.get("semantic_object_type") or ""), factory
    assert "Result<" in str(factory.get("destination_type") or ""), factory

    conflicting_factory_rows = call_rows(
        audit, function_name="main", callee_marker=CONFLICTING_FACTORY_CALLEE
    )
    assert len(conflicting_factory_rows) == 1, conflicting_factory_rows
    conflicting_factory = conflicting_factory_rows[0]
    assert (
        conflicting_factory.get("lowering_kind")
        == "semantic_scope_unsolved_heap_object_candidate"
    ), conflicting_factory
    assert conflicting_factory.get("rewrite_status") == AMBIGUOUS_STATUS, conflicting_factory
    assert (
        conflicting_factory.get("replacement_resolution_status")
        == "rustc_middle_multiple_heap_object_types_not_lowered"
    ), conflicting_factory
    conflicting_text = json.dumps(conflicting_factory, sort_keys=True)
    assert "Vec<u8" in conflicting_text, conflicting_factory
    assert "Box<str" in conflicting_text, conflicting_factory

    runtime = next(
        json.loads(line)
        for line in stdout.splitlines()
        if line.startswith("{") and PROBE_NAME in line
    )
    assert runtime["arbitrary_result_ok"] is True, runtime
    assert runtime["factory_result_correct"] is True, runtime
    assert runtime["conflicting_factory_result_correct"] is True, runtime
    assert int(runtime["arbitrary_typed_allocations"]) >= 1, runtime
    assert int(runtime["arbitrary_typed_deallocations"]) >= 1, runtime
    assert int(runtime["arbitrary_recovery_mismatch_delta"]) == 0, runtime
    assert int(runtime["recovery_identity_mismatches"]) == 0, runtime
    assert int(runtime["side_cache_corrupt_slots"]) == 0, runtime

    return {
        "arbitrary_result_scope_rows": len(arbitrary_rows),
        "internal_vec_reserve": {
            "semantic_object_type": reserve.get("semantic_object_type"),
            "rewrite_status": reserve.get("rewrite_status"),
        },
        "fallible_factory_positive_control": {
            "destination_type": factory.get("destination_type"),
            "semantic_object_type": factory.get("semantic_object_type"),
            "rewrite_status": factory.get("rewrite_status"),
        },
        "fallible_factory_conflicting_error_owner": {
            "destination_type": conflicting_factory.get("destination_type"),
            "semantic_object_type": conflicting_factory.get("semantic_object_type"),
            "rewrite_status": conflicting_factory.get("rewrite_status"),
            "replacement_resolution_status": conflicting_factory.get(
                "replacement_resolution_status"
            ),
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

    with tempfile.TemporaryDirectory(prefix="unialloc-result-error-owner-") as raw:
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
                "validated": True,
                "evidence": evidence,
            },
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
