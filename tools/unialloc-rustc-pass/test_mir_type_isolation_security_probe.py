#!/usr/bin/env python3
"""Run and validate the deterministic MIR type-isolation security probe."""

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
PASS_SOURCE = ROOT / "tools/unialloc-rustc-pass/unialloc-rustc-mir-rewrite-dry-run.rs"
PROBE_NAME = "rustc_driver_mir_type_isolation_security_probe"
PROBE_SOURCE = ROOT / "unialloc/src/bin" / f"{PROBE_NAME}.rs"
TYPE_ISOLATED = 0x1
CROSS_THREAD_RECOVERY = 0x8000


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


def applied_type_rows(audit: Dict[str, Any], marker: str) -> List[Dict[str, Any]]:
    return [
        row
        for row in audit.get("rewrite_candidates", [])
        if isinstance(row, dict)
        and marker in str(row.get("semantic_object_type") or "")
        and row.get("lowering_kind") == "semantic_scope_enter_exit_rewrite"
        and row.get("rewrite_status") == "actual_semantic_scope_enter_exit_rewrite_applied"
    ]


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
    assert int(summary.get("semantic_scope_unsolved_candidate_count") or 0) == 0
    assert int(summary.get("semantic_scope_drop_unsolved_candidate_count") or 0) == 0
    assert int(summary.get("cross_thread_recovery_hint_count") or 0) > 0

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
    assert any(
        row.get("placement_hint_basis") == "manual_and_auto_cross_thread_escape"
        for row in producer_rows
    ), "producer allocation scopes did not record the MIR-visible thread escape"

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
    assert int(runtime.get("recovery_identity_mismatches") or 0) == 0
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
        else Path(tempfile.mkdtemp(prefix="unialloc-mir-typeiso-security-")).resolve()
    )
    output_dir.mkdir(parents=True, exist_ok=True)
    if any(output_dir.iterdir()):
        raise SystemExit(f"output directory must be empty: {output_dir}")
    rewrites_dir = output_dir / "rewrites"
    logs_dir = output_dir / "logs"
    target_dir = output_dir / "cargo-target"
    rewrites_dir.mkdir()
    logs_dir.mkdir()
    target_dir.mkdir()

    rustc = shutil.which("rustc") or "rustc"
    cargo = shutil.which("cargo") or "cargo"
    sysroot = checked_output([rustc, f"+{toolchain}", "--print", "sysroot"])
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

    summary = {
        "schema_version": 1,
        "source": "mir_type_isolation_security_probe_summary",
        "validated": True,
        "fixed_heap": args.fixed_heap,
        "toolchain": toolchain,
        "rustc": checked_output([rustc, f"+{toolchain}", "-Vv"]),
        "sysroot": sysroot,
        "git_head": checked_output(["git", "rev-parse", "HEAD"]),
        "git_status": checked_output(["git", "status", "--short"]),
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
            "Functional security probe only; no timing or paper-performance claim.",
            "Address values vary by process; the deterministic assertions are set disjointness and complete unique producer reuse.",
            "The source contains no manual semantic metadata or allocation ABI calls; identities come from actual rustc_driver semantic-scope rewriting.",
            "The runner supplies the TYPE_ISOLATED policy and cross-thread-recovery placement bit uniformly so the compared cache keys differ only by compiler-derived type identity.",
            "This covers two same-layout Rust types and one worker lifecycle, not universal UAF prevention or unmodified-toolchain deployment.",
        ],
    }
    summary_path = output_dir / "summary.json"
    summary_path.write_text(json.dumps(summary, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(json.dumps({"summary": str(summary_path), "validated": True}, sort_keys=True))
    return 0


if __name__ == "__main__":
    sys.exit(main())
