#!/usr/bin/env python3
"""Prove old-rustc list-form vec! transfers its Box-array identity to Vec."""

from __future__ import annotations

import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import tempfile


ROOT = Path(__file__).resolve().parents[2]
PASS_SOURCE = ROOT / "tools/unialloc-rustc-pass/unialloc-rustc-mir-rewrite-dry-run.rs"
PROBE_NAME = "vec_macro_array_unsize_rebind_probe"
TOOLCHAIN = "nightly-2022-07-01"
TYPE_ISOLATED = 1
PAYLOAD_BYTES = 32


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
# Pin UniAlloc's spin transitive dependency to a Rust-1.64-compatible release.
lock_api = "=0.4.3"
''',
        encoding="utf-8",
    )
    (app / "src/main.rs").write_text(
        r'''use std::fmt::Write as _;
use unialloc::{
    semantic_auto_metadata_disable, semantic_fallback_attribution_snapshot,
    semantic_metadata_validation_snapshot, semantic_ownership_transfer_snapshot,
    semantic_stats_recording_disable, semantic_stats_reset, semantic_stats_snapshot,
    semantic_type_stats_recording_disable, semantic_type_stats_snapshot,
    type_isolation_side_cache_snapshot, SemanticTypeStatsSnapshot, UniAlloc,
};

#[global_allocator]
static ALLOCATOR: UniAlloc = UniAlloc;

const PAYLOAD: [u8; 32] = [
    0x89, 0x50, 0x4e, 0x47, 0x0d, 0x0a, 0x1a, 0x0a,
    0x89, 0x50, 0x4e, 0x47, 0x0d, 0x0a, 0x1a, 0x0a,
    0x89, 0x50, 0x4e, 0x47, 0x0d, 0x0a, 0x1a, 0x0a,
    0x89, 0x50, 0x4e, 0x47, 0x0d, 0x0a, 0x1a, 0x0a,
];

#[inline(never)]
fn vec_macro_list() -> Vec<u8> {
    vec![
        0x89, 0x50, 0x4e, 0x47, 0x0d, 0x0a, 0x1a, 0x0a,
        0x89, 0x50, 0x4e, 0x47, 0x0d, 0x0a, 0x1a, 0x0a,
        0x89, 0x50, 0x4e, 0x47, 0x0d, 0x0a, 0x1a, 0x0a,
        0x89, 0x50, 0x4e, 0x47, 0x0d, 0x0a, 0x1a, 0x0a,
    ]
}

#[inline(never)]
fn make_array_box() -> Box<[u8; 32]> {
    Box::new(PAYLOAD)
}

#[inline(never)]
fn make_vec() -> Vec<u8> {
    Vec::<u8>::with_capacity(PAYLOAD.len())
}

fn type_rows_json(rows: &[SemanticTypeStatsSnapshot], row_count: usize) -> String {
    let mut out = String::from("[");
    let mut emitted = 0usize;
    for row in rows.iter().take(std::cmp::min(row_count, rows.len())) {
        if row.allocations == 0 && row.deallocations == 0 {
            continue;
        }
        if emitted != 0 {
            out.push(',');
        }
        emitted += 1;
        let _ = write!(
            out,
            concat!(
                "{{",
                "\"type_id\":{},",
                "\"module_id\":{},",
                "\"callsite\":{},",
                "\"allocations\":{},",
                "\"allocated_bytes\":{},",
                "\"deallocations\":{},",
                "\"cache_hits\":{},",
                "\"cache_inserts\":{},",
                "\"cache_bypasses\":{},",
                "\"observed_alloc_size\":{},",
                "\"observed_alloc_align\":{},",
                "\"observed_dealloc_size\":{},",
                "\"observed_dealloc_align\":{},",
                "\"policy_flags_seen\":{}",
                "}}"
            ),
            row.type_id,
            row.module_id,
            row.callsite,
            row.allocations,
            row.allocated_bytes,
            row.deallocations,
            row.cache_hits,
            row.cache_inserts,
            row.cache_bypasses,
            row.observed_alloc_size,
            row.observed_alloc_align,
            row.observed_dealloc_size,
            row.observed_dealloc_align,
            row.policy_flags_seen,
        );
    }
    out.push(']');
    out
}

fn main() {
    semantic_auto_metadata_disable();
    semantic_stats_reset();

    let transfer_before = semantic_ownership_transfer_snapshot();
    let converted = vec_macro_list();
    let transfer_after = semantic_ownership_transfer_snapshot();
    assert_eq!(converted.as_slice(), PAYLOAD.as_slice());
    assert_eq!(converted.capacity(), PAYLOAD.len());
    assert_eq!(transfer_after.attempted - transfer_before.attempted, 1);
    assert_eq!(transfer_after.applied - transfer_before.applied, 1);
    assert_eq!(transfer_after.rejected - transfer_before.rejected, 0);

    let converted_pointer = converted.as_ptr() as usize;
    drop(converted);

    let wrong_box = make_array_box();
    let wrong_box_pointer = wrong_box.as_ptr() as usize;
    assert_ne!(wrong_box_pointer, converted_pointer);
    drop(wrong_box);

    let recovered_vec = make_vec();
    let recovered_vec_pointer = recovered_vec.as_ptr() as usize;
    assert_eq!(recovered_vec_pointer, converted_pointer);
    drop(recovered_vec);

    let stats = semantic_stats_snapshot();
    let fallback = semantic_fallback_attribution_snapshot();
    let validation = semantic_metadata_validation_snapshot();
    let side_cache = type_isolation_side_cache_snapshot();
    let mut rows = [SemanticTypeStatsSnapshot::empty(); 16];
    let row_count = semantic_type_stats_snapshot(&mut rows);

    assert_eq!(stats.typed_allocations, 3, "{stats:?}");
    assert_eq!(stats.typed_deallocations, 3, "{stats:?}");
    assert_eq!(stats.typed_cache_hits, 1, "{stats:?}");
    assert_eq!(stats.typed_cache_inserts, 3, "{stats:?}");
    assert_eq!(stats.fallback_allocations, 0, "{stats:?}");
    assert_eq!(stats.fallback_deallocations, 0, "{stats:?}");
    assert_eq!(fallback.raw_alloc_no_metadata, 0, "{fallback:?}");
    assert_eq!(fallback.raw_dealloc_no_metadata, 0, "{fallback:?}");
    assert_eq!(fallback.raw_realloc_no_metadata, 0, "{fallback:?}");
    assert_eq!(validation.recovery_identity_mismatches, 0, "{validation:?}");
    assert_eq!(side_cache.corrupt_slots, 0, "{side_cache:?}");

    semantic_type_stats_recording_disable();
    semantic_stats_recording_disable();
    let type_rows = type_rows_json(&rows, row_count);
    println!(
        concat!(
            "{{",
            "\"source\":\"vec_macro_array_unsize_rebind_probe\",",
            "\"converted_pointer\":{},",
            "\"wrong_box_pointer\":{},",
            "\"recovered_vec_pointer\":{},",
            "\"payload_preserved\":{},",
            "\"wrong_box_non_reuse\":{},",
            "\"same_vec_reuse\":{},",
            "\"transfer_attempted\":{},",
            "\"transfer_applied\":{},",
            "\"transfer_rejected\":{},",
            "\"typed_allocations\":{},",
            "\"typed_deallocations\":{},",
            "\"typed_cache_hits\":{},",
            "\"typed_cache_inserts\":{},",
            "\"fallback_allocations\":{},",
            "\"fallback_deallocations\":{},",
            "\"recovery_identity_mismatches\":{},",
            "\"side_cache_corrupt_slots\":{},",
            "\"type_rows\":{}",
            "}}"
        ),
        converted_pointer,
        wrong_box_pointer,
        recovered_vec_pointer,
        true,
        wrong_box_pointer != converted_pointer,
        recovered_vec_pointer == converted_pointer,
        transfer_after.attempted - transfer_before.attempted,
        transfer_after.applied - transfer_before.applied,
        transfer_after.rejected - transfer_before.rejected,
        stats.typed_allocations,
        stats.typed_deallocations,
        stats.typed_cache_hits,
        stats.typed_cache_inserts,
        stats.fallback_allocations,
        stats.fallback_deallocations,
        validation.recovery_identity_mismatches,
        side_cache.corrupt_slots,
        type_rows,
    );
}
''',
        encoding="utf-8",
    )
    return app


def function_matches(row: dict[str, object], function_name: str) -> bool:
    function = str(row.get("mir_function") or "")
    return function == function_name or function.endswith(f"::{function_name}")


def aggregate_type_rows(rows: list[dict[str, object]], type_id: int) -> dict[str, object]:
    matching = [row for row in rows if int(row.get("type_id") or 0) == type_id]
    assert matching, (type_id, rows)
    totals: dict[str, object] = {
        "allocations": 0,
        "allocated_bytes": 0,
        "deallocations": 0,
        "cache_hits": 0,
        "cache_inserts": 0,
        "cache_bypasses": 0,
        "policy_flags_seen": 0,
        "observed_alloc_sizes": [],
        "observed_dealloc_sizes": [],
    }
    alloc_sizes: set[int] = set()
    dealloc_sizes: set[int] = set()
    for row in matching:
        for field in (
            "allocations",
            "allocated_bytes",
            "deallocations",
            "cache_hits",
            "cache_inserts",
            "cache_bypasses",
        ):
            totals[field] = int(totals[field]) + int(row.get(field) or 0)
        totals["policy_flags_seen"] = int(totals["policy_flags_seen"]) | int(
            row.get("policy_flags_seen") or 0
        )
        if int(row.get("observed_alloc_size") or 0):
            alloc_sizes.add(int(row["observed_alloc_size"]))
        if int(row.get("observed_dealloc_size") or 0):
            dealloc_sizes.add(int(row["observed_dealloc_size"]))
    totals["observed_alloc_sizes"] = sorted(alloc_sizes)
    totals["observed_dealloc_sizes"] = sorted(dealloc_sizes)
    return totals


def load_runtime(stdout: str) -> dict[str, object]:
    for line in stdout.splitlines():
        if line.startswith("{"):
            value = json.loads(line)
            if isinstance(value, dict) and value.get("source") == PROBE_NAME:
                return value
    raise AssertionError(f"missing {PROBE_NAME} JSON event:\n{stdout}")


def validate(audit: dict[str, object], stdout: str) -> dict[str, object]:
    summary = audit.get("summary")
    assert isinstance(summary, dict), summary
    assert summary.get("provider_override_installed") is True, summary
    assert summary.get("body_clone_returned_to_rustc") is True, summary
    assert summary.get("actual_semantic_scope_rewrite") is True, summary
    assert int(summary.get("semantic_ownership_transfer_candidate_count") or 0) == 1
    assert int(summary.get("semantic_ownership_transfer_rewrite_applied_count") or 0) == 1

    rows = audit.get("rewrite_candidates")
    assert isinstance(rows, list), rows
    direct = [
        row
        for row in rows
        if isinstance(row, dict)
        and function_matches(row, "vec_macro_list")
        and row.get("lowering_kind") == "direct_allocator_call_rewrite"
        and row.get("rewrite_status") == "actual_allocator_call_replacement_applied"
        and str(row.get("semantic_object_type") or "").startswith(
            f"std::boxed::Box<[u8; {PAYLOAD_BYTES}]"
        )
    ]
    transfer = [
        row
        for row in rows
        if isinstance(row, dict)
        and function_matches(row, "vec_macro_list")
        and row.get("lowering_kind") == "semantic_ownership_transfer_rewrite"
        and row.get("rewrite_status") == "actual_semantic_ownership_transfer_rewrite_applied"
    ]
    assert len(direct) == 1, direct
    assert len(transfer) == 1, transfer
    direct_row = direct[0]
    transfer_row = transfer[0]
    array_type_id = int(direct_row.get("type_id") or 0)
    vec_type_id = int(transfer_row.get("type_id") or 0)
    assert array_type_id not in (0, vec_type_id), (direct_row, transfer_row)
    assert transfer_row.get("replacement_symbol") == "__unialloc_semantic_box_slice_into_vec"
    assert transfer_row.get("argument_types") == ["std::boxed::Box<[u8]>"]
    assert transfer_row.get("destination_type") == "std::vec::Vec<u8>"
    preview = str(transfer_row.get("replacement_preview") or "")
    old_match = re.search(r"\bold_type_id=(\d+)\b", preview)
    new_match = re.search(r"\bnew_type_id=(\d+)\b", preview)
    assert old_match is not None, transfer_row
    assert new_match is not None, transfer_row
    assert int(old_match.group(1)) == array_type_id, transfer_row
    assert int(new_match.group(1)) == vec_type_id, transfer_row
    assert (
        f"old_owner_type=std::boxed::Box<[u8; {PAYLOAD_BYTES}]>" in preview
    ), transfer_row
    assert "argument_owner_type=std::boxed::Box<[u8]>" in preview, transfer_row
    assert "old_owner_basis=exact_immediate_box_array_unsize" in preview, transfer_row

    runtime = load_runtime(stdout)
    assert runtime["payload_preserved"] is True, runtime
    assert runtime["wrong_box_non_reuse"] is True, runtime
    assert runtime["same_vec_reuse"] is True, runtime
    assert int(runtime["transfer_attempted"]) == 1, runtime
    assert int(runtime["transfer_applied"]) == 1, runtime
    assert int(runtime["transfer_rejected"]) == 0, runtime
    for field, expected in (
        ("typed_allocations", 3),
        ("typed_deallocations", 3),
        ("typed_cache_hits", 1),
        ("typed_cache_inserts", 3),
        ("fallback_allocations", 0),
        ("fallback_deallocations", 0),
        ("recovery_identity_mismatches", 0),
        ("side_cache_corrupt_slots", 0),
    ):
        assert int(runtime[field]) == expected, (field, runtime)

    type_rows = runtime.get("type_rows")
    assert isinstance(type_rows, list), runtime
    array_totals = aggregate_type_rows(type_rows, array_type_id)
    vec_totals = aggregate_type_rows(type_rows, vec_type_id)
    expected_array = {
        "allocations": 2,
        "allocated_bytes": 2 * PAYLOAD_BYTES,
        "deallocations": 1,
        "cache_hits": 0,
        "cache_inserts": 1,
        "observed_alloc_sizes": [PAYLOAD_BYTES],
        "observed_dealloc_sizes": [PAYLOAD_BYTES],
    }
    expected_vec = {
        "allocations": 1,
        "allocated_bytes": PAYLOAD_BYTES,
        "deallocations": 2,
        "cache_hits": 1,
        "cache_inserts": 2,
        "observed_alloc_sizes": [PAYLOAD_BYTES],
        "observed_dealloc_sizes": [PAYLOAD_BYTES],
    }
    for field, expected in expected_array.items():
        assert array_totals[field] == expected, (field, array_totals)
    for field, expected in expected_vec.items():
        assert vec_totals[field] == expected, (field, vec_totals)
    assert int(array_totals["policy_flags_seen"]) & TYPE_ISOLATED, array_totals
    assert int(vec_totals["policy_flags_seen"]) & TYPE_ISOLATED, vec_totals

    return {
        "compiler_type_ids": {"box_array_32": array_type_id, "vec": vec_type_id},
        "direct_allocation": direct_row,
        "ownership_transfer": transfer_row,
        "runtime_type_totals": {"box_array_32": array_totals, "vec": vec_totals},
        "runtime": runtime,
    }


def main() -> int:
    rustc = shutil.which("rustc") or "rustc"
    cargo = shutil.which("cargo") or "cargo"
    installed = subprocess.run(
        [rustc, f"+{TOOLCHAIN}", "--version"],
        cwd=ROOT,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    assert installed.returncode == 0, installed.stderr
    sysroot = subprocess.check_output(
        [rustc, f"+{TOOLCHAIN}", "--print", "sysroot"], cwd=ROOT, text=True
    ).strip()

    with tempfile.TemporaryDirectory(prefix="unialloc-vec-macro-array-unsize-") as raw:
        workspace = Path(raw)
        app = write_probe(workspace)
        pass_binary = workspace / "unialloc-rustc-mir-rewrite-dry-run"
        build_env = os.environ.copy()
        build_env["RUSTC_BOOTSTRAP"] = "1"
        run(
            [rustc, f"+{TOOLCHAIN}", str(PASS_SOURCE), "-o", str(pass_binary)],
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
                "UNIALLOC_LOWERING_POLICY_FLAGS": str(TYPE_ISOLATED),
                "UNIALLOC_RUSTC_SYSROOT": sysroot,
                "CARGO_NET_OFFLINE": "true",
                "CARGO_INCREMENTAL": "0",
                "CARGO_TARGET_DIR": str(workspace / "target"),
            }
        )
        stdout = run(
            [
                cargo,
                f"+{TOOLCHAIN}",
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
                "source": "mir_vec_macro_array_unsize_rebind_probe",
                "validated": True,
                "single_run": True,
                "benchmark": False,
                "toolchain": TOOLCHAIN,
                "evidence": evidence,
            },
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
