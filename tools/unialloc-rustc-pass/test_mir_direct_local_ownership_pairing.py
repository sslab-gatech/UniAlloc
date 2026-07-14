#!/usr/bin/env python3
"""Prove direct-local semantic scopes fail closed for an escaping owner."""

from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile


ROOT = Path(__file__).resolve().parents[2]
PASS_SOURCE = ROOT / "tools/unialloc-rustc-pass/unialloc-rustc-mir-rewrite-dry-run.rs"
PROBE_NAME = "direct_local_ownership_probe"
CROSS_THREAD_RECOVERY = 1 << 15
DEFAULT_RECOVERY_BASIS = "default_recovery_backed_semantic_scope"
EXACT_LOCAL_BASIS = "exact_local_no_recovery"


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


def current_rustc_cfg(toolchain: str) -> list[str]:
    normalized = toolchain.lstrip("+").strip()
    if normalized == "nightly" or normalized.startswith(("nightly-2025", "nightly-2026")):
        return ["--cfg", "unialloc_rustc_current"]
    return []


def write_probe(workspace: Path) -> None:
    consumer = workspace / "escape-consumer"
    app = workspace / PROBE_NAME
    (consumer / "src").mkdir(parents=True)
    (app / "src").mkdir(parents=True)
    (consumer / "Cargo.toml").write_text(
        """[package]
name = "escape-consumer"
version = "0.1.0"
edition = "2021"
""",
        encoding="utf-8",
    )
    (consumer / "src/lib.rs").write_text(
        """#[inline(never)]
pub fn consume_box(value: Box<[u64; 16]>) -> u64 {
    value.iter().fold(0_u64, |sum, item| sum.wrapping_add(*item))
}

#[inline(never)]
pub fn consume_and_replace(value: &mut Box<[u64; 16]>) -> u64 {
    let old = core::mem::replace(value, Box::new([53_u64; 16]));
    old.iter().fold(0_u64, |sum, item| sum.wrapping_add(*item))
}
""",
        encoding="utf-8",
    )
    (app / "Cargo.toml").write_text(
        f"""[package]
name = "{PROBE_NAME.replace('_', '-')}"
version = "0.1.0"
edition = "2021"

[dependencies]
escape-consumer = {{ path = "../escape-consumer" }}
unialloc = {{ path = {json.dumps(str(ROOT / 'unialloc'))}, features = ["stats", "type_isolation"] }}
""",
        encoding="utf-8",
    )
    (app / "src/main.rs").write_text(
        r"""use unialloc::{
    semantic_auto_metadata_disable, semantic_fallback_attribution_snapshot,
    semantic_stats_reset, semantic_stats_snapshot, UniAlloc,
};

#[global_allocator]
static ALLOCATOR: UniAlloc = UniAlloc;

#[inline(never)]
fn mixed_escape_and_local_drop() -> u64 {
    let escaping = Box::new([11_u64; 16]);
    let local = Box::new([29_u64; 16]);
    let value = escape_consumer::consume_box(escaping);
    drop(local);
    value
}

#[inline(never)]
fn conditional_escape_or_drop(escape: bool) -> u64 {
    let owner = Box::new([37_u64; 16]);
    if escape {
        return escape_consumer::consume_box(owner);
    }
    owner[0]
}

#[inline(never)]
fn exact_local_drop() -> u64 {
    let _local = Box::new([41_u64; 16]);
    41
}

#[inline(never)]
fn hidden_mut_alias_escape() -> u64 {
    let mut owner = Box::new([47_u64; 16]);
    let alias = &mut owner;
    let consumed = escape_consumer::consume_and_replace(alias);
    consumed ^ owner[0]
}

fn main() {
    semantic_auto_metadata_disable();
    semantic_stats_reset();

    let hidden_checksum = hidden_mut_alias_escape();
    let hidden_stats = semantic_stats_snapshot();
    let hidden_fallback = semantic_fallback_attribution_snapshot();
    println!(
        "{{\"source\":\"hidden_mut_alias_escape\",\"checksum\":{},\"typed_allocations\":{},\"typed_deallocations\":{},\"fallback_allocations\":{},\"fallback_deallocations\":{},\"raw_dealloc_no_metadata\":{}}}",
        hidden_checksum,
        hidden_stats.typed_allocations,
        hidden_stats.typed_deallocations,
        hidden_stats.fallback_allocations,
        hidden_stats.fallback_deallocations,
        hidden_fallback.raw_dealloc_no_metadata,
    );

    semantic_stats_reset();
    let checksum =
        mixed_escape_and_local_drop() ^ conditional_escape_or_drop(true) ^ exact_local_drop();
    let stats = semantic_stats_snapshot();
    let fallback = semantic_fallback_attribution_snapshot();
    println!(
        "{{\"source\":\"direct_local_ownership_probe\",\"checksum\":{},\"typed_allocations\":{},\"typed_deallocations\":{},\"fallback_allocations\":{},\"fallback_deallocations\":{},\"raw_dealloc_no_metadata\":{}}}",
        checksum,
        stats.typed_allocations,
        stats.typed_deallocations,
        stats.fallback_allocations,
        stats.fallback_deallocations,
        fallback.raw_dealloc_no_metadata,
    );
}
""",
        encoding="utf-8",
    )


def applied_rows(
    audit: dict[str, object], function: str, lowering_kind: str
) -> list[dict[str, object]]:
    return [
        row
        for row in audit.get("rewrite_candidates", [])  # type: ignore[union-attr]
        if isinstance(row, dict)
        and function in str(row.get("mir_function") or "")
        and row.get("lowering_kind") == lowering_kind
        and row.get("rewrite_status")
        in {
            "actual_semantic_scope_enter_exit_rewrite_applied",
            "actual_semantic_scope_drop_rewrite_applied",
        }
    ]


def local_symbol(row: dict[str, object]) -> bool:
    return str(row.get("replacement_symbol") or "").endswith("_local")


def assert_default_recovery_scope(
    rows: list[dict[str, object]], label: str
) -> None:
    assert rows, label
    for row in rows:
        assert row.get("replacement_symbol") == "__unialloc_semantic_scope_push", (
            label,
            row,
        )
        assert int(row.get("placement_hint") or 0) == CROSS_THREAD_RECOVERY, (
            label,
            row,
        )
        assert row.get("cross_thread_recovery_hint") is True, (label, row)
        assert row.get("placement_hint_basis") == DEFAULT_RECOVERY_BASIS, (
            label,
            row,
        )


def assert_exact_local_scope(rows: list[dict[str, object]], label: str) -> None:
    assert rows, label
    for row in rows:
        assert row.get("replacement_symbol") == "__unialloc_semantic_scope_push_local", (
            label,
            row,
        )
        assert int(row.get("placement_hint") or 0) == 0, (label, row)
        assert row.get("cross_thread_recovery_hint") is False, (label, row)
        assert row.get("placement_hint_basis") == EXACT_LOCAL_BASIS, (label, row)


def validate(audit: dict[str, object], stdout: str) -> None:
    summary = audit.get("summary")
    assert isinstance(summary, dict), summary
    assert (
        summary.get("semantic_scope_replacement_resolution_status")
        == "resolved_unialloc_semantic_scope_push_mixed_local_recovery_pop"
    ), summary
    mixed_allocations = [
        row
        for row in applied_rows(
            audit,
            "mixed_escape_and_local_drop",
            "semantic_scope_enter_exit_rewrite",
        )
        if "::new" in str(row.get("callee") or "")
    ]
    mixed_drops = applied_rows(
        audit, "mixed_escape_and_local_drop", "semantic_scope_drop_rewrite"
    )
    positive_allocations = [
        row
        for row in applied_rows(
            audit, "exact_local_drop", "semantic_scope_enter_exit_rewrite"
        )
        if "::new" in str(row.get("callee") or "")
    ]
    positive_drops = applied_rows(audit, "exact_local_drop", "semantic_scope_drop_rewrite")
    conditional_allocations = [
        row
        for row in applied_rows(
            audit,
            "conditional_escape_or_drop",
            "semantic_scope_enter_exit_rewrite",
        )
        if "::new" in str(row.get("callee") or "")
    ]
    conditional_drops = applied_rows(
        audit, "conditional_escape_or_drop", "semantic_scope_drop_rewrite"
    )
    alias_allocations = [
        row
        for row in applied_rows(
            audit,
            "hidden_mut_alias_escape",
            "semantic_scope_enter_exit_rewrite",
        )
        if "::new" in str(row.get("callee") or "")
    ]
    alias_drops = applied_rows(
        audit, "hidden_mut_alias_escape", "semantic_scope_drop_rewrite"
    )

    assert len(conditional_allocations) == 1, conditional_allocations
    assert conditional_drops, conditional_drops
    assert all(not local_symbol(row) for row in conditional_allocations + conditional_drops), (
        "a matching Drop on only one CFG branch is not an ownership proof",
        conditional_allocations,
        conditional_drops,
    )
    assert len(mixed_allocations) == 2, mixed_allocations
    assert mixed_drops, "mixed function must expose its normal/cleanup Drop evidence"
    assert all(not local_symbol(row) for row in mixed_allocations + mixed_drops), (
        "same-type mixed escape/local ownership must fail closed as one recovery-backed group",
        mixed_allocations,
        mixed_drops,
    )
    assert len(alias_allocations) == 1, alias_allocations
    assert alias_drops, alias_drops
    assert all(not local_symbol(row) for row in alias_allocations + alias_drops), (
        "a hidden &mut alias passed cross-crate must make the whole owner group recovery-backed",
        alias_allocations,
        alias_drops,
    )
    assert len(positive_allocations) == 1, positive_allocations
    assert positive_drops, positive_drops
    assert all(local_symbol(row) for row in positive_allocations + positive_drops), (
        "an exact local allocation/Drop pair must preserve the local ABI",
        positive_allocations,
        positive_drops,
    )
    assert_default_recovery_scope(
        conditional_allocations + conditional_drops,
        "conditional escape allocation and matching Drop",
    )
    assert_default_recovery_scope(
        mixed_allocations + mixed_drops,
        "mixed escape allocation and matching Drops",
    )
    assert_default_recovery_scope(
        alias_allocations + alias_drops,
        "aliased escape allocation and matching Drop",
    )
    assert_exact_local_scope(
        positive_allocations + positive_drops,
        "exact local allocation and matching Drop",
    )

    hidden_runtime = next(
        json.loads(line)
        for line in stdout.splitlines()
        if line.startswith("{") and "hidden_mut_alias_escape" in line
    )
    assert hidden_runtime["checksum"] != 0
    assert hidden_runtime["typed_allocations"] == 1, hidden_runtime
    assert hidden_runtime["typed_deallocations"] == 1, hidden_runtime
    assert hidden_runtime["fallback_allocations"] == 1, hidden_runtime
    assert hidden_runtime["fallback_deallocations"] == 1, hidden_runtime
    assert hidden_runtime["raw_dealloc_no_metadata"] == 1, (
        "the cross-crate replacement pointer has no recovery record, so its later non-local Drop must remain raw rather than fabricating typed attribution",
        hidden_runtime,
    )

    runtime = next(
        json.loads(line)
        for line in stdout.splitlines()
        if line.startswith("{") and "direct_local_ownership_probe" in line
    )
    assert runtime["checksum"] != 0
    assert runtime["typed_allocations"] == 4, runtime
    assert runtime["typed_deallocations"] == 4, runtime
    assert runtime["fallback_allocations"] == 0, runtime
    assert runtime["fallback_deallocations"] == 0, runtime
    assert runtime["raw_dealloc_no_metadata"] == 0, runtime


def main() -> int:
    toolchain = (ROOT / "rust-toolchain").read_text(encoding="utf-8").strip()
    rustc = shutil.which("rustc") or "rustc"
    cargo = shutil.which("cargo") or "cargo"
    sysroot = subprocess.check_output(
        [rustc, f"+{toolchain}", "--print", "sysroot"], cwd=ROOT, text=True
    ).strip()

    with tempfile.TemporaryDirectory(prefix="unialloc-direct-local-owner-") as raw:
        workspace = Path(raw)
        write_probe(workspace)
        shutil.copy2(ROOT / "Cargo.lock", workspace / PROBE_NAME / "Cargo.lock")
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
            run_env[variable] = f"{sysroot}/lib" + (os.pathsep + current if current else "")
        run_env.update(
            {
                "RUSTC_WRAPPER": str(pass_binary),
                "UNIALLOC_RUSTC_TARGET_CRATES": PROBE_NAME,
                "UNIALLOC_REWRITE_AUDIT_DIR": str(rewrites),
                "UNIALLOC_PASS_LOG_DIR": str(logs),
                "UNIALLOC_CONTINUE_COMPILATION": "1",
                "UNIALLOC_ACTUAL_MIR_REWRITE": "1",
                "UNIALLOC_ACTUAL_SEMANTIC_SCOPE_REWRITE": "1",
                "UNIALLOC_DIRECT_LOCAL_METADATA_ABI": "1",
                "UNIALLOC_DIRECT_LOCAL_SIZE_ALIGN_WITH_SEMANTIC_DROP": "1",
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
                str(workspace / PROBE_NAME / "Cargo.toml"),
            ],
            cwd=workspace,
            env=run_env,
        )
        audit_paths = sorted(rewrites.glob("*.json"))
        assert len(audit_paths) == 1, audit_paths
        audit = json.loads(audit_paths[0].read_text(encoding="utf-8"))
        validate(audit, stdout)

    print("direct-local ownership pairing probe: PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
