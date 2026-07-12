#!/usr/bin/env python3
"""Prove actual MIR metadata keeps equal type ids isolated by crate module id."""

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
SHARED_CRATE_NAME = "shared_owner"
APPLIED_SCOPE_STATUS = "actual_semantic_scope_enter_exit_rewrite_applied"
TYPE_ISOLATED = 0x1
PAYLOAD_BYTES = 64


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
    app = workspace / "application"
    alpha = workspace / "module-alpha"
    beta = workspace / "module-beta"
    for crate in (app, alpha, beta):
        (crate / "src").mkdir(parents=True)

    (workspace / "Cargo.toml").write_text(
        '''[workspace]
members = ["application", "module-alpha", "module-beta"]
resolver = "2"
''',
        encoding="utf-8",
    )

    unialloc_path = json.dumps(str(ROOT / "unialloc"))
    (app / "Cargo.toml").write_text(
        f'''[package]
name = "multicrate-module-isolation-probe"
version = "0.1.0"
edition = "2021"

[dependencies]
alpha = {{ package = "module-alpha-fixture", path = "../module-alpha" }}
beta = {{ package = "module-beta-fixture", path = "../module-beta" }}
unialloc = {{ path = {unialloc_path}, features = ["stats", "type_isolation"] }}
''',
        encoding="utf-8",
    )

    dependency_manifest = f'''[package]
name = "__PACKAGE__"
version = "0.1.0"
edition = "2021"

[lib]
name = "{SHARED_CRATE_NAME}"
path = "src/lib.rs"

[dependencies]
unialloc = {{ path = {unialloc_path}, features = ["stats", "type_isolation"] }}
'''
    (alpha / "Cargo.toml").write_text(
        dependency_manifest.replace("__PACKAGE__", "module-alpha-fixture"),
        encoding="utf-8",
    )
    (beta / "Cargo.toml").write_text(
        dependency_manifest.replace("__PACKAGE__", "module-beta-fixture"),
        encoding="utf-8",
    )

    dependency_source = f'''use std::hint::black_box;

#[repr(C)]
struct Payload([u64; 8]);

#[inline(never)]
pub fn allocation_cycle(seed: u64) -> usize {{
    assert_eq!(std::mem::size_of::<Payload>(), {PAYLOAD_BYTES});
    // Keep the UniAlloc dependency in this compilation unit so the MIR provider
    // can resolve the semantic-scope ABI that it inserts around Box::new/Drop.
    black_box(unialloc::semantic_stats_snapshot().total_allocations);
    let value = Box::new(Payload([
        seed,
        seed ^ 0x1111_1111_1111_1111,
        seed ^ 0x2222_2222_2222_2222,
        seed ^ 0x3333_3333_3333_3333,
        seed ^ 0x4444_4444_4444_4444,
        seed ^ 0x5555_5555_5555_5555,
        seed ^ 0x6666_6666_6666_6666,
        seed ^ 0x7777_7777_7777_7777,
    ]));
    black_box(value.0[0]);
    let address = (&*value as *const Payload) as usize;
    drop(value);
    address
}}
'''
    (alpha / "src/lib.rs").write_text(dependency_source, encoding="utf-8")
    (beta / "src/lib.rs").write_text(dependency_source, encoding="utf-8")

    (app / "src/main.rs").write_text(
        r'''use unialloc::{
    semantic_auto_metadata_disable, semantic_metadata_validation_snapshot,
    semantic_stats_recording_disable, semantic_stats_reset,
    type_isolation_side_cache_snapshot, UniAlloc,
};

#[global_allocator]
static ALLOCATOR: UniAlloc = UniAlloc;

fn main() {
    semantic_auto_metadata_disable();
    semantic_stats_reset();

    let alpha_seed = alpha::allocation_cycle(0xA11C_0001);
    let beta_probe = beta::allocation_cycle(0xB37A_0001);
    let alpha_recovery = alpha::allocation_cycle(0xA11C_0002);

    assert_ne!(
        beta_probe, alpha_seed,
        "equal compiler type ids from distinct crates must not share cached storage"
    );
    assert_eq!(
        alpha_recovery, alpha_seed,
        "the original crate module id must recover its own cached storage"
    );

    let validation = semantic_metadata_validation_snapshot();
    let side_cache = type_isolation_side_cache_snapshot();
    assert_eq!(validation.recovery_identity_mismatches, 0, "{validation:?}");
    assert_eq!(side_cache.corrupt_slots, 0, "{side_cache:?}");
    semantic_stats_recording_disable();

    println!(
        "{{\"source\":\"multicrate_module_isolation_probe\",\"alpha_seed\":{},\"beta_probe\":{},\"alpha_recovery\":{},\"wrong_module_non_reuse\":true,\"same_module_reuse\":true,\"recovery_identity_mismatches\":{},\"side_cache_corrupt_slots\":{}}}",
        alpha_seed,
        beta_probe,
        alpha_recovery,
        validation.recovery_identity_mismatches,
        side_cache.corrupt_slots,
    );
}
''',
        encoding="utf-8",
    )
    return app


def applied_payload_row(audit: dict[str, object]) -> dict[str, object]:
    records = audit.get("rewrite_candidates") or []
    rows = [
        row
        for row in records
        if isinstance(row, dict)
        and row.get("rewrite_status") == APPLIED_SCOPE_STATUS
        and "Box<Payload" in str(row.get("semantic_object_type") or "")
    ]
    assert rows, (
        "audit has no applied Box<Payload> semantic scope",
        [
            (
                row.get("rewrite_status"),
                row.get("semantic_object_type"),
                row.get("callee"),
            )
            for row in records
            if isinstance(row, dict)
        ],
        audit.get("summary"),
        audit.get("rustc_args"),
    )
    allocation_rows = [row for row in rows if "Box" in str(row.get("callee") or "")]
    selected = allocation_rows or rows
    type_ids = {int(row.get("type_id") or 0) for row in selected}
    module_ids = {int(row.get("module_id") or 0) for row in selected}
    object_types = {str(row.get("semantic_object_type") or "") for row in selected}
    assert len(type_ids) == len(module_ids) == len(object_types) == 1, selected
    row = dict(selected[0])
    row["type_id"] = next(iter(type_ids))
    row["module_id"] = next(iter(module_ids))
    row["semantic_object_type"] = next(iter(object_types))
    return row


def validate(audits: list[dict[str, object]], stdout: str) -> dict[str, object]:
    assert len(audits) == 2, f"expected two {SHARED_CRATE_NAME} audits, got {len(audits)}"
    rows = [applied_payload_row(audit) for audit in audits]
    type_ids = {int(row["type_id"]) for row in rows}
    module_ids = {int(row["module_id"]) for row in rows}
    object_types = {str(row["semantic_object_type"]) for row in rows}
    assert len(object_types) == 1, rows
    assert len(type_ids) == 1 and next(iter(type_ids)) != 0, rows
    assert len(module_ids) == 2 and 0 not in module_ids, (
        "distinct rustc crate disambiguators must produce distinct nonzero module ids",
        rows,
    )

    runtime_lines = [line for line in stdout.splitlines() if line.startswith("{")]
    assert len(runtime_lines) == 1, stdout
    runtime = json.loads(runtime_lines[0])
    assert runtime.get("wrong_module_non_reuse") is True, runtime
    assert runtime.get("same_module_reuse") is True, runtime
    assert int(runtime["alpha_seed"]) != int(runtime["beta_probe"]), runtime
    assert int(runtime["alpha_seed"]) == int(runtime["alpha_recovery"]), runtime
    assert int(runtime["recovery_identity_mismatches"]) == 0, runtime
    assert int(runtime["side_cache_corrupt_slots"]) == 0, runtime
    return {
        "type_id": next(iter(type_ids)),
        "module_ids": sorted(module_ids),
        "semantic_object_type": next(iter(object_types)),
        "runtime": runtime,
    }


def validate_primary_input_fallback(
    pass_binary: Path, workspace: Path, sysroot: str, env: dict[str, str]
) -> dict[str, object]:
    module_ids: list[int] = []
    for label in ("direct-alpha", "direct-beta"):
        source_dir = workspace / label
        source_dir.mkdir()
        source = source_dir / "lib.rs"
        source.write_text("pub fn marker() {}\n", encoding="utf-8")
        audit_path = source_dir / "audit.json"
        command = [
            str(pass_binary),
            "--unialloc-rewrite-audit-out",
            str(audit_path),
            "--",
            "--crate-name",
            SHARED_CRATE_NAME,
            "--crate-type",
            "lib",
            "--edition=2021",
            str(source),
            "--sysroot",
            sysroot,
        ]
        result = run(command, cwd=workspace, env=env)
        require_success(result, command)
        audit = json.loads(audit_path.read_text(encoding="utf-8"))
        module_id = int((audit.get("compiler_pass") or {}).get("module_id") or 0)
        assert module_id != 0, audit
        module_ids.append(module_id)

    assert len(set(module_ids)) == 2, (
        "same-name direct rustc invocations without -C metadata must be isolated "
        "by their distinct primary input identities",
        module_ids,
    )
    return {"module_ids": module_ids, "distinct": True}


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--pass-source", type=Path, default=DEFAULT_PASS_SOURCE)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    pass_source = args.pass_source.resolve()
    assert pass_source.is_file(), pass_source
    toolchain = (ROOT / "rust-toolchain").read_text(encoding="utf-8").strip()
    rustc = shutil.which("rustc") or "rustc"
    cargo = shutil.which("cargo") or "cargo"
    sysroot = subprocess.check_output(
        [rustc, f"+{toolchain}", "--print", "sysroot"], cwd=ROOT, text=True
    ).strip()

    with tempfile.TemporaryDirectory(prefix="unialloc-multicrate-module-isolation-") as raw:
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
                "UNIALLOC_RUSTC_TARGET_CRATES": SHARED_CRATE_NAME,
                "UNIALLOC_REWRITE_AUDIT_DIR": str(rewrites),
                "UNIALLOC_PASS_LOG_DIR": str(logs),
                "UNIALLOC_CONTINUE_COMPILATION": "1",
                "UNIALLOC_ACTUAL_MIR_REWRITE": "1",
                "UNIALLOC_ACTUAL_SEMANTIC_SCOPE_REWRITE": "1",
                "UNIALLOC_LOWERING_POLICY_FLAGS": str(TYPE_ISOLATED),
                "UNIALLOC_RUSTC_SYSROOT": sysroot,
                "CARGO_NET_OFFLINE": "true",
                "CARGO_INCREMENTAL": "0",
                "CARGO_TARGET_DIR": str(workspace / "target"),
            }
        )
        primary_input_fallback = validate_primary_input_fallback(
            pass_binary, workspace, sysroot, run_env
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

        audit_paths = sorted(rewrites.glob(f"{SHARED_CRATE_NAME}-*.json"))
        audits = [json.loads(path.read_text(encoding="utf-8")) for path in audit_paths]
        evidence = validate(audits, runtime.stdout)

    print(
        json.dumps(
            {
                "source": "mir_multicrate_module_isolation_probe",
                "validated": True,
                "single_run": True,
                "benchmark": False,
                "pass_source": str(pass_source),
                "boundaries": [
                    "Functional compiler-pass and allocator module-isolation regression only; no benchmark or performance claim.",
                    "Two packages deliberately use the same rustc crate name and identical source so their compiler type ids match; only the crate disambiguator-derived module id may separate reuse.",
                ],
                "evidence": {
                    **evidence,
                    "primary_input_fallback": primary_input_fallback,
                },
            },
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
