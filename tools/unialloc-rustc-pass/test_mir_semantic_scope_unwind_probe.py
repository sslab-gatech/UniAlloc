#!/usr/bin/env python3
"""Run the compiler-driven nested semantic-scope unwind regression."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
from typing import Any, Dict, Iterable, List


ROOT = Path(__file__).resolve().parents[2]
RUNNER_SOURCE = Path(__file__).resolve()
PASS_SOURCE = ROOT / "tools/unialloc-rustc-pass/unialloc-rustc-mir-rewrite-dry-run.rs"
PROBE_NAME = "rustc_driver_mir_semantic_scope_unwind_probe"
PROBE_SOURCE = ROOT / "unialloc/src/bin" / f"{PROBE_NAME}.rs"
TYPE_ISOLATED = 0x1
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
    RUNNER_SOURCE.relative_to(ROOT),
)


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
        tempfile.mkdtemp(prefix="unialloc-mir-nested-unwind-", dir=str(parent))
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
    drift = {
        key: {"start": start.get(key), "end": end.get(key)}
        for key in start
        if start.get(key) != end.get(key)
    }
    assert not drift, (
        "source/toolchain binding drifted during probe: "
        f"{json.dumps(drift, sort_keys=True)}"
    )


def current_rustc_cfg(toolchain: str) -> List[str]:
    normalized = toolchain.lstrip("+").strip()
    if normalized == "nightly" or normalized.startswith(("nightly-2025", "nightly-2026")):
        return ["--cfg", "unialloc_rustc_current"]
    return []


def load_runtime_event(stdout_path: Path) -> Dict[str, Any]:
    for raw in stdout_path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line.startswith("{"):
            continue
        value = json.loads(line)
        if isinstance(value, dict) and value.get("source") == PROBE_NAME:
            return value
    raise AssertionError(f"missing {PROBE_NAME} JSON event in {stdout_path}")


def applied_rows(audit: Dict[str, Any], marker: str) -> List[Dict[str, Any]]:
    return [
        row
        for row in audit.get("rewrite_candidates", [])
        if isinstance(row, dict)
        and marker in str(row.get("semantic_object_type") or "")
        and row.get("rewrite_status")
        in {
            "actual_semantic_scope_enter_exit_rewrite_applied",
            "actual_semantic_scope_drop_rewrite_applied",
        }
    ]


def unique_type_id(rows: List[Dict[str, Any]], label: str) -> int:
    values = {int(row.get("type_id") or 0) for row in rows}
    assert len(values) == 1, f"{label} must have one type_id, got {sorted(values)}"
    value = next(iter(values))
    assert value != 0, f"{label} type_id must be nonzero"
    return value


def aggregate_runtime_type(rows: List[Dict[str, Any]], type_id: int) -> Dict[str, int]:
    matching = [row for row in rows if int(row.get("type_id") or 0) == type_id]
    assert matching, f"runtime type rows omit compiler type_id {type_id}"
    totals = {
        "allocations": 0,
        "allocated_bytes": 0,
        "deallocations": 0,
        "policy_flags_seen": 0,
    }
    for row in matching:
        totals["allocations"] += int(row.get("allocations") or 0)
        totals["allocated_bytes"] += int(row.get("allocated_bytes") or 0)
        totals["deallocations"] += int(row.get("deallocations") or 0)
        totals["policy_flags_seen"] |= int(row.get("policy_flags_seen") or 0)
    return totals


def validate_audit(audit: Dict[str, Any]) -> Dict[str, Any]:
    summary = audit.get("summary") or {}
    assert summary.get("provider_override_installed") is True
    assert summary.get("body_clone_returned_to_rustc") is True
    assert summary.get("actual_semantic_scope_rewrite") is True
    assert int(summary.get("semantic_scope_unwind_pop_inserted_count") or 0) >= 1

    inner_rows = [
        row
        for row in applied_rows(audit, "Vec<PanicOnClone")
        if "trigger_panicking_vec_extend" in str(row.get("mir_function") or "")
        and "extend_from_slice" in str(row.get("callee") or "")
        and row.get("lowering_kind") == "semantic_scope_enter_exit_rewrite"
    ]
    assert inner_rows, "missing applied inner Vec::extend_from_slice scope"
    assert any(
        row.get("semantic_scope_unwind_pop_inserted") is True
        and row.get("metadata_pairing_contract") == "semantic_scope_active_metadata"
        for row in inner_rows
    ), "inner Vec scope has no compiler-inserted unwind pop"

    box_rows = applied_rows(audit, "Box<PostUnwindPayload")
    outer_rows = [
        row
        for row in box_rows
        if "outer_box_after_caught_panic" in str(row.get("callee") or "")
        and row.get("lowering_kind") == "semantic_scope_enter_exit_rewrite"
    ]
    allocation_rows = [
        row
        for row in box_rows
        if "Box" in str(row.get("callee") or "")
        and "new" in str(row.get("callee") or "")
        and row.get("lowering_kind") == "semantic_scope_enter_exit_rewrite"
    ]
    drop_rows = [
        row
        for row in box_rows
        if row.get("lowering_kind") == "semantic_scope_drop_rewrite"
    ]
    assert outer_rows, "outer Box-returning call did not receive a semantic scope"
    assert allocation_rows, "post-unwind Box::new did not receive a semantic scope"
    assert drop_rows, "post-unwind Box Drop did not receive a semantic scope"

    box_type_id = unique_type_id(outer_rows + allocation_rows + drop_rows, "post-unwind Box")
    for row in inner_rows + outer_rows + allocation_rows + drop_rows:
        assert int(row.get("flags") or 0) & TYPE_ISOLATED
    return {
        "inner_unwind_rows": len(inner_rows),
        "outer_scope_rows": len(outer_rows),
        "post_unwind_allocation_rows": len(allocation_rows),
        "post_unwind_drop_rows": len(drop_rows),
        "post_unwind_box_type_id": box_type_id,
    }


def validate_runtime(runtime: Dict[str, Any], box_type_id: int) -> Dict[str, Any]:
    assert runtime.get("panic_observed") is True
    for prefix in ("initial", "post_unwind", "final"):
        assert int(runtime.get(f"{prefix}_main_depth") or 0) == 0
        assert int(runtime.get(f"{prefix}_overflow_depth") or 0) == 0
        assert int(runtime.get(f"{prefix}_represented_depth") or 0) == 0
    assert int(runtime.get("restored_outer_main_depth") or 0) == 1
    assert int(runtime.get("restored_outer_overflow_depth") or 0) == 0
    assert int(runtime.get("restored_outer_represented_depth") or 0) == 1
    assert int(runtime.get("post_unwind_address") or 0) != 0
    assert int(runtime.get("post_unwind_checksum") or 0) != 0
    assert int(runtime.get("post_alloc_total_allocations") or 0) == 1
    assert int(runtime.get("post_alloc_typed_allocations") or 0) == 1
    assert int(runtime.get("post_alloc_fallback_allocations") or 0) == 0
    assert int(runtime.get("post_drop_typed_deallocations") or 0) == 1
    assert int(runtime.get("post_drop_fallback_deallocations") or 0) == 0
    assert int(runtime.get("recovery_identity_mismatches") or 0) == 0
    assert int(runtime.get("side_cache_corrupt_slots") or 0) == 0

    type_rows = runtime.get("type_rows") or []
    assert isinstance(type_rows, list)
    aggregate = aggregate_runtime_type(type_rows, box_type_id)
    assert aggregate["allocations"] == 1
    assert aggregate["allocated_bytes"] == 64
    assert aggregate["deallocations"] == 1
    assert aggregate["policy_flags_seen"] & TYPE_ISOLATED
    return {"post_unwind_box_runtime": aggregate}


def validate(audit: Dict[str, Any], runtime: Dict[str, Any]) -> Dict[str, Any]:
    audit_evidence = validate_audit(audit)
    runtime_evidence = validate_runtime(
        runtime, int(audit_evidence["post_unwind_box_type_id"])
    )
    return {"audit": audit_evidence, "runtime": runtime_evidence}


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
    output_dir = args.output_dir.resolve() if args.output_dir else default_output_dir()
    reject_output_inside_repo(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    if any(output_dir.iterdir()):
        raise SystemExit(f"output directory must be empty: {output_dir}")

    rustc = shutil.which("rustc") or "rustc"
    cargo = shutil.which("cargo") or "cargo"
    source_binding_start = source_binding_snapshot(toolchain, rustc)
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
            "UNIALLOC_LOWERING_POLICY_FLAGS": str(TYPE_ISOLATED),
            "UNIALLOC_RUSTC_SYSROOT": sysroot,
            "CARGO_NET_OFFLINE": "true",
            "CARGO_INCREMENTAL": "0",
            # The workspace's normal dev profile intentionally uses panic=abort.
            # This functional probe must exercise MIR cleanup edges and catch the
            # panic, so opt only this isolated Cargo invocation into unwind.
            "CARGO_PROFILE_DEV_PANIC": "unwind",
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
        raise SystemExit(
            f"expected one target audit, found {len(audit_paths)} in {rewrites_dir}"
        )
    audit_path = audit_paths[0]
    audit = json.loads(audit_path.read_text(encoding="utf-8"))
    runtime = load_runtime_event(Path(run["stdout"]))
    validation = validate(audit, runtime)
    shutil.rmtree(target_dir)
    source_binding_end = source_binding_snapshot(toolchain, rustc)
    assert_source_binding_stable(source_binding_start, source_binding_end)

    summary = {
        "schema_version": 1,
        "source": "mir_semantic_scope_nested_unwind_probe_summary",
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
            "Functional nested-unwind regression only; no timing or paper-performance claim.",
            "The Rust source uses ordinary Vec, Box, catch_unwind, and Drop behavior with no manual metadata allocator ABI calls.",
            "This adds the previously missing invariant that an inner compiler scope unwind restores a still-active outer compiler scope before a subsequent allocation/drop pair.",
            "The isolated probe overrides the workspace dev panic strategy to unwind; normal project profiles remain unchanged.",
            "One hosted and one fixed-heap process cover this bounded lifecycle, not arbitrary panics, payloads, or allocator clients.",
        ],
    }
    summary_path = output_dir / "summary.json"
    summary_path.write_text(
        json.dumps(summary, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    print(json.dumps({"summary": str(summary_path), "validated": True}, sort_keys=True))
    return 0


if __name__ == "__main__":
    sys.exit(main())
