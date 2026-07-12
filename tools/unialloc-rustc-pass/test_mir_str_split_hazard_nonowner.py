#!/usr/bin/env python3
"""Prove exact str split iterators are non-owners only in by-value hazard scans."""

from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile


ROOT = Path(__file__).resolve().parents[2]
PASS_SOURCE = ROOT / "tools/unialloc-rustc-pass/unialloc-rustc-mir-rewrite-dry-run.rs"
PROBE_NAME = "str_split_hazard_nonowner_probe"
SPLIT_FUNCTION = "collect_split"
SPLIT_INCLUSIVE_FUNCTION = "collect_split_inclusive"
CUSTOM_RAW_FUNCTION = "collect_custom_raw"
APPLIED_STATUS = "actual_semantic_scope_enter_exit_rewrite_applied"
UNRESOLVED_STATUS = "semantic_scope_rewrite_skipped_unresolved_heap_object_type"


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
    semantic_auto_metadata_disable, semantic_fallback_attribution_snapshot,
    semantic_metadata_validation_snapshot, semantic_ownership_transfer_snapshot,
    semantic_stats_recording_disable, semantic_stats_reset, semantic_stats_snapshot,
    type_isolation_side_cache_snapshot, UniAlloc,
};

#[global_allocator]
static ALLOCATOR: UniAlloc = UniAlloc;

const INPUT: &str = "aa|aa|aa|aa|aa|aa|aa|aa|aa|aa|aa|aa|aa|aa|aa|aa|aa|aa|aa|aa|aa|aa|aa|aa|aa|aa|aa|aa|aa|aa|aa|aa";
const PIECES: usize = 32;

#[inline(never)]
fn collect_split(input: &'static str) -> Vec<&'static str> {
    input.split('|').collect()
}

#[inline(never)]
fn collect_split_inclusive(input: &'static str) -> Vec<&'static str> {
    input.split_inclusive('|').collect()
}

struct RawStrIter<'a> {
    current: *const &'a str,
    end: *const &'a str,
}

impl<'a> RawStrIter<'a> {
    fn new(input: &'a [&'a str]) -> Self {
        Self {
            current: input.as_ptr(),
            end: unsafe { input.as_ptr().add(input.len()) },
        }
    }
}

impl<'a> Iterator for RawStrIter<'a> {
    type Item = &'a str;

    fn next(&mut self) -> Option<Self::Item> {
        if self.current == self.end {
            return None;
        }
        let value = unsafe { *self.current };
        self.current = unsafe { self.current.add(1) };
        Some(value)
    }
}

#[inline(never)]
fn collect_custom_raw<'a>(input: &'a [&'a str]) -> Vec<&'a str> {
    RawStrIter::new(input).collect()
}

#[inline(never)]
fn opaque_false() -> bool {
    unsafe { std::ptr::read_volatile(&false) }
}

fn main() {
    semantic_auto_metadata_disable();
    semantic_stats_reset();
    let transfer_before = semantic_ownership_transfer_snapshot();

    let first = collect_split(INPUT);
    assert_eq!(first.len(), PIECES);
    assert!(first.iter().all(|piece| *piece == "aa"));
    let first_pointer = first.as_ptr() as usize;
    let first_capacity = first.capacity();
    let first_type_id = semantic_stats_snapshot().last_type_id;
    drop(first);

    // Same allocation layout, different Vec identity. It must not consume the
    // Vec<&str> cache entry released above.
    let wrong = Vec::<[usize; 2]>::with_capacity(first_capacity);
    let wrong_pointer = wrong.as_ptr() as usize;
    let wrong_type_id = semantic_stats_snapshot().last_type_id;
    let wrong_type_non_reuse = wrong_pointer != first_pointer;
    drop(wrong);

    let second = collect_split_inclusive(INPUT);
    assert_eq!(second.len(), PIECES);
    assert!(second[..PIECES - 1].iter().all(|piece| *piece == "aa|"));
    assert_eq!(second[PIECES - 1], "aa");
    let second_pointer = second.as_ptr() as usize;
    let second_capacity = second.capacity();
    let second_type_id = semantic_stats_snapshot().last_type_id;
    let exact_type_reuse = second_pointer == first_pointer;

    // Keep the negative control in optimized MIR without executing it and
    // perturbing the deterministic cache sequence.
    if opaque_false() {
        let custom = collect_custom_raw(&["aa", "bb", "cc", "dd"]);
        assert_eq!(custom.as_slice(), ["aa", "bb", "cc", "dd"]);
        drop(custom);
    }

    let transfer_after = semantic_ownership_transfer_snapshot();
    let stats = semantic_stats_snapshot();
    let fallback = semantic_fallback_attribution_snapshot();
    let validation = semantic_metadata_validation_snapshot();
    let side_cache = type_isolation_side_cache_snapshot();
    semantic_stats_recording_disable();

    println!(
        concat!(
            "{{",
            "\"source\":\"str_split_hazard_nonowner_probe\",",
            "\"first_pointer\":{},",
            "\"wrong_pointer\":{},",
            "\"second_pointer\":{},",
            "\"first_capacity\":{},",
            "\"second_capacity\":{},",
            "\"first_type_id\":{},",
            "\"wrong_type_id\":{},",
            "\"second_type_id\":{},",
            "\"wrong_type_non_reuse\":{},",
            "\"exact_type_reuse\":{},",
            "\"transfer_attempted\":{},",
            "\"transfer_applied\":{},",
            "\"transfer_rejected\":{},",
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
        wrong_pointer,
        second_pointer,
        first_capacity,
        second_capacity,
        first_type_id,
        wrong_type_id,
        second_type_id,
        wrong_type_non_reuse,
        exact_type_reuse,
        transfer_after.attempted.saturating_sub(transfer_before.attempted),
        transfer_after.applied.saturating_sub(transfer_before.applied),
        transfer_after.rejected.saturating_sub(transfer_before.rejected),
        stats.fallback_allocations,
        stats.fallback_deallocations,
        fallback.raw_alloc_no_metadata,
        fallback.raw_dealloc_no_metadata,
        fallback.raw_realloc_no_metadata,
        validation.recovery_identity_mismatches,
        side_cache.corrupt_slots,
    );

    drop(second);
}
''',
        encoding="utf-8",
    )
    return app


def function_matches(row: dict[str, object], function_name: str) -> bool:
    function = str(row.get("mir_function") or "")
    return function == function_name or function.endswith(f"::{function_name}")


def collect_rows(
    rows: list[object], function_name: str
) -> list[dict[str, object]]:
    return [
        row
        for row in rows
        if isinstance(row, dict)
        and function_matches(row, function_name)
        and "collect" in str(row.get("callee") or "")
    ]


def load_runtime(stdout: str) -> dict[str, object]:
    return next(
        json.loads(line)
        for line in stdout.splitlines()
        if line.startswith("{") and PROBE_NAME in line
    )


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
    for function_name in (SPLIT_FUNCTION, SPLIT_INCLUSIVE_FUNCTION):
        matches = collect_rows(rows, function_name)
        assert len(matches) == 1, matches
        row = matches[0]
        assert row.get("lowering_kind") == "semantic_scope_enter_exit_rewrite", row
        assert row.get("rewrite_status") == APPLIED_STATUS, row
        assert "Vec<&" in str(row.get("semantic_object_type") or ""), row
        assert "str" in str(row.get("semantic_object_type") or ""), row
        assert int(row.get("type_id") or 0) != 0, row
        positive[function_name] = row

    assert positive[SPLIT_FUNCTION].get("type_id") == positive[
        SPLIT_INCLUSIVE_FUNCTION
    ].get("type_id"), positive

    custom_rows = collect_rows(rows, CUSTOM_RAW_FUNCTION)
    assert len(custom_rows) == 1, custom_rows
    custom = custom_rows[0]
    assert custom.get("lowering_kind") == "semantic_scope_unsolved_heap_object_candidate", custom
    assert custom.get("rewrite_status") == UNRESOLVED_STATUS, custom
    assert custom.get("metadata_pairing_contract") == "audit_only_unresolved_heap_object_type", custom

    runtime = load_runtime(stdout)
    assert runtime["wrong_type_non_reuse"] is True, runtime
    assert runtime["exact_type_reuse"] is True, runtime
    assert int(runtime["first_pointer"]) != int(runtime["wrong_pointer"]), runtime
    assert int(runtime["first_pointer"]) == int(runtime["second_pointer"]), runtime
    assert int(runtime["first_capacity"]) == int(runtime["second_capacity"]), runtime
    assert int(runtime["first_type_id"]) != 0, runtime
    assert int(runtime["wrong_type_id"]) != 0, runtime
    assert int(runtime["first_type_id"]) != int(runtime["wrong_type_id"]), runtime
    assert int(runtime["first_type_id"]) == int(runtime["second_type_id"]), runtime
    assert int(runtime["first_type_id"]) == int(positive[SPLIT_FUNCTION]["type_id"]), (
        runtime,
        positive,
    )
    for field in (
        "transfer_attempted",
        "transfer_applied",
        "transfer_rejected",
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
        "split": {
            "semantic_object_type": positive[SPLIT_FUNCTION].get("semantic_object_type"),
            "rewrite_status": positive[SPLIT_FUNCTION].get("rewrite_status"),
            "type_id": positive[SPLIT_FUNCTION].get("type_id"),
        },
        "split_inclusive": {
            "semantic_object_type": positive[SPLIT_INCLUSIVE_FUNCTION].get(
                "semantic_object_type"
            ),
            "rewrite_status": positive[SPLIT_INCLUSIVE_FUNCTION].get("rewrite_status"),
            "type_id": positive[SPLIT_INCLUSIVE_FUNCTION].get("type_id"),
        },
        "custom_raw_collect_status": custom.get("rewrite_status"),
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

    with tempfile.TemporaryDirectory(prefix="unialloc-str-split-hazard-") as raw:
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
            },
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
