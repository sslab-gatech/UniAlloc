#!/usr/bin/env python3
"""Run and validate the compiler-driven Vec realloc identity probe."""

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
PROBE_NAME = "rustc_driver_mir_vec_realloc_identity_probe"
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
        tempfile.mkdtemp(prefix="unialloc-mir-vec-realloc-", dir=str(parent))
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


def applied_type_rows(audit: Dict[str, Any], marker: str) -> List[Dict[str, Any]]:
    return [
        row
        for row in audit.get("rewrite_candidates", [])
        if isinstance(row, dict)
        and marker in str(row.get("semantic_object_type") or "")
        and row.get("rewrite_status")
        in (
            "actual_semantic_scope_enter_exit_rewrite_applied",
            "actual_semantic_scope_drop_rewrite_applied",
        )
    ]


def unique_int(rows: List[Dict[str, Any]], field: str, label: str) -> int:
    values = {int(row.get(field) or 0) for row in rows}
    assert len(values) == 1, f"{label} must have one {field}, got {sorted(values)}"
    value = next(iter(values))
    assert value != 0, f"{label} {field} must be nonzero"
    return value


def matching_callee_rows(rows: List[Dict[str, Any]], marker: str) -> List[Dict[str, Any]]:
    return [row for row in rows if marker in str(row.get("callee") or "")]


def drop_rows(rows: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    return [
        row
        for row in rows
        if row.get("lowering_kind") == "semantic_scope_drop_rewrite"
        and row.get("rewrite_status") == "actual_semantic_scope_drop_rewrite_applied"
    ]


def validate_positive_control_direct_rewrites(audit: Dict[str, Any]) -> Dict[str, Any]:
    function_name = "direct_allocator_rewrite_positive_control"
    rows = [
        row
        for row in audit.get("rewrite_candidates", [])
        if isinstance(row, dict) and row.get("mir_function") == function_name
    ]
    assert len(rows) == 2, (
        f"{function_name} must have exactly two direct rewrite rows "
        f"(alloc and dealloc), got {len(rows)}"
    )
    for row in rows:
        assert row.get("lowering_kind") == "direct_allocator_call_rewrite", (
            f"{function_name} lowering_kind must be direct_allocator_call_rewrite"
        )
        assert row.get("rewrite_status") == (
            "actual_allocator_call_replacement_applied"
        ), f"{function_name} rewrite_status must prove an applied replacement"

    expected_resolutions = {
        "__unialloc_alloc_layout_with_metadata_hints": (
            "resolved_unialloc_alloc_layout_with_metadata_hints"
        ),
        "__unialloc_dealloc_layout_with_metadata_hints": (
            "resolved_unialloc_dealloc_layout_with_metadata_hints"
        ),
    }
    by_symbol = {str(row.get("replacement_symbol") or ""): row for row in rows}
    assert set(by_symbol) == set(expected_resolutions), (
        f"{function_name} replacement symbols must be the resolved UniAlloc "
        f"alloc/dealloc metadata ABI pair, got {sorted(by_symbol)}"
    )
    for symbol, expected_resolution in expected_resolutions.items():
        row = by_symbol[symbol]
        assert row.get("replacement_resolution_status") == expected_resolution, (
            f"{function_name} {symbol} resolution must be {expected_resolution}"
        )
        assert int(row.get("flags") or 0) & TYPE_ISOLATED
        assert int(row.get("placement_hint") or 0) & CROSS_THREAD_RECOVERY

    type_id = unique_int(rows, "type_id", function_name)
    module_id = unique_int(rows, "module_id", function_name)
    object_types = {str(row.get("semantic_object_type") or "") for row in rows}
    assert len(object_types) == 1 and "" not in object_types, (
        f"{function_name} alloc/dealloc rows must share one semantic object type"
    )
    return {
        "function": function_name,
        "row_count": len(rows),
        "type_id": type_id,
        "module_id": module_id,
        "semantic_object_type": next(iter(object_types)),
        "replacement_symbols": sorted(by_symbol),
    }


def aggregate_runtime_type(rows: List[Dict[str, Any]], type_id: int) -> Dict[str, Any]:
    matching = [row for row in rows if int(row.get("type_id") or 0) == type_id]
    assert matching, f"runtime type rows omit compiler type_id {type_id}"
    totals: Dict[str, Any] = {
        "allocations": 0,
        "allocated_bytes": 0,
        "deallocations": 0,
        "cache_hits": 0,
        "cache_inserts": 0,
        "cache_bypasses": 0,
        "policy_flags_seen": 0,
        "observed_alloc_sizes": [],
        "observed_dealloc_sizes": [],
    }
    alloc_sizes = set()
    dealloc_sizes = set()
    for row in matching:
        for field in (
            "allocations",
            "allocated_bytes",
            "deallocations",
            "cache_hits",
            "cache_inserts",
            "cache_bypasses",
        ):
            totals[field] += int(row.get(field) or 0)
        totals["policy_flags_seen"] |= int(row.get("policy_flags_seen") or 0)
        if int(row.get("observed_alloc_size") or 0):
            alloc_sizes.add(int(row["observed_alloc_size"]))
        if int(row.get("observed_dealloc_size") or 0):
            dealloc_sizes.add(int(row["observed_dealloc_size"]))
    totals["observed_alloc_sizes"] = sorted(alloc_sizes)
    totals["observed_dealloc_sizes"] = sorted(dealloc_sizes)
    return totals


def validate(audit: Dict[str, Any], runtime: Dict[str, Any]) -> Dict[str, Any]:
    summary = audit.get("summary") or {}
    assert summary.get("provider_override_installed") is True
    assert summary.get("body_clone_returned_to_rustc") is True
    assert summary.get("actual_allocator_call_replacement_requested") is True, (
        "actual_allocator_call_replacement_requested must be true"
    )
    assert summary.get("actual_allocator_call_replacement") is True, (
        "actual_allocator_call_replacement must be true"
    )
    assert int(summary.get("rewrite_applied_count") or 0) >= 2
    assert summary.get("actual_semantic_scope_rewrite_requested") is True, (
        "actual_semantic_scope_rewrite_requested must be true"
    )
    assert summary.get("actual_semantic_scope_rewrite") is True
    assert int(summary.get("semantic_scope_rewrite_applied_count") or 0) > 0
    assert int(summary.get("semantic_scope_unsolved_candidate_count") or 0) == 0
    assert int(summary.get("semantic_scope_drop_unsolved_candidate_count") or 0) == 0
    assert int(summary.get("cross_thread_recovery_hint_count") or 0) > 0

    direct_rewrite_evidence = validate_positive_control_direct_rewrites(audit)

    producer_rows = applied_type_rows(audit, "Vec<ProducerPayload")
    consumer_rows = applied_type_rows(audit, "Vec<ConsumerPayload")
    assert producer_rows, "audit has no applied Vec<ProducerPayload> scope"
    assert consumer_rows, "audit has no applied Vec<ConsumerPayload> scope"
    producer_type_id = unique_int(producer_rows, "type_id", "Vec<ProducerPayload>")
    consumer_type_id = unique_int(consumer_rows, "type_id", "Vec<ConsumerPayload>")
    assert producer_type_id != consumer_type_id
    producer_module_id = unique_int(producer_rows, "module_id", "Vec<ProducerPayload>")
    consumer_module_id = unique_int(consumer_rows, "module_id", "Vec<ConsumerPayload>")
    assert producer_module_id == consumer_module_id

    producer_alloc_rows = matching_callee_rows(producer_rows, "with_capacity")
    producer_growth_rows = matching_callee_rows(producer_rows, "reserve_exact")
    producer_push_rows = matching_callee_rows(producer_rows, "push")
    producer_drop_rows = drop_rows(producer_rows)
    consumer_alloc_rows = matching_callee_rows(consumer_rows, "with_capacity")
    consumer_drop_rows = drop_rows(consumer_rows)
    assert producer_alloc_rows, "Vec<ProducerPayload> allocation scope was not rewritten"
    assert producer_growth_rows, "Vec<ProducerPayload> reserve_exact scope was not rewritten"
    assert producer_push_rows, "Vec<ProducerPayload> push scope was not rewritten"
    assert producer_drop_rows, "Vec<ProducerPayload> Drop scope was not rewritten"
    assert consumer_alloc_rows, "Vec<ConsumerPayload> allocation scope was not rewritten"
    assert consumer_drop_rows, "Vec<ConsumerPayload> Drop scope was not rewritten"
    for row in producer_rows + consumer_rows:
        assert int(row.get("flags") or 0) & TYPE_ISOLATED
        assert int(row.get("placement_hint") or 0) & CROSS_THREAD_RECOVERY

    for field in (
        "direct_allocator_rewrite_positive_control",
        "allocated_on_creator_thread",
        "transferred_to_distinct_worker",
        "transferred_buffer_verified_before_growth",
        "growth_completed_on_worker_thread",
        "drop_completed_on_worker_thread",
    ):
        assert runtime.get(field) is True, f"{field} must be true"
    allocation_type_id = int(runtime.get("allocation_type_id") or 0)
    growth_type_id = int(runtime.get("growth_type_id") or 0)
    assert allocation_type_id == growth_type_id, (
        "growth_type_id must preserve allocation_type_id across the thread transfer: "
        f"allocation={allocation_type_id}, growth={growth_type_id}"
    )
    assert allocation_type_id == producer_type_id, (
        "runtime allocation_type_id must equal compiler producer type_id: "
        f"runtime={allocation_type_id}, compiler={producer_type_id}"
    )

    assert runtime.get("capacity_growth_forced") is True
    assert runtime.get("wrong_type_reuse_blocked") is True
    assert runtime.get("producer_buffer_recovered") is True
    assert int(runtime.get("same_layout_element_bytes") or 0) == 64
    assert int(runtime.get("final_capacity") or 0) > int(runtime.get("initial_capacity") or 0)
    assert int(runtime.get("growth_typed_allocations") or 0) == 1
    assert int(runtime.get("growth_typed_deallocations") or 0) in (0, 1)
    assert int(runtime.get("growth_raw_realloc_no_metadata") or 0) == 0
    assert int(runtime.get("growth_recorded_old_metadata_fallback") or 0) == 0
    assert int(runtime.get("growth_recovery_identity_mismatches") or 0) == 0, (
        "growth_recovery_identity_mismatches must be zero"
    )
    assert int(runtime.get("drop_typed_deallocations") or 0) == 1, (
        "drop_typed_deallocations must be exactly one"
    )
    assert int(runtime.get("drop_raw_dealloc_no_metadata") or 0) == 0, (
        "drop_raw_dealloc_no_metadata must be zero"
    )
    assert int(runtime.get("drop_recovery_identity_mismatches") or 0) == 0, (
        "drop_recovery_identity_mismatches must be zero"
    )

    producer_buffer = int(runtime.get("producer_final_buffer") or 0)
    consumer_buffer = int(runtime.get("consumer_buffer") or 0)
    recovered_buffer = int(runtime.get("recovered_producer_buffer") or 0)
    assert producer_buffer != 0 and consumer_buffer != 0 and recovered_buffer != 0
    assert producer_buffer != consumer_buffer
    assert recovered_buffer == producer_buffer
    assert int(runtime.get("typed_allocations") or 0) >= 4
    assert int(runtime.get("typed_deallocations") or 0) >= 3
    assert int(runtime.get("typed_cache_hits") or 0) >= 1
    assert int(runtime.get("semantic_type_stats_dropped_events") or 0) == 0
    assert int(runtime.get("recovery_identity_mismatches") or 0) == 0
    assert int(runtime.get("side_cache_corrupt_slots") or 0) == 0

    runtime_rows = runtime.get("type_rows") or []
    assert isinstance(runtime_rows, list)
    producer_runtime = aggregate_runtime_type(runtime_rows, producer_type_id)
    consumer_runtime = aggregate_runtime_type(runtime_rows, consumer_type_id)
    assert producer_runtime["allocations"] >= 3
    assert producer_runtime["deallocations"] >= 2
    assert producer_runtime["cache_hits"] >= 1
    assert producer_runtime["cache_inserts"] >= 2
    assert producer_runtime["policy_flags_seen"] & TYPE_ISOLATED
    assert consumer_runtime["allocations"] >= 1
    assert consumer_runtime["deallocations"] >= 1
    assert consumer_runtime["cache_hits"] == 0
    assert consumer_runtime["cache_inserts"] >= 1
    assert consumer_runtime["policy_flags_seen"] & TYPE_ISOLATED

    return {
        "producer_type_id": producer_type_id,
        "consumer_type_id": consumer_type_id,
        "module_id": producer_module_id,
        "allocation_type_id": allocation_type_id,
        "growth_type_id": growth_type_id,
        "cross_thread_growth_proven": True,
        "worker_drop_pairing_proven": True,
        "producer_allocation_scope_rows": len(producer_alloc_rows),
        "producer_growth_scope_rows": len(producer_growth_rows),
        "producer_push_scope_rows": len(producer_push_rows),
        "producer_drop_scope_rows": len(producer_drop_rows),
        "consumer_allocation_scope_rows": len(consumer_alloc_rows),
        "consumer_drop_scope_rows": len(consumer_drop_rows),
        "direct_allocator_rewrite_positive_control": direct_rewrite_evidence,
        "producer_runtime": producer_runtime,
        "consumer_runtime": consumer_runtime,
        "growth_moved_buffer_observed": bool(runtime.get("growth_moved_buffer")),
        "recovery_identity_matches_observed": int(
            runtime.get("recovery_identity_matches") or 0
        ),
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
        "source": "mir_vec_realloc_identity_probe_summary",
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
            "Functional Vec realloc and type-isolation probe only; no timing or paper-performance claim.",
            "The primary lifecycle uses ordinary Vec operations and no manual metadata or allocator ABI calls.",
            "A separate standard alloc/dealloc positive control, executed after the Vec evidence snapshots, proves direct allocator-call replacement without contributing to the lifecycle counters.",
            "The compiler evidence is an actual rustc optimized-MIR provider override with both direct allocator-call replacement and applied semantic-scope rewrites around allocator-causing Vec operations; liballoc internals are not rebuilt by this target-crate probe.",
            "The initial Vec allocation occurs on the creator thread; the distinct worker verifies the transferred buffer, grows it through typed realloc, and performs its typed Drop/deallocation.",
            "Capacity growth is required; whether the allocator moves the buffer is reported as an observation, not a pass condition.",
            "The deterministic isolation invariant is worker-local final-buffer recovery by Vec<ProducerPayload> and non-reuse by same-layout Vec<ConsumerPayload> after the cross-thread realloc/drop lifecycle.",
            "This invocation covers one hosted or fixed-heap lifecycle according to the fixed_heap field, not universal container or allocator behavior.",
        ],
    }
    summary_path = output_dir / "summary.json"
    summary_path.write_text(json.dumps(summary, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(json.dumps({"summary": str(summary_path), "validated": True}, sort_keys=True))
    return 0


if __name__ == "__main__":
    sys.exit(main())
