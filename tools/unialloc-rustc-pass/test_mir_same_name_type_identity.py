#!/usr/bin/env python3
"""Regress exact TypeId isolation for distinct crates with identical type text."""

from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile


ROOT = Path(__file__).resolve().parents[2]
PASS_SOURCE = ROOT / "tools/unialloc-rustc-pass/unialloc-rustc-mir-rewrite-dry-run.rs"
TARGET_CRATE = "same_name_type_identity_probe"


def run(command: list[str], *, cwd: Path, env: dict[str, str]) -> subprocess.CompletedProcess[str]:
    completed = subprocess.run(
        command,
        cwd=cwd,
        env=env,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=600,
        check=False,
    )
    if completed.returncode != 0:
        raise AssertionError(
            f"command failed ({completed.returncode}): {' '.join(command)}\n"
            f"stdout:\n{completed.stdout}\nstderr:\n{completed.stderr}"
        )
    return completed


def current_rustc_cfg(toolchain: str) -> list[str]:
    if toolchain == "nightly" or toolchain.startswith(("nightly-2025", "nightly-2026")):
        return ["--cfg", "unialloc_rustc_current"]
    return []


def write_workspace(workspace: Path) -> Path:
    app = workspace / "app"
    alpha = workspace / "owner-a"
    beta = workspace / "owner-b"
    for crate in (app, alpha, beta):
        (crate / "src").mkdir(parents=True)

    spin_candidates = sorted((Path.home() / ".cargo/registry/src").glob("*/spin-0.9.0"))
    assert spin_candidates, "spin 0.9.0 must be present in the Cargo source cache"
    (workspace / "Cargo.toml").write_text(
        f'''[workspace]
members = ["app", "owner-a", "owner-b"]
resolver = "2"

[patch.crates-io]
spin = {{ path = {json.dumps(str(spin_candidates[-1]))} }}
''',
        encoding="utf-8",
    )
    dependency_manifest = '''[package]
name = "__PACKAGE__"
version = "0.0.1"
edition = "2021"

[lib]
name = "shared_owner"
'''
    (alpha / "Cargo.toml").write_text(
        dependency_manifest.replace("__PACKAGE__", "owner-a-package"), encoding="utf-8"
    )
    (beta / "Cargo.toml").write_text(
        dependency_manifest.replace("__PACKAGE__", "owner-b-package"), encoding="utf-8"
    )
    payload = '''#[repr(transparent)]
#[derive(Clone, Copy, Debug)]
pub struct Payload(pub u64);
'''
    (alpha / "src/lib.rs").write_text(payload, encoding="utf-8")
    (beta / "src/lib.rs").write_text(payload, encoding="utf-8")

    (app / "Cargo.toml").write_text(
        f'''[package]
name = "same-name-type-identity-probe"
version = "0.0.1"
edition = "2021"

[dependencies]
alpha = {{ package = "owner-a-package", path = "../owner-a" }}
beta = {{ package = "owner-b-package", path = "../owner-b" }}
unialloc = {{ path = {json.dumps(str(ROOT / "unialloc"))}, features = ["stats", "type_isolation"] }}
''',
        encoding="utf-8",
    )
    (app / "src/main.rs").write_text(
        r'''use std::alloc::{alloc, dealloc, Layout};
use unialloc::{semantic_type_id, UniAlloc};

#[global_allocator]
static ALLOCATOR: UniAlloc = UniAlloc;

#[inline(never)]
fn allocate_alpha() -> Vec<alpha::Payload> {
    Vec::with_capacity(64)
}

#[inline(never)]
fn allocate_beta() -> Vec<beta::Payload> {
    Vec::with_capacity(64)
}

#[inline(never)]
fn allocate_borrow<'a>(value: &'a u8) -> Box<&'a u8> {
    Box::new(value)
}

#[inline(never)]
fn transfer_string(value: String) -> Vec<u8> {
    value.into_bytes()
}

#[inline(never)]
unsafe fn raw_layout_cycle() {
    let layout = Layout::new::<u64>();
    let pointer = alloc(layout);
    assert!(!pointer.is_null());
    pointer.cast::<u64>().write(0xfeed_face_cafe_beef);
    assert_eq!(pointer.cast::<u64>().read(), 0xfeed_face_cafe_beef);
    dealloc(pointer, layout);
}

struct BothOwners(Vec<alpha::Payload>, Vec<beta::Payload>);

#[inline(never)]
fn consume_both(value: BothOwners) {
    std::hint::black_box(value);
}

fn main() {
    let borrowed_value = 7u8;
    let borrowed = allocate_borrow(&borrowed_value);
    assert_eq!(**borrowed, 7);
    drop(borrowed);
    let transferred = transfer_string(String::from("stable-type-id"));
    assert_eq!(transferred, b"stable-type-id");
    unsafe { raw_layout_cycle() };

    let alpha = allocate_alpha();
    let alpha_ptr = alpha.as_ptr() as usize;
    drop(alpha);

    let beta = allocate_beta();
    let beta_ptr = beta.as_ptr() as usize;
    drop(beta);

    let alpha_again = allocate_alpha();
    let alpha_again_ptr = alpha_again.as_ptr() as usize;
    let alpha_type_id = semantic_type_id::<Vec<alpha::Payload>>();
    let beta_type_id = semantic_type_id::<Vec<beta::Payload>>();
    let string_type_id = semantic_type_id::<String>();
    let byte_vec_type_id = semantic_type_id::<Vec<u8>>();
    let display_equal = std::any::type_name::<Vec<alpha::Payload>>()
        == std::any::type_name::<Vec<beta::Payload>>();

    assert!(display_equal);
    assert_ne!(alpha_type_id, beta_type_id);
    assert_ne!(alpha_ptr, beta_ptr, "beta must not consume alpha's typed cache entry");
    assert_eq!(
        alpha_ptr, alpha_again_ptr,
        "the original exact type must recover its own cache entry"
    );
    consume_both(BothOwners(
        Vec::<alpha::Payload>::with_capacity(2),
        Vec::<beta::Payload>::with_capacity(2),
    ));
    println!(
        "{{\"alpha_type_id\":{},\"beta_type_id\":{},\"string_type_id\":{},\"byte_vec_type_id\":{},\"display_equal\":{},\"wrong_type_non_reuse\":true,\"same_type_reuse\":true}}",
        alpha_type_id, beta_type_id, string_type_id, byte_vec_type_id, display_equal
    );
}
''',
        encoding="utf-8",
    )
    return app


def validate(audit: dict[str, object], runtime: dict[str, object]) -> None:
    records = [row for row in audit.get("rewrite_candidates", []) if isinstance(row, dict)]
    allocations = {
        str(row.get("mir_function")): row
        for row in records
        if row.get("rewrite_status") == "actual_semantic_scope_enter_exit_rewrite_applied"
        and str(row.get("mir_function")) in {"allocate_alpha", "allocate_beta"}
        and "Vec<shared_owner::Payload" in str(row.get("semantic_object_type"))
    }
    assert set(allocations) == {"allocate_alpha", "allocate_beta"}, allocations
    alpha_id = int(allocations["allocate_alpha"].get("type_id") or 0)
    beta_id = int(allocations["allocate_beta"].get("type_id") or 0)
    assert alpha_id != 0 and beta_id != 0 and alpha_id != beta_id, allocations
    assert {
        str(allocations[name].get("type_id_basis")) for name in allocations
    } == {"rustc_type_id_hash_runtime_equivalent"}, allocations
    assert (
        allocations["allocate_alpha"].get("semantic_object_type")
        == allocations["allocate_beta"].get("semantic_object_type")
    ), allocations

    runtime_ids = {int(runtime["alpha_type_id"]), int(runtime["beta_type_id"])}
    assert {alpha_id, beta_id} == runtime_ids, (allocations, runtime)
    drops = {
        int(row.get("type_id") or 0)
        for row in records
        if row.get("rewrite_status") == "actual_semantic_scope_drop_rewrite_applied"
        and "Vec<shared_owner::Payload" in str(row.get("semantic_object_type"))
    }
    assert runtime_ids <= drops, (drops, runtime_ids)
    borrowed_scopes = [
        row
        for row in records
        if row.get("mir_function") == "allocate_borrow"
        and row.get("rewrite_status")
        == "actual_semantic_scope_generic_type_rewrite_applied"
    ]
    assert len(borrowed_scopes) == 1, borrowed_scopes
    assert int(borrowed_scopes[0].get("type_id") or 0) == 0, borrowed_scopes
    assert borrowed_scopes[0].get("type_id_basis") == "monomorphized_compiler_type_id_runtime"
    transfers = [
        row
        for row in records
        if row.get("mir_function") == "transfer_string"
        and row.get("rewrite_status")
        == "actual_semantic_ownership_transfer_rewrite_applied"
    ]
    assert len(transfers) == 1, transfers
    assert int(transfers[0].get("type_id") or 0) == int(runtime["byte_vec_type_id"])
    assert f"old_type_id={int(runtime['string_type_id'])}" in str(
        transfers[0].get("replacement_preview")
    )
    raw_direct_rows = [
        row
        for row in records
        if row.get("mir_function") == "raw_layout_cycle"
        and row.get("lowering_kind") == "direct_allocator_call_rewrite"
        and row.get("rewrite_status") == "actual_allocator_call_replacement_applied"
    ]
    assert len(raw_direct_rows) >= 2, raw_direct_rows
    assert {int(row.get("type_id") or 0) for row in raw_direct_rows} == {0}
    assert {
        str(row.get("type_id_basis")) for row in raw_direct_rows
    } == {"direct_allocator_recovery_delegated"}
    multi_owner_drops = [
        row
        for row in records
        if row.get("mir_function") == "consume_both"
        and row.get("rewrite_status")
        == "semantic_scope_drop_rewrite_skipped_multiple_heap_owners"
    ]
    assert len(multi_owner_drops) == 1, multi_owner_drops
    assert str(multi_owner_drops[0].get("semantic_object_type")).count(
        "Vec<shared_owner::Payload"
    ) == 2
    assert runtime.get("display_equal") is True, runtime
    assert runtime.get("wrong_type_non_reuse") is True, runtime
    assert runtime.get("same_type_reuse") is True, runtime


def main() -> int:
    toolchain = (ROOT / "rust-toolchain").read_text(encoding="utf-8").strip()
    rustc = shutil.which("rustc") or "rustc"
    cargo = shutil.which("cargo") or "cargo"
    sysroot = subprocess.check_output(
        [rustc, f"+{toolchain}", "--print", "sysroot"], cwd=ROOT, text=True
    ).strip()

    with tempfile.TemporaryDirectory(prefix="unialloc-same-name-type-id-") as raw:
        workspace = Path(raw)
        app = write_workspace(workspace)
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
        rewrites.mkdir()
        run_env = os.environ.copy()
        run_env.update(
            {
                "RUSTC_WRAPPER": str(pass_binary),
                "UNIALLOC_RUSTC_TARGET_CRATES": TARGET_CRATE,
                "UNIALLOC_REWRITE_AUDIT_DIR": str(rewrites),
                "UNIALLOC_CONTINUE_COMPILATION": "1",
                "UNIALLOC_ACTUAL_MIR_REWRITE": "1",
                "UNIALLOC_ACTUAL_SEMANTIC_SCOPE_REWRITE": "1",
                "UNIALLOC_LOWERING_POLICY_FLAGS": "1",
                "UNIALLOC_RUSTC_SYSROOT": sysroot,
                "CARGO_NET_OFFLINE": "true",
                "CARGO_INCREMENTAL": "0",
                "CARGO_TARGET_DIR": str(workspace / "target"),
                "GLIBC_TUNABLES": "glibc.pthread.rseq=0",
            }
        )
        for variable in ("LD_LIBRARY_PATH", "DYLD_LIBRARY_PATH"):
            current = run_env.get(variable)
            run_env[variable] = f"{sysroot}/lib" + (
                os.pathsep + current if current else ""
            )
        completed = run(
            [cargo, f"+{toolchain}", "run", "--quiet", "--manifest-path", str(app / "Cargo.toml")],
            cwd=workspace,
            env=run_env,
        )
        runtime_lines = [line for line in completed.stdout.splitlines() if line.startswith("{")]
        assert len(runtime_lines) == 1, completed.stdout
        runtime = json.loads(runtime_lines[0])
        audits = sorted(rewrites.glob(f"{TARGET_CRATE}-*.json"))
        assert len(audits) == 1, audits
        audit = json.loads(audits[0].read_text(encoding="utf-8"))
        validate(audit, runtime)

    print(json.dumps({"source": "same_name_type_identity", "validated": True}, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
