#!/usr/bin/env python3
"""Prove exact Arc::new scopes use the direct outer Arc identity."""

from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile


ROOT = Path(__file__).resolve().parents[2]
PASS_SOURCE = ROOT / "tools/unialloc-rustc-pass/unialloc-rustc-mir-rewrite-dry-run.rs"
PROBE_NAME = "arc_new_outer_owner_probe"
PRODUCER_FUNCTION = "make_producer"
CONSUMER_FUNCTION = "make_consumer"
CUSTOM_FUNCTION = "custom_arc_like_negative"
APPLIED_STATUS = "actual_semantic_scope_enter_exit_rewrite_applied"


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
        r'''use std::mem::{align_of, size_of};
use std::sync::atomic::{AtomicUsize, Ordering};
use std::sync::Arc;
use unialloc::{
    semantic_auto_metadata_disable, semantic_fallback_attribution_snapshot,
    semantic_metadata_validation_snapshot, semantic_stats_recording_disable,
    semantic_stats_reset, semantic_stats_snapshot, type_isolation_side_cache_snapshot,
    UniAlloc,
};

#[global_allocator]
static ALLOCATOR: UniAlloc = UniAlloc;

static PRODUCER_DROPS: AtomicUsize = AtomicUsize::new(0);
static CONSUMER_DROPS: AtomicUsize = AtomicUsize::new(0);

#[repr(C)]
struct ProducerWithVec {
    owner: Vec<u8>,
}

impl Drop for ProducerWithVec {
    fn drop(&mut self) {
        PRODUCER_DROPS.fetch_add(1, Ordering::SeqCst);
    }
}

#[repr(C)]
struct ConsumerWithBox {
    owner: Box<[u8; 0]>,
    pad_len: usize,
    pad_cap: usize,
}

impl Drop for ConsumerWithBox {
    fn drop(&mut self) {
        CONSUMER_DROPS.fetch_add(1, Ordering::SeqCst);
    }
}

struct Ambiguous {
    vec_owner: Vec<u8>,
    box_owner: Box<[u8; 0]>,
}

struct ArcLike;

impl ArcLike {
    #[inline(never)]
    fn new(value: Ambiguous) -> Arc<Ambiguous> {
        Arc::new(value)
    }
}

#[inline(never)]
fn make_producer(seed: u8) -> Arc<ProducerWithVec> {
    assert!(seed != 0);
    Arc::new(ProducerWithVec { owner: Vec::new() })
}

#[inline(never)]
fn make_consumer(seed: u8) -> Arc<ConsumerWithBox> {
    assert!(seed != 0);
    Arc::new(ConsumerWithBox {
        owner: Box::new([]),
        pad_len: seed as usize,
        pad_cap: seed as usize,
    })
}

#[inline(never)]
fn custom_arc_like_negative(value: Ambiguous) -> Arc<Ambiguous> {
    ArcLike::new(value)
}

#[inline(never)]
fn opaque_false() -> bool {
    unsafe { std::ptr::read_volatile(&false) }
}

fn main() {
    assert_eq!(size_of::<ProducerWithVec>(), size_of::<ConsumerWithBox>());
    assert_eq!(align_of::<ProducerWithVec>(), align_of::<ConsumerWithBox>());

    // Keep the custom constructor in MIR without perturbing the address oracle.
    if opaque_false() {
        let custom = custom_arc_like_negative(Ambiguous {
            vec_owner: Vec::new(),
            box_owner: Box::new([]),
        });
        assert!(custom.vec_owner.is_empty());
        assert!(custom.box_owner.is_empty());
        drop(custom);
    }

    semantic_auto_metadata_disable();
    semantic_stats_reset();

    let first = make_producer(11);
    assert!(first.owner.is_empty());
    let first_pointer = Arc::as_ptr(&first) as usize;
    let first_type_id = semantic_stats_snapshot().last_type_id;
    drop(first);
    assert_eq!(PRODUCER_DROPS.load(Ordering::SeqCst), 1);

    let wrong = make_consumer(22);
    assert!(wrong.owner.is_empty());
    assert_eq!(wrong.pad_len, 22);
    assert_eq!(wrong.pad_cap, 22);
    let wrong_pointer = Arc::as_ptr(&wrong) as usize;
    let wrong_type_id = semantic_stats_snapshot().last_type_id;
    drop(wrong);
    assert_eq!(CONSUMER_DROPS.load(Ordering::SeqCst), 1);

    let recovered = make_producer(33);
    assert!(recovered.owner.is_empty());
    let recovered_pointer = Arc::as_ptr(&recovered) as usize;
    let recovered_type_id = semantic_stats_snapshot().last_type_id;
    let wrong_type_non_reuse = wrong_pointer != first_pointer;
    let exact_type_reuse = recovered_pointer == first_pointer;
    drop(recovered);
    assert_eq!(PRODUCER_DROPS.load(Ordering::SeqCst), 2);

    let stats = semantic_stats_snapshot();
    let fallback = semantic_fallback_attribution_snapshot();
    let validation = semantic_metadata_validation_snapshot();
    let side_cache = type_isolation_side_cache_snapshot();
    semantic_stats_recording_disable();

    println!(
        concat!(
            "{{",
            "\"source\":\"arc_new_outer_owner_probe\",",
            "\"payload_layout_bytes\":{},",
            "\"payload_layout_align\":{},",
            "\"first_pointer\":{},",
            "\"wrong_pointer\":{},",
            "\"recovered_pointer\":{},",
            "\"first_type_id\":{},",
            "\"wrong_type_id\":{},",
            "\"recovered_type_id\":{},",
            "\"wrong_type_non_reuse\":{},",
            "\"exact_type_reuse\":{},",
            "\"producer_drops\":{},",
            "\"consumer_drops\":{},",
            "\"typed_allocations\":{},",
            "\"typed_deallocations\":{},",
            "\"typed_cache_hits\":{},",
            "\"typed_cache_inserts\":{},",
            "\"fallback_allocations\":{},",
            "\"fallback_deallocations\":{},",
            "\"raw_alloc_no_metadata\":{},",
            "\"raw_dealloc_no_metadata\":{},",
            "\"raw_realloc_no_metadata\":{},",
            "\"recovery_identity_mismatches\":{},",
            "\"side_cache_corrupt_slots\":{}",
            "}}"
        ),
        size_of::<ProducerWithVec>(),
        align_of::<ProducerWithVec>(),
        first_pointer,
        wrong_pointer,
        recovered_pointer,
        first_type_id,
        wrong_type_id,
        recovered_type_id,
        wrong_type_non_reuse,
        exact_type_reuse,
        PRODUCER_DROPS.load(Ordering::SeqCst),
        CONSUMER_DROPS.load(Ordering::SeqCst),
        stats.typed_allocations,
        stats.typed_deallocations,
        stats.typed_cache_hits,
        stats.typed_cache_inserts,
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


def arc_new_rows(
    rows: list[object], function_name: str
) -> list[dict[str, object]]:
    return [
        row
        for row in rows
        if isinstance(row, dict)
        and function_matches(row, function_name)
        and "sync" in str(row.get("callee") or "")
        and "new" in str(row.get("callee") or "")
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
    positive: dict[str, dict[str, object]] = {}
    for function_name, owner, nested in (
        (PRODUCER_FUNCTION, "Arc<ProducerWithVec", "Vec<u8"),
        (CONSUMER_FUNCTION, "Arc<ConsumerWithBox", "Box<[u8; 0]"),
    ):
        matches = arc_new_rows(rows, function_name)
        assert len(matches) == 1, [
            row
            for row in rows
            if isinstance(row, dict) and function_matches(row, function_name)
        ]
        row = matches[0]
        assert row.get("lowering_kind") == "semantic_scope_enter_exit_rewrite", row
        assert row.get("rewrite_status") == APPLIED_STATUS, row
        semantic = str(row.get("semantic_object_type") or "")
        assert owner in semantic, row
        assert nested not in semantic, row
        assert function_name.removeprefix("make_").capitalize() in json.dumps(
            row.get("argument_types") or []
        ), row
        assert int(row.get("type_id") or 0) != 0, row
        assert row.get("metadata_pairing_contract") == "semantic_scope_active_metadata", row
        positive[function_name] = row

    producer_type_id = int(positive[PRODUCER_FUNCTION]["type_id"])
    consumer_type_id = int(positive[CONSUMER_FUNCTION]["type_id"])
    assert producer_type_id != consumer_type_id, positive

    custom_rows = [
        row
        for row in rows
        if isinstance(row, dict)
        and function_matches(row, CUSTOM_FUNCTION)
    ]
    assert len(custom_rows) == 1, [
        row
        for row in rows
        if isinstance(row, dict) and function_matches(row, CUSTOM_FUNCTION)
    ]
    custom = custom_rows[0]
    assert custom.get("lowering_kind") == "semantic_scope_unsolved_heap_object_candidate", custom
    assert custom.get("rewrite_status") != APPLIED_STATUS, custom
    assert custom.get("metadata_pairing_contract") in {
        "audit_only_ambiguous_heap_object_type",
        "audit_only_unresolved_heap_object_type",
    }, custom

    runtime = next(
        json.loads(line)
        for line in stdout.splitlines()
        if line.startswith("{") and PROBE_NAME in line
    )
    assert int(runtime["payload_layout_bytes"]) == 24, runtime
    assert int(runtime["payload_layout_align"]) == 8, runtime
    assert runtime["wrong_type_non_reuse"] is True, runtime
    assert runtime["exact_type_reuse"] is True, runtime
    assert int(runtime["first_pointer"]) != int(runtime["wrong_pointer"]), runtime
    assert int(runtime["first_pointer"]) == int(runtime["recovered_pointer"]), runtime
    assert int(runtime["first_type_id"]) == producer_type_id, runtime
    assert int(runtime["wrong_type_id"]) == consumer_type_id, runtime
    assert int(runtime["recovered_type_id"]) == producer_type_id, runtime
    assert int(runtime["producer_drops"]) == 2, runtime
    assert int(runtime["consumer_drops"]) == 1, runtime
    assert int(runtime["typed_allocations"]) == 3, runtime
    assert int(runtime["typed_deallocations"]) == 3, runtime
    assert int(runtime["typed_cache_hits"]) == 1, runtime
    assert int(runtime["typed_cache_inserts"]) == 3, runtime
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
        "producer_arc_new": {
            "callee": positive[PRODUCER_FUNCTION].get("callee"),
            "semantic_object_type": positive[PRODUCER_FUNCTION].get("semantic_object_type"),
            "type_id": producer_type_id,
            "rewrite_status": positive[PRODUCER_FUNCTION].get("rewrite_status"),
        },
        "consumer_arc_new": {
            "callee": positive[CONSUMER_FUNCTION].get("callee"),
            "semantic_object_type": positive[CONSUMER_FUNCTION].get("semantic_object_type"),
            "type_id": consumer_type_id,
            "rewrite_status": positive[CONSUMER_FUNCTION].get("rewrite_status"),
        },
        "custom_arc_like_status": custom.get("rewrite_status"),
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

    with tempfile.TemporaryDirectory(prefix="unialloc-arc-new-outer-") as raw:
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
                    "Exact alloc-crate Arc::new only; Arc::new_in, Rc, custom ArcLike constructors, and arbitrary factories are not selected.",
                    "The address oracle covers two same-layout Arc payload types and exact typed-cache reuse in one process, not universal Arc safety or performance.",
                ],
            },
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
