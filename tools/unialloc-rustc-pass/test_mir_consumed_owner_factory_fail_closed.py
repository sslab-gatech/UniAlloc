#!/usr/bin/env python3
"""Fail closed when one call consumes and produces distinct heap owners."""

from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile


ROOT = Path(__file__).resolve().parents[2]
PASS_SOURCE = ROOT / "tools/unialloc-rustc-pass/unialloc-rustc-mir-rewrite-dry-run.rs"
PROBE_NAME = "consumed_owner_factory_probe"
PROBE_FUNCTION = "main"
CONFLICTING_CALLEE = "consume_conflicting"
SAME_OWNER_CALLEE = "consume_same_owner"
CONFLICTING_RECEIVER_FUNCTION = "extend_conflicting_receiver"
SAME_OWNER_RECEIVER_FUNCTION = "extend_same_owner_receiver"
RECEIVER_CALLEE = "extend"
AMBIGUOUS_STATUS = "semantic_scope_rewrite_skipped_ambiguous_heap_object_type"
AMBIGUOUS_RESOLUTION = "rustc_middle_multiple_heap_object_types_not_lowered"
APPLIED_STATUS = "actual_semantic_scope_enter_exit_rewrite_applied"
UNRESOLVED_STATUS = "semantic_scope_rewrite_skipped_unresolved_heap_object_type"
UNRESOLVED_RESOLUTION = "rustc_middle_heap_object_type_not_solved"
APPLIED_OR_PLANNED_SCOPE_STATUSES = {
    APPLIED_STATUS,
    "semantic_scope_enter_exit_rewrite_planned",
    "actual_semantic_scope_drop_rewrite_applied",
    "semantic_scope_drop_rewrite_planned",
}


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


def write_probe(workspace: Path) -> None:
    factory = workspace / "consumed-owner-factory"
    app = workspace / PROBE_NAME
    (factory / "src").mkdir(parents=True)
    (app / "src").mkdir(parents=True)
    (factory / "Cargo.toml").write_text(
        """[package]
name = "consumed-owner-factory"
version = "0.1.0"
edition = "2021"
""",
        encoding="utf-8",
    )
    (factory / "src/lib.rs").write_text(
        r'''#[inline(never)]
pub fn consume_conflicting(input: String) -> Vec<u8> {
    assert_eq!(input.as_str(), "consumed-string-owner");
    drop(input);

    let mut output = Vec::with_capacity(4);
    output.extend_from_slice(&[7, 11, 13, 17]);
    output
}

#[inline(never)]
pub fn consume_same_owner(input: Vec<u8>) -> Vec<u8> {
    assert_eq!(input.as_slice(), [2, 3, 5, 7]);
    drop(input);

    let mut output = Vec::with_capacity(4);
    output.extend_from_slice(&[19, 23, 29, 31]);
    output
}

pub struct ByteSource {
    bytes: [u8; 4],
    index: usize,
    label: String,
}

impl ByteSource {
    #[inline(never)]
    pub fn new(label: String) -> Self {
        Self {
            bytes: [37, 41, 43, 47],
            index: 0,
            label,
        }
    }
}

impl Iterator for ByteSource {
    type Item = u8;

    #[inline(never)]
    fn next(&mut self) -> Option<Self::Item> {
        let value = self.bytes.get(self.index).copied();
        self.index += usize::from(value.is_some());
        value
    }
}

impl Drop for ByteSource {
    fn drop(&mut self) {
        assert_eq!(self.label, "receiver-string-owner");
    }
}
''',
        encoding="utf-8",
    )
    (app / "Cargo.toml").write_text(
        f'''[package]
name = "{PROBE_NAME.replace('_', '-')}"
version = "0.1.0"
edition = "2021"

[dependencies]
consumed-owner-factory = {{ path = "../consumed-owner-factory" }}
unialloc = {{ path = {json.dumps(str(ROOT / "unialloc"))}, features = ["stats", "type_isolation"] }}
''',
        encoding="utf-8",
    )
    (app / "src/main.rs").write_text(
        r'''use unialloc::{
    semantic_auto_metadata_disable, semantic_metadata_validation_snapshot,
    semantic_stats_reset, type_isolation_side_cache_snapshot, UniAlloc,
};

#[global_allocator]
static ALLOCATOR: UniAlloc = UniAlloc;

#[inline(never)]
fn extend_conflicting_receiver(
    receiver: &mut Vec<u8>,
    source: consumed_owner_factory::ByteSource,
) {
    receiver.extend(source);
}

#[inline(never)]
fn extend_same_owner_receiver(receiver: &mut Vec<u8>, source: Vec<u8>) {
    receiver.extend(source);
}

fn main() {
    semantic_auto_metadata_disable();
    semantic_stats_reset();

    let mut same_owner_input = Vec::with_capacity(4);
    same_owner_input.extend_from_slice(&[2, 3, 5, 7]);
    let before_same_owner = semantic_metadata_validation_snapshot();
    let same_owner_output =
        consumed_owner_factory::consume_same_owner(same_owner_input);
    let after_same_owner = semantic_metadata_validation_snapshot();
    assert_eq!(same_owner_output.as_slice(), [19, 23, 29, 31]);
    let same_owner_mismatch_delta = after_same_owner
        .recovery_identity_mismatches
        .saturating_sub(before_same_owner.recovery_identity_mismatches);
    drop(same_owner_output);

    let mut conflicting_input = String::with_capacity(32);
    conflicting_input.push_str("consumed-string-owner");
    let before_conflicting = semantic_metadata_validation_snapshot();
    let conflicting_output =
        consumed_owner_factory::consume_conflicting(conflicting_input);
    let after_conflicting = semantic_metadata_validation_snapshot();
    assert_eq!(conflicting_output.as_slice(), [7, 11, 13, 17]);
    let conflicting_mismatch_delta = after_conflicting
        .recovery_identity_mismatches
        .saturating_sub(before_conflicting.recovery_identity_mismatches);
    drop(conflicting_output);

    let mut same_owner_receiver = Vec::with_capacity(4);
    let mut same_owner_source = Vec::with_capacity(4);
    same_owner_source.extend_from_slice(&[53, 59, 61, 67]);
    let before_same_owner_receiver = semantic_metadata_validation_snapshot();
    extend_same_owner_receiver(&mut same_owner_receiver, same_owner_source);
    let after_same_owner_receiver = semantic_metadata_validation_snapshot();
    assert_eq!(same_owner_receiver.as_slice(), [53, 59, 61, 67]);
    let same_owner_receiver_mismatch_delta = after_same_owner_receiver
        .recovery_identity_mismatches
        .saturating_sub(before_same_owner_receiver.recovery_identity_mismatches);
    drop(same_owner_receiver);

    let mut receiver_label = String::with_capacity(32);
    receiver_label.push_str("receiver-string-owner");
    let conflicting_receiver_source =
        consumed_owner_factory::ByteSource::new(receiver_label);
    let mut conflicting_receiver = Vec::with_capacity(4);
    let before_conflicting_receiver = semantic_metadata_validation_snapshot();
    extend_conflicting_receiver(
        &mut conflicting_receiver,
        conflicting_receiver_source,
    );
    let after_conflicting_receiver = semantic_metadata_validation_snapshot();
    assert_eq!(conflicting_receiver.as_slice(), [37, 41, 43, 47]);
    let conflicting_receiver_mismatch_delta = after_conflicting_receiver
        .recovery_identity_mismatches
        .saturating_sub(before_conflicting_receiver.recovery_identity_mismatches);
    drop(conflicting_receiver);

    let final_validation = semantic_metadata_validation_snapshot();
    let side_cache = type_isolation_side_cache_snapshot();
    println!(
        concat!(
            "{{",
            "\"source\":\"consumed_owner_factory_probe\",",
            "\"same_owner_mismatch_delta\":{},",
            "\"conflicting_mismatch_delta\":{},",
            "\"same_owner_receiver_mismatch_delta\":{},",
            "\"conflicting_receiver_mismatch_delta\":{},",
            "\"recovery_identity_mismatches\":{},",
            "\"last_mismatch_requested_type_id\":{},",
            "\"last_mismatch_recorded_type_id\":{},",
            "\"side_cache_corrupt_slots\":{},",
            "\"same_owner_result_correct\":true,",
            "\"conflicting_result_correct\":true",
            "}}"
        ),
        same_owner_mismatch_delta,
        conflicting_mismatch_delta,
        same_owner_receiver_mismatch_delta,
        conflicting_receiver_mismatch_delta,
        final_validation.recovery_identity_mismatches,
        final_validation.last_mismatch_requested_type_id,
        final_validation.last_mismatch_recorded_type_id,
        side_cache.corrupt_slots,
    );
}
''',
        encoding="utf-8",
    )


def probe_function_matches(row: dict[str, object]) -> bool:
    function = str(row.get("mir_function") or "")
    return function == PROBE_FUNCTION or function.endswith(f"::{PROBE_FUNCTION}")


def call_rows(audit: dict[str, object], callee_marker: str) -> list[dict[str, object]]:
    rows = audit.get("rewrite_candidates", [])
    assert isinstance(rows, list), rows
    return [
        row
        for row in rows
        if isinstance(row, dict)
        and probe_function_matches(row)
        and callee_marker in str(row.get("callee") or "")
    ]


def function_call_rows(
    audit: dict[str, object], function_name: str, callee_marker: str
) -> list[dict[str, object]]:
    rows = audit.get("rewrite_candidates", [])
    assert isinstance(rows, list), rows
    return [
        row
        for row in rows
        if isinstance(row, dict)
        and (
            str(row.get("mir_function") or "") == function_name
            or str(row.get("mir_function") or "").endswith(f"::{function_name}")
        )
        and callee_marker in str(row.get("callee") or "")
    ]


def validate(audit: dict[str, object], stdout: str) -> dict[str, object]:
    summary = audit.get("summary")
    assert isinstance(summary, dict), audit
    assert summary.get("provider_override_installed") is True, summary
    assert summary.get("body_clone_returned_to_rustc") is True, summary
    assert summary.get("actual_semantic_scope_rewrite_requested") is True, summary
    assert summary.get("actual_semantic_scope_rewrite") is True, summary

    conflicting_rows = call_rows(audit, CONFLICTING_CALLEE)
    assert len(conflicting_rows) == 1, (
        f"{CONFLICTING_CALLEE} must emit exactly one target audit row",
        conflicting_rows,
    )
    conflicting = conflicting_rows[0]
    assert (
        conflicting.get("lowering_kind")
        == "semantic_scope_unsolved_heap_object_candidate"
    ), conflicting
    assert conflicting.get("rewrite_status") == AMBIGUOUS_STATUS, conflicting
    assert (
        conflicting.get("replacement_resolution_status") == AMBIGUOUS_RESOLUTION
    ), conflicting
    assert (
        conflicting.get("metadata_pairing_contract")
        == "audit_only_ambiguous_heap_object_type"
    ), conflicting
    assert conflicting.get("semantic_scope_unwind_pop_inserted") is False, conflicting
    conflicting_text = json.dumps(conflicting, sort_keys=True)
    for owner in ("std::vec::Vec<u8", "std::string::String"):
        assert owner in conflicting_text, (
            f"consumed-owner audit omitted {owner}: {conflicting!r}"
        )
    assert "Vec<u8" in str(conflicting.get("destination_type") or ""), conflicting
    assert "String" in json.dumps(conflicting.get("argument_types") or []), conflicting
    assert not [
        row
        for row in conflicting_rows
        if row.get("rewrite_status") in APPLIED_OR_PLANNED_SCOPE_STATUSES
    ], conflicting_rows

    same_owner_rows = call_rows(audit, SAME_OWNER_CALLEE)
    assert len(same_owner_rows) == 1, (
        f"{SAME_OWNER_CALLEE} must emit exactly one target audit row",
        same_owner_rows,
    )
    same_owner = same_owner_rows[0]
    assert same_owner.get("lowering_kind") == "semantic_scope_unsolved_heap_object_candidate", (
        same_owner
    )
    assert same_owner.get("rewrite_status") == UNRESOLVED_STATUS, same_owner
    assert same_owner.get("replacement_resolution_status") == UNRESOLVED_RESOLUTION, (
        same_owner
    )
    assert same_owner.get("metadata_pairing_contract") == (
        "audit_only_unresolved_heap_object_type"
    ), same_owner
    assert same_owner.get("semantic_scope_unwind_pop_inserted") is False, same_owner
    assert "Vec<u8" in str(same_owner.get("destination_type") or ""), same_owner
    assert "Vec<u8" in json.dumps(same_owner.get("argument_types") or []), same_owner
    assert not [
        row
        for row in same_owner_rows
        if row.get("rewrite_status") in APPLIED_OR_PLANNED_SCOPE_STATUSES
    ], same_owner_rows

    conflicting_receiver_rows = function_call_rows(
        audit, CONFLICTING_RECEIVER_FUNCTION, RECEIVER_CALLEE
    )
    assert len(conflicting_receiver_rows) == 1, conflicting_receiver_rows
    conflicting_receiver = conflicting_receiver_rows[0]
    assert (
        conflicting_receiver.get("lowering_kind")
        == "semantic_scope_unsolved_heap_object_candidate"
    ), conflicting_receiver
    assert conflicting_receiver.get("rewrite_status") == AMBIGUOUS_STATUS, (
        conflicting_receiver
    )
    assert (
        conflicting_receiver.get("replacement_resolution_status")
        == AMBIGUOUS_RESOLUTION
    ), conflicting_receiver
    assert (
        conflicting_receiver.get("metadata_pairing_contract")
        == "audit_only_ambiguous_heap_object_type"
    ), conflicting_receiver
    conflicting_receiver_text = json.dumps(conflicting_receiver, sort_keys=True)
    for owner in ("std::vec::Vec<u8", "std::string::String"):
        assert owner in conflicting_receiver_text, conflicting_receiver

    same_owner_receiver_rows = function_call_rows(
        audit, SAME_OWNER_RECEIVER_FUNCTION, RECEIVER_CALLEE
    )
    assert len(same_owner_receiver_rows) == 1, same_owner_receiver_rows
    same_owner_receiver = same_owner_receiver_rows[0]
    assert same_owner_receiver.get("lowering_kind") == "semantic_scope_enter_exit_rewrite", (
        same_owner_receiver
    )
    assert same_owner_receiver.get("rewrite_status") == APPLIED_STATUS, (
        same_owner_receiver
    )
    assert "Vec<u8" in str(same_owner_receiver.get("semantic_object_type") or ""), (
        same_owner_receiver
    )

    runtime = next(
        json.loads(line)
        for line in stdout.splitlines()
        if line.startswith("{") and PROBE_NAME in line
    )
    assert runtime["same_owner_result_correct"] is True, runtime
    assert runtime["conflicting_result_correct"] is True, runtime
    assert int(runtime["same_owner_mismatch_delta"]) == 0, runtime
    assert int(runtime["conflicting_mismatch_delta"]) == 0, runtime
    assert int(runtime["same_owner_receiver_mismatch_delta"]) == 0, runtime
    assert int(runtime["conflicting_receiver_mismatch_delta"]) == 0, runtime
    assert int(runtime["recovery_identity_mismatches"]) == 0, runtime
    assert int(runtime["side_cache_corrupt_slots"]) == 0, runtime

    return {
        "conflicting_consumed_owner": {
            "callee": conflicting.get("callee"),
            "destination_type": conflicting.get("destination_type"),
            "argument_types": conflicting.get("argument_types"),
            "rewrite_status": conflicting.get("rewrite_status"),
            "replacement_resolution_status": conflicting.get(
                "replacement_resolution_status"
            ),
            "replacement_preview": conflicting.get("replacement_preview"),
            "metadata_pairing_contract": conflicting.get("metadata_pairing_contract"),
        },
        "same_owner_dependency_factory_fail_closed": {
            "callee": same_owner.get("callee"),
            "destination_type": same_owner.get("destination_type"),
            "argument_types": same_owner.get("argument_types"),
            "rewrite_status": same_owner.get("rewrite_status"),
            "replacement_resolution_status": same_owner.get(
                "replacement_resolution_status"
            ),
            "metadata_pairing_contract": same_owner.get("metadata_pairing_contract"),
        },
        "conflicting_receiver_consumed_owner": {
            "callee": conflicting_receiver.get("callee"),
            "argument_types": conflicting_receiver.get("argument_types"),
            "rewrite_status": conflicting_receiver.get("rewrite_status"),
            "replacement_resolution_status": conflicting_receiver.get(
                "replacement_resolution_status"
            ),
            "replacement_preview": conflicting_receiver.get("replacement_preview"),
        },
        "same_owner_receiver_positive_control": {
            "callee": same_owner_receiver.get("callee"),
            "argument_types": same_owner_receiver.get("argument_types"),
            "semantic_object_type": same_owner_receiver.get("semantic_object_type"),
            "rewrite_status": same_owner_receiver.get("rewrite_status"),
        },
        "runtime": runtime,
    }


def main() -> int:
    toolchain = (ROOT / "rust-toolchain").read_text(encoding="utf-8").strip()
    rustc = shutil.which("rustc") or "rustc"
    cargo = shutil.which("cargo") or "cargo"
    sysroot = subprocess.check_output(
        [rustc, f"+{toolchain}", "--print", "sysroot"], cwd=ROOT, text=True
    ).strip()

    with tempfile.TemporaryDirectory(prefix="unialloc-consumed-owner-factory-") as raw:
        workspace = Path(raw)
        write_probe(workspace)
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
                str(workspace / PROBE_NAME / "Cargo.toml"),
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
                "source": "consumed_owner_factory_fail_closed_probe",
                "validated": True,
                "evidence": evidence,
            },
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
