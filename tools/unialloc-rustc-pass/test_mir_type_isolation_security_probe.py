#!/usr/bin/env python3
"""Run and validate the deterministic MIR type-isolation security probe."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
from typing import Any, Dict, Iterable, List


ROOT = Path(__file__).resolve().parents[2]
RUNNER_SOURCE = Path(__file__).resolve()
PASS_SOURCE = ROOT / "tools/unialloc-rustc-pass/unialloc-rustc-mir-rewrite-dry-run.rs"
CLONE_CLASSIFICATION_SOURCE = (
    ROOT
    / "tools/unialloc-rustc-pass/fixtures/mir_clone_candidate_classification.rs"
)
PROBE_NAME = "rustc_driver_mir_type_isolation_security_probe"
PROBE_SOURCE = ROOT / "unialloc/src/bin" / f"{PROBE_NAME}.rs"
TYPE_ISOLATED = 0x1
CROSS_THREAD_RECOVERY = 0x8000
SOURCE_BINDING_PATHS = (
    Path("Cargo.toml"),
    Path("Cargo.lock"),
    Path("rust-toolchain"),
    Path("alloc_macros/Cargo.toml"),
    Path("alloc_macros/src"),
    Path("unialloc/Cargo.toml"),
    Path("unialloc/build.rs"),
    Path("unialloc/src"),
    PASS_SOURCE.relative_to(ROOT),
    CLONE_CLASSIFICATION_SOURCE.relative_to(ROOT),
    RUNNER_SOURCE.relative_to(ROOT),
)

CALL_CLASSIFICATION_KINDS = {
    "semantic_scope_enter_exit_rewrite",
    "semantic_scope_non_heap_object_skipped",
    "semantic_scope_unsolved_heap_object_candidate",
}
NON_HEAP_CLONE_FUNCTIONS = (
    "clone_nonheap_token",
    "clone_nonheap_option",
    "clone_nonheap_result",
    "clone_nonowning_reference",
    "clone_standalone_arc",
    "clone_standalone_rc",
)
NESTED_HEAP_CLONE_FUNCTION = "clone_nested_vec_owner"
REFCOUNTED_SINGLE_OWNER_CLONE_FUNCTIONS = (
    "clone_arc_vec_owner",
    "clone_rc_vec_owner",
)
REFCOUNTED_NO_OWNER_CLONE_FUNCTIONS = (
    "clone_standalone_arc",
    "clone_standalone_rc",
)
MULTI_OWNER_HEADERS_CLONE_FUNCTION = "clone_multi_owner_headers"
RAW_POINTER_CLONE_FUNCTION = "clone_raw_pointer_wrapper"
CONST_GENERIC_CLONE_FUNCTION = "clone_const_generic"
MULTI_OWNER_DROP_FUNCTIONS = (
    "drop_multi_owner_struct",
    "drop_multi_owner_enum",
)
GENERIC_DROP_FUNCTION = "generic_drop"


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def command_text(command: Iterable[str]) -> str:
    return " ".join(command)


def run_logged(
    command: List[str],
    *,
    label: str,
    output_dir: Path,
    timeout: int,
    env: Dict[str, str],
) -> Dict[str, Any]:
    stdout_path = output_dir / f"{label}.stdout.txt"
    stderr_path = output_dir / f"{label}.stderr.txt"
    try:
        result = subprocess.run(
            command,
            cwd=ROOT,
            env=env,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=timeout,
            check=False,
        )
        timed_out = False
        returncode = result.returncode
        stdout = result.stdout
        stderr = result.stderr
    except subprocess.TimeoutExpired as exc:
        timed_out = True
        returncode = 124
        stdout = exc.stdout or ""
        stderr = exc.stderr or ""
    stdout_path.write_text(stdout, encoding="utf-8")
    stderr_path.write_text(stderr, encoding="utf-8")
    return {
        "command": command,
        "command_text": command_text(command),
        "returncode": returncode,
        "timed_out": timed_out,
        "stdout": str(stdout_path),
        "stderr": str(stderr_path),
    }


def checked_output(command: List[str]) -> str:
    return subprocess.check_output(command, cwd=ROOT, text=True).strip()


def reject_output_inside_repo(output_dir: Path) -> None:
    resolved = output_dir.resolve()
    root = ROOT.resolve()
    if resolved == root or root in resolved.parents:
        raise AssertionError(f"output directory must be outside repository: {resolved}")


def default_output_dir() -> Path:
    parent = Path(tempfile.gettempdir()).resolve()
    reject_output_inside_repo(parent)
    return Path(
        tempfile.mkdtemp(prefix="unialloc-mir-typeiso-security-", dir=str(parent))
    ).resolve()


def scoped_source_hashes() -> Dict[str, str]:
    files: List[Path] = []
    for relative in SOURCE_BINDING_PATHS:
        path = ROOT / relative
        assert path.exists(), f"source-binding path is missing: {relative}"
        if path.is_dir():
            files.extend(child for child in path.rglob("*") if child.is_file())
        else:
            files.append(path)
    return {
        str(path.relative_to(ROOT)): sha256(path)
        for path in sorted(set(files))
    }


def scoped_source_fingerprint(file_hashes: Dict[str, str]) -> str:
    digest = hashlib.sha256()
    for path, file_hash in sorted(file_hashes.items()):
        digest.update(path.encode("utf-8"))
        digest.update(b"\0")
        digest.update(file_hash.encode("ascii"))
        digest.update(b"\n")
    return digest.hexdigest()


def source_binding_snapshot(toolchain: str, rustc: str) -> Dict[str, Any]:
    status = checked_output(["git", "status", "--short"])
    assert not status, f"commit-bound evidence requires a clean working tree:\n{status}"
    file_hashes = scoped_source_hashes()
    return {
        "git_head": checked_output(["git", "rev-parse", "HEAD"]),
        "git_status": status,
        "clean_head": True,
        "toolchain": toolchain,
        "rustc_verbose_version": checked_output([rustc, f"+{toolchain}", "-Vv"]),
        "sysroot": checked_output([rustc, f"+{toolchain}", "--print", "sysroot"]),
        "scoped_paths": [str(path) for path in SOURCE_BINDING_PATHS],
        "scoped_file_count": len(file_hashes),
        "scoped_file_hashes": file_hashes,
        "scoped_fingerprint_sha256": scoped_source_fingerprint(file_hashes),
    }


def assert_source_binding_stable(start: Dict[str, Any], end: Dict[str, Any]) -> None:
    keys = (
        "git_head",
        "git_status",
        "clean_head",
        "toolchain",
        "rustc_verbose_version",
        "sysroot",
        "scoped_paths",
        "scoped_file_count",
        "scoped_file_hashes",
        "scoped_fingerprint_sha256",
    )
    drift = {
        key: {"start": start.get(key), "end": end.get(key)}
        for key in keys
        if start.get(key) != end.get(key)
    }
    assert not drift, f"source/toolchain binding drifted during probe: {json.dumps(drift, sort_keys=True)}"


def current_rustc_cfg(toolchain: str) -> List[str]:
    normalized = toolchain.lstrip("+").strip()
    if normalized == "nightly" or normalized.startswith(("nightly-2025", "nightly-2026")):
        return ["--cfg", "unialloc_rustc_current"]
    return []


def compile_clone_classification_fixture(
    *,
    pass_binary: Path,
    sysroot: str,
    output_dir: Path,
    timeout: int,
) -> Dict[str, Any]:
    fixture_dir = output_dir / "clone-classification"
    fixture_dir.mkdir()
    audit_path = fixture_dir / "rewrite-audit.json"
    binary_path = fixture_dir / "fixture"
    env = os.environ.copy()
    for variable in ("DYLD_LIBRARY_PATH", "LD_LIBRARY_PATH"):
        current = env.get(variable)
        env[variable] = f"{sysroot}/lib" + (os.pathsep + current if current else "")
    env.update(
        {
            "UNIALLOC_ACTUAL_MIR_REWRITE": "0",
            "UNIALLOC_ACTUAL_SEMANTIC_SCOPE_REWRITE": "0",
            "UNIALLOC_CONTINUE_COMPILATION": "1",
        }
    )
    run = run_logged(
        [
            str(pass_binary),
            "--unialloc-rewrite-audit-out",
            str(audit_path),
            "--unialloc-continue-compilation",
            "--unialloc-dry-run-only",
            "--",
            "--sysroot",
            sysroot,
            "--crate-name",
            "mir_clone_candidate_classification",
            "--edition=2021",
            "-Zmir-opt-level=0",
            str(CLONE_CLASSIFICATION_SOURCE),
            "-o",
            str(binary_path),
        ],
        label="clone-classification",
        output_dir=output_dir,
        timeout=timeout,
        env=env,
    )
    if run["returncode"] != 0:
        raise AssertionError(
            f"clone classification fixture compile failed; see {run['stderr']}"
        )
    if not audit_path.is_file():
        raise AssertionError(f"clone classification audit is missing: {audit_path}")
    return {
        "run": run,
        "audit_path": audit_path,
        "binary_path": binary_path,
        "audit": json.loads(audit_path.read_text(encoding="utf-8")),
    }


def load_runtime_event(stdout_path: Path) -> Dict[str, Any]:
    for raw in stdout_path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line.startswith("{"):
            continue
        value = json.loads(line)
        if isinstance(value, dict) and value.get("source") == PROBE_NAME:
            return value
    raise AssertionError(f"missing {PROBE_NAME} JSON event in {stdout_path}")


def applied_type_rows(audit: Dict[str, Any], marker: str) -> List[Dict[str, Any]]:
    return [
        row
        for row in audit.get("rewrite_candidates", [])
        if isinstance(row, dict)
        and marker in str(row.get("semantic_object_type") or "")
        and row.get("lowering_kind") == "semantic_scope_enter_exit_rewrite"
        and row.get("rewrite_status") == "actual_semantic_scope_enter_exit_rewrite_applied"
    ]


def callee_mentions_exact_function(callee: Any, function_name: str) -> bool:
    return re.search(
        rf"(?<![A-Za-z0-9_]){re.escape(function_name)}(?![A-Za-z0-9_])",
        str(callee or ""),
    ) is not None


def validate_fail_closed_factory_provenance(audit: Dict[str, Any]) -> Dict[str, Any]:
    summary = audit.get("summary") or {}
    rows = [
        row
        for row in audit.get("rewrite_candidates") or []
        if isinstance(row, dict)
        and row.get("lowering_kind")
        == "semantic_scope_unsolved_heap_object_candidate"
    ]
    assert int(summary.get("semantic_scope_unsolved_candidate_count") or 0) == len(rows), (
        "unsolved semantic-scope summary must match row-level fail-closed evidence"
    )

    contracts = {
        "semantic_scope_rewrite_skipped_unresolved_heap_object_type": (
            "rustc_middle_heap_object_type_not_solved",
            "audit_only_unresolved_heap_object_type",
        ),
        "semantic_scope_rewrite_skipped_ambiguous_heap_object_type": (
            "rustc_middle_multiple_heap_object_types_not_lowered",
            "audit_only_ambiguous_heap_object_type",
        ),
    }
    for row in rows:
        rewrite_status = str(row.get("rewrite_status") or "")
        assert rewrite_status in contracts, (
            f"unsolved factory candidate did not fail closed: {row}"
        )
        resolution, pairing = contracts[rewrite_status]
        assert row.get("replacement_resolution_status") == resolution, row
        assert row.get("metadata_pairing_contract") == pairing, row
        assert row.get("semantic_object_type") == "<unknown-heap-object-type>", row

    opaque_factory_counts: Dict[str, int] = {}
    for function_name, destination_marker in (
        ("producer_box", "Box<ProducerPayload"),
        ("consumer_box", "Box<ConsumerPayload"),
    ):
        matching = [
            row
            for row in rows
            if callee_mentions_exact_function(row.get("callee"), function_name)
            and destination_marker in str(row.get("destination_type") or "")
        ]
        assert matching, (
            f"opaque {function_name} calls must stay audit-only unless the pass proves "
            "their factory body"
        )
        assert all(
            row.get("rewrite_status")
            == "semantic_scope_rewrite_skipped_unresolved_heap_object_type"
            and row.get("metadata_pairing_contract")
            == "audit_only_unresolved_heap_object_type"
            for row in matching
        ), matching
        opaque_factory_counts[function_name] = len(matching)

    return {
        "validated": True,
        "unsolved_candidate_count": len(rows),
        "opaque_factory_fail_closed_rows": opaque_factory_counts,
    }


def clone_classification_rows(
    audit: Dict[str, Any], function_name: str
) -> List[Dict[str, Any]]:
    return [
        row
        for row in audit.get("rewrite_candidates", [])
        if isinstance(row, dict)
        and (
            str(row.get("mir_function") or "") == function_name
            or str(row.get("mir_function") or "").endswith(f"::{function_name}")
        )
        and row.get("lowering_kind") in CALL_CLASSIFICATION_KINDS
    ]


def drop_classification_rows(
    audit: Dict[str, Any], function_name: str
) -> List[Dict[str, Any]]:
    return [
        row
        for row in audit.get("rewrite_candidates", [])
        if isinstance(row, dict)
        and (
            str(row.get("mir_function") or "") == function_name
            or str(row.get("mir_function") or "").endswith(f"::{function_name}")
        )
        and str(row.get("lowering_kind") or "").startswith("semantic_scope_drop")
    ]


def validate_clone_candidate_classification(audit: Dict[str, Any]) -> Dict[str, Any]:
    errors: List[str] = []

    single_heap_rows = clone_classification_rows(audit, "clone_single_heap")
    if len(single_heap_rows) != 1:
        errors.append(
            f"clone_single_heap expected one call-classification row, got {len(single_heap_rows)}"
        )
    else:
        row = single_heap_rows[0]
        if row.get("lowering_kind") != "semantic_scope_enter_exit_rewrite":
            errors.append(
                "clone_single_heap was not classified as a semantic-scope rewrite"
            )
        if row.get("rewrite_status") != "semantic_scope_enter_exit_rewrite_planned":
            errors.append("clone_single_heap did not remain a planned audit-only rewrite")
        semantic_object_type = str(row.get("semantic_object_type") or "")
        if "std::vec::Vec<u8" not in semantic_object_type:
            errors.append(
                "clone_single_heap did not resolve the sole nested Vec heap owner"
            )

    non_heap_counts: Dict[str, int] = {}
    for function_name in NON_HEAP_CLONE_FUNCTIONS:
        rows = clone_classification_rows(audit, function_name)
        non_heap_counts[function_name] = len(rows)
        if len(rows) != 1:
            errors.append(
                f"{function_name} expected one call-classification row, got {len(rows)}"
            )
            continue
        row = rows[0]
        if row.get("lowering_kind") != "semantic_scope_non_heap_object_skipped":
            errors.append(f"{function_name} was not classified as a non-heap skip")
        if row.get("rewrite_status") != "semantic_scope_rewrite_skipped_non_heap_object_type":
            errors.append(f"{function_name} did not record the non-heap skip status")

    reference_rows = clone_classification_rows(audit, "clone_nonowning_reference")
    if len(reference_rows) == 1:
        destination_type = str(reference_rows[0].get("destination_type") or "")
        if not destination_type.startswith("&") or "std::vec::Vec<u8" not in destination_type:
            errors.append(
                "clone_nonowning_reference did not preserve the borrowed Vec destination type"
            )

    nested_heap_rows = clone_classification_rows(audit, NESTED_HEAP_CLONE_FUNCTION)
    if len(nested_heap_rows) != 1:
        errors.append(
            f"{NESTED_HEAP_CLONE_FUNCTION} expected one call-classification row, "
            f"got {len(nested_heap_rows)}"
        )
    else:
        row = nested_heap_rows[0]
        if row.get("lowering_kind") != "semantic_scope_enter_exit_rewrite":
            errors.append(
                f"{NESTED_HEAP_CLONE_FUNCTION} did not resolve its stored Vec owner"
            )
        if "std::vec::Vec<u8" not in str(row.get("semantic_object_type") or ""):
            errors.append(
                f"{NESTED_HEAP_CLONE_FUNCTION} omitted its stored Vec owner type"
            )

    refcounted_single_owner_counts: Dict[str, int] = {}
    for function_name in REFCOUNTED_SINGLE_OWNER_CLONE_FUNCTIONS:
        rows = clone_classification_rows(audit, function_name)
        refcounted_single_owner_counts[function_name] = len(rows)
        if len(rows) != 1:
            errors.append(
                f"{function_name} expected one call-classification row, got {len(rows)}"
            )
            continue
        row = rows[0]
        if row.get("lowering_kind") != "semantic_scope_enter_exit_rewrite":
            errors.append(
                f"{function_name} did not resolve the Vec field as its sole allocation owner"
            )
        if row.get("rewrite_status") != "semantic_scope_enter_exit_rewrite_planned":
            errors.append(f"{function_name} did not remain a planned fixture rewrite")
        semantic_object_type = str(row.get("semantic_object_type") or "")
        if "std::vec::Vec<u8" not in semantic_object_type:
            errors.append(f"{function_name} omitted its sole Vec allocation owner")
        if "Arc<" in semantic_object_type or "Rc<" in semantic_object_type:
            errors.append(
                f"{function_name} incorrectly treated its reference-counted field as a new owner"
            )

    for function_name, marker in (
        ("clone_standalone_arc", "Arc<"),
        ("clone_standalone_rc", "Rc<"),
    ):
        rows = clone_classification_rows(audit, function_name)
        if len(rows) != 1:
            continue
        row = rows[0]
        if row.get("replacement_resolution_status") != "rustc_middle_no_supported_heap_owner_not_lowered":
            errors.append(f"{function_name} omitted the exact no-owner resolution")
        if row.get("metadata_pairing_contract") != "audit_only_no_supported_heap_owner":
            errors.append(f"{function_name} omitted the audit-only no-owner contract")
        if marker not in str(row.get("destination_type") or ""):
            errors.append(f"{function_name} omitted its reference-counted destination type")

    raw_pointer_rows = clone_classification_rows(audit, RAW_POINTER_CLONE_FUNCTION)
    if len(raw_pointer_rows) != 1:
        errors.append(
            f"{RAW_POINTER_CLONE_FUNCTION} expected one call-classification row, "
            f"got {len(raw_pointer_rows)}"
        )
    else:
        row = raw_pointer_rows[0]
        if row.get("lowering_kind") != "semantic_scope_unsolved_heap_object_candidate":
            errors.append(
                f"{RAW_POINTER_CLONE_FUNCTION} must remain fail-closed as unresolved"
            )
        if (
            row.get("rewrite_status")
            != "semantic_scope_rewrite_skipped_unresolved_heap_object_type"
        ):
            errors.append(
                f"{RAW_POINTER_CLONE_FUNCTION} omitted the unresolved-owner status"
            )

    const_generic_rows = clone_classification_rows(audit, CONST_GENERIC_CLONE_FUNCTION)
    if len(const_generic_rows) != 1:
        errors.append(
            f"{CONST_GENERIC_CLONE_FUNCTION} expected one call-classification row, "
            f"got {len(const_generic_rows)}"
        )
    else:
        row = const_generic_rows[0]
        if row.get("lowering_kind") != "semantic_scope_unsolved_heap_object_candidate":
            errors.append(
                f"{CONST_GENERIC_CLONE_FUNCTION} must remain fail-closed as unresolved"
            )
        if (
            row.get("rewrite_status")
            != "semantic_scope_rewrite_skipped_unresolved_heap_object_type"
        ):
            errors.append(
                f"{CONST_GENERIC_CLONE_FUNCTION} omitted the unresolved-parameter status"
            )
        destination_type = str(row.get("destination_type") or "")
        if "ConstGenericClone" not in destination_type:
            errors.append(
                f"{CONST_GENERIC_CLONE_FUNCTION} omitted its const-generic destination"
            )

    ambiguous_rows = clone_classification_rows(audit, "clone_ambiguous_result")
    if len(ambiguous_rows) != 1:
        errors.append(
            f"clone_ambiguous_result expected one call-classification row, got {len(ambiguous_rows)}"
        )
    else:
        row = ambiguous_rows[0]
        if row.get("lowering_kind") != "semantic_scope_unsolved_heap_object_candidate":
            errors.append("clone_ambiguous_result was not kept fail-closed as unsolved")
        if row.get("rewrite_status") != "semantic_scope_rewrite_skipped_ambiguous_heap_object_type":
            errors.append("clone_ambiguous_result did not record the ambiguous skip status")
        if (
            row.get("replacement_resolution_status")
            != "rustc_middle_multiple_heap_object_types_not_lowered"
        ):
            errors.append("clone_ambiguous_result did not record the multiple-owner reason")
        row_text = json.dumps(row, sort_keys=True)
        for owner in ("std::vec::Vec<u8", "std::string::String"):
            if owner not in row_text:
                errors.append(f"clone_ambiguous_result omitted owner {owner}")

    headers_rows = clone_classification_rows(
        audit, MULTI_OWNER_HEADERS_CLONE_FUNCTION
    )
    if len(headers_rows) != 1:
        errors.append(
            f"{MULTI_OWNER_HEADERS_CLONE_FUNCTION} expected one call-classification row, "
            f"got {len(headers_rows)}"
        )
    else:
        row = headers_rows[0]
        if row.get("lowering_kind") != "semantic_scope_unsolved_heap_object_candidate":
            errors.append(
                f"{MULTI_OWNER_HEADERS_CLONE_FUNCTION} was not kept fail-closed"
            )
        if row.get("rewrite_status") != "semantic_scope_rewrite_skipped_ambiguous_heap_object_type":
            errors.append(
                f"{MULTI_OWNER_HEADERS_CLONE_FUNCTION} omitted the ambiguous-owner status"
            )
        row_text = json.dumps(row, sort_keys=True)
        for owner in ("std::vec::Vec<u8", "std::string::String"):
            if owner not in row_text:
                errors.append(
                    f"{MULTI_OWNER_HEADERS_CLONE_FUNCTION} omitted owner {owner}"
                )

    summary = audit.get("summary") or {}
    non_heap_skipped = sum(
        1
        for function_name in NON_HEAP_CLONE_FUNCTIONS
        for row in clone_classification_rows(audit, function_name)
        if row.get("lowering_kind") == "semantic_scope_non_heap_object_skipped"
    )
    unsolved_rows = [
        row
        for row in audit.get("rewrite_candidates", [])
        if isinstance(row, dict)
        and row.get("lowering_kind")
        == "semantic_scope_unsolved_heap_object_candidate"
    ]
    unsolved = int(summary.get("semantic_scope_unsolved_candidate_count") or 0)
    if non_heap_skipped != len(NON_HEAP_CLONE_FUNCTIONS):
        errors.append(
            "non-heap skipped row count "
            f"expected {len(NON_HEAP_CLONE_FUNCTIONS)}, got {non_heap_skipped}"
        )
    if unsolved != len(unsolved_rows):
        errors.append(
            "semantic_scope_unsolved_candidate_count does not match row-level "
            f"evidence: summary={unsolved}, rows={len(unsolved_rows)}"
        )

    multi_owner_drop_counts: Dict[str, int] = {}
    for function_name in MULTI_OWNER_DROP_FUNCTIONS:
        rows = drop_classification_rows(audit, function_name)
        multi_owner_drop_counts[function_name] = len(rows)
        if not rows:
            errors.append(f"{function_name} has no aggregate Drop audit row")
            continue
        for row in rows:
            if row.get("lowering_kind") != "semantic_scope_drop_multiple_heap_owners_skipped":
                errors.append(f"{function_name} did not fail closed for multiple owners")
            if (
                row.get("rewrite_status")
                != "semantic_scope_drop_rewrite_skipped_multiple_heap_owners"
            ):
                errors.append(f"{function_name} has the wrong multi-owner Drop status")
            if (
                row.get("replacement_resolution_status")
                != "rustc_middle_drop_multiple_heap_owners_not_lowered"
            ):
                errors.append(f"{function_name} has the wrong multi-owner Drop resolution")
            if row.get("metadata_pairing_contract") != "audit_only_multiple_heap_owner_drop_type":
                errors.append(f"{function_name} has the wrong multi-owner Drop contract")
            row_text = json.dumps(row, sort_keys=True)
            for owner in ("std::vec::Vec<u8", "std::string::String"):
                if owner not in row_text:
                    errors.append(f"{function_name} omitted owner {owner}")

    if errors:
        raise AssertionError(
            "clone candidate classification failed:\n- " + "\n- ".join(errors)
        )
    return {
        "single_heap_scope_rows": len(single_heap_rows),
        "nested_heap_scope_rows": len(nested_heap_rows),
        "refcounted_single_owner_rows_by_function": refcounted_single_owner_counts,
        "refcounted_no_owner_rows_by_function": {
            function_name: len(clone_classification_rows(audit, function_name))
            for function_name in REFCOUNTED_NO_OWNER_CLONE_FUNCTIONS
        },
        "non_heap_rows_by_function": non_heap_counts,
        "non_heap_skipped_count": non_heap_skipped,
        "ambiguous_unsolved_count": len(ambiguous_rows),
        "headers_multi_owner_unsolved_count": len(headers_rows),
        "raw_pointer_unresolved_count": len(raw_pointer_rows),
        "const_generic_unresolved_count": len(const_generic_rows),
        "total_unsolved_count": unsolved,
        "multi_owner_drop_rows_by_function": multi_owner_drop_counts,
    }


def validate_actual_refcounted_clone_classification(
    audit: Dict[str, Any],
) -> Dict[str, Any]:
    errors: List[str] = []
    applied_counts: Dict[str, int] = {}
    for function_name in REFCOUNTED_SINGLE_OWNER_CLONE_FUNCTIONS:
        rows = clone_classification_rows(audit, function_name)
        applied_counts[function_name] = len(rows)
        if len(rows) != 1:
            errors.append(
                f"actual {function_name} expected one call-classification row, got {len(rows)}"
            )
            continue
        row = rows[0]
        if row.get("lowering_kind") != "semantic_scope_enter_exit_rewrite":
            errors.append(f"actual {function_name} was not a semantic-scope rewrite")
        if row.get("rewrite_status") != "actual_semantic_scope_enter_exit_rewrite_applied":
            errors.append(f"actual {function_name} did not apply the MIR rewrite")
        if not str(row.get("replacement_resolution_status") or "").startswith("resolved_unialloc_"):
            errors.append(f"actual {function_name} did not resolve the UniAlloc scope ABI")
        semantic_object_type = str(row.get("semantic_object_type") or "")
        if "std::vec::Vec<u8" not in semantic_object_type:
            errors.append(f"actual {function_name} omitted its sole Vec owner")
        if "Arc<" in semantic_object_type or "Rc<" in semantic_object_type:
            errors.append(
                f"actual {function_name} forged a reference-counted allocation owner"
            )

    skipped_counts: Dict[str, int] = {}
    for function_name in REFCOUNTED_NO_OWNER_CLONE_FUNCTIONS:
        rows = clone_classification_rows(audit, function_name)
        skipped_counts[function_name] = len(rows)
        if len(rows) != 1:
            errors.append(
                f"actual {function_name} expected one call-classification row, got {len(rows)}"
            )
            continue
        row = rows[0]
        if row.get("lowering_kind") != "semantic_scope_non_heap_object_skipped":
            errors.append(f"actual {function_name} was not an audit-only no-owner row")
        if row.get("rewrite_status") != "semantic_scope_rewrite_skipped_non_heap_object_type":
            errors.append(f"actual {function_name} did not skip semantic-scope lowering")
        if row.get("replacement_resolution_status") != "rustc_middle_no_supported_heap_owner_not_lowered":
            errors.append(f"actual {function_name} omitted the no-owner resolution")
        if row.get("metadata_pairing_contract") != "audit_only_no_supported_heap_owner":
            errors.append(f"actual {function_name} omitted the audit-only contract")

    if errors:
        raise AssertionError(
            "actual reference-counted Clone classification failed:\n- "
            + "\n- ".join(errors)
        )
    return {
        "actual_single_owner_rows_by_function": applied_counts,
        "actual_no_owner_rows_by_function": skipped_counts,
    }


def target_drop_or_deallocation_rows(
    audit: Dict[str, Any], marker: str
) -> List[Dict[str, Any]]:
    rows = audit.get("rewrite_candidates") or []
    assert isinstance(rows, list), "rewrite_candidates must be a list"
    matching: List[Dict[str, Any]] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        row_text = json.dumps(row, sort_keys=True)
        scope_text = " ".join(
            str(row.get(field) or "")
            for field in (
                "lowering_kind",
                "rewrite_status",
                "callee",
                "metadata_pairing_contract",
            )
        ).lower()
        if marker in row_text and ("drop" in scope_text or "dealloc" in scope_text):
            matching.append(row)
    return matching


def validate_allocation_side_recovery_requirement(
    audit: Dict[str, Any], runtime: Dict[str, Any]
) -> Dict[str, Any]:
    target_counts: Dict[str, int] = {}
    for marker in ("ProducerPayload", "ConsumerPayload"):
        rows = target_drop_or_deallocation_rows(audit, marker)
        for row in rows:
            assert row.get("lowering_kind") == "semantic_scope_enter_exit_rewrite", row
            assert row.get("rewrite_status") == (
                "actual_semantic_scope_enter_exit_rewrite_applied"
            ), row
            assert row.get("metadata_pairing_contract") in {
                "semantic_scope_active_metadata",
                "semantic_scope_drop_active_metadata",
            }, row
            assert int(row.get("type_id") or 0) != 0, row
            assert int(row.get("module_id") or 0) != 0, row
            assert int(row.get("flags") or 0) & TYPE_ISOLATED, row
            assert int(row.get("placement_hint") or 0) & CROSS_THREAD_RECOVERY, row
        target_counts[marker] = len(rows)
    matches = int(runtime.get("recovery_identity_matches") or 0)
    mismatches = int(runtime.get("recovery_identity_mismatches") or 0)
    assert mismatches == 0, "requested and recorded recovery identities disagreed"
    target_scope_count = sum(target_counts.values())
    if target_scope_count == 0:
        assert matches == 0, (
            "runtime reported exact requested-identity recovery without any "
            "target drop/deallocation scope"
        )
        mechanism = "allocation_side_recovery"
    else:
        assert matches == target_scope_count, (
            "every applied target drop/deallocation scope must produce one exact "
            "requested-identity recovery match"
        )
        mechanism = "exact_requested_identity"
    return {
        "pairing_mechanism": mechanism,
        "allocation_side_recovery_required": target_scope_count == 0,
        "producer_target_drop_or_deallocation_scope_rows": target_counts["ProducerPayload"],
        "consumer_target_drop_or_deallocation_scope_rows": target_counts["ConsumerPayload"],
        "recovery_identity_matches": matches,
        "recovery_identity_mismatches": mismatches,
    }


def validate_generic_drop_recovery_requirement(
    audit: Dict[str, Any], runtime: Dict[str, Any]
) -> Dict[str, Any]:
    helper_rows = [
        row
        for row in audit.get("rewrite_candidates") or []
        if (
            str(row.get("mir_function") or "") == GENERIC_DROP_FUNCTION
            or str(row.get("mir_function") or "").endswith(
                f"::{GENERIC_DROP_FUNCTION}"
            )
        )
    ]
    applied_or_planned = [
        row
        for row in helper_rows
        if row.get("lowering_kind") == "semantic_scope_enter_exit_rewrite"
        or "applied" in str(row.get("rewrite_status") or "")
        or "planned" in str(row.get("rewrite_status") or "")
    ]
    assert not applied_or_planned, (
        f"{GENERIC_DROP_FUNCTION} unexpectedly has an applied/planned semantic scope: "
        f"{applied_or_planned}"
    )
    skipped = [
        row
        for row in helper_rows
        if row.get("lowering_kind")
        == "semantic_scope_drop_generic_type_parameter_skipped"
    ]
    unresolved = [
        row
        for row in helper_rows
        if row.get("lowering_kind")
        == "semantic_scope_unsolved_heap_object_candidate"
    ]
    if skipped:
        assert not unresolved, (
            f"{GENERIC_DROP_FUNCTION} must use one audit-only representation, got "
            f"specialized={skipped}, unresolved={unresolved}"
        )
        assert len(skipped) == 1, (
            f"{GENERIC_DROP_FUNCTION} must have exactly one generic Drop<T> "
            f"audit-only skip, got {len(skipped)}"
        )
        row = skipped[0]
        assert (
            row.get("rewrite_status")
            == "semantic_scope_drop_rewrite_skipped_generic_type_parameter"
        ), f"{GENERIC_DROP_FUNCTION} has the wrong generic Drop rewrite status"
        assert (
            row.get("replacement_resolution_status")
            == "rustc_middle_drop_generic_type_parameter_not_lowered"
        ), f"{GENERIC_DROP_FUNCTION} has the wrong generic Drop resolution status"
        assert row.get("metadata_pairing_contract") == "audit_only_generic_drop_type", (
            f"{GENERIC_DROP_FUNCTION} must use the audit-only pairing contract"
        )
        assert row.get("semantic_object_type") == "<unknown-heap-object-type>", (
            f"{GENERIC_DROP_FUNCTION} must not invent a concrete heap-object identity"
        )
        assert str(row.get("destination_type") or "") == "T", (
            f"{GENERIC_DROP_FUNCTION} must audit the unresolved generic destination"
        )
        provider_exposure = "audit_only_generic_drop_skip"
    elif unresolved:
        assert len(unresolved) == 1, (
            f"{GENERIC_DROP_FUNCTION} must have exactly one unresolved audit-only "
            f"row, got {len(unresolved)}"
        )
        row = unresolved[0]
        assert row.get("rewrite_status") == (
            "semantic_scope_rewrite_skipped_unresolved_heap_object_type"
        ), row
        assert row.get("replacement_resolution_status") == (
            "rustc_middle_heap_object_type_not_solved"
        ), row
        assert row.get("metadata_pairing_contract") == (
            "audit_only_unresolved_heap_object_type"
        ), row
        assert row.get("semantic_object_type") == "<unknown-heap-object-type>", row
        assert "drop::<T>" in str(row.get("callee") or ""), row
        assert row.get("argument_types") == ["T"], row
        assert str(row.get("destination_type") or "") == "()", row
        provider_exposure = "audit_only_unresolved_generic_drop_skip"
    else:
        assert not helper_rows, (
            f"{GENERIC_DROP_FUNCTION} was exposed without the required generic Drop "
            f"audit-only row: {helper_rows}"
        )
        summary = audit.get("summary") or {}
        assert summary.get("provider_override_installed") is True, (
            "generic Drop provider-omission evidence requires the real optimized_mir "
            "provider override"
        )
        assert summary.get("body_clone_returned_to_rustc") is True, (
            "generic Drop provider-omission evidence requires returned cloned MIR bodies"
        )
        observed_drop_rows = [
            row
            for row in audit.get("rewrite_candidates") or []
            if isinstance(row, dict)
            and row.get("lowering_kind") == "semantic_scope_drop_rewrite"
            and not (
                str(row.get("mir_function") or "") == GENERIC_DROP_FUNCTION
                or str(row.get("mir_function") or "").endswith(
                    f"::{GENERIC_DROP_FUNCTION}"
                )
            )
        ]
        assert observed_drop_rows, (
            "generic Drop provider-omission evidence requires row-level actual Drop evidence"
        )
        unexpected_drop_statuses = [
            row
            for row in observed_drop_rows
            if row.get("rewrite_status")
            != "actual_semantic_scope_drop_rewrite_applied"
        ]
        assert not unexpected_drop_statuses, (
            "generic Drop provider-omission evidence requires exact applied Drop rows: "
            f"{unexpected_drop_statuses}"
        )
        candidate_count = int(
            summary.get("semantic_scope_drop_candidate_count") or 0
        )
        applied_count = int(
            summary.get("semantic_scope_drop_rewrite_applied_count") or 0
        )
        assert candidate_count == len(observed_drop_rows), (
            "generic Drop provider-omission candidate summary must match row-level "
            f"Drop evidence: summary={candidate_count}, rows={len(observed_drop_rows)}"
        )
        assert applied_count == len(observed_drop_rows), (
            "generic Drop provider-omission applied summary must match row-level "
            f"Drop evidence: summary={applied_count}, rows={len(observed_drop_rows)}"
        )
        assert (
            int(
                summary.get(
                    "semantic_scope_drop_generic_type_parameter_skipped_count"
                )
                or 0
            )
            == 0
        ), "generic Drop provider-omission evidence must not manufacture a skip count"
        provider_exposure = "not_exposed_by_optimized_mir_provider"

    expected_deltas = {
        "generic_drop_typed_deallocations_delta": 4,
        "generic_drop_fallback_deallocations_delta": 0,
        "generic_drop_typed_cache_inserts_delta": 4,
    }
    for field, expected in expected_deltas.items():
        actual = int(runtime.get(field) or 0)
        assert actual == expected, (
            f"generic Drop runtime delta {field} must be {expected}, got {actual}"
        )
    return {
        "generic_drop_recovery_validated": True,
        "generic_drop_audit_only_skip_count": len(skipped) + len(unresolved),
        "generic_drop_specialized_skip_count": len(skipped),
        "generic_drop_unresolved_skip_count": len(unresolved),
        "generic_drop_provider_exposure": provider_exposure,
        **expected_deltas,
    }


def unique_int(rows: List[Dict[str, Any]], field: str, label: str) -> int:
    values = {int(row.get(field) or 0) for row in rows}
    assert len(values) == 1, f"{label} must have one {field}, got {sorted(values)}"
    value = next(iter(values))
    assert value != 0, f"{label} {field} must be nonzero"
    return value


def aggregate_runtime_type(rows: List[Dict[str, Any]], type_id: int) -> Dict[str, int]:
    matching = [row for row in rows if int(row.get("type_id") or 0) == type_id]
    assert matching, f"runtime type rows omit compiler type_id {type_id}"
    totals = {
        "allocations": 0,
        "deallocations": 0,
        "cache_hits": 0,
        "cache_inserts": 0,
        "cache_bypasses": 0,
        "policy_flags_seen": 0,
    }
    for row in matching:
        for field in (
            "allocations",
            "deallocations",
            "cache_hits",
            "cache_inserts",
            "cache_bypasses",
        ):
            totals[field] += int(row.get(field) or 0)
        totals["policy_flags_seen"] |= int(row.get("policy_flags_seen") or 0)
        for size_field in ("observed_alloc_size", "observed_dealloc_size"):
            value = int(row.get(size_field) or 0)
            assert value in (0, 64), f"unexpected {size_field}={value} for type_id {type_id}"
    return totals


def validate(audit: Dict[str, Any], runtime: Dict[str, Any]) -> Dict[str, Any]:
    summary = audit.get("summary") or {}
    assert summary.get("provider_override_installed") is True
    assert summary.get("body_clone_returned_to_rustc") is True
    assert summary.get("actual_semantic_scope_rewrite") is True
    assert int(summary.get("semantic_scope_rewrite_applied_count") or 0) > 0
    assert int(summary.get("semantic_scope_drop_unsolved_candidate_count") or 0) == 0
    assert int(summary.get("cross_thread_recovery_hint_count") or 0) > 0
    fail_closed_factory_provenance = validate_fail_closed_factory_provenance(audit)
    refcounted_clone_classification = (
        validate_actual_refcounted_clone_classification(audit)
    )

    producer_rows = applied_type_rows(audit, "ProducerPayload")
    consumer_rows = applied_type_rows(audit, "ConsumerPayload")
    assert producer_rows, "audit has no applied ProducerPayload semantic scope"
    assert consumer_rows, "audit has no applied ConsumerPayload semantic scope"
    producer_type_id = unique_int(producer_rows, "type_id", "ProducerPayload")
    consumer_type_id = unique_int(consumer_rows, "type_id", "ConsumerPayload")
    assert producer_type_id != consumer_type_id
    producer_module_id = unique_int(producer_rows, "module_id", "ProducerPayload")
    consumer_module_id = unique_int(consumer_rows, "module_id", "ConsumerPayload")
    assert producer_module_id == consumer_module_id
    for row in producer_rows + consumer_rows:
        assert int(row.get("flags") or 0) & TYPE_ISOLATED
        assert int(row.get("placement_hint") or 0) & CROSS_THREAD_RECOVERY
        assert row.get("placement_hint_basis") == "manual_cross_thread_recovery_hint", row
    # These exact Box::new scopes live inside opaque wrapper functions. The
    # wrapper calls are deliberately fail-closed above, so the thread escape is
    # not an interprocedural proof for their bodies. Depending on rustc MIR
    # exposure, deallocation either has an exact requested identity or requires
    # the allocation-side record. The validator records which bounded mechanism
    # was exercised without pretending the pass proved the missing connection.
    recovery_requirement = validate_allocation_side_recovery_requirement(audit, runtime)
    generic_drop_recovery = validate_generic_drop_recovery_requirement(audit, runtime)

    assert runtime.get("wrong_type_reuse_blocked") is True
    assert runtime.get("producer_reuse_complete") is True
    assert int(runtime.get("same_layout_bytes") or 0) == 64
    producer_addresses = [int(value) for value in runtime.get("producer_addresses") or []]
    consumer_addresses = [int(value) for value in runtime.get("consumer_addresses") or []]
    recovered_addresses = [
        int(value) for value in runtime.get("recovered_producer_addresses") or []
    ]
    assert len(producer_addresses) == len(consumer_addresses) == len(recovered_addresses) == 4
    assert len(set(producer_addresses)) == 4
    assert len(set(consumer_addresses)) == 4
    assert len(set(recovered_addresses)) == 4
    assert set(producer_addresses).isdisjoint(consumer_addresses)
    assert set(recovered_addresses) == set(producer_addresses)
    assert int(runtime.get("typed_allocations") or 0) >= 12
    assert int(runtime.get("typed_deallocations") or 0) >= 12
    assert int(runtime.get("typed_cache_hits") or 0) >= 4
    assert int(runtime.get("typed_cache_inserts") or 0) >= 8
    assert int(runtime.get("semantic_type_stats_dropped_events") or 0) == 0
    assert int(runtime.get("side_cache_corrupt_slots") or 0) == 0

    runtime_rows = runtime.get("type_rows") or []
    assert isinstance(runtime_rows, list)
    producer_runtime = aggregate_runtime_type(runtime_rows, producer_type_id)
    consumer_runtime = aggregate_runtime_type(runtime_rows, consumer_type_id)
    assert producer_runtime["allocations"] >= 8
    assert producer_runtime["deallocations"] >= 8
    assert producer_runtime["cache_hits"] >= 4
    assert producer_runtime["cache_inserts"] >= 8
    assert producer_runtime["policy_flags_seen"] & TYPE_ISOLATED
    assert consumer_runtime["allocations"] >= 4
    assert consumer_runtime["deallocations"] >= 4
    assert consumer_runtime["cache_hits"] == 0
    assert consumer_runtime["cache_inserts"] >= 4
    assert consumer_runtime["policy_flags_seen"] & TYPE_ISOLATED

    return {
        "producer_type_id": producer_type_id,
        "consumer_type_id": consumer_type_id,
        "module_id": producer_module_id,
        "producer_applied_scope_rows": len(producer_rows),
        "consumer_applied_scope_rows": len(consumer_rows),
        "semantic_scope_rewrite_applied_count": int(
            summary.get("semantic_scope_rewrite_applied_count") or 0
        ),
        "semantic_scope_drop_rewrite_applied_count": int(
            summary.get("semantic_scope_drop_rewrite_applied_count") or 0
        ),
        "cross_thread_recovery_hint_count": int(
            summary.get("cross_thread_recovery_hint_count") or 0
        ),
        "producer_runtime": producer_runtime,
        "consumer_runtime": consumer_runtime,
        "recovery_requirement": recovery_requirement,
        "generic_drop_recovery": generic_drop_recovery,
        "fail_closed_factory_provenance": fail_closed_factory_provenance,
        "refcounted_clone_classification": refcounted_clone_classification,
    }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", type=Path)
    parser.add_argument("--toolchain")
    parser.add_argument("--timeout", type=int, default=300)
    parser.add_argument("--fixed-heap", action="store_true")
    return parser.parse_args()


def main() -> int:
    if not __debug__:
        raise SystemExit("do not run this assertion-based validator with python -O")
    args = parse_args()
    toolchain = args.toolchain or (ROOT / "rust-toolchain").read_text(encoding="utf-8").strip()
    output_dir = (
        args.output_dir.resolve()
        if args.output_dir
        else default_output_dir()
    )
    reject_output_inside_repo(output_dir)
    rustc = shutil.which("rustc") or "rustc"
    cargo = shutil.which("cargo") or "cargo"
    source_binding_start = source_binding_snapshot(toolchain, rustc)
    output_dir.mkdir(parents=True, exist_ok=True)
    if any(output_dir.iterdir()):
        raise SystemExit(f"output directory must be empty: {output_dir}")
    rewrites_dir = output_dir / "rewrites"
    logs_dir = output_dir / "logs"
    target_dir = output_dir / "cargo-target"
    rewrites_dir.mkdir()
    logs_dir.mkdir()
    target_dir.mkdir()

    sysroot = str(source_binding_start["sysroot"])
    pass_binary = output_dir / "unialloc-rustc-mir-rewrite-dry-run"
    build_env = os.environ.copy()
    build_env["RUSTC_BOOTSTRAP"] = "1"
    build = run_logged(
        [
            rustc,
            f"+{toolchain}",
            *current_rustc_cfg(toolchain),
            str(PASS_SOURCE),
            "-o",
            str(pass_binary),
        ],
        label="pass-build",
        output_dir=output_dir,
        timeout=args.timeout,
        env=build_env,
    )
    if build["returncode"] != 0:
        raise SystemExit(f"pass build failed; see {build['stderr']}")

    clone_classification = compile_clone_classification_fixture(
        pass_binary=pass_binary,
        sysroot=sysroot,
        output_dir=output_dir,
        timeout=args.timeout,
    )
    clone_classification_validation = validate_clone_candidate_classification(
        clone_classification["audit"]
    )

    run_env = os.environ.copy()
    for variable in ("DYLD_LIBRARY_PATH", "LD_LIBRARY_PATH"):
        current = run_env.get(variable)
        run_env[variable] = f"{sysroot}/lib" + (os.pathsep + current if current else "")
    run_env.update(
        {
            "RUSTC_WRAPPER": str(pass_binary),
            "UNIALLOC_RUSTC_TARGET_CRATES": PROBE_NAME,
            "UNIALLOC_REWRITE_AUDIT_DIR": str(rewrites_dir),
            "UNIALLOC_PASS_LOG_DIR": str(logs_dir),
            "UNIALLOC_CONTINUE_COMPILATION": "1",
            "UNIALLOC_ACTUAL_MIR_REWRITE": "1",
            "UNIALLOC_ACTUAL_SEMANTIC_SCOPE_REWRITE": "1",
            "UNIALLOC_LOWERING_AUTO_CROSS_THREAD_HINT": "1",
            "UNIALLOC_LOWERING_POLICY_FLAGS": str(TYPE_ISOLATED),
            "UNIALLOC_LOWERING_PLACEMENT_HINT": str(CROSS_THREAD_RECOVERY),
            "UNIALLOC_RUSTC_SYSROOT": sysroot,
            "CARGO_NET_OFFLINE": "true",
            "CARGO_INCREMENTAL": "0",
            "CARGO_TARGET_DIR": str(target_dir),
        }
    )
    features = ["stats", "type_isolation"]
    if args.fixed_heap:
        features.append("fixed_heap")
    run = run_logged(
        [
            cargo,
            f"+{toolchain}",
            "run",
            "--quiet",
            "-p",
            "unialloc",
            "--bin",
            PROBE_NAME,
            "--features",
            ",".join(features),
        ],
        label="probe-run",
        output_dir=output_dir,
        timeout=args.timeout,
        env=run_env,
    )
    if run["returncode"] != 0:
        raise SystemExit(f"probe run failed; see {run['stderr']}")

    audit_paths = sorted(rewrites_dir.glob("*.json"))
    if len(audit_paths) != 1:
        raise SystemExit(f"expected one target audit, found {len(audit_paths)} in {rewrites_dir}")
    audit_path = audit_paths[0]
    audit = json.loads(audit_path.read_text(encoding="utf-8"))
    runtime = load_runtime_event(Path(run["stdout"]))
    validation = validate(audit, runtime)
    shutil.rmtree(target_dir)
    source_binding_end = source_binding_snapshot(toolchain, rustc)
    assert_source_binding_stable(source_binding_start, source_binding_end)

    summary = {
        "schema_version": 1,
        "source": "mir_type_isolation_security_probe_summary",
        "validated": True,
        "fixed_heap": args.fixed_heap,
        "toolchain": toolchain,
        "rustc": source_binding_start["rustc_verbose_version"],
        "sysroot": sysroot,
        "git_head": source_binding_start["git_head"],
        "git_status": source_binding_start["git_status"],
        "source_binding": {
            "start": source_binding_start,
            "end": source_binding_end,
            "drift_checked": True,
            "commit_bound": True,
        },
        "features": features,
        "build": build,
        "clone_candidate_classification": {
            "run": clone_classification["run"],
            "validation": clone_classification_validation,
            "artifacts": {
                "fixture_source": str(CLONE_CLASSIFICATION_SOURCE),
                "fixture_source_sha256": sha256(CLONE_CLASSIFICATION_SOURCE),
                "fixture_binary": str(clone_classification["binary_path"]),
                "fixture_binary_sha256": sha256(clone_classification["binary_path"]),
                "rewrite_audit": str(clone_classification["audit_path"]),
                "rewrite_audit_sha256": sha256(clone_classification["audit_path"]),
            },
        },
        "run": run,
        "artifacts": {
            "pass_source": str(PASS_SOURCE),
            "pass_source_sha256": sha256(PASS_SOURCE),
            "pass_binary": str(pass_binary),
            "pass_binary_sha256": sha256(pass_binary),
            "probe_source": str(PROBE_SOURCE),
            "probe_source_sha256": sha256(PROBE_SOURCE),
            "rewrite_audit": str(audit_path),
            "rewrite_audit_sha256": sha256(audit_path),
        },
        "validation": validation,
        "runtime": runtime,
        "boundaries": [
            "Functional security probe only; no timing or paper-performance claim.",
            "Address values vary by process; the deterministic assertions are set disjointness and complete unique producer reuse.",
            "The source contains no manual semantic metadata or allocation ABI calls; identities come from actual rustc_driver semantic-scope rewriting.",
            "The runner supplies the TYPE_ISOLATED policy and cross-thread-recovery placement bit uniformly so the compared cache keys differ only by compiler-derived type identity.",
            "The target lifecycle must use one of two audited pairing mechanisms: exact applied target drop/deallocation scopes with one runtime recovery match per scope, or no target scope and zero matches with allocation-side recovery. Both require zero identity mismatches; the selected mechanism and counts are recorded in validation.",
            "The generic_drop helper must never receive an applied/planned semantic scope. If optimized_mir exposes its generic body, the audit requires exactly one fail-closed row: either the specialized generic Drop<T> skip or the older unresolved-heap-object representation. Current rustc may instead codegen the monomorphized instance without exposing a helper row, which is reported as a provider boundary rather than a manufactured skip. Every representation still requires exact four-object typed deallocation/cache-insert deltas with zero fallback.",
            "Commit-bound evidence requires a clean HEAD, a repository-external output directory, and identical scoped source/toolchain bindings before and after the run.",
            "This covers two same-layout Rust types and one worker lifecycle, not universal UAF prevention or unmodified-toolchain deployment.",
        ],
    }
    summary_path = output_dir / "summary.json"
    summary_path.write_text(json.dumps(summary, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(json.dumps({"summary": str(summary_path), "validated": True}, sort_keys=True))
    return 0


if __name__ == "__main__":
    sys.exit(main())
