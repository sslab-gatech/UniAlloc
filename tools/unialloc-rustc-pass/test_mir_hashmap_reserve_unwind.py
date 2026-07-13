#!/usr/bin/env python3
"""Exercise fail-closed HashMap reserve and callback-free Vec unwind cleanup."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile


ROOT = Path(__file__).resolve().parents[2]
PASS_SOURCE = ROOT / "tools/unialloc-rustc-pass/unialloc-rustc-mir-rewrite-dry-run.rs"
PROBE_NAME = "hashmap_reserve_unwind_probe"
HELPER = "reserve_with_panicking_hash"
VEC_HELPER = "force_vec_capacity_overflow"
APPLIED_STATUS = "actual_semantic_scope_enter_exit_rewrite_applied"


def run(
    command: list[str],
    *,
    cwd: Path,
    env: dict[str, str],
    timeout: int,
) -> subprocess.CompletedProcess[str]:
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
    return completed


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
use std::hash::{Hash, Hasher};
use std::panic::{catch_unwind, AssertUnwindSafe};
use std::sync::atomic::{AtomicBool, Ordering};
use unialloc::{semantic_scope_depth_snapshot, UniAlloc};

#[global_allocator]
static ALLOCATOR: UniAlloc = UniAlloc;

static PANIC_IN_HASH: AtomicBool = AtomicBool::new(false);

#[derive(Eq, PartialEq)]
struct CallbackKey(u64);

impl Hash for CallbackKey {
    #[inline(never)]
    fn hash<H: Hasher>(&self, state: &mut H) {
        if PANIC_IN_HASH.load(Ordering::SeqCst) {
            panic!("hash callback panic");
        }
        self.0.hash(state);
    }
}

#[inline(never)]
fn reserve_with_panicking_hash(map: &mut HashMap<CallbackKey, u64>) {
    map.reserve(100);
}

#[inline(never)]
fn force_vec_capacity_overflow(values: &mut Vec<u8>) {
    values.reserve(usize::MAX);
}

fn main() {
    let mut map = HashMap::with_capacity(3);
    for value in 0..3 {
        map.insert(CallbackKey(value), value);
    }

    let before = semantic_scope_depth_snapshot();
    PANIC_IN_HASH.store(true, Ordering::SeqCst);
    let panicked = catch_unwind(AssertUnwindSafe(|| {
        reserve_with_panicking_hash(&mut map)
    }))
    .is_err();
    PANIC_IN_HASH.store(false, Ordering::SeqCst);
    let after_hashmap = semantic_scope_depth_snapshot();

    let mut values = Vec::new();
    let before_vec = semantic_scope_depth_snapshot();
    let vec_panicked = catch_unwind(AssertUnwindSafe(|| {
        force_vec_capacity_overflow(&mut values)
    }))
    .is_err();
    let after_vec = semantic_scope_depth_snapshot();

    println!(
        concat!(
            "{{",
            "\"source\":\"hashmap_reserve_unwind_probe\",",
            "\"panicked\":{},",
            "\"before_main\":{},",
            "\"before_overflow\":{},",
            "\"before_represented\":{},",
            "\"after_hashmap_main\":{},",
            "\"after_hashmap_overflow\":{},",
            "\"after_hashmap_represented\":{},",
            "\"vec_panicked\":{},",
            "\"before_vec_main\":{},",
            "\"before_vec_overflow\":{},",
            "\"before_vec_represented\":{},",
            "\"after_vec_main\":{},",
            "\"after_vec_overflow\":{},",
            "\"after_vec_represented\":{}",
            "}}"
        ),
        panicked,
        before.main_depth,
        before.overflow_depth,
        before.represented_depth,
        after_hashmap.main_depth,
        after_hashmap.overflow_depth,
        after_hashmap.represented_depth,
        vec_panicked,
        before_vec.main_depth,
        before_vec.overflow_depth,
        before_vec.represented_depth,
        after_vec.main_depth,
        after_vec.overflow_depth,
        after_vec.represented_depth,
    );
}
''',
        encoding="utf-8",
    )
    return app


def function_matches(row: dict[str, object], function: str) -> bool:
    mir_function = str(row.get("mir_function") or "")
    return mir_function == function or mir_function.endswith(f"::{function}")


def validate(audit: dict[str, object], stdout: str) -> dict[str, object]:
    compiler_pass = audit.get("compiler_pass")
    assert isinstance(compiler_pass, dict), compiler_pass
    summary = audit.get("summary")
    assert isinstance(summary, dict), summary
    assert summary.get("provider_override_installed") is True, summary
    assert summary.get("body_clone_returned_to_rustc") is True, summary
    assert summary.get("actual_semantic_scope_rewrite") is True, summary
    assert int(summary.get("semantic_scope_callback_capable_skipped_count") or 0) == 1, summary
    assert (
        int(
            compiler_pass.get("semantic_scope_callback_capable_skipped_count")
            or 0
        )
        == 1
    ), compiler_pass

    rows = audit.get("rewrite_candidates")
    assert isinstance(rows, list), rows
    reserve_rows = [
        row
        for row in rows
        if isinstance(row, dict)
        and function_matches(row, HELPER)
        and "std" in str(row.get("callee") or "")
        and "collections::hash::map" in str(row.get("callee") or "")
        and "::reserve" in str(row.get("callee") or "")
    ]
    assert len(reserve_rows) == 1, reserve_rows
    hashmap_reserve = reserve_rows[0]
    assert hashmap_reserve.get("rewrite_status") != APPLIED_STATUS, hashmap_reserve
    assert (
        hashmap_reserve.get("rewrite_status")
        == "semantic_scope_rewrite_skipped_callback_capable_receiver"
    ), hashmap_reserve
    assert (
        hashmap_reserve.get("lowering_kind")
        == "semantic_scope_callback_capable_receiver_skipped"
    ), hashmap_reserve
    assert hashmap_reserve.get("semantic_scope_unwind_pop_inserted") is False, hashmap_reserve
    assert (
        hashmap_reserve.get("metadata_pairing_contract")
        == "audit_only_callback_capable_receiver"
    ), hashmap_reserve

    local_helper_rows = [
        row
        for row in rows
        if isinstance(row, dict)
        and HELPER in str(row.get("callee") or "")
        and not function_matches(row, HELPER)
    ]
    assert not local_helper_rows, local_helper_rows

    vec_rows = [
        row
        for row in rows
        if isinstance(row, dict)
        and function_matches(row, VEC_HELPER)
        and "alloc" in str(row.get("callee") or "")
        and "vec" in str(row.get("callee") or "")
        and "::reserve" in str(row.get("callee") or "")
    ]
    assert len(vec_rows) == 1, vec_rows
    vec_reserve = vec_rows[0]
    assert vec_reserve.get("rewrite_status") == APPLIED_STATUS, vec_reserve
    assert vec_reserve.get("lowering_kind") == "semantic_scope_enter_exit_rewrite", vec_reserve
    assert vec_reserve.get("semantic_scope_unwind_pop_inserted") is True, vec_reserve
    assert vec_reserve.get("metadata_pairing_contract") == "semantic_scope_active_metadata", vec_reserve
    assert "Vec<u8" in str(vec_reserve.get("semantic_object_type") or ""), vec_reserve

    runtime = next(
        json.loads(line)
        for line in stdout.splitlines()
        if line.startswith("{") and PROBE_NAME in line
    )
    assert runtime.get("panicked") is True, runtime
    assert runtime.get("vec_panicked") is True, runtime
    for field in (
        "before_main",
        "before_overflow",
        "before_represented",
        "after_hashmap_main",
        "after_hashmap_overflow",
        "after_hashmap_represented",
        "before_vec_main",
        "before_vec_overflow",
        "before_vec_represented",
        "after_vec_main",
        "after_vec_overflow",
        "after_vec_represented",
    ):
        assert int(runtime[field]) == 0, (field, runtime)

    return {
        "exact_hashmap_reserve": {
            "callee": hashmap_reserve.get("callee"),
            "rewrite_status": hashmap_reserve.get("rewrite_status"),
            "semantic_scope_unwind_pop_inserted": hashmap_reserve.get(
                "semantic_scope_unwind_pop_inserted"
            ),
            "semantic_object_type": hashmap_reserve.get("semantic_object_type"),
        },
        "exact_vec_reserve": {
            "callee": vec_reserve.get("callee"),
            "rewrite_status": vec_reserve.get("rewrite_status"),
            "semantic_scope_unwind_pop_inserted": vec_reserve.get(
                "semantic_scope_unwind_pop_inserted"
            ),
            "semantic_object_type": vec_reserve.get("semantic_object_type"),
        },
        "local_helper_applied_rows": len(local_helper_rows),
        "runtime": runtime,
    }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", type=Path)
    parser.add_argument("--timeout", type=int, default=300)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    toolchain = (ROOT / "rust-toolchain").read_text(encoding="utf-8").strip()
    rustc = shutil.which("rustc") or "rustc"
    cargo = shutil.which("cargo") or "cargo"
    sysroot = subprocess.check_output(
        [rustc, f"+{toolchain}", "--print", "sysroot"], cwd=ROOT, text=True
    ).strip()

    temporary: tempfile.TemporaryDirectory[str] | None = None
    if args.output_dir:
        workspace = args.output_dir.resolve()
        workspace.mkdir(parents=True, exist_ok=True)
        if any(workspace.iterdir()):
            raise SystemExit(f"output directory must be empty: {workspace}")
    else:
        temporary = tempfile.TemporaryDirectory(prefix="unialloc-hashmap-reserve-unwind-")
        workspace = Path(temporary.name)

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
        timeout=args.timeout,
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
            "CARGO_PROFILE_DEV_PANIC": "unwind",
            "CARGO_TARGET_DIR": str(workspace / "target"),
        }
    )
    completed = run(
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
        timeout=args.timeout,
    )
    audit_paths = sorted(rewrites.glob("*.json"))
    assert len(audit_paths) == 1, audit_paths
    audit = json.loads(audit_paths[0].read_text(encoding="utf-8"))
    evidence = validate(audit, completed.stdout)
    result = {
        "source": PROBE_NAME,
        "toolchain": toolchain,
        "validated": True,
        "evidence": evidence,
        "boundaries": [
            "Functional actual-RUSTC_WRAPPER panic-unwind regression only; no performance claim.",
            "Exact std HashMap reserve-like calls stay audit-only because rehash may execute user Hash/Eq callbacks; a local helper whose name merely contains reserve is not selected either.",
            "The positive unwind scope is callback-free Vec::reserve(usize::MAX); its caught capacity-overflow panic proves the compiler-inserted pop restores the pre-call depth in this bounded process.",
        ],
    }
    (workspace / "summary.json").write_text(
        json.dumps(result, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    print(json.dumps(result, sort_keys=True))
    if temporary is not None:
        temporary.cleanup()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
