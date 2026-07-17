#!/usr/bin/env python3
"""Run and validate the real-rustc same-class nonzero realloc shrink probe."""

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
EVALUATION_SCRIPT_DIR = ROOT / "evaluation" / "scripts"
if str(EVALUATION_SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(EVALUATION_SCRIPT_DIR))

import rustc_pass_source_closure as pass_closure  # noqa: E402

RUNNER_SOURCE = Path(__file__).resolve()
PASS_SOURCE = ROOT / pass_closure.PASS_ENTRYPOINT_RELATIVE_PATH
PROBE_NAME = "rustc_driver_direct_allocator_mir_probe"
PROBE_SOURCE = ROOT / "unialloc/src/bin" / f"{PROBE_NAME}.rs"
HELPER = "run_realloc_shrink_same_class_lifecycle"
TYPE_ISOLATED = 0x1
SOURCE_BINDING_PATHS = (
    Path("Cargo.toml"), Path("Cargo.lock"), Path("rust-toolchain"),
    Path("alloc_macros/Cargo.toml"), Path("alloc_macros/src"),
    Path("unialloc/Cargo.toml"), Path("unialloc/build.rs"), Path("unialloc/src"),
    *[Path(path) for path in pass_closure.source_closure_relative_paths(ROOT)], RUNNER_SOURCE.relative_to(ROOT),
)


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def command_text(command: Iterable[str]) -> str:
    return " ".join(command)


def run_logged(command: List[str], *, label: str, output_dir: Path, timeout: int,
               env: Dict[str, str]) -> Dict[str, Any]:
    stdout_path = output_dir / f"{label}.stdout.txt"
    stderr_path = output_dir / f"{label}.stderr.txt"
    try:
        result = subprocess.run(command, cwd=ROOT, env=env, text=True,
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                timeout=timeout, check=False)
        returncode, timed_out = result.returncode, False
        stdout, stderr = result.stdout, result.stderr
    except subprocess.TimeoutExpired as exc:
        returncode, timed_out = 124, True
        stdout, stderr = exc.stdout or "", exc.stderr or ""
    stdout_path.write_text(stdout, encoding="utf-8")
    stderr_path.write_text(stderr, encoding="utf-8")
    return {"command": command, "command_text": command_text(command),
            "returncode": returncode, "timed_out": timed_out,
            "stdout": str(stdout_path), "stderr": str(stderr_path)}


def checked_output(command: List[str]) -> str:
    return subprocess.check_output(command, cwd=ROOT, text=True).strip()


def reject_output_inside_repo(output_dir: Path) -> None:
    resolved, root = output_dir.resolve(), ROOT.resolve()
    if resolved == root or root in resolved.parents:
        raise AssertionError(f"output directory must be outside repository: {resolved}")


def default_output_dir() -> Path:
    parent = Path(tempfile.gettempdir()).resolve()
    reject_output_inside_repo(parent)
    return Path(tempfile.mkdtemp(prefix="unialloc-mir-realloc-shrink-", dir=str(parent))).resolve()


def scoped_source_hashes() -> Dict[str, str]:
    files: List[Path] = []
    for relative in SOURCE_BINDING_PATHS:
        path = ROOT / relative
        assert path.exists(), f"source-binding path is missing: {relative}"
        files.extend(child for child in path.rglob("*") if child.is_file()) if path.is_dir() else files.append(path)
    return {str(path.relative_to(ROOT)): sha256(path) for path in sorted(set(files))}


def source_binding_snapshot(toolchain: str, rustc: str) -> Dict[str, Any]:
    status = checked_output(["git", "status", "--short"])
    assert not status, f"commit-bound evidence requires a clean working tree:\n{status}"
    hashes = scoped_source_hashes()
    digest = hashlib.sha256()
    for path, value in sorted(hashes.items()):
        digest.update(path.encode()); digest.update(b"\0"); digest.update(value.encode()); digest.update(b"\n")
    return {"git_head": checked_output(["git", "rev-parse", "HEAD"]), "git_status": status,
            "clean_head": True, "toolchain": toolchain,
            "rustc_verbose_version": checked_output([rustc, f"+{toolchain}", "-Vv"]),
            "sysroot": checked_output([rustc, f"+{toolchain}", "--print", "sysroot"]),
            "scoped_file_hashes": hashes, "scoped_fingerprint_sha256": digest.hexdigest()}


def assert_source_binding_stable(start: Dict[str, Any], end: Dict[str, Any]) -> None:
    drift = {key: {"start": start.get(key), "end": end.get(key)} for key in start if start.get(key) != end.get(key)}
    assert not drift, f"source/toolchain binding drifted during probe: {json.dumps(drift, sort_keys=True)}"


def current_rustc_cfg(toolchain: str) -> List[str]:
    normalized = toolchain.lstrip("+").strip()
    return ["--cfg", "unialloc_rustc_current"] if normalized == "nightly" or normalized.startswith(("nightly-2025", "nightly-2026")) else []


def load_runtime_event(stdout_path: Path) -> Dict[str, Any]:
    for raw in stdout_path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if line.startswith("{"):
            value = json.loads(line)
            if isinstance(value, dict) and value.get("source") == PROBE_NAME:
                return value
    raise AssertionError(f"missing {PROBE_NAME} JSON event in {stdout_path}")


def helper_rows(audit: Dict[str, Any]) -> List[Dict[str, Any]]:
    return [row for row in audit.get("rewrite_candidates", []) if isinstance(row, dict)
            and str(row.get("mir_function") or "").endswith(HELPER)
            and row.get("lowering_kind") == "direct_allocator_call_rewrite"]


def operation(row: Dict[str, Any]) -> str:
    symbol = str(row.get("replacement_symbol") or "")
    if "__unialloc_realloc_layout_with_metadata" in symbol: return "realloc"
    if "__unialloc_dealloc_layout_with_metadata" in symbol: return "dealloc"
    if "__unialloc_alloc_layout_with_metadata" in symbol: return "alloc"
    return ""


def validate(audit: Dict[str, Any], runtime: Dict[str, Any]) -> Dict[str, Any]:
    summary = audit.get("summary") or {}
    assert summary.get("provider_override_installed") is True
    assert summary.get("body_clone_returned_to_rustc") is True
    assert summary.get("actual_allocator_call_replacement_requested") is True
    assert summary.get("actual_allocator_call_replacement") is True

    rows = helper_rows(audit)
    assert len(rows) == 3, f"{HELPER} must have exactly three direct rewrite rows, got {len(rows)}"
    by_operation: Dict[str, Dict[str, Any]] = {}
    for row in rows:
        assert row.get("lowering_kind") == "direct_allocator_call_rewrite", "helper lowering_kind"
        assert row.get("rewrite_status") == "actual_allocator_call_replacement_applied", "helper rewrite_status"
        op = operation(row)
        assert op and op not in by_operation, f"helper replacement symbols: {rows}"
        symbol = str(row.get("replacement_symbol") or "")
        assert row.get("replacement_resolution_status") == "resolved" + symbol[1:], "helper resolution"
        by_operation[op] = row
    assert set(by_operation) == {"alloc", "realloc", "dealloc"}, f"helper operations: {by_operation}"
    callsites = {int(row.get("callsite") or 0) for row in rows}
    assert len(callsites) == 3 and 0 not in callsites, f"helper callsite provenance: {sorted(callsites)}"
    alloc_row = by_operation["alloc"]
    assert int(alloc_row.get("type_id") or 0) != 0, "unresolved alloc fallback identity"
    assert int(alloc_row.get("module_id") or 0) != 0, "unresolved alloc module identity"
    assert int(alloc_row.get("flags") or 0) & TYPE_ISOLATED, "unresolved alloc policy"
    assert alloc_row.get("type_id_basis") == "direct_allocator_callsite_key", "alloc fallback basis"
    assert alloc_row.get("replacement_symbol") == "__unialloc_alloc_layout_with_metadata", "alloc recovery ABI"
    for op in ("realloc", "dealloc"):
        row = by_operation[op]
        assert int(row.get("type_id") or 0) == 0, f"{op} delegated type_id"
        assert int(row.get("module_id") or 0) == 0, f"{op} delegated module_id"
        assert int(row.get("flags") or 0) == 0, f"{op} delegated flags"
        assert int(row.get("lifetime_hint") or 0) == 0, f"{op} delegated lifetime_hint"
        assert int(row.get("placement_hint") or 0) == 0, f"{op} delegated placement_hint"
        assert row.get("type_id_basis") == "direct_allocator_recovery_delegated", f"{op} delegated basis"
        assert row.get("replacement_symbol") == f"__unialloc_{op}_layout_with_metadata", f"{op} recovery ABI"
        assert not str(row.get("replacement_symbol") or "").endswith("_local"), f"{op} local ABI forbidden"

    shrink = runtime.get("realloc_shrink")
    assert isinstance(shrink, dict) and shrink.get("enabled") is True, "realloc_shrink enabled"
    assert int(shrink.get("old_size") or 0) == 63
    assert int(shrink.get("new_size") or 0) == 57
    assert int(shrink.get("align") or 0) == 64
    assert int(shrink.get("old_pointer") or 0) != 0
    assert int(shrink.get("new_pointer") or 0) == int(shrink.get("old_pointer") or 0), "pointer_reused"
    assert shrink.get("pointer_reused") is True, "pointer_reused"
    assert shrink.get("pointer_aligned") is True, "pointer_aligned"
    assert int(shrink.get("new_pointer") or 0) % 64 == 0, "pointer alignment"
    assert shrink.get("payload_preserved") is True, "payload_preserved"
    assert int(shrink.get("realloc_typed_allocations") or 0) == 1
    assert int(shrink.get("realloc_typed_deallocations") or 0) == 1
    assert int(shrink.get("final_typed_deallocations") or 0) == 1
    assert int(shrink.get("realloc_fallback_allocations") or 0) == 0
    assert int(shrink.get("realloc_raw_realloc_no_metadata") or 0) == 0
    assert int(shrink.get("final_raw_dealloc_no_metadata") or 0) == 0
    assert int(shrink.get("recovery_identity_matches") or 0) == 0
    assert int(shrink.get("recovery_identity_mismatches") or 0) == 0
    assert int(shrink.get("side_cache_corrupt_slots") or 0) == 0
    assert shrink.get("recovery_record_after_final_dealloc") is False

    runtime_rows = shrink.get("type_rows")
    assert isinstance(runtime_rows, list), "realloc_shrink type_rows"
    runtime_by_callsite = {int(row.get("callsite") or 0): row for row in runtime_rows if isinstance(row, dict)}
    assert len(runtime_by_callsite) == 3, f"runtime helper identity rows: {runtime_rows}"
    expected = {
        "alloc": (1, 1, 63, 63),
        "realloc": (1, 0, 57, 0),
        "dealloc": (0, 1, 0, 57),
    }
    compiler_type = int(alloc_row["type_id"]); compiler_module = int(alloc_row["module_id"])
    for op, row in by_operation.items():
        observed = runtime_by_callsite.get(int(row["callsite"]))
        assert observed is not None, f"missing runtime {op} callsite row"
        assert int(observed.get("type_id") or 0) == compiler_type, f"runtime {op} type_id"
        assert int(observed.get("module_id") or 0) == compiler_module, f"runtime {op} module_id"
        allocations, deallocations, alloc_size, dealloc_size = expected[op]
        assert int(observed.get("allocations") or 0) == allocations, f"runtime {op} allocations"
        assert int(observed.get("deallocations") or 0) == deallocations, f"runtime {op} deallocations"
        assert int(observed.get("observed_alloc_size") or 0) == alloc_size, f"runtime {op} alloc size"
        assert int(observed.get("observed_dealloc_size") or 0) == dealloc_size, f"runtime {op} dealloc size"
        if alloc_size: assert int(observed.get("observed_alloc_align") or 0) == 64, f"runtime {op} alloc align"
        if dealloc_size: assert int(observed.get("observed_dealloc_align") or 0) == 64, f"runtime {op} dealloc align"
        assert int(observed.get("policy_flags_seen") or 0) & TYPE_ISOLATED
    return {"compiler_type_id": compiler_type, "compiler_module_id": compiler_module,
            "callsites": {op: int(row["callsite"]) for op, row in by_operation.items()}}


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", type=Path); parser.add_argument("--toolchain")
    parser.add_argument("--timeout", type=int, default=300); parser.add_argument("--fixed-heap", action="store_true")
    return parser.parse_args()


def main() -> int:
    if not __debug__: raise SystemExit("do not run this assertion-based validator with python -O")
    args = parse_args(); toolchain = args.toolchain or (ROOT / "rust-toolchain").read_text().strip()
    output_dir = args.output_dir.resolve() if args.output_dir else default_output_dir()
    reject_output_inside_repo(output_dir); output_dir.mkdir(parents=True, exist_ok=True)
    if any(output_dir.iterdir()): raise SystemExit(f"output directory must be empty: {output_dir}")
    rustc, cargo = shutil.which("rustc") or "rustc", shutil.which("cargo") or "cargo"
    start = source_binding_snapshot(toolchain, rustc); sysroot = str(start["sysroot"])
    rewrites, logs, target = output_dir / "rewrites", output_dir / "logs", output_dir / "cargo-target"
    rewrites.mkdir(); logs.mkdir(); target.mkdir(); pass_binary = output_dir / "unialloc-rustc-pass"
    build_env = os.environ.copy(); build_env["RUSTC_BOOTSTRAP"] = "1"
    build = run_logged([rustc, f"+{toolchain}", *current_rustc_cfg(toolchain), str(PASS_SOURCE), "-o", str(pass_binary)],
                       label="pass-build", output_dir=output_dir, timeout=args.timeout, env=build_env)
    if build["returncode"] != 0: raise SystemExit(f"pass build failed; see {build['stderr']}")
    run_env = os.environ.copy()
    for variable in ("DYLD_LIBRARY_PATH", "LD_LIBRARY_PATH"):
        current = run_env.get(variable); run_env[variable] = f"{sysroot}/lib" + (os.pathsep + current if current else "")
    run_env.update({"RUSTC_WRAPPER": str(pass_binary), "UNIALLOC_RUSTC_TARGET_CRATES": PROBE_NAME,
                    "UNIALLOC_REWRITE_AUDIT_DIR": str(rewrites), "UNIALLOC_PASS_LOG_DIR": str(logs),
                    "UNIALLOC_CONTINUE_COMPILATION": "1", "UNIALLOC_ACTUAL_MIR_REWRITE": "1",
                    "UNIALLOC_LOWERING_POLICY_FLAGS": "1", "UNIALLOC_RUSTC_SYSROOT": sysroot,
                    "UNIALLOC_REALLOC_SHRINK_PROBE": "1", "CARGO_NET_OFFLINE": "true",
                    "CARGO_INCREMENTAL": "0", "CARGO_TARGET_DIR": str(target)})
    features = ["stats", "type_isolation"] + (["fixed_heap"] if args.fixed_heap else [])
    run = run_logged([cargo, f"+{toolchain}", "run", "--quiet", "-p", "unialloc", "--bin", PROBE_NAME,
                      "--features", ",".join(features)], label="probe-run", output_dir=output_dir,
                     timeout=args.timeout, env=run_env)
    if run["returncode"] != 0: raise SystemExit(f"probe run failed; see {run['stderr']}")
    audit_paths = sorted(rewrites.glob("*.json"))
    if len(audit_paths) != 1: raise SystemExit(f"expected one target audit, found {len(audit_paths)}")
    audit = json.loads(audit_paths[0].read_text()); runtime = load_runtime_event(Path(run["stdout"])); validation = validate(audit, runtime)
    shutil.rmtree(target); end = source_binding_snapshot(toolchain, rustc); assert_source_binding_stable(start, end)
    summary = {"schema_version": 1, "source": "mir_realloc_shrink_probe_summary", "validated": True,
               "fixed_heap": args.fixed_heap, "toolchain": toolchain, "git_head": start["git_head"],
               "source_binding": {"start": start, "end": end, "drift_checked": True, "commit_bound": True},
               "features": features, "build": build, "run": run, "validation": validation, "runtime": runtime,
               "artifacts": {"pass_source": str(PASS_SOURCE), "pass_source_sha256": sha256(PASS_SOURCE),
            "pass_source_closure": pass_closure.source_closure_provenance(ROOT),
                             "pass_binary": str(pass_binary), "pass_binary_sha256": sha256(pass_binary),
                             "probe_source": str(PROBE_SOURCE), "probe_source_sha256": sha256(PROBE_SOURCE),
                             "rewrite_audit": str(audit_paths[0]), "rewrite_audit_sha256": sha256(audit_paths[0])},
               "boundaries": ["Functional actual-rustc regression only; no benchmark or paper claim.",
                              "Uses contract-valid nonzero realloc shrink sizes 63 to 57 with alignment 64."]}
    summary_path = output_dir / "summary.json"; summary_path.write_text(json.dumps(summary, indent=2, sort_keys=True) + "\n")
    print(json.dumps({"summary": str(summary_path), "validated": True}, sort_keys=True)); return 0

if __name__ == "__main__": sys.exit(main())
