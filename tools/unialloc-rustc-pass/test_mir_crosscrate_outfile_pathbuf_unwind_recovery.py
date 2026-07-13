#!/usr/bin/env python3
"""Prove allocation-time OutFile/PathBuf identity remains authoritative during cleanup unwind."""

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
PRODUCER_CRATE = "outfile_pathbuf_unwind_producer"
APP_CRATE = "outfile_pathbuf_unwind_app"
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
name = "outfile-pathbuf-unwind-producer"
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

#[inline(never)]
pub fn clone_drop_and_return_pointer(source: &OutFile) -> usize {{
    let value = source.clone();
    let pointer = value.pointer();
    assert_eq!(value.len(), PATH_LEN);
    pointer
}}
''',
        encoding="utf-8",
    )
    (app / "Cargo.toml").write_text(
        f'''[package]
name = "outfile-pathbuf-unwind-app"
version = "0.1.0"
edition = "2021"

[dependencies]
lock_api = "=0.4.3"
outfile_pathbuf_unwind_producer = {{ package = "outfile-pathbuf-unwind-producer", path = "../producer" }}
unialloc = {{ path = {unialloc_path}, features = ["stats", "type_isolation"] }}
''',
        encoding="utf-8",
    )
    (app / "src/main.rs").write_text(
        r'''use std::fmt::Write as _;
use std::panic::{catch_unwind, AssertUnwindSafe};
use unialloc::{
    semantic_auto_metadata_disable, semantic_fallback_attribution_snapshot,
    semantic_metadata_validation_snapshot, semantic_stats_recording_disable,
    semantic_stats_recording_enable, semantic_stats_reset, semantic_stats_snapshot,
    semantic_type_stats_recording_disable, semantic_type_stats_snapshot,
    type_isolation_side_cache_snapshot, SemanticTypeStatsSnapshot, UniAlloc,
};

#[global_allocator]
static ALLOCATOR: UniAlloc = UniAlloc;

struct EnableStatsOnCleanup;
struct DisableStatsAfterValue;

impl Drop for EnableStatsOnCleanup {
    fn drop(&mut self) {
        // `value` was declared first, so this guard runs before its cleanup
        // Drop and makes the allocator-bearing deallocation observable again.
        semantic_stats_recording_enable();
    }
}

impl Drop for DisableStatsAfterValue {
    fn drop(&mut self) {
        // This guard was declared before the OutFile and therefore runs just
        // after it, excluding the rest of std's unwind transport.
        semantic_stats_recording_disable();
    }
}

#[inline(never)]
fn unwind_returned_outfile(
    source: &outfile_pathbuf_unwind_producer::OutFile,
    pointer: &mut usize,
) -> ! {
    let _disable_stats_after_value = DisableStatsAfterValue;
    let value = outfile_pathbuf_unwind_producer::clone_outfile(source);
    assert_eq!(
        value.len(),
        outfile_pathbuf_unwind_producer::PATH_LEN,
    );
    *pointer = value.pointer();
    let _enable_stats_on_cleanup = EnableStatsOnCleanup;
    // Exclude std's panic transport allocations without hiding the OutFile
    // cleanup Drop: the guard above re-enables accounting before `value` is
    // destroyed. Metadata recovery validation itself remains active.
    semantic_stats_recording_disable();
    panic!("exercise returned OutFile cleanup Drop");
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
    let producer_seed = outfile_pathbuf_unwind_producer::OutFile::seed(b'p');
    assert_eq!(
        producer_seed.len(),
        outfile_pathbuf_unwind_producer::PATH_LEN,
    );
    semantic_stats_reset();

    let mut unwind_pointer = 0usize;
    let before_unwind = semantic_metadata_validation_snapshot();
    let unwind_result = catch_unwind(AssertUnwindSafe(|| {
        unwind_returned_outfile(&producer_seed, &mut unwind_pointer);
    }));
    // Do not charge destruction of std's caught panic payload to the
    // allocator lifecycle under test.
    semantic_stats_recording_disable();
    let panic_observed = unwind_result.is_err();
    drop(unwind_result);
    semantic_stats_recording_enable();
    assert!(panic_observed, "the OutFile lifecycle must unwind");
    assert_ne!(unwind_pointer, 0);

    let after_unwind = semantic_metadata_validation_snapshot();
    let unwind_mismatch_delta = after_unwind
        .recovery_identity_mismatches
        .saturating_sub(before_unwind.recovery_identity_mismatches);
    assert_eq!(
        unwind_mismatch_delta, 1,
        "one returned aggregate cleanup Drop must produce one expected module recovery mismatch"
    );

    let producer_recovery_pointer =
        outfile_pathbuf_unwind_producer::clone_drop_and_return_pointer(&producer_seed);
    assert_eq!(
        producer_recovery_pointer, unwind_pointer,
        "the producer module must reuse its recovered PathBuf storage after unwind"
    );

    let stats = semantic_stats_snapshot();
    let fallback = semantic_fallback_attribution_snapshot();
    let validation = semantic_metadata_validation_snapshot();
    let side_cache = type_isolation_side_cache_snapshot();
    let mut rows = [SemanticTypeStatsSnapshot::empty(); 16];
    let row_count = semantic_type_stats_snapshot(&mut rows);

    assert_eq!(
        validation.recovery_identity_mismatches,
        after_unwind.recovery_identity_mismatches,
        "the producer same-domain lifecycle must not add a recovery mismatch"
    );
    assert_eq!(stats.typed_allocations, 2, "{stats:?}");
    assert_eq!(stats.typed_deallocations, 2, "{stats:?}");
    assert_eq!(stats.typed_cache_hits, 1, "{stats:?}");
    assert_eq!(stats.typed_cache_inserts, 2, "{stats:?}");
    assert_eq!(stats.fallback_allocations, 0, "{stats:?}");
    assert_eq!(stats.fallback_deallocations, 0, "{stats:?}");
    assert_eq!(stats.semantic_type_stats_dropped_events, 0, "{stats:?}");
    assert_eq!(fallback.raw_alloc_no_metadata, 0, "{fallback:?}");
    assert_eq!(fallback.raw_dealloc_no_metadata, 0, "{fallback:?}");
    assert_eq!(fallback.raw_realloc_no_metadata, 0, "{fallback:?}");
    assert_eq!(side_cache.corrupt_slots, 0, "{side_cache:?}");

    semantic_type_stats_recording_disable();
    semantic_stats_recording_disable();
    std::mem::forget(producer_seed);
    let type_rows = type_rows_json(&rows, row_count);
    println!(
        concat!(
            "{{",
            "\"source\":\"crosscrate_outfile_pathbuf_unwind_recovery\",",
            "\"panic_observed\":{},",
            "\"unwind_pointer\":{},",
            "\"producer_recovery_pointer\":{},",
            "\"producer_exact_reuse_after_unwind\":true,",
            "\"unwind_mismatch_delta\":{},",
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
            "\"mismatch_requested_type_id\":{},",
            "\"mismatch_recorded_type_id\":{},",
            "\"mismatch_requested_module_id\":{},",
            "\"mismatch_recorded_module_id\":{},",
            "\"mismatch_requested_callsite\":{},",
            "\"mismatch_recorded_callsite\":{},",
            "\"side_cache_corrupt_slots\":{},",
            "\"semantic_type_stats_dropped_events\":{},",
            "\"type_rows\":{}",
            "}}"
        ),
        panic_observed,
        unwind_pointer,
        producer_recovery_pointer,
        unwind_mismatch_delta,
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
        after_unwind.last_mismatch_requested_type_id,
        after_unwind.last_mismatch_recorded_type_id,
        after_unwind.last_mismatch_requested_module_id,
        after_unwind.last_mismatch_recorded_module_id,
        after_unwind.last_mismatch_requested_callsite,
        after_unwind.last_mismatch_recorded_callsite,
        side_cache.corrupt_slots,
        stats.semantic_type_stats_dropped_events,
        type_rows,
    );
}''',
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
        and "PathBuf" in str(row.get("semantic_object_type") or "")
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
            if str(row.get("mir_function") or "")
            == "<OutFile as std::clone::Clone>::clone"
            and row.get("destination_type")
            == "std::option::Option<std::path::PathBuf>"
            and "clone::Clone" in str(row.get("callee") or "")
            and row.get("lowering_kind") == "semantic_scope_enter_exit_rewrite"
        ],
        "producer OutFile::clone Option<PathBuf> allocation",
    )
    producer_drops = [
        row
        for row in producer_rows
        if "clone_drop_and_return_pointer" in str(row.get("mir_function") or "")
        and row.get("lowering_kind") == "semantic_scope_drop_rewrite"
        and "OutFile" in str(row.get("destination_type") or "")
    ]
    assert producer_drops, "missing producer same-domain OutFile Drop"
    cleanup_drop = unique_row(
        [
            row
            for row in app_rows
            if "unwind_returned_outfile" in str(row.get("mir_function") or "")
            and row.get("lowering_kind") == "semantic_scope_drop_rewrite"
            and "OutFile" in str(row.get("destination_type") or "")
        ],
        "application OutFile cleanup Drop",
    )

    type_id = int(producer_alloc.get("type_id") or 0)
    producer_module = int(producer_alloc.get("module_id") or 0)
    producer_callsite = int(producer_alloc.get("callsite") or 0)
    app_module = int(cleanup_drop.get("module_id") or 0)
    cleanup_callsite = int(cleanup_drop.get("callsite") or 0)
    producer_drop_callsites = [int(row.get("callsite") or 0) for row in producer_drops]
    assert type_id != 0
    assert producer_module != 0 and app_module != 0 and producer_module != app_module
    assert producer_callsite != 0 and cleanup_callsite != 0
    assert all(producer_drop_callsites), producer_drops
    assert int(cleanup_drop.get("type_id") or 0) == type_id, cleanup_drop
    assert all(int(row.get("type_id") or 0) == type_id for row in producer_drops), producer_drops
    assert all(
        int(row.get("module_id") or 0) == producer_module for row in producer_drops
    ), producer_drops
    for row in (producer_alloc, cleanup_drop, *producer_drops):
        assert int(row.get("flags") or 0) & TYPE_ISOLATED, row
    assert producer_alloc.get("metadata_pairing_contract") == (
        "semantic_scope_active_metadata"
    ), producer_alloc
    for row in (*producer_drops, cleanup_drop):
        assert row.get("callee") == "TerminatorKind::Drop", row
        assert row.get("metadata_pairing_contract") == (
            "semantic_scope_drop_active_metadata"
        ), row

    # `unwind_returned_outfile` has return type `!` and its sole returned
    # aggregate can only be destroyed from the panic cleanup block. A cleanup
    # Drop cannot itself install a second unwind-pop edge. The runtime mismatch
    # callsite check below proves that this exact audited row executed.
    assert cleanup_drop.get("semantic_scope_unwind_pop_inserted") is False, cleanup_drop
    assert "original unwind cleanup also passes through pop" not in str(
        cleanup_drop.get("replacement_preview") or ""
    ), cleanup_drop
    assert str(cleanup_drop.get("basic_block") or "").startswith("bb"), cleanup_drop
    assert "src/main.rs" in str(cleanup_drop.get("source_span") or ""), cleanup_drop

    runtime_lines = [line for line in stdout.splitlines() if line.startswith("{")]
    assert len(runtime_lines) == 1, stdout
    runtime = json.loads(runtime_lines[0])
    assert runtime.get("source") == "crosscrate_outfile_pathbuf_unwind_recovery", runtime
    assert runtime.get("panic_observed") is True, runtime
    assert runtime.get("producer_exact_reuse_after_unwind") is True, runtime
    assert int(runtime["unwind_pointer"]) != 0, runtime
    assert int(runtime["unwind_pointer"]) == int(runtime["producer_recovery_pointer"]), runtime
    assert int(runtime["unwind_mismatch_delta"]) == 1, runtime
    assert int(runtime["recovery_identity_matches"]) == 1, runtime
    assert int(runtime["recovery_identity_mismatches"]) == 1, runtime
    assert int(runtime["side_cache_corrupt_slots"]) == 0, runtime
    for field, expected in (
        ("typed_allocations", 2),
        ("typed_deallocations", 2),
        ("typed_cache_hits", 1),
        ("typed_cache_inserts", 2),
        ("fallback_allocations", 0),
        ("fallback_deallocations", 0),
        ("raw_alloc_no_metadata", 0),
        ("raw_dealloc_no_metadata", 0),
        ("raw_realloc_no_metadata", 0),
        ("semantic_type_stats_dropped_events", 0),
    ):
        assert int(runtime[field]) == expected, (field, runtime)

    assert int(runtime["mismatch_requested_type_id"]) == type_id, runtime
    assert int(runtime["mismatch_recorded_type_id"]) == type_id, runtime
    assert int(runtime["mismatch_requested_module_id"]) == app_module, runtime
    assert int(runtime["mismatch_recorded_module_id"]) == producer_module, runtime
    assert int(runtime["mismatch_requested_callsite"]) == cleanup_callsite, runtime
    assert int(runtime["mismatch_recorded_callsite"]) == producer_callsite, runtime

    type_rows = runtime.get("type_rows")
    assert isinstance(type_rows, list), runtime
    matching = [
        row
        for row in type_rows
        if isinstance(row, dict)
        and int(row.get("type_id") or 0) == type_id
        and int(row.get("module_id") or 0) == producer_module
    ]
    assert matching, type_rows
    assert all(
        int(row.get("policy_flags_seen") or 0) & TYPE_ISOLATED for row in matching
    ), matching
    producer_runtime = {
        field: sum(int(row.get(field) or 0) for row in matching)
        for field in ("allocations", "deallocations", "cache_hits", "cache_inserts")
    }
    assert producer_runtime == {
        "allocations": 2,
        "deallocations": 2,
        "cache_hits": 1,
        "cache_inserts": 2,
    }, (producer_runtime, type_rows)
    assert not [
        row
        for row in type_rows
        if isinstance(row, dict)
        and int(row.get("type_id") or 0) == type_id
        and int(row.get("module_id") or 0) == app_module
        and (int(row.get("allocations") or 0) or int(row.get("deallocations") or 0))
    ], type_rows

    return {
        "type_id": type_id,
        "producer_module_id": producer_module,
        "application_module_id": app_module,
        "producer_allocation_callsite": producer_callsite,
        "application_cleanup_drop_callsite": cleanup_callsite,
        "producer_drop_callsites": producer_drop_callsites,
        "cleanup_drop_basic_block": cleanup_drop.get("basic_block"),
        "cleanup_drop_source_span": cleanup_drop.get("source_span"),
        "producer_runtime": producer_runtime,
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
    if not __debug__:
        raise SystemExit("do not run this assertion-based validator with python -O")
    args = parse_args()
    pass_source = args.pass_source.resolve()
    toolchain = args.toolchain.lstrip("+").strip()
    assert pass_source.is_file(), pass_source
    rustc = shutil.which("rustc") or "rustc"
    cargo = shutil.which("cargo") or "cargo"
    sysroot = subprocess.check_output(
        [rustc, f"+{toolchain}", "--print", "sysroot"], cwd=ROOT, text=True
    ).strip()

    with tempfile.TemporaryDirectory(prefix="unialloc-crosscrate-outfile-unwind-") as raw:
        workspace = Path(raw)
        app = write_fixture(workspace)
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
                # The repository normally aborts in dev builds. This isolated
                # actual-wrapper regression must execute cleanup Drop edges.
                "CARGO_PROFILE_DEV_PANIC": "unwind",
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
                "source": "mir_crosscrate_outfile_pathbuf_unwind_recovery_probe",
                "validated": True,
                "single_run": True,
                "benchmark": False,
                "toolchain": toolchain,
                "pass_source": str(pass_source),
                "boundaries": [
                    "Functional actual-rustc cross-crate OutFile/PathBuf cleanup-unwind recovery regression only; the sibling normal-return probe carries the wrong-module non-reuse/module-isolation oracle; no benchmark or performance claim.",
                    "The one visible module mismatch is expected when the consumer cleanup Drop cannot reconstruct the producer clone module; the allocation-time recovery record remains authoritative.",
                    "The isolated Cargo invocation opts into panic=unwind solely to execute the returned aggregate cleanup edge; repository profiles remain unchanged.",
                    "This bounded OutFile-like fixture does not establish universal cross-crate owner coverage or whole-application exact pairing.",
                ],
                "evidence": evidence,
            },
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
