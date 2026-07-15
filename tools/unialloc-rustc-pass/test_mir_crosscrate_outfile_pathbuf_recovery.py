#!/usr/bin/env python3
"""Prove unsupported OutFile-like PathBuf clones stay fail-closed."""

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
PRODUCER_CRATE = "outfile_pathbuf_producer"
APP_CRATE = "outfile_pathbuf_app"
TYPE_ISOLATED = 0x1
PATH_LEN = 96


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
name = "outfile-pathbuf-producer"
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
        f'''use std::path::PathBuf;

pub const PATH_LEN: usize = {PATH_LEN};

#[derive(Clone)]
pub struct OutFile {{
    path: Option<PathBuf>,
}}

impl OutFile {{
    #[inline(never)]
    pub fn seed(byte: u8) -> Self {{
        // Keep the UniAlloc ABI visible in this compilation unit so the MIR
        // pass can resolve the semantic-scope symbols it inserts.
        let observed = unialloc::semantic_stats_snapshot().total_allocations;
        unsafe {{ std::ptr::read_volatile(&observed); }}
        let text: String = std::iter::repeat(char::from(byte)).take(PATH_LEN).collect();
        Self {{ path: Some(PathBuf::from(text)) }}
    }}

    #[inline(never)]
    pub fn len(&self) -> usize {{
        self.path.as_ref().unwrap().to_str().unwrap().len()
    }}

    #[inline(never)]
    pub fn pointer(&self) -> usize {{
        self.path.as_ref().unwrap().to_str().unwrap().as_ptr() as usize
    }}
}}

#[inline(never)]
pub fn clone_outfile(source: &OutFile) -> OutFile {{
    source.clone()
}}
''',
        encoding="utf-8",
    )
    (app / "Cargo.toml").write_text(
        f'''[package]
name = "outfile-pathbuf-app"
version = "0.1.0"
edition = "2021"

[dependencies]
lock_api = "=0.4.3"
outfile_pathbuf_producer = {{ package = "outfile-pathbuf-producer", path = "../producer" }}
unialloc = {{ path = {unialloc_path}, features = ["stats", "type_isolation"] }}
''',
        encoding="utf-8",
    )
    (app / "src/main.rs").write_text(
        r'''use std::fmt::Write as _;
use std::path::PathBuf;
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
fn make_local_seed(byte: u8) -> PathBuf {
    let text: String = std::iter::repeat(char::from(byte))
        .take(outfile_pathbuf_producer::PATH_LEN)
        .collect();
    PathBuf::from(text)
}

#[inline(never)]
fn clone_local_path(source: &PathBuf) -> PathBuf {
    source.clone()
}

#[inline(never)]
fn path_pointer(path: &PathBuf) -> usize {
    path.to_str().unwrap().as_ptr() as usize
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
    let producer_seed = outfile_pathbuf_producer::OutFile::seed(b'p');
    let application_seed = make_local_seed(b'a');
    assert_eq!(producer_seed.len(), outfile_pathbuf_producer::PATH_LEN);
    assert_eq!(
        application_seed.to_str().unwrap().len(),
        outfile_pathbuf_producer::PATH_LEN
    );
    semantic_stats_reset();

    let producer_pointer;
    let before_first_drop;
    {
        let first = outfile_pathbuf_producer::clone_outfile(&producer_seed);
        assert_eq!(first.len(), outfile_pathbuf_producer::PATH_LEN);
        producer_pointer = first.pointer();
        before_first_drop = semantic_metadata_validation_snapshot();
    }
    let after_first_drop = semantic_metadata_validation_snapshot();
    let first_crosscrate_mismatch_delta = after_first_drop
        .recovery_identity_mismatches
        .saturating_sub(before_first_drop.recovery_identity_mismatches);

    let local_pointer;
    {
        let local = clone_local_path(&application_seed);
        local_pointer = path_pointer(&local);
        // The producer aggregate is currently unsupported and follows the raw
        // fallback route, so allocator placement is outside this probe's
        // compiler-contract assertions.
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
        let second = outfile_pathbuf_producer::clone_outfile(&producer_seed);
        producer_recovery_pointer = second.pointer();
        before_second_drop = semantic_metadata_validation_snapshot();
    }
    let after_second_drop = semantic_metadata_validation_snapshot();
    let second_crosscrate_mismatch_delta = after_second_drop
        .recovery_identity_mismatches
        .saturating_sub(before_second_drop.recovery_identity_mismatches);

    let local_recovery_pointer;
    {
        let local_again = clone_local_path(&application_seed);
        local_recovery_pointer = path_pointer(&local_again);
    }

    let stats = semantic_stats_snapshot();
    let fallback = semantic_fallback_attribution_snapshot();
    let validation = semantic_metadata_validation_snapshot();
    let side_cache = type_isolation_side_cache_snapshot();
    let mut rows = [SemanticTypeStatsSnapshot::empty(); 16];
    let row_count = semantic_type_stats_snapshot(&mut rows);

    assert_eq!(stats.typed_allocations, 0, "{stats:?}");
    assert_eq!(stats.typed_deallocations, 0, "{stats:?}");
    assert_eq!(stats.typed_cache_hits, 0, "{stats:?}");
    assert_eq!(stats.typed_cache_inserts, 0, "{stats:?}");
    assert_eq!(stats.fallback_allocations, 4, "{stats:?}");
    assert_eq!(stats.fallback_deallocations, 4, "{stats:?}");
    assert_eq!(stats.semantic_type_stats_dropped_events, 0, "{stats:?}");
    assert_eq!(fallback.raw_alloc_no_metadata, 4, "{fallback:?}");
    assert_eq!(fallback.raw_dealloc_no_metadata, 4, "{fallback:?}");
    assert_eq!(fallback.raw_realloc_no_metadata, 0, "{fallback:?}");
    assert_eq!(side_cache.corrupt_slots, 0, "{side_cache:?}");
    assert_eq!(side_cache.occupied_entries, 0, "{side_cache:?}");
    assert_eq!(side_cache.retained_bytes, 0, "{side_cache:?}");

    semantic_type_stats_recording_disable();
    semantic_stats_recording_disable();
    std::mem::forget(producer_seed);
    std::mem::forget(application_seed);
    let type_rows = type_rows_json(&rows, row_count);
    println!(
        concat!(
            "{{",
            "\"source\":\"crosscrate_outfile_pathbuf_recovery\",",
            "\"producer_pointer\":{},",
            "\"local_pointer\":{},",
            "\"producer_recovery_pointer\":{},",
            "\"local_recovery_pointer\":{},",
            "\"pointer_placement_asserted\":false,",
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
            "\"first_mismatch_requested_type_id\":{},",
            "\"first_mismatch_recorded_type_id\":{},",
            "\"first_mismatch_requested_module_id\":{},",
            "\"first_mismatch_recorded_module_id\":{},",
            "\"first_mismatch_requested_callsite\":{},",
            "\"first_mismatch_recorded_callsite\":{},",
            "\"last_mismatch_requested_type_id\":{},",
            "\"last_mismatch_recorded_type_id\":{},",
            "\"last_mismatch_requested_module_id\":{},",
            "\"last_mismatch_recorded_module_id\":{},",
            "\"last_mismatch_requested_callsite\":{},",
            "\"last_mismatch_recorded_callsite\":{},",
            "\"side_cache_corrupt_slots\":{},",
            "\"side_cache_occupied_entries\":{},",
            "\"side_cache_retained_bytes\":{},",
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
        after_first_drop.last_mismatch_requested_type_id,
        after_first_drop.last_mismatch_recorded_type_id,
        after_first_drop.last_mismatch_requested_module_id,
        after_first_drop.last_mismatch_recorded_module_id,
        after_first_drop.last_mismatch_requested_callsite,
        after_first_drop.last_mismatch_recorded_callsite,
        validation.last_mismatch_requested_type_id,
        validation.last_mismatch_recorded_type_id,
        validation.last_mismatch_requested_module_id,
        validation.last_mismatch_recorded_module_id,
        validation.last_mismatch_requested_callsite,
        validation.last_mismatch_recorded_callsite,
        side_cache.corrupt_slots,
        side_cache.occupied_entries,
        side_cache.retained_bytes,
        stats.semantic_type_stats_dropped_events,
        type_rows,
    );
}
''',
        encoding="utf-8",
    )
    return app


def rewrite_rows(audit: dict[str, object]) -> list[dict[str, object]]:
    rows = audit.get("rewrite_candidates") or []
    assert isinstance(rows, list), rows
    return [row for row in rows if isinstance(row, dict)]


def unique_row(rows: list[dict[str, object]], label: str) -> dict[str, object]:
    assert len(rows) == 1, (label, rows)
    return rows[0]


def validate(
    producer_audit: dict[str, object], app_audit: dict[str, object], stdout: str
) -> dict[str, object]:
    producer_rows = rewrite_rows(producer_audit)
    app_rows = rewrite_rows(app_audit)

    def is_unresolved_candidate(row: dict[str, object]) -> bool:
        return (
            row.get("lowering_kind")
            == "semantic_scope_unsolved_heap_object_candidate"
            and row.get("rewrite_status")
            == "semantic_scope_rewrite_skipped_unresolved_heap_object_type"
            and row.get("metadata_pairing_contract")
            == "audit_only_unresolved_heap_object_type"
            and int(row.get("type_id") or 0) == 0
        )

    def is_authenticated_drop_skip(row: dict[str, object]) -> bool:
        return (
            row.get("callee") == "TerminatorKind::Drop"
            and row.get("lowering_kind")
            == "semantic_scope_drop_effectful_owner_recovery_skipped"
            and row.get("rewrite_status")
            == "semantic_scope_drop_rewrite_skipped_effectful_owner_recovery"
            and row.get("metadata_pairing_contract")
            == "allocation_scope_to_authenticated_recovery_record"
            and row.get("replacement_symbol")
            == "authenticated_allocation_recovery_record"
            and int(row.get("type_id") or 0) == 0
        )

    producer_clone = unique_row(
        [
            row
            for row in producer_rows
            if row.get("mir_function") == "<OutFile as std::clone::Clone>::clone"
            and row.get("destination_type")
            == "std::option::Option<std::path::PathBuf>"
            and is_unresolved_candidate(row)
        ],
        "producer OutFile::clone fail-closed Option<PathBuf> candidate",
    )
    app_clone_outfile = [
        row
        for row in app_rows
        if row.get("mir_function") == "main"
        and "clone_outfile" in str(row.get("callee") or "")
        and row.get("destination_type") == "outfile_pathbuf_producer::OutFile"
        and is_unresolved_candidate(row)
    ]
    app_clone_path = [
        row
        for row in app_rows
        if row.get("mir_function") == "main"
        and "clone_local_path" in str(row.get("callee") or "")
        and row.get("destination_type") == "std::path::PathBuf"
        and is_unresolved_candidate(row)
    ]
    assert len(app_clone_outfile) == 2, app_clone_outfile
    assert len(app_clone_path) == 2, app_clone_path
    authenticated_drop_rows = [
        row
        for row in app_rows
        if row.get("destination_type") == "outfile_pathbuf_producer::OutFile"
        and row.get("semantic_object_type") == "std::path::PathBuf"
        and is_authenticated_drop_skip(row)
    ]
    assert len(authenticated_drop_rows) == 5, authenticated_drop_rows
    assert not any(
        "_local" in str(row.get("replacement_symbol") or "")
        for row in [*producer_rows, *app_rows]
    ), "unsupported OutFile/PathBuf rows must never select the local ABI"

    runtime_lines = [line for line in stdout.splitlines() if line.startswith("{")]
    assert len(runtime_lines) == 1, stdout
    runtime = json.loads(runtime_lines[0])
    assert runtime.get("source") == "crosscrate_outfile_pathbuf_recovery", runtime
    assert runtime.get("pointer_placement_asserted") is False, runtime
    for field, expected in (
        ("typed_allocations", 0),
        ("typed_deallocations", 0),
        ("typed_cache_hits", 0),
        ("typed_cache_inserts", 0),
        ("fallback_allocations", 4),
        ("fallback_deallocations", 4),
        ("raw_alloc_no_metadata", 4),
        ("raw_dealloc_no_metadata", 4),
        ("raw_realloc_no_metadata", 0),
        ("recovery_identity_matches", 0),
        ("recovery_identity_mismatches", 0),
        ("first_crosscrate_mismatch_delta", 0),
        ("second_crosscrate_mismatch_delta", 0),
        ("side_cache_corrupt_slots", 0),
        ("side_cache_occupied_entries", 0),
        ("side_cache_retained_bytes", 0),
        ("semantic_type_stats_dropped_events", 0),
    ):
        assert int(runtime[field]) == expected, (field, runtime)
    for prefix in ("first_mismatch", "last_mismatch"):
        for suffix in (
            "requested_type_id",
            "recorded_type_id",
            "requested_module_id",
            "recorded_module_id",
            "requested_callsite",
            "recorded_callsite",
        ):
            assert int(runtime[f"{prefix}_{suffix}"]) == 0, (prefix, suffix, runtime)
    assert runtime.get("type_rows") == [], runtime

    return {
        "producer_clone_status": producer_clone["rewrite_status"],
        "producer_clone_contract": producer_clone["metadata_pairing_contract"],
        "app_clone_outfile_fail_closed_count": len(app_clone_outfile),
        "app_clone_pathbuf_fail_closed_count": len(app_clone_path),
        "authenticated_drop_fail_closed_count": len(authenticated_drop_rows),
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

    with tempfile.TemporaryDirectory(prefix="unialloc-crosscrate-outfile-") as raw:
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
                "source": "mir_crosscrate_outfile_pathbuf_recovery_probe",
                "validated": True,
                "single_run": True,
                "benchmark": False,
                "toolchain": toolchain,
                "pass_source": str(pass_source),
                "boundaries": [
                    "Functional actual-rustc cross-crate OutFile/PathBuf fail-closed regression only; no benchmark or performance claim.",
                    "The producer aggregate remains unsupported: allocation and Drop candidates carry type_id zero and stay on the raw fallback route.",
                    "Pointer addresses remain diagnostic because raw fallback placement is outside this compiler-contract probe.",
                ],
                "evidence": evidence,
            },
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
