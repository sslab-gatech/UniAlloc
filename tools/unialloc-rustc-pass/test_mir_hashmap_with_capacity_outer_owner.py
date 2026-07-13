#!/usr/bin/env python3
"""Prove exact std HashMap::with_capacity scopes use the direct table identity."""

from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile


ROOT = Path(__file__).resolve().parents[2]
PASS_SOURCE = ROOT / "tools/unialloc-rustc-pass/unialloc-rustc-mir-rewrite-dry-run.rs"
PROBE_NAME = "hashmap_with_capacity_outer_owner_probe"
PRODUCER_FUNCTION = "make_producer"
CONSUMER_FUNCTION = "make_consumer"
CUSTOM_FUNCTION = "custom_hashmap_like_negative"
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
        r'''use std::collections::HashMap;
use std::mem::{align_of, size_of};
use unialloc::{
    semantic_auto_metadata_disable, semantic_fallback_attribution_snapshot,
    semantic_metadata_validation_snapshot, semantic_stats_recording_disable,
    semantic_stats_reset, semantic_stats_snapshot, type_isolation_side_cache_snapshot,
    UniAlloc,
};

#[global_allocator]
static ALLOCATOR: UniAlloc = UniAlloc;

#[repr(C)]
struct ProducerKey {
    nested_owner: Vec<u8>,
}

#[repr(C)]
struct ConsumerKey {
    nested_owner: Box<[u8; 0]>,
    pad_len: usize,
    pad_cap: usize,
}

struct AmbiguousKey {
    vec_owner: Vec<u8>,
    box_owner: Box<[u8; 0]>,
}

struct HashMapLike;

impl HashMapLike {
    #[inline(never)]
    fn with_capacity(_capacity: usize) -> HashMap<AmbiguousKey, u8> {
        HashMap::new()
    }
}

#[inline(never)]
fn make_producer(capacity: usize) -> HashMap<ProducerKey, u8> {
    HashMap::with_capacity(capacity)
}

#[inline(never)]
fn make_consumer(capacity: usize) -> HashMap<ConsumerKey, u8> {
    HashMap::with_capacity(capacity)
}

#[inline(never)]
fn custom_hashmap_like_negative(capacity: usize) -> HashMap<AmbiguousKey, u8> {
    HashMapLike::with_capacity(capacity)
}

#[inline(never)]
fn opaque_false() -> bool {
    unsafe { std::ptr::read_volatile(&false) }
}

fn main() {
    assert_eq!(size_of::<ProducerKey>(), size_of::<ConsumerKey>());
    assert_eq!(align_of::<ProducerKey>(), align_of::<ConsumerKey>());
    assert_eq!(size_of::<HashMap<ProducerKey, u8>>(), size_of::<HashMap<ConsumerKey, u8>>());
    assert_eq!(
        align_of::<HashMap<ProducerKey, u8>>(),
        align_of::<HashMap<ConsumerKey, u8>>()
    );

    // Keep the local same-name constructor in optimized MIR without executing
    // it or adding an allocation to the runtime cache oracle.
    if opaque_false() {
        let custom = custom_hashmap_like_negative(1);
        assert!(custom.is_empty());
        drop(custom);
    }

    semantic_auto_metadata_disable();
    semantic_stats_reset();

    let first = make_producer(1);
    let first_capacity = first.capacity();
    let after_first_alloc = semantic_stats_snapshot();
    let first_type_id = after_first_alloc.last_type_id;
    drop(first);
    let after_first_drop = semantic_stats_snapshot();

    let wrong = make_consumer(1);
    let wrong_capacity = wrong.capacity();
    let after_wrong_alloc = semantic_stats_snapshot();
    let wrong_type_id = after_wrong_alloc.last_type_id;
    let wrong_type_cache_hit_delta = after_wrong_alloc
        .typed_cache_hits
        .saturating_sub(after_first_drop.typed_cache_hits);
    drop(wrong);
    let after_wrong_drop = semantic_stats_snapshot();

    let recovered = make_producer(1);
    let recovered_capacity = recovered.capacity();
    let after_recovered_alloc = semantic_stats_snapshot();
    let recovered_type_id = after_recovered_alloc.last_type_id;
    let exact_type_cache_hit_delta = after_recovered_alloc
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
            "\"source\":\"hashmap_with_capacity_outer_owner_probe\",",
            "\"key_layout_bytes\":{},",
            "\"key_layout_align\":{},",
            "\"map_layout_bytes\":{},",
            "\"map_layout_align\":{},",
            "\"first_capacity\":{},",
            "\"wrong_capacity\":{},",
            "\"recovered_capacity\":{},",
            "\"first_type_id\":{},",
            "\"wrong_type_id\":{},",
            "\"recovered_type_id\":{},",
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
        size_of::<ProducerKey>(),
        align_of::<ProducerKey>(),
        size_of::<HashMap<ProducerKey, u8>>(),
        align_of::<HashMap<ProducerKey, u8>>(),
        first_capacity,
        wrong_capacity,
        recovered_capacity,
        first_type_id,
        wrong_type_id,
        recovered_type_id,
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


def with_capacity_rows(
    rows: list[object], function_name: str
) -> list[dict[str, object]]:
    return [
        row
        for row in rows
        if isinstance(row, dict)
        and function_matches(row, function_name)
        and "with_capacity" in str(row.get("callee") or "")
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
        (PRODUCER_FUNCTION, "HashMap<", "Vec<u8"),
        (CONSUMER_FUNCTION, "HashMap<", "Box<[u8"),
    ):
        matches = with_capacity_rows(rows, function_name)
        assert len(matches) == 1, [
            row
            for row in rows
            if isinstance(row, dict) and function_matches(row, function_name)
        ]
        row = matches[0]
        assert row.get("lowering_kind") == "semantic_scope_enter_exit_rewrite", row
        assert row.get("rewrite_status") == APPLIED_STATUS, row
        semantic = str(row.get("semantic_object_type") or "")
        destination = str(row.get("destination_type") or "")
        assert owner in semantic, row
        assert function_name.removeprefix("make_").capitalize() + "Key" in semantic, row
        assert semantic == destination, row
        assert nested not in semantic, row
        assert row.get("argument_types") == ["usize"], row
        assert int(row.get("type_id") or 0) != 0, row
        assert row.get("metadata_pairing_contract") == "semantic_scope_active_metadata", row
        positive[function_name] = row

    producer_type_id = int(positive[PRODUCER_FUNCTION]["type_id"])
    consumer_type_id = int(positive[CONSUMER_FUNCTION]["type_id"])
    assert producer_type_id != consumer_type_id, positive

    custom_rows = with_capacity_rows(rows, CUSTOM_FUNCTION)
    assert len(custom_rows) == 1, [
        row
        for row in rows
        if isinstance(row, dict) and function_matches(row, CUSTOM_FUNCTION)
    ]
    custom = custom_rows[0]
    custom_callee = str(custom.get("callee") or "")
    assert "with_capacity" in custom_callee, custom
    assert (
        "HashMapLike::with_capacity" in custom_callee or PROBE_NAME in custom_callee
    ), custom
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
    assert int(runtime["key_layout_bytes"]) == 24, runtime
    assert int(runtime["key_layout_align"]) == 8, runtime
    assert int(runtime["first_capacity"]) == int(runtime["wrong_capacity"]), runtime
    assert int(runtime["first_capacity"]) == int(runtime["recovered_capacity"]), runtime
    assert int(runtime["first_type_id"]) == producer_type_id, runtime
    assert int(runtime["wrong_type_id"]) == consumer_type_id, runtime
    assert int(runtime["recovered_type_id"]) == producer_type_id, runtime
    assert int(runtime["wrong_type_cache_hit_delta"]) == 0, runtime
    assert int(runtime["exact_type_cache_hit_delta"]) == 1, runtime
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
        "producer_with_capacity": {
            "callee": positive[PRODUCER_FUNCTION].get("callee"),
            "semantic_object_type": positive[PRODUCER_FUNCTION].get("semantic_object_type"),
            "type_id": producer_type_id,
            "rewrite_status": positive[PRODUCER_FUNCTION].get("rewrite_status"),
        },
        "consumer_with_capacity": {
            "callee": positive[CONSUMER_FUNCTION].get("callee"),
            "semantic_object_type": positive[CONSUMER_FUNCTION].get("semantic_object_type"),
            "type_id": consumer_type_id,
            "rewrite_status": positive[CONSUMER_FUNCTION].get("rewrite_status"),
        },
        "custom_same_name_status": custom.get("rewrite_status"),
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

    with tempfile.TemporaryDirectory(prefix="unialloc-hashmap-capacity-outer-") as raw:
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
                    "Exact std-owned HashMap::<K, V>::with_capacity only; with_capacity_and_hasher, with_hasher, new_in, HashSet, hashbrown, IndexMap, and local same-name constructors are not selected by this exception.",
                    "The runtime oracle uses equal K/V table geometry plus typed-cache hit deltas because stable HashMap exposes no deterministic raw-table address; it proves identity-directed cache selection in these runs, not universal address behavior or performance.",
                ],
            },
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
