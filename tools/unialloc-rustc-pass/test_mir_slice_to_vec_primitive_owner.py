#!/usr/bin/env python3
"""Prove canonical slice::to_vec scopes only callback-free primitive Vec owners."""

from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile


ROOT = Path(__file__).resolve().parents[2]
PASS_SOURCE = ROOT / "tools/unialloc-rustc-pass/unialloc-rustc-mir-rewrite-dry-run.rs"
TOOLCHAIN = "nightly-2026-06-11"
PROBE_NAME = "slice_to_vec_primitive_probe"
TYPE_ISOLATED = 1


def run(command: list[str], *, cwd: Path, env: dict[str, str], timeout: int = 300) -> str:
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
    spin_candidates = sorted((Path.home() / ".cargo/registry/src").glob("*/spin-0.9.0"))
    assert spin_candidates, "spin 0.9.0 must be present in the Cargo source cache"
    (app / "Cargo.toml").write_text(
        f'''[package]
name = "{PROBE_NAME.replace('_', '-')}"
version = "0.0.0"
edition = "2021"

[dependencies]
unialloc = {{ path = {json.dumps(str(ROOT / "unialloc"))}, features = ["stats", "type_isolation"] }}

[patch.crates-io]
spin = {{ path = {json.dumps(str(spin_candidates[-1]))} }}
''',
        encoding="utf-8",
    )
    (app / "src/main.rs").write_text(
        r'''use unialloc::{
    semantic_metadata_validation_snapshot, semantic_stats_recording_disable,
    semantic_stats_recording_enable, semantic_stats_reset, semantic_stats_snapshot,
    semantic_type_id, UniAlloc,
};

#[global_allocator]
static ALLOCATOR: UniAlloc = UniAlloc;

#[inline(never)]
fn clone_u8(source: &[u8]) -> Vec<u8> {
    source.to_vec()
}

#[inline(never)]
fn clone_u64(source: &[u64]) -> Vec<u64> {
    source.to_vec()
}

#[inline(never)]
fn clone_generic<T: Clone>(source: &[T]) -> Vec<T> {
    source.to_vec()
}

#[derive(Clone)]
struct DropClone(u64);

impl Drop for DropClone {
    fn drop(&mut self) {}
}

#[inline(never)]
fn clone_dropful(source: &[DropClone]) -> Vec<DropClone> {
    source.to_vec()
}

struct SliceLike<T>(Vec<T>);

impl<T: Clone> SliceLike<T> {
    #[inline(never)]
    fn to_vec(&self) -> Vec<T> {
        self.0.clone()
    }
}

#[inline(never)]
fn call_custom_to_vec(source: &SliceLike<u8>) -> Vec<u8> {
    source.to_vec()
}

#[inline(never)]
fn custom_factory(source: &[u8]) -> Vec<u8> {
    source.to_vec()
}

#[inline(never)]
fn call_custom_factory(source: &[u8]) -> Vec<u8> {
    custom_factory(source)
}

fn main() {
    if std::hint::black_box(false) {
        drop(clone_generic(&[31u8, 37u8]));
        drop(clone_dropful(&[DropClone(41), DropClone(43)]));
        drop(call_custom_to_vec(&SliceLike(vec![47u8, 53u8])));
        drop(call_custom_factory(&[59u8, 61u8]));
    }

    semantic_stats_reset();
    semantic_stats_recording_enable();
    let validation_before = semantic_metadata_validation_snapshot();

    let bytes = [17u8; 64];
    let words = [23u64; 8];
    let byte_vec = clone_u8(&bytes);
    let word_vec = clone_u64(&words);
    assert_eq!(byte_vec.as_slice(), bytes.as_slice());
    assert_eq!(word_vec.as_slice(), words.as_slice());
    drop(byte_vec);
    drop(word_vec);

    let stats = semantic_stats_snapshot();
    let validation_after = semantic_metadata_validation_snapshot();
    semantic_stats_recording_disable();
    println!(
        concat!(
            "{{\"source\":\"slice_to_vec_primitive_probe\",",
            "\"u8_type_id\":{},\"u64_type_id\":{},",
            "\"typed_allocations\":{},\"typed_deallocations\":{},",
            "\"fallback_allocations\":{},\"fallback_deallocations\":{},",
            "\"recovery_identity_mismatches\":{}}}"
        ),
        semantic_type_id::<Vec<u8>>(),
        semantic_type_id::<Vec<u64>>(),
        stats.typed_allocations,
        stats.typed_deallocations,
        stats.fallback_allocations,
        stats.fallback_deallocations,
        validation_after
            .recovery_identity_mismatches
            .saturating_sub(validation_before.recovery_identity_mismatches),
    );
}
''',
        encoding="utf-8",
    )
    return app


def function_matches(row: dict[str, object], suffix: str) -> bool:
    function = str(row.get("mir_function") or "")
    return function == suffix or function.endswith(f"::{suffix}")


def function_rows(audit: dict[str, object], suffix: str) -> list[dict[str, object]]:
    rows = audit.get("rewrite_candidates")
    assert isinstance(rows, list), rows
    return [
        row
        for row in rows
        if isinstance(row, dict) and function_matches(row, suffix)
    ]


def load_event(stdout: str) -> dict[str, object]:
    for line in stdout.splitlines():
        if line.startswith("{"):
            value = json.loads(line)
            if value.get("source") == PROBE_NAME:
                return value
    raise AssertionError(f"missing probe JSON:\n{stdout}")


def validate(audit: dict[str, object], event: dict[str, object]) -> None:
    positive_type_ids: list[int] = []
    for function_name, element_marker in (("clone_u8", "u8"), ("clone_u64", "u64")):
        candidates = [
            row
            for row in function_rows(audit, function_name)
            if "to_vec" in str(row.get("callee") or "")
        ]
        assert len(candidates) == 1, candidates
        row = candidates[0]
        assert row.get("rewrite_status") == "actual_semantic_scope_enter_exit_rewrite_applied", row
        assert row.get("lowering_kind") == "semantic_scope_enter_exit_rewrite", row
        assert row.get("type_id_basis") == "rustc_type_id_hash_runtime_equivalent", row
        assert int(row.get("type_id") or 0) != 0, row
        assert f"Vec<{element_marker}" in str(row.get("semantic_object_type") or ""), row
        assert row.get("metadata_pairing_contract") == "semantic_scope_active_metadata", row
        positive_type_ids.append(int(row["type_id"]))
    assert positive_type_ids[0] != positive_type_ids[1], positive_type_ids
    assert positive_type_ids == [int(event["u8_type_id"]), int(event["u64_type_id"])], event

    negative_expectations = {
        "clone_generic": "to_vec",
        "clone_dropful": "to_vec",
        "call_custom_to_vec": "to_vec",
        "call_custom_factory": "custom_factory",
    }
    for function_name, callee_marker in negative_expectations.items():
        candidates = [
            row
            for row in function_rows(audit, function_name)
            if callee_marker in str(row.get("callee") or "").lower()
        ]
        assert candidates, (function_name, function_rows(audit, function_name))
        for row in candidates:
            assert "applied" not in str(row.get("rewrite_status") or ""), row
            assert "planned" not in str(row.get("rewrite_status") or ""), row
            assert int(row.get("type_id") or 0) == 0, row

    assert int(event["typed_allocations"]) == 2, event
    assert int(event["typed_deallocations"]) == 2, event
    assert int(event["fallback_allocations"]) == 0, event
    assert int(event["fallback_deallocations"]) == 0, event
    assert int(event["recovery_identity_mismatches"]) == 0, event


def main() -> int:
    rustc = shutil.which("rustc") or "rustc"
    cargo = shutil.which("cargo") or "cargo"
    sysroot = subprocess.check_output(
        [rustc, f"+{TOOLCHAIN}", "--print", "sysroot"], text=True
    ).strip()
    with tempfile.TemporaryDirectory(prefix="unialloc-slice-to-vec-") as raw:
        workspace = Path(raw)
        app = write_probe(workspace)
        pass_binary = workspace / "unialloc-rustc-mir-rewrite-dry-run"
        env = os.environ.copy()
        env["RUSTC_BOOTSTRAP"] = "1"
        run(
            [
                rustc,
                f"+{TOOLCHAIN}",
                "--cfg",
                "unialloc_rustc_current",
                str(PASS_SOURCE),
                "-o",
                str(pass_binary),
            ],
            cwd=ROOT,
            env=env,
        )
        audits = workspace / "audits"
        audits.mkdir()
        env.update(
            {
                "RUSTC_WRAPPER": str(pass_binary),
                "UNIALLOC_RUSTC_TARGET_CRATES": PROBE_NAME,
                "UNIALLOC_REWRITE_AUDIT_DIR": str(audits),
                "UNIALLOC_CONTINUE_COMPILATION": "1",
                "UNIALLOC_ACTUAL_MIR_REWRITE": "1",
                "UNIALLOC_ACTUAL_SEMANTIC_SCOPE_REWRITE": "1",
                "UNIALLOC_LOWERING_POLICY_FLAGS": str(TYPE_ISOLATED),
                "UNIALLOC_RUSTC_SYSROOT": sysroot,
                "LD_LIBRARY_PATH": f"{sysroot}/lib",
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
            env=env,
        )
        audit_paths = sorted(audits.glob("*.json"))
        assert len(audit_paths) == 1, audit_paths
        validate(
            json.loads(audit_paths[0].read_text(encoding="utf-8")),
            load_event(stdout),
        )
    print(json.dumps({"source": "slice_to_vec_primitive_owner", "validated": True}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
