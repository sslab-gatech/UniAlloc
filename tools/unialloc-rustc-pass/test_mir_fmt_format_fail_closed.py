#!/usr/bin/env python3
"""Regression-check ``fmt::format`` fail-closed behavior for an allocating callback."""

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
PROBE_NAME = "fmt_format_fail_closed_probe"
HELPER_NAME = "fmt_allocating_helper"
ALLOCATING_FUNCTION = "format_allocating_display"
PLAIN_FUNCTION = "format_plain_str"
APPLIED_STATUS = "actual_semantic_scope_enter_exit_rewrite_applied"
UNRESOLVED_CONTRACT = "audit_only_unresolved_heap_object_type"


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


def write_helper(workspace: Path) -> Path:
    helper = workspace / HELPER_NAME
    (helper / "src").mkdir(parents=True)
    (helper / "Cargo.toml").write_text(
        f'''[package]
name = "{HELPER_NAME.replace('_', '-')}"
version = "0.1.0"
edition = "2021"
''',
        encoding="utf-8",
    )
    (helper / "src/lib.rs").write_text(
        r'''use std::fmt;
use std::sync::atomic::{AtomicUsize, Ordering};

static CALLBACKS: AtomicUsize = AtomicUsize::new(0);

pub struct AllocatingDisplay<'a> {
    payload: &'a str,
}

impl<'a> AllocatingDisplay<'a> {
    pub fn new(payload: &'a str) -> Self {
        Self { payload }
    }
}

impl fmt::Display for AllocatingDisplay<'_> {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        CALLBACKS.fetch_add(1, Ordering::Relaxed);
        let mut same_layout_vec = Vec::with_capacity(self.payload.len());
        same_layout_vec.extend_from_slice(self.payload.as_bytes());
        let rendered = std::str::from_utf8(&same_layout_vec).expect("ASCII payload");
        formatter.write_str(rendered)
    }
}

pub fn reset_callbacks() {
    CALLBACKS.store(0, Ordering::Relaxed);
}

pub fn callbacks() -> usize {
    CALLBACKS.load(Ordering::Relaxed)
}
''',
        encoding="utf-8",
    )
    return helper


def write_probe(workspace: Path, helper: Path) -> Path:
    app = workspace / PROBE_NAME
    (app / "src").mkdir(parents=True)
    (app / "Cargo.toml").write_text(
        f'''[package]
name = "{PROBE_NAME.replace('_', '-')}"
version = "0.1.0"
edition = "2021"

[dependencies]
{HELPER_NAME.replace('_', '-')} = {{ path = {json.dumps(str(helper))} }}
# Keep this transient probe resolvable by nightly-2022-07-01; UniAlloc's loose
# lock_api requirement would otherwise select a release requiring rustc 1.71.
lock_api = "=0.4.3"
unialloc = {{ path = {json.dumps(str(ROOT / "unialloc"))}, features = ["stats", "type_isolation"] }}
''',
        encoding="utf-8",
    )
    (app / "src/main.rs").write_text(
        f'''use {HELPER_NAME}::{{callbacks, reset_callbacks, AllocatingDisplay}};
use unialloc::{{
    semantic_auto_metadata_disable, semantic_fallback_attribution_snapshot,
    semantic_metadata_validation_snapshot, semantic_stats_recording_disable,
    semantic_stats_reset, semantic_stats_snapshot, type_isolation_side_cache_snapshot,
    UniAlloc,
}};

#[global_allocator]
static ALLOCATOR: UniAlloc = UniAlloc;

const PAYLOAD: &str = "0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef";

#[inline(never)]
fn {ALLOCATING_FUNCTION}(value: &AllocatingDisplay<'_>) -> String {{
    std::fmt::format(format_args!("{{}}", value))
}}

#[inline(never)]
fn {PLAIN_FUNCTION}(value: &str) -> String {{
    std::fmt::format(format_args!("{{}}", value))
}}

fn main() {{
    assert_eq!(PAYLOAD.len(), 192);
    semantic_auto_metadata_disable();
    semantic_stats_reset();
    reset_callbacks();

    let value = AllocatingDisplay::new(PAYLOAD);
    let allocating = {ALLOCATING_FUNCTION}(&value);
    assert_eq!(allocating, PAYLOAD);
    let plain = {PLAIN_FUNCTION}(PAYLOAD);
    assert_eq!(plain, PAYLOAD);

    let callback_count = callbacks();
    drop(allocating);
    drop(plain);
    let stats = semantic_stats_snapshot();
    let fallback = semantic_fallback_attribution_snapshot();
    let validation = semantic_metadata_validation_snapshot();
    let side_cache = type_isolation_side_cache_snapshot();
    semantic_stats_recording_disable();

    println!(
        r#"{{{{"source":"{PROBE_NAME}","callback_count":{{}},"typed_allocations":{{}},"typed_deallocations":{{}},"typed_cache_hits":{{}},"typed_cache_inserts":{{}},"fallback_allocations":{{}},"fallback_deallocations":{{}},"raw_alloc_no_metadata":{{}},"raw_dealloc_no_metadata":{{}},"raw_realloc_no_metadata":{{}},"recovery_identity_mismatches":{{}},"side_cache_corrupt_slots":{{}}}}}}"#,
        callback_count,
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

}}
''',
        encoding="utf-8",
    )
    return app


def function_matches(row: dict[str, object], function_name: str) -> bool:
    function = str(row.get("mir_function") or "")
    return function == function_name or function.endswith(f"::{function_name}")


def function_rows(
    rows: list[object], function_name: str
) -> list[dict[str, object]]:
    return [
        row
        for row in rows
        if isinstance(row, dict) and function_matches(row, function_name)
    ]


def fmt_format_rows(
    rows: list[object], function_name: str
) -> list[dict[str, object]]:
    return [
        row
        for row in function_rows(rows, function_name)
        if "fmt::format" in str(row.get("callee") or "")
    ]


def assert_fmt_format_stays_unresolved(row: dict[str, object]) -> None:
    assert "fmt::format" in str(row.get("callee") or ""), row
    assert row.get("rewrite_status") != APPLIED_STATUS, row
    assert row.get("metadata_pairing_contract") == UNRESOLVED_CONTRACT, row
    assert row.get("semantic_object_type") == "<unknown-heap-object-type>", row


def validate(audit: dict[str, object], stdout: str) -> dict[str, object]:
    summary = audit.get("summary")
    assert isinstance(summary, dict), summary
    assert summary.get("provider_override_installed") is True, summary
    assert summary.get("body_clone_returned_to_rustc") is True, summary
    assert summary.get("actual_semantic_scope_rewrite_requested") is True, summary

    rows = audit.get("rewrite_candidates")
    assert isinstance(rows, list), rows
    allocating_function_rows = function_rows(rows, ALLOCATING_FUNCTION)
    plain_function_rows = function_rows(rows, PLAIN_FUNCTION)
    allocating_matches = fmt_format_rows(rows, ALLOCATING_FUNCTION)
    plain_matches = fmt_format_rows(rows, PLAIN_FUNCTION)
    assert len(allocating_matches) == 1, allocating_matches
    assert len(plain_matches) == 1, plain_matches
    assert not any(
        row.get("rewrite_status") == APPLIED_STATUS
        for row in allocating_function_rows + plain_function_rows
    ), allocating_function_rows + plain_function_rows
    assert_fmt_format_stays_unresolved(allocating_matches[0])
    assert_fmt_format_stays_unresolved(plain_matches[0])
    assert not any(
        HELPER_NAME in str(row.get("mir_function") or "")
        for row in rows
        if isinstance(row, dict)
    ), rows

    runtime = next(
        json.loads(line)
        for line in stdout.splitlines()
        if line.startswith("{") and PROBE_NAME in line
    )
    assert int(runtime["callback_count"]) == 1, runtime
    assert int(runtime["typed_allocations"]) == 0, runtime
    assert int(runtime["typed_deallocations"]) == 0, runtime
    assert int(runtime["typed_cache_hits"]) == 0, runtime
    assert int(runtime["typed_cache_inserts"]) == 0, runtime
    assert int(runtime["fallback_allocations"]) > 0, runtime
    assert int(runtime["fallback_deallocations"]) > 0, runtime
    assert int(runtime["raw_alloc_no_metadata"]) > 0, runtime
    assert int(runtime["raw_dealloc_no_metadata"]) > 0, runtime
    assert int(runtime["recovery_identity_mismatches"]) == 0, runtime
    assert int(runtime["side_cache_corrupt_slots"]) == 0, runtime

    return {
        "allocating_display_fmt": {
            key: allocating_matches[0].get(key)
            for key in (
                "callee",
                "rewrite_status",
                "metadata_pairing_contract",
                "semantic_object_type",
            )
        },
        "plain_str_fmt": {
            key: plain_matches[0].get(key)
            for key in (
                "callee",
                "rewrite_status",
                "metadata_pairing_contract",
                "semantic_object_type",
            )
        },
        "runtime": runtime,
    }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--toolchain")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    toolchain = args.toolchain or (ROOT / "rust-toolchain").read_text(
        encoding="utf-8"
    ).strip()
    rustc = shutil.which("rustc") or "rustc"
    cargo = shutil.which("cargo") or "cargo"
    sysroot = subprocess.check_output(
        [rustc, f"+{toolchain}", "--print", "sysroot"], cwd=ROOT, text=True
    ).strip()

    with tempfile.TemporaryDirectory(prefix="unialloc-fmt-format-fail-closed-") as raw:
        workspace = Path(raw)
        helper = write_helper(workspace)
        app = write_probe(workspace, helper)
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
                    "Both exact alloc::fmt::format calls remain fail closed because fmt::Arguments erases arbitrary Display callbacks.",
                    "The helper crate is deliberately excluded from compiler rewriting and allocates a same-size Vec inside Display::fmt.",
                    "This proves absence of outer String scope attribution for one adversarial callback and one plain-str call, not universal formatting safety or performance.",
                ],
            },
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
