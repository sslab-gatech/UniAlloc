#!/usr/bin/env python3
"""Prove exact VecDeque capacity APIs use the direct ring-buffer identity."""

from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile


ROOT = Path(__file__).resolve().parents[2]
PASS_SOURCE = ROOT / "tools/unialloc-rustc-pass/unialloc-rustc-mir-rewrite-dry-run.rs"
PROBE_NAME = "vecdeque_capacity_outer_owner_probe"
PRODUCER_FUNCTION = "make_producer"
CONSUMER_FUNCTION = "make_consumer"
GROW_FUNCTION = "grow_producer"
PUSH_NEGATIVE_FUNCTION = "push_back_negative"
CUSTOM_NEGATIVE_FUNCTION = "custom_vecdeque_like_negative"
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
        r'''use std::collections::VecDeque;
use std::mem::{align_of, size_of};
use unialloc::{
    semantic_auto_metadata_disable, semantic_fallback_attribution_snapshot,
    semantic_metadata_validation_snapshot, semantic_stats_recording_disable,
    semantic_stats_reset, semantic_stats_snapshot, type_isolation_side_cache_snapshot,
    UniAlloc,
};

#[global_allocator]
static ALLOCATOR: UniAlloc = UniAlloc;

const SMALL_CAPACITY: usize = 4;
const GROW_ADDITIONAL: usize = 64;

#[repr(C)]
struct ProducerPayload {
    nested_owner: Vec<u8>,
}

#[repr(C)]
struct ConsumerPayload {
    nested_owner: Box<[u8; 0]>,
    pad_len: usize,
    pad_cap: usize,
}

struct AmbiguousPayload {
    vec_owner: Vec<u8>,
    box_owner: Box<[u8; 0]>,
}

struct VecDequeLike;

impl VecDequeLike {
    #[inline(never)]
    fn with_capacity(_capacity: usize) -> VecDeque<AmbiguousPayload> {
        VecDeque::new()
    }
}

#[inline(never)]
fn make_producer(capacity: usize) -> VecDeque<ProducerPayload> {
    VecDeque::with_capacity(capacity)
}

#[inline(never)]
fn make_consumer(capacity: usize) -> VecDeque<ConsumerPayload> {
    VecDeque::with_capacity(capacity)
}

#[inline(never)]
fn grow_producer(value: &mut VecDeque<ProducerPayload>) {
    value.reserve_exact(GROW_ADDITIONAL);
}

#[inline(never)]
fn push_back_negative(value: &mut VecDeque<ProducerPayload>) {
    value.push_back(ProducerPayload {
        nested_owner: Vec::new(),
    });
}

#[inline(never)]
fn custom_vecdeque_like_negative(capacity: usize) -> VecDeque<AmbiguousPayload> {
    VecDequeLike::with_capacity(capacity)
}

#[inline(never)]
fn opaque_false() -> bool {
    unsafe { std::ptr::read_volatile(&false) }
}

fn main() {
    assert_eq!(size_of::<ProducerPayload>(), size_of::<ConsumerPayload>());
    assert_eq!(align_of::<ProducerPayload>(), align_of::<ConsumerPayload>());

    // Retain the fail-closed controls in optimized MIR without perturbing the
    // deterministic runtime cache sequence below.
    if opaque_false() {
        let custom = custom_vecdeque_like_negative(1);
        drop(custom);
        let mut push = VecDeque::<ProducerPayload>::new();
        push_back_negative(&mut push);
        drop(push);
    }

    semantic_auto_metadata_disable();
    semantic_stats_reset();

    let before_first = semantic_stats_snapshot();
    let mut first = make_producer(SMALL_CAPACITY);
    first.push_back(ProducerPayload {
        nested_owner: Vec::new(),
    });
    let after_small = semantic_stats_snapshot();
    let small_capacity = first.capacity();
    grow_producer(&mut first);
    let after_grow = semantic_stats_snapshot();
    let large_capacity = first.capacity();
    let first_buffer = first.front().expect("producer element") as *const _ as usize;
    let producer_type_id = after_grow.last_type_id;
    drop(first);
    let after_first_drop = semantic_stats_snapshot();

    let mut wrong = make_consumer(large_capacity);
    wrong.push_back(ConsumerPayload {
        nested_owner: Box::new([]),
        pad_len: 0,
        pad_cap: 0,
    });
    let wrong_buffer = wrong.front().expect("consumer element") as *const _ as usize;
    let after_wrong = semantic_stats_snapshot();
    let consumer_type_id = after_wrong.last_type_id;
    let wrong_type_cache_hit_delta = after_wrong
        .typed_cache_hits
        .saturating_sub(after_first_drop.typed_cache_hits);
    drop(wrong);
    let after_wrong_drop = semantic_stats_snapshot();

    let mut recovered = make_producer(large_capacity);
    recovered.push_back(ProducerPayload {
        nested_owner: Vec::new(),
    });
    let recovered_buffer = recovered.front().expect("recovered element") as *const _ as usize;
    let after_recovered = semantic_stats_snapshot();
    let recovered_type_id = after_recovered.last_type_id;
    let exact_type_cache_hit_delta = after_recovered
        .typed_cache_hits
        .saturating_sub(after_wrong_drop.typed_cache_hits);
    drop(recovered);

    let stats = semantic_stats_snapshot();
    let fallback = semantic_fallback_attribution_snapshot();
    let validation = semantic_metadata_validation_snapshot();
    let side_cache = type_isolation_side_cache_snapshot();
    semantic_stats_recording_disable();

    println!(
        concat!(
            "{{",
            "\"source\":\"vecdeque_capacity_outer_owner_probe\",",
            "\"payload_layout_bytes\":{},",
            "\"payload_layout_align\":{},",
            "\"small_capacity\":{},",
            "\"large_capacity\":{},",
            "\"producer_type_id\":{},",
            "\"consumer_type_id\":{},",
            "\"recovered_type_id\":{},",
            "\"first_buffer\":{},",
            "\"wrong_buffer\":{},",
            "\"recovered_buffer\":{},",
            "\"wrong_type_non_reuse\":{},",
            "\"same_type_recovery\":{},",
            "\"small_typed_allocations\":{},",
            "\"grow_typed_allocations\":{},",
            "\"grow_typed_deallocations\":{},",
            "\"wrong_type_cache_hit_delta\":{},",
            "\"exact_type_cache_hit_delta\":{},",
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
        size_of::<ProducerPayload>(),
        align_of::<ProducerPayload>(),
        small_capacity,
        large_capacity,
        producer_type_id,
        consumer_type_id,
        recovered_type_id,
        first_buffer,
        wrong_buffer,
        recovered_buffer,
        wrong_buffer != first_buffer,
        recovered_buffer == first_buffer,
        after_small
            .typed_allocations
            .saturating_sub(before_first.typed_allocations),
        after_grow
            .typed_allocations
            .saturating_sub(after_small.typed_allocations),
        after_grow
            .typed_deallocations
            .saturating_sub(after_small.typed_deallocations),
        wrong_type_cache_hit_delta,
        exact_type_cache_hit_delta,
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


def function_method_rows(
    rows: list[object], function_name: str, method: str
) -> list[dict[str, object]]:
    return [
        row
        for row in rows
        if isinstance(row, dict)
        and function_matches(row, function_name)
        and method in str(row.get("callee") or "")
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
    positives: dict[str, dict[str, object]] = {}
    for function_name, method, payload in (
        (PRODUCER_FUNCTION, "with_capacity", "ProducerPayload"),
        (CONSUMER_FUNCTION, "with_capacity", "ConsumerPayload"),
        (GROW_FUNCTION, "reserve_exact", "ProducerPayload"),
    ):
        matches = function_method_rows(rows, function_name, method)
        assert len(matches) == 1, [
            row
            for row in rows
            if isinstance(row, dict) and function_matches(row, function_name)
        ]
        row = matches[0]
        assert row.get("lowering_kind") == "semantic_scope_enter_exit_rewrite", row
        assert row.get("rewrite_status") == APPLIED_STATUS, row
        semantic = str(row.get("semantic_object_type") or "")
        assert "VecDeque<" in semantic and payload in semantic, row
        assert "Vec<u8" not in semantic and "Box<[u8" not in semantic, row
        assert int(row.get("type_id") or 0) != 0, row
        assert row.get("metadata_pairing_contract") == "semantic_scope_active_metadata", row
        positives[function_name] = row

    producer_type_id = int(positives[PRODUCER_FUNCTION]["type_id"])
    consumer_type_id = int(positives[CONSUMER_FUNCTION]["type_id"])
    assert producer_type_id != consumer_type_id, positives
    assert int(positives[GROW_FUNCTION]["type_id"]) == producer_type_id, positives
    assert positives[PRODUCER_FUNCTION].get("argument_types") == ["usize"], positives
    assert positives[CONSUMER_FUNCTION].get("argument_types") == ["usize"], positives
    grow_arguments = json.dumps(positives[GROW_FUNCTION].get("argument_types") or [])
    assert "VecDeque<ProducerPayload" in grow_arguments and "usize" in grow_arguments, positives

    custom_rows = function_method_rows(rows, CUSTOM_NEGATIVE_FUNCTION, "with_capacity")
    assert len(custom_rows) == 1, custom_rows
    custom = custom_rows[0]
    assert custom.get("lowering_kind") == "semantic_scope_unsolved_heap_object_candidate", custom
    assert custom.get("rewrite_status") != APPLIED_STATUS, custom
    assert custom.get("metadata_pairing_contract") in {
        "audit_only_ambiguous_heap_object_type",
        "audit_only_unresolved_heap_object_type",
    }, custom

    push_rows = function_method_rows(rows, PUSH_NEGATIVE_FUNCTION, "push_back")
    assert len(push_rows) == 1, push_rows
    push = push_rows[0]
    assert push.get("lowering_kind") == "semantic_scope_unsolved_heap_object_candidate", push
    assert push.get("rewrite_status") in {
        AMBIGUOUS_STATUS,
        "semantic_scope_rewrite_skipped_unresolved_heap_object_type",
    }, push
    assert push.get("metadata_pairing_contract") in {
        "audit_only_ambiguous_heap_object_type",
        "audit_only_unresolved_heap_object_type",
    }, push
    push_text = json.dumps(push, sort_keys=True)
    assert "VecDeque<ProducerPayload" in push_text and "ProducerPayload" in push_text, push

    drop_rows = [
        row
        for row in rows
        if isinstance(row, dict)
        and row.get("callee") == "TerminatorKind::Drop"
        and any(
            marker in json.dumps(row, sort_keys=True)
            for marker in ("VecDeque<ProducerPayload", "VecDeque<ConsumerPayload")
        )
    ]
    assert drop_rows, "expected nested-owner VecDeque Drop audit rows"
    for row in drop_rows:
        assert (
            row.get("rewrite_status")
            == "semantic_scope_drop_rewrite_skipped_multiple_heap_owners"
        ), row
        assert (
            row.get("metadata_pairing_contract")
            == "audit_only_multiple_heap_owner_drop_type"
        ), row
        assert (
            row.get("lowering_kind")
            == "semantic_scope_drop_multiple_heap_owners_skipped"
        ), row

    runtime = next(
        json.loads(line)
        for line in stdout.splitlines()
        if line.startswith("{") and PROBE_NAME in line
    )
    assert int(runtime["payload_layout_bytes"]) == 24, runtime
    assert int(runtime["payload_layout_align"]) == 8, runtime
    assert int(runtime["large_capacity"]) > int(runtime["small_capacity"]), runtime
    assert int(runtime["producer_type_id"]) == producer_type_id, runtime
    assert int(runtime["consumer_type_id"]) == consumer_type_id, runtime
    assert int(runtime["recovered_type_id"]) == producer_type_id, runtime
    assert runtime["wrong_type_non_reuse"] is True, runtime
    assert runtime["same_type_recovery"] is True, runtime
    assert int(runtime["small_typed_allocations"]) == 1, runtime
    assert int(runtime["grow_typed_allocations"]) == 1, runtime
    assert int(runtime["grow_typed_deallocations"]) == 1, runtime
    assert int(runtime["wrong_type_cache_hit_delta"]) == 0, runtime
    assert int(runtime["exact_type_cache_hit_delta"]) == 1, runtime
    assert int(runtime["typed_allocations"]) == 4, runtime
    assert int(runtime["typed_deallocations"]) == 4, runtime
    assert int(runtime["typed_cache_hits"]) == 1, runtime
    assert int(runtime["typed_cache_inserts"]) == 4, runtime
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
        "producer_with_capacity": positives[PRODUCER_FUNCTION],
        "producer_reserve_exact": positives[GROW_FUNCTION],
        "consumer_with_capacity": positives[CONSUMER_FUNCTION],
        "custom_same_name_status": custom.get("rewrite_status"),
        "push_back_status": push.get("rewrite_status"),
        "drop_statuses": sorted({str(row.get("rewrite_status")) for row in drop_rows}),
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

    with tempfile.TemporaryDirectory(prefix="unialloc-vecdeque-capacity-owner-") as raw:
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
                    "Exact alloc-owned VecDeque with_capacity and six capacity-only reserve/shrink methods only; element-affecting methods, Drop, custom same-name helpers, and allocator-specific APIs remain fail closed.",
                    "Single actual-RUSTC_WRAPPER functional alloc/realloc/drop and typed-cache oracle; not universal container coverage or performance evidence.",
                ],
            },
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
