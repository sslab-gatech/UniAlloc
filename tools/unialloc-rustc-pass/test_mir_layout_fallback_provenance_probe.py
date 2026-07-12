#!/usr/bin/env python3
"""Run the real-rustc Layout fallback provenance regression."""

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
PROBE_NAME = "rustc_driver_mir_layout_fallback_provenance_probe"
PROBE_SOURCE = ROOT / "unialloc/src/bin" / f"{PROBE_NAME}.rs"
UNKNOWN_HEAP_OBJECT_TYPE = "<unknown-heap-object-type>"
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
        returncode = result.returncode
        timed_out = False
        stdout = result.stdout
        stderr = result.stderr
    except subprocess.TimeoutExpired as exc:
        returncode = 124
        timed_out = True
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
        tempfile.mkdtemp(prefix="unialloc-mir-layout-fallback-", dir=str(parent))
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


def direct_rows(audit: Dict[str, Any], function: str) -> List[Dict[str, Any]]:
    return [
        row
        for row in audit.get("rewrite_candidates", [])
        if isinstance(row, dict)
        and row.get("lowering_kind") == "direct_allocator_call_rewrite"
        and str(row.get("mir_function") or "").endswith(function)
    ]


def rows_for_symbol(rows: List[Dict[str, Any]], marker: str) -> List[Dict[str, Any]]:
    return [row for row in rows if marker in str(row.get("replacement_symbol") or "")]


def aggregate_runtime_type(rows: List[Dict[str, Any]], type_id: int) -> Dict[str, Any]:
    matching = [row for row in rows if int(row.get("type_id") or 0) == type_id]
    assert matching, f"runtime type rows omit compiler type_id {type_id}"
    return {
        "allocations": sum(int(row.get("allocations") or 0) for row in matching),
        "deallocations": sum(int(row.get("deallocations") or 0) for row in matching),
        "alloc_sizes": sorted(
            {
                int(row.get("observed_alloc_size") or 0)
                for row in matching
                if int(row.get("observed_alloc_size") or 0)
            }
        ),
        "alloc_aligns": sorted(
            {
                int(row.get("observed_alloc_align") or 0)
                for row in matching
                if int(row.get("observed_alloc_align") or 0)
            }
        ),
        "dealloc_sizes": sorted(
            {
                int(row.get("observed_dealloc_size") or 0)
                for row in matching
                if int(row.get("observed_dealloc_size") or 0)
            }
        ),
        "callsites": sorted({int(row.get("callsite") or 0) for row in matching}),
        "module_ids": sorted({int(row.get("module_id") or 0) for row in matching}),
    }


def validate(audit: Dict[str, Any], runtime: Dict[str, Any]) -> Dict[str, Any]:
    summary = audit.get("summary") or {}
    assert summary.get("provider_override_installed") is True
    assert summary.get("body_clone_returned_to_rustc") is True
    assert summary.get("actual_allocator_call_replacement") is True
    assert int(summary.get("direct_layout_allocator_rewrite_applied_count") or 0) >= 3

    positive = direct_rows(audit, "run_expect_positive_control")
    fallback = direct_rows(audit, "run_fallback_negative_control")
    positive_alloc = rows_for_symbol(positive, "__unialloc_alloc_layout_with_metadata")
    positive_dealloc = rows_for_symbol(positive, "__unialloc_dealloc_layout_with_metadata")
    fallback_alloc = rows_for_symbol(fallback, "__unialloc_alloc_layout_with_metadata")
    assert len(positive_alloc) == 1, positive
    assert len(positive_dealloc) == 1, positive
    assert len(fallback_alloc) == 1, fallback

    for row in positive_alloc + positive_dealloc + fallback_alloc:
        assert row.get("rewrite_status") == "actual_allocator_call_replacement_applied"

    positive_type_id = int(positive_alloc[0].get("type_id") or 0)
    assert positive_type_id != 0
    assert int(positive_dealloc[0].get("type_id") or 0) == positive_type_id
    assert "[u64; 4" in str(positive_alloc[0].get("semantic_object_type") or "")
    assert positive_alloc[0].get("type_id_basis") != "direct_allocator_callsite_key"

    fallback_row = fallback_alloc[0]
    fallback_type_id = int(fallback_row.get("type_id") or 0)
    assert fallback_type_id != 0
    assert fallback_row.get("semantic_object_type") == UNKNOWN_HEAP_OBJECT_TYPE
    assert fallback_row.get("type_id_basis") == "direct_allocator_callsite_key"
    assert fallback_type_id != positive_type_id, (
        "fallback-producing Layout inherited the positive-control type identity"
    )

    assert runtime.get("manual_metadata_abi_calls") is False
    assert int(runtime.get("positive_layout_size") or 0) == 32
    assert int(runtime.get("positive_layout_align") or 0) == 64
    assert int(runtime.get("fallback_layout_size") or 0) == 37
    assert int(runtime.get("fallback_layout_align") or 0) == 1
    assert int(runtime.get("fallback_pointer") or 0) != 0
    assert int(runtime.get("positive_checksum") or 0) != 0
    assert int(runtime.get("fallback_checksum") or 0) != 0
    assert int(runtime.get("typed_allocations") or 0) == 2
    assert int(runtime.get("typed_deallocations") or 0) == 1
    assert int(runtime.get("fallback_allocations") or 0) == 0
    assert int(runtime.get("recovery_identity_mismatches") or 0) == 0
    assert int(runtime.get("side_cache_corrupt_slots") or 0) == 0

    runtime_rows = runtime.get("type_rows") or []
    assert isinstance(runtime_rows, list)
    positive_runtime = aggregate_runtime_type(runtime_rows, positive_type_id)
    fallback_runtime = aggregate_runtime_type(runtime_rows, fallback_type_id)
    assert positive_runtime["allocations"] == 1
    assert positive_runtime["deallocations"] == 1
    assert positive_runtime["alloc_sizes"] == [32]
    assert positive_runtime["alloc_aligns"] == [64]
    assert positive_runtime["dealloc_sizes"] == [32]
    assert fallback_runtime["allocations"] == 1
    assert fallback_runtime["deallocations"] == 0
    assert fallback_runtime["alloc_sizes"] == [37]
    assert fallback_runtime["alloc_aligns"] == [1]
    assert int(positive_alloc[0].get("callsite") or 0) in positive_runtime["callsites"]
    assert int(positive_dealloc[0].get("callsite") or 0) in positive_runtime["callsites"]
    assert int(fallback_row.get("callsite") or 0) in fallback_runtime["callsites"]
    assert positive_runtime["module_ids"] == fallback_runtime["module_ids"]

    return {
        "positive_type_id": positive_type_id,
        "fallback_callsite_type_id": fallback_type_id,
        "positive_runtime": positive_runtime,
        "fallback_runtime": fallback_runtime,
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
    output_dir = args.output_dir.resolve() if args.output_dir else default_output_dir()
    reject_output_inside_repo(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    if any(output_dir.iterdir()):
        raise SystemExit(f"output directory must be empty: {output_dir}")

    rustc = shutil.which("rustc") or "rustc"
    cargo = shutil.which("cargo") or "cargo"
    source_binding_start = source_binding_snapshot(toolchain, rustc)
    sysroot = str(source_binding_start["sysroot"])
    rewrites_dir = output_dir / "rewrites"
    logs_dir = output_dir / "logs"
    target_dir = output_dir / "cargo-target"
    rewrites_dir.mkdir()
    logs_dir.mkdir()
    target_dir.mkdir()
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
            "UNIALLOC_LOWERING_POLICY_FLAGS": "1",
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
        raise SystemExit(f"expected one target audit, found {len(audit_paths)}")
    audit = json.loads(audit_paths[0].read_text(encoding="utf-8"))
    runtime = load_runtime_event(Path(run["stdout"]))
    validation = validate(audit, runtime)

    shutil.rmtree(target_dir)
    source_binding_end = source_binding_snapshot(toolchain, rustc)
    assert_source_binding_stable(source_binding_start, source_binding_end)
    summary = {
        "schema_version": 1,
        "source": "mir_layout_fallback_provenance_probe_summary",
        "validated": True,
        "fixed_heap": args.fixed_heap,
        "toolchain": toolchain,
        "git_head": source_binding_start["git_head"],
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
            "rewrite_audit": str(audit_paths[0]),
            "rewrite_audit_sha256": sha256(audit_paths[0]),
        },
        "validation": validation,
        "runtime": runtime,
        "boundaries": [
            "Functional compiler-provenance regression only; no benchmark or paper claim.",
            "Result::expect is the positive control; unwrap_or_else synthesizing a different Layout is the negative control.",
            "The negative allocation intentionally remains live so a second callsite fallback cannot obscure the tested identity.",
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
