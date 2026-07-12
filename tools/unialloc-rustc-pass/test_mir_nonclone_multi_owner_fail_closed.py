#!/usr/bin/env python3
"""Exercise non-Clone multi-owner return classification through the real pass."""

from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile


ROOT = Path(__file__).resolve().parents[2]
PASS_SOURCE = ROOT / "tools/unialloc-rustc-pass/unialloc-rustc-mir-rewrite-dry-run.rs"
PROBE_NAME = "nonclone_multi_owner_probe"
PROBE_FUNCTION = "main"
FACTORY_FUNCTION = "make_multi_owner"
RESIZE_FUNCTION = "resize_nested_strings"
RESIZE_CALLEE = "resize"
AMBIGUOUS_STATUS = "semantic_scope_rewrite_skipped_ambiguous_heap_object_type"
AMBIGUOUS_RESOLUTION = "rustc_middle_multiple_heap_object_types_not_lowered"
APPLIED_OR_PLANNED_SCOPE_STATUSES = {
    "actual_semantic_scope_enter_exit_rewrite_applied",
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
    factory = workspace / "multi-owner-factory"
    app = workspace / PROBE_NAME
    (factory / "src").mkdir(parents=True)
    (app / "src").mkdir(parents=True)
    (factory / "Cargo.toml").write_text(
        """[package]
name = "multi-owner-factory"
version = "0.1.0"
edition = "2021"
""",
        encoding="utf-8",
    )
    (factory / "src/lib.rs").write_text(
        r'''#[inline(never)]
pub fn make_multi_owner(seed: u8) -> (Vec<u8>, String) {
    let bytes = vec![seed, seed + 1, seed + 2, seed + 3];
    let label = format!("owner-{seed}");
    (bytes, label)
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
multi-owner-factory = {{ path = "../multi-owner-factory" }}
unialloc = {{ path = {json.dumps(str(ROOT / "unialloc"))}, features = ["stats", "type_isolation"] }}
''',
        encoding="utf-8",
    )
    (app / "src/main.rs").write_text(
        r'''use unialloc::UniAlloc;

#[global_allocator]
static ALLOCATOR: UniAlloc = UniAlloc;

fn main() {
    let seed = 17_u8;
    let (bytes, label) = multi_owner_factory::make_multi_owner(seed);
    let expected_bytes = [17_u8, 18, 19, 20];
    assert_eq!(bytes.as_slice(), expected_bytes.as_slice());
    assert_eq!(label, "owner-17");
    let byte_sum = bytes.iter().map(|value| u64::from(*value)).sum::<u64>();

    let mut nested = vec![String::from("seed")];
    resize_nested_strings(&mut nested);
    assert_eq!(nested.len(), 3);
    assert_eq!(nested[0], "seed");
    assert_eq!(nested[1], "fill");
    assert_eq!(nested[2], "fill");
    println!(
        "{{\"source\":\"nonclone_multi_owner_probe\",\"byte_sum\":{},\"vector_len\":{},\"label\":\"{}\",\"label_len\":{},\"resized_len\":{},\"resized_first\":\"{}\",\"resized_last\":\"{}\",\"conventional_result_correct\":true,\"resize_result_correct\":true}}",
        byte_sum,
        bytes.len(),
        label,
        label.len(),
        nested.len(),
        nested[0],
        nested[2],
    );
}

#[inline(never)]
fn resize_nested_strings(values: &mut Vec<String>) {
    values.resize(3, String::from("fill"));
}
''',
        encoding="utf-8",
    )


def probe_function_matches(row: dict[str, object]) -> bool:
    function = str(row.get("mir_function") or "")
    return function == PROBE_FUNCTION or function.endswith(f"::{PROBE_FUNCTION}")


def factory_call_rows(audit: dict[str, object]) -> list[dict[str, object]]:
    rows = audit.get("rewrite_candidates", [])
    assert isinstance(rows, list), rows
    return [
        row
        for row in rows
        if isinstance(row, dict)
        and probe_function_matches(row)
        and FACTORY_FUNCTION in str(row.get("callee") or "")
    ]


def resize_call_rows(audit: dict[str, object]) -> list[dict[str, object]]:
    rows = audit.get("rewrite_candidates", [])
    assert isinstance(rows, list), rows
    return [
        row
        for row in rows
        if isinstance(row, dict)
        and (
            str(row.get("mir_function") or "") == RESIZE_FUNCTION
            or str(row.get("mir_function") or "").endswith(f"::{RESIZE_FUNCTION}")
        )
        and RESIZE_CALLEE in str(row.get("callee") or "")
    ]


def validate(audit: dict[str, object], stdout: str) -> dict[str, object]:
    summary = audit.get("summary")
    assert isinstance(summary, dict), summary
    assert summary.get("provider_override_installed") is True, summary
    assert summary.get("body_clone_returned_to_rustc") is True, summary

    rows = factory_call_rows(audit)
    assert len(rows) == 1, (
        f"{PROBE_FUNCTION} must emit exactly one {FACTORY_FUNCTION} audit row",
        rows,
    )
    row = rows[0]
    callee = str(row.get("callee") or "")
    assert "clone" not in callee.lower(), f"probe accidentally exercised Clone: {row!r}"
    assert row.get("lowering_kind") == "semantic_scope_unsolved_heap_object_candidate", row
    assert row.get("rewrite_status") == AMBIGUOUS_STATUS, row
    assert row.get("replacement_resolution_status") == AMBIGUOUS_RESOLUTION, row
    assert row.get("metadata_pairing_contract") == "audit_only_ambiguous_heap_object_type", row
    assert row.get("semantic_scope_unwind_pop_inserted") is False, row
    assert int(summary.get("semantic_scope_unsolved_candidate_count") or 0) >= 1, summary

    row_text = json.dumps(row, sort_keys=True)
    for owner in ("std::vec::Vec<u8", "std::string::String"):
        assert owner in row_text, f"ambiguous audit omitted {owner}: {row!r}"
    destination = str(row.get("destination_type") or "")
    assert "Vec<u8" in destination and "String" in destination, destination

    applied_or_planned = [
        candidate
        for candidate in rows
        if candidate.get("rewrite_status") in APPLIED_OR_PLANNED_SCOPE_STATUSES
    ]
    assert not applied_or_planned, (
        "a non-Clone return with multiple supported owners must fail closed, "
        "not receive a semantic scope",
        applied_or_planned,
    )

    resize_rows = resize_call_rows(audit)
    assert len(resize_rows) == 1, (
        f"{RESIZE_FUNCTION} must emit exactly one relevant {RESIZE_CALLEE} audit row",
        resize_rows,
    )
    resize_row = resize_rows[0]
    assert (
        resize_row.get("lowering_kind")
        == "semantic_scope_unsolved_heap_object_candidate"
    ), resize_row
    assert resize_row.get("rewrite_status") == AMBIGUOUS_STATUS, resize_row
    assert (
        resize_row.get("replacement_resolution_status") == AMBIGUOUS_RESOLUTION
    ), resize_row
    assert (
        resize_row.get("metadata_pairing_contract")
        == "audit_only_ambiguous_heap_object_type"
    ), resize_row
    assert resize_row.get("semantic_scope_unwind_pop_inserted") is False, resize_row
    resize_text = json.dumps(resize_row, sort_keys=True)
    for owner in (
        "std::vec::Vec<std::string::String",
        "std::string::String",
    ):
        assert owner in resize_text, f"resize audit omitted {owner}: {resize_row!r}"
    resize_arguments = json.dumps(resize_row.get("argument_types") or [])
    assert "Vec<std::string::String" in resize_arguments, resize_arguments
    resize_applied_or_planned = [
        candidate
        for candidate in resize_rows
        if candidate.get("rewrite_status") in APPLIED_OR_PLANNED_SCOPE_STATUSES
    ]
    assert not resize_applied_or_planned, (
        "Vec<String>::resize has both the receiver allocation and inner String "
        "allocation identity and must not receive one typed semantic scope",
        resize_applied_or_planned,
    )

    runtime = next(
        json.loads(line)
        for line in stdout.splitlines()
        if line.startswith("{") and PROBE_NAME in line
    )
    assert runtime == {
        "source": PROBE_NAME,
        "byte_sum": 74,
        "vector_len": 4,
        "label": "owner-17",
        "label_len": 8,
        "resized_len": 3,
        "resized_first": "seed",
        "resized_last": "fill",
        "conventional_result_correct": True,
        "resize_result_correct": True,
    }, runtime
    return {
        "factory_return": {
            "mir_function": row.get("mir_function"),
            "callee": row.get("callee"),
            "destination_type": row.get("destination_type"),
            "lowering_kind": row.get("lowering_kind"),
            "rewrite_status": row.get("rewrite_status"),
            "replacement_resolution_status": row.get(
                "replacement_resolution_status"
            ),
            "metadata_pairing_contract": row.get("metadata_pairing_contract"),
            "semantic_scope_unwind_pop_inserted": row.get(
                "semantic_scope_unwind_pop_inserted"
            ),
        },
        "receiver_resize": {
            "mir_function": resize_row.get("mir_function"),
            "callee": resize_row.get("callee"),
            "argument_types": resize_row.get("argument_types"),
            "lowering_kind": resize_row.get("lowering_kind"),
            "rewrite_status": resize_row.get("rewrite_status"),
            "replacement_resolution_status": resize_row.get(
                "replacement_resolution_status"
            ),
            "metadata_pairing_contract": resize_row.get(
                "metadata_pairing_contract"
            ),
            "semantic_scope_unwind_pop_inserted": resize_row.get(
                "semantic_scope_unwind_pop_inserted"
            ),
        },
        "conventional_result": runtime,
    }


def main() -> int:
    toolchain = (ROOT / "rust-toolchain").read_text(encoding="utf-8").strip()
    rustc = shutil.which("rustc") or "rustc"
    cargo = shutil.which("cargo") or "cargo"
    sysroot = subprocess.check_output(
        [rustc, f"+{toolchain}", "--print", "sysroot"], cwd=ROOT, text=True
    ).strip()

    with tempfile.TemporaryDirectory(prefix="unialloc-nonclone-multi-owner-") as raw:
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
                "source": "nonclone_multi_owner_fail_closed_probe",
                "validated": True,
                "evidence": evidence,
            },
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
