#!/usr/bin/env python3
"""Prove cross-crate returned owners recover allocation-module isolation."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile


ROOT = Path(__file__).resolve().parents[2]
DEFAULT_PASS_SOURCE = (
    ROOT / "tools/unialloc-rustc-pass/unialloc-rustc-mir-rewrite-dry-run.rs"
)
PRODUCER_CRATE = "returned_string_producer"
APP_CRATE = "returned_string_app"
TYPE_ISOLATED = 0x1
CAPACITY = 64


def run(
    command: list[str], *, cwd: Path, env: dict[str, str], timeout: int = 300
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        command,
        cwd=cwd,
        env=env,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=timeout,
        check=False,
    )


def require_success(result: subprocess.CompletedProcess[str], command: list[str]) -> None:
    if result.returncode != 0:
        raise AssertionError(
            f"command failed ({result.returncode}): {' '.join(command)}\n"
            f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"
        )


def current_rustc_cfg(toolchain: str) -> list[str]:
    normalized = toolchain.lstrip("+").strip()
    if normalized == "nightly" or normalized.startswith(("nightly-2025", "nightly-2026")):
        return ["--cfg", "unialloc_rustc_current"]
    return []


def write_fixture(workspace: Path) -> Path:
    producer = workspace / "producer"
    app = workspace / "application"
    for crate in (producer, app):
        (crate / "src").mkdir(parents=True)

    (workspace / "Cargo.toml").write_text(
        '''[workspace]
members = ["producer", "application"]
resolver = "2"
''',
        encoding="utf-8",
    )
    unialloc_path = json.dumps(str(ROOT / "unialloc"))
    (producer / "Cargo.toml").write_text(
        f'''[package]
name = "returned-string-producer"
version = "0.1.0"
edition = "2021"

[lib]
name = "{PRODUCER_CRATE}"
path = "src/lib.rs"

[dependencies]
unialloc = {{ path = {unialloc_path}, features = ["stats", "type_isolation"] }}
''',
        encoding="utf-8",
    )
    (producer / "src/lib.rs").write_text(
        f'''pub const CAPACITY: usize = {CAPACITY};

static OPAQUE_FALSE: bool = false;

pub enum ReturnedString {{
    Value(String),
    Empty,
}}

impl ReturnedString {{
    #[inline(never)]
    pub fn capacity(&self) -> usize {{
        match self {{
            Self::Value(value) => value.capacity(),
            Self::Empty => 0,
        }}
    }}

    #[inline(never)]
    pub fn pointer(&self) -> usize {{
        match self {{
            Self::Value(value) => value.as_ptr() as usize,
            Self::Empty => 0,
        }}
    }}
}}

#[inline(never)]
pub fn make_string(byte: u8) -> ReturnedString {{
    // Keep the UniAlloc ABI visible in this compilation unit so the MIR pass
    // can resolve the semantic-scope symbols it inserts.
    let observed = unialloc::semantic_stats_snapshot().total_allocations;
    unsafe {{ std::ptr::read_volatile(&observed); }}
    let mut value = String::with_capacity(CAPACITY);
    value.push(char::from(byte));
    if unsafe {{ std::ptr::read_volatile(&OPAQUE_FALSE) }} {{
        ReturnedString::Empty
    }} else {{
        ReturnedString::Value(value)
    }}
}}
''',
        encoding="utf-8",
    )
    (app / "Cargo.toml").write_text(
        f'''[package]
name = "returned-string-app"
version = "0.1.0"
edition = "2021"

[dependencies]
lock_api = "=0.4.3"
returned_string_producer = {{ package = "returned-string-producer", path = "../producer" }}
unialloc = {{ path = {unialloc_path}, features = ["stats", "type_isolation"] }}
''',
        encoding="utf-8",
    )
    (app / "src/main.rs").write_text(
        r'''use std::fmt::Write as _;
use unialloc::{
    semantic_auto_metadata_disable, semantic_fallback_attribution_snapshot,
    semantic_metadata_validation_snapshot, semantic_stats_recording_disable,
    semantic_stats_reset, semantic_stats_snapshot, semantic_type_stats_recording_disable,
    semantic_type_stats_snapshot, type_isolation_side_cache_snapshot,
    SemanticTypeStatsSnapshot, UniAlloc,
};

#[global_allocator]
static ALLOCATOR: UniAlloc = UniAlloc;

#[inline(never)]
fn make_local_string(byte: u8) -> String {
    let mut value = String::with_capacity(returned_string_producer::CAPACITY);
    value.push(char::from(byte));
    value
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
                "\"deallocations\":{},",
                "\"cache_hits\":{},",
                "\"cache_inserts\":{},",
                "\"policy_flags_seen\":{}",
                "}}"
            ),
            row.type_id,
            row.module_id,
            row.callsite,
            row.allocations,
            row.deallocations,
            row.cache_hits,
            row.cache_inserts,
            row.policy_flags_seen,
        );
    }
    out.push(']');
    out
}

fn main() {
    semantic_auto_metadata_disable();
    semantic_stats_reset();

    let producer_pointer;
    let before_first_drop;
    {
        let first = returned_string_producer::make_string(b'a');
        assert_eq!(first.capacity(), returned_string_producer::CAPACITY);
        producer_pointer = first.pointer();
        before_first_drop = semantic_metadata_validation_snapshot();
    }
    let after_first_drop = semantic_metadata_validation_snapshot();
    let first_crosscrate_mismatch_delta = after_first_drop
        .recovery_identity_mismatches
        .saturating_sub(before_first_drop.recovery_identity_mismatches);

    let local_pointer;
    {
        let local = make_local_string(b'b');
        local_pointer = local.as_ptr() as usize;
        assert_ne!(
            local_pointer, producer_pointer,
            "the application module must not reuse the producer module cache"
        );
    }
    let after_local_drop = semantic_metadata_validation_snapshot();
    assert_eq!(
        after_local_drop.recovery_identity_mismatches,
        after_first_drop.recovery_identity_mismatches,
        "an application-local allocation/drop pair must not add a mismatch"
    );

    let producer_recovery_pointer;
    let before_second_drop;
    {
        let second = returned_string_producer::make_string(b'c');
        producer_recovery_pointer = second.pointer();
        assert_eq!(
            producer_recovery_pointer, producer_pointer,
            "the producer module must recover its own cached String storage"
        );
        before_second_drop = semantic_metadata_validation_snapshot();
    }
    let after_second_drop = semantic_metadata_validation_snapshot();
    let second_crosscrate_mismatch_delta = after_second_drop
        .recovery_identity_mismatches
        .saturating_sub(before_second_drop.recovery_identity_mismatches);

    let local_recovery_pointer;
    {
        let local_again = make_local_string(b'd');
        local_recovery_pointer = local_again.as_ptr() as usize;
        assert_eq!(
            local_recovery_pointer, local_pointer,
            "the application module must recover its own cached String storage"
        );
    }

    let stats = semantic_stats_snapshot();
    let fallback = semantic_fallback_attribution_snapshot();
    let validation = semantic_metadata_validation_snapshot();
    let side_cache = type_isolation_side_cache_snapshot();
    let mut rows = [SemanticTypeStatsSnapshot::empty(); 16];
    let row_count = semantic_type_stats_snapshot(&mut rows);

    assert_eq!(stats.typed_allocations, 4, "{stats:?}");
    assert_eq!(stats.typed_deallocations, 4, "{stats:?}");
    assert_eq!(stats.typed_cache_hits, 2, "{stats:?}");
    assert_eq!(stats.typed_cache_inserts, 4, "{stats:?}");
    assert_eq!(stats.fallback_allocations, 0, "{stats:?}");
    assert_eq!(stats.fallback_deallocations, 0, "{stats:?}");
    assert_eq!(stats.semantic_type_stats_dropped_events, 0, "{stats:?}");
    assert_eq!(fallback.raw_alloc_no_metadata, 0, "{fallback:?}");
    assert_eq!(fallback.raw_dealloc_no_metadata, 0, "{fallback:?}");
    assert_eq!(fallback.raw_realloc_no_metadata, 0, "{fallback:?}");
    assert_eq!(side_cache.corrupt_slots, 0, "{side_cache:?}");

    semantic_type_stats_recording_disable();
    semantic_stats_recording_disable();
    let type_rows = type_rows_json(&rows, row_count);
    println!(
        concat!(
            "{{",
            "\"source\":\"crosscrate_returned_string_recovery\",",
            "\"producer_pointer\":{},",
            "\"local_pointer\":{},",
            "\"producer_recovery_pointer\":{},",
            "\"local_recovery_pointer\":{},",
            "\"wrong_module_non_reuse\":true,",
            "\"producer_exact_reuse\":true,",
            "\"application_exact_reuse\":true,",
            "\"first_crosscrate_mismatch_delta\":{},",
            "\"second_crosscrate_mismatch_delta\":{},",
            "\"typed_allocations\":{},",
            "\"typed_deallocations\":{},",
            "\"typed_cache_hits\":{},",
            "\"typed_cache_inserts\":{},",
            "\"fallback_allocations\":{},",
            "\"fallback_deallocations\":{},",
            "\"raw_alloc_no_metadata\":{},",
            "\"raw_dealloc_no_metadata\":{},",
            "\"raw_realloc_no_metadata\":{},",
            "\"recovery_identity_matches\":{},",
            "\"recovery_identity_mismatches\":{},",
            "\"last_mismatch_requested_type_id\":{},",
            "\"last_mismatch_recorded_type_id\":{},",
            "\"last_mismatch_requested_module_id\":{},",
            "\"last_mismatch_recorded_module_id\":{},",
            "\"last_mismatch_requested_callsite\":{},",
            "\"last_mismatch_recorded_callsite\":{},",
            "\"side_cache_corrupt_slots\":{},",
            "\"semantic_type_stats_dropped_events\":{},",
            "\"type_rows\":{}",
            "}}"
        ),
        producer_pointer,
        local_pointer,
        producer_recovery_pointer,
        local_recovery_pointer,
        first_crosscrate_mismatch_delta,
        second_crosscrate_mismatch_delta,
        stats.typed_allocations,
        stats.typed_deallocations,
        stats.typed_cache_hits,
        stats.typed_cache_inserts,
        stats.fallback_allocations,
        stats.fallback_deallocations,
        fallback.raw_alloc_no_metadata,
        fallback.raw_dealloc_no_metadata,
        fallback.raw_realloc_no_metadata,
        validation.recovery_identity_matches,
        validation.recovery_identity_mismatches,
        validation.last_mismatch_requested_type_id,
        validation.last_mismatch_recorded_type_id,
        validation.last_mismatch_requested_module_id,
        validation.last_mismatch_recorded_module_id,
        validation.last_mismatch_requested_callsite,
        validation.last_mismatch_recorded_callsite,
        side_cache.corrupt_slots,
        stats.semantic_type_stats_dropped_events,
        type_rows,
    );
}
''',
        encoding="utf-8",
    )
    return app


def applied_rows(audit: dict[str, object]) -> list[dict[str, object]]:
    rows = audit.get("rewrite_candidates") or []
    return [
        row
        for row in rows
        if isinstance(row, dict)
        and row.get("rewrite_status")
        in {
            "actual_semantic_scope_enter_exit_rewrite_applied",
            "actual_semantic_scope_drop_rewrite_applied",
        }
        and "String" in str(row.get("semantic_object_type") or "")
    ]


def unique_row(rows: list[dict[str, object]], label: str) -> dict[str, object]:
    assert len(rows) == 1, (label, rows)
    return rows[0]


def validate(
    producer_audit: dict[str, object], app_audit: dict[str, object], stdout: str
) -> dict[str, object]:
    producer_rows = applied_rows(producer_audit)
    app_rows = applied_rows(app_audit)
    producer_alloc = unique_row(
        [
            row
            for row in producer_rows
            if "make_string" in str(row.get("mir_function") or "")
            and "with_capacity" in str(row.get("callee") or "")
            and row.get("lowering_kind") == "semantic_scope_enter_exit_rewrite"
        ],
        "producer String::with_capacity allocation",
    )
    app_alloc = unique_row(
        [
            row
            for row in app_rows
            if "make_local_string" in str(row.get("mir_function") or "")
            and "with_capacity" in str(row.get("callee") or "")
            and row.get("lowering_kind") == "semantic_scope_enter_exit_rewrite"
        ],
        "application String::with_capacity allocation",
    )

    type_id = int(producer_alloc.get("type_id") or 0)
    producer_module = int(producer_alloc.get("module_id") or 0)
    app_module = int(app_alloc.get("module_id") or 0)
    producer_callsite = int(producer_alloc.get("callsite") or 0)
    assert type_id != 0 and int(app_alloc.get("type_id") or 0) == type_id
    assert producer_module != 0 and app_module != 0 and producer_module != app_module
    assert producer_callsite != 0
    assert int(producer_alloc.get("flags") or 0) & TYPE_ISOLATED, producer_alloc
    assert int(app_alloc.get("flags") or 0) & TYPE_ISOLATED, app_alloc

    runtime_lines = [line for line in stdout.splitlines() if line.startswith("{")]
    assert len(runtime_lines) == 1, stdout
    runtime = json.loads(runtime_lines[0])
    assert runtime.get("source") == "crosscrate_returned_string_recovery", runtime
    assert runtime.get("wrong_module_non_reuse") is True, runtime
    assert runtime.get("producer_exact_reuse") is True, runtime
    assert runtime.get("application_exact_reuse") is True, runtime
    assert int(runtime["producer_pointer"]) != int(runtime["local_pointer"]), runtime
    assert int(runtime["producer_pointer"]) == int(runtime["producer_recovery_pointer"]), runtime
    assert int(runtime["local_pointer"]) == int(runtime["local_recovery_pointer"]), runtime
    assert int(runtime["first_crosscrate_mismatch_delta"]) == 0, runtime
    assert int(runtime["second_crosscrate_mismatch_delta"]) == 0, runtime
    assert int(runtime["recovery_identity_matches"]) == 2, runtime
    assert int(runtime["recovery_identity_mismatches"]) == 0, runtime
    assert int(runtime["side_cache_corrupt_slots"]) == 0, runtime
    for field, expected in (
        ("typed_allocations", 4),
        ("typed_deallocations", 4),
        ("typed_cache_hits", 2),
        ("typed_cache_inserts", 4),
        ("fallback_allocations", 0),
        ("fallback_deallocations", 0),
        ("raw_alloc_no_metadata", 0),
        ("raw_dealloc_no_metadata", 0),
        ("raw_realloc_no_metadata", 0),
        ("semantic_type_stats_dropped_events", 0),
    ):
        assert int(runtime[field]) == expected, (field, runtime)

    app_rewrite_rows = app_audit.get("rewrite_candidates") or []
    assert isinstance(app_rewrite_rows, list), app_rewrite_rows
    recovery_rows = [
        row
        for row in app_rewrite_rows
        if isinstance(row, dict)
        and row.get("mir_function") == "main"
        and row.get("callee") == "TerminatorKind::Drop"
        and row.get("lowering_kind")
        == "semantic_scope_drop_effectful_owner_recovery_skipped"
        and row.get("rewrite_status")
        == "semantic_scope_drop_rewrite_skipped_effectful_owner_recovery"
        and row.get("metadata_pairing_contract")
        == "allocation_scope_to_authenticated_recovery_record"
        and row.get("destination_type")
        == "returned_string_producer::ReturnedString"
        and row.get("semantic_object_type") == "std::string::String"
        and row.get("replacement_symbol")
        == "authenticated_allocation_recovery_record"
    ]
    assert len(recovery_rows) == 4, recovery_rows
    assert all(int(row.get("type_id") or 0) == 0 for row in recovery_rows), recovery_rows
    for suffix in (
        "requested_type_id",
        "recorded_type_id",
        "requested_module_id",
        "recorded_module_id",
        "requested_callsite",
        "recorded_callsite",
    ):
        assert int(runtime[f"last_mismatch_{suffix}"]) == 0, (suffix, runtime)

    type_rows = runtime.get("type_rows")
    assert isinstance(type_rows, list), runtime

    def aggregate_runtime(module_id: int, label: str) -> dict[str, int]:
        matching = [
            row
            for row in type_rows
            if isinstance(row, dict)
            and int(row.get("type_id") or 0) == type_id
            and int(row.get("module_id") or 0) == module_id
        ]
        assert matching, (label, type_rows)
        assert all(
            int(row.get("policy_flags_seen") or 0) & TYPE_ISOLATED for row in matching
        ), (label, matching)
        return {
            field: sum(int(row.get(field) or 0) for row in matching)
            for field in ("allocations", "deallocations", "cache_hits", "cache_inserts")
        }

    producer_runtime = aggregate_runtime(producer_module, "producer")
    app_runtime = aggregate_runtime(app_module, "application")
    expected_runtime = {
        "allocations": 2,
        "deallocations": 2,
        "cache_hits": 1,
        "cache_inserts": 2,
    }
    assert producer_runtime == expected_runtime, (producer_runtime, type_rows)
    assert app_runtime == expected_runtime, (app_runtime, type_rows)

    return {
        "type_id": type_id,
        "producer_module_id": producer_module,
        "application_module_id": app_module,
        "producer_allocation_callsite": producer_callsite,
        "authenticated_recovery_drop_row_count": len(recovery_rows),
        "producer_runtime": producer_runtime,
        "application_runtime": app_runtime,
        "runtime": runtime,
    }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--pass-source", type=Path, default=DEFAULT_PASS_SOURCE)
    parser.add_argument(
        "--toolchain",
        default=(ROOT / "rust-toolchain").read_text(encoding="utf-8").strip(),
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    pass_source = args.pass_source.resolve()
    toolchain = args.toolchain.lstrip("+").strip()
    assert pass_source.is_file(), pass_source
    rustc = shutil.which("rustc") or "rustc"
    cargo = shutil.which("cargo") or "cargo"
    sysroot = subprocess.check_output(
        [rustc, f"+{toolchain}", "--print", "sysroot"], cwd=ROOT, text=True
    ).strip()

    with tempfile.TemporaryDirectory(prefix="unialloc-crosscrate-string-") as raw:
        workspace = Path(raw)
        app = write_fixture(workspace)
        # Preserve the repository's intentionally pinned, yanked dependency
        # versions in this detached offline workspace.
        shutil.copy2(ROOT / "Cargo.lock", workspace / "Cargo.lock")
        pass_binary = workspace / "unialloc-rustc-mir-rewrite-dry-run"
        build_env = os.environ.copy()
        build_env["RUSTC_BOOTSTRAP"] = "1"
        build_command = [
            rustc,
            f"+{toolchain}",
            *current_rustc_cfg(toolchain),
            str(pass_source),
            "-o",
            str(pass_binary),
        ]
        build = run(build_command, cwd=ROOT, env=build_env)
        require_success(build, build_command)

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
                "UNIALLOC_RUSTC_TARGET_CRATES": f"{PRODUCER_CRATE},{APP_CRATE}",
                "UNIALLOC_REWRITE_AUDIT_DIR": str(rewrites),
                "UNIALLOC_PASS_LOG_DIR": str(logs),
                "UNIALLOC_CONTINUE_COMPILATION": "1",
                "UNIALLOC_ACTUAL_MIR_REWRITE": "1",
                "UNIALLOC_ACTUAL_SEMANTIC_SCOPE_REWRITE": "1",
                "UNIALLOC_DIRECT_LOCAL_METADATA_ABI": "1",
                "UNIALLOC_DIRECT_LOCAL_SIZE_ALIGN_WITH_SEMANTIC_DROP": "1",
                "UNIALLOC_LOWERING_POLICY_FLAGS": str(TYPE_ISOLATED),
                "UNIALLOC_RUSTC_SYSROOT": sysroot,
                "CARGO_NET_OFFLINE": "true",
                "CARGO_INCREMENTAL": "0",
                "CARGO_TARGET_DIR": str(workspace / "target"),
            }
        )
        run_command = [
            cargo,
            f"+{toolchain}",
            "run",
            "--quiet",
            "--manifest-path",
            str(app / "Cargo.toml"),
        ]
        runtime = run(run_command, cwd=workspace, env=run_env)
        require_success(runtime, run_command)

        producer_paths = sorted(rewrites.glob(f"{PRODUCER_CRATE}-*.json"))
        app_paths = sorted(rewrites.glob(f"{APP_CRATE}-*.json"))
        assert len(producer_paths) == 1, producer_paths
        assert len(app_paths) == 1, app_paths
        producer_audit = json.loads(producer_paths[0].read_text(encoding="utf-8"))
        app_audit = json.loads(app_paths[0].read_text(encoding="utf-8"))
        evidence = validate(producer_audit, app_audit, runtime.stdout)

    print(
        json.dumps(
            {
                "source": "mir_crosscrate_returned_string_recovery_probe",
                "validated": True,
                "single_run": True,
                "benchmark": False,
                "toolchain": toolchain,
                "pass_source": str(pass_source),
                "boundaries": [
                    "Functional actual-rustc cross-crate recovery and module-isolation regression only; no benchmark or performance claim.",
                    "Returned aggregate Drop is deliberately fail-closed to the authenticated allocation-time recovery record, so producer identity is recovered without manufacturing a consumer identity mismatch.",
                    "This bounded String fixture does not establish universal cross-crate owner coverage or prove the Oxipng OutFile/PathBuf aggregate path.",
                ],
                "evidence": evidence,
            },
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
