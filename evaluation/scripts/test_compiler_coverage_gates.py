#!/usr/bin/env python3
"""Regression tests for C002 compiler-coverage claim-grade gates."""

from __future__ import annotations

import importlib.util
import contextlib
import copy
import io
import json
import os
import pathlib
import sys
import tempfile
import threading
import time
import types
import unittest
from unittest import mock


ROOT = pathlib.Path(__file__).resolve().parents[2]
EVALUATE_PATH = ROOT / "evaluation" / "scripts" / "evaluate.py"
DOCKER_DRIVER_PATH = ROOT / "evaluation" / "scripts" / "paper_collections_docker_driver.py"


spec = importlib.util.spec_from_file_location("unialloc_evaluate", EVALUATE_PATH)
assert spec is not None and spec.loader is not None
evaluate = importlib.util.module_from_spec(spec)
spec.loader.exec_module(evaluate)

docker_driver_spec = importlib.util.spec_from_file_location("paper_collections_docker_driver", DOCKER_DRIVER_PATH)
assert docker_driver_spec is not None and docker_driver_spec.loader is not None
paper_collections_docker_driver = importlib.util.module_from_spec(docker_driver_spec)
docker_driver_spec.loader.exec_module(paper_collections_docker_driver)


def write_json(path: pathlib.Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8")


@contextlib.contextmanager
def temporary_eval_results_and_raw(results: pathlib.Path):
    """Point evaluate.py at a temp results/raw pair for claim-gate fixtures."""

    raw = results.parent / "raw"
    raw.mkdir(parents=True, exist_ok=True)
    old_results = evaluate.RESULTS
    old_raw = evaluate.RAW
    try:
        evaluate.RESULTS = results
        evaluate.RAW = raw
        yield raw
    finally:
        evaluate.RESULTS = old_results
        evaluate.RAW = old_raw


def write_manifest_fixture(tmp: pathlib.Path, *, basis: str, replay: bool) -> pathlib.Path:
    events = tmp / "coverage-events.jsonl"
    events.write_text(
        json.dumps(
            {
                "event": "typed_allocation_site",
                "callsite": 4096,
                "source": "compiler_instrumented_std_bench",
                "typed": True,
                "type_id": 1,
                "count": 100,
                "bytes": 1000,
                "type_id_basis": basis,
                **({"compiler_site_replay": True} if replay else {}),
            },
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )
    type_mapping = tmp / "type-mapping.json"
    write_json(
        type_mapping,
        [
            {
                "allocation_site_id": "mir:test::bench:bb0:stmt0",
                "type_id": 1,
                "callsite": 4096,
                "object_type": "alloc::vec::Vec<u64>",
                "mir_location": "test::bench::bb0[0]",
                "compiler_pass": "unialloc-allocation-site-type-id-pass",
            }
        ],
    )
    pass_log = tmp / "compiler-pass.log"
    pass_log.write_text("compiler pass active\n", encoding="utf-8")
    run_summary = tmp / "benchmark-run-summary.json"
    write_json(
        run_summary,
        {
            "exit_code": 0,
            "benchmark_target": "std_bench",
            "benchmarks": ["vec_push"],
        },
    )
    manifest = tmp / "manifest.json"
    write_json(
        manifest,
        {
            "schema_version": 1,
            "claim_grade": True,
            "complete_for_claim": True,
            "events": str(events),
            "type_id_basis": basis,
            "compiler_pass": {
                "kind": "rustc-mir",
                "name": "unialloc-allocation-site-type-id-pass",
                "source_changes_to_benchmarks": False,
            },
            "toolchain": {
                "rustc": "rustc test-fixture",
                "channel": "nightly",
                "target": "test-target",
            },
            "benchmark_suite": {
                "name": "standard Rust alloc benchmarks",
                "benchmark_target": "std_bench",
                "without_source_changes": True,
                "complete_for_claim": True,
                "paper_equivalent": True,
                "expected_benchmarks": ["vec_push"],
                "observed_benchmarks": ["vec_push"],
                "observed_benchmark_source": {
                    "compiler_instrumented": True,
                    "run": "final C002 compiler-instrumented run",
                },
            },
            "benchmark_command": ["cargo", "bench", "-p", "unialloc", "--bench", "std_bench"],
            "evidence": [
                {
                    "path": str(events),
                    "kind": "coverage_events",
                    "sha256": evaluate.file_sha256(events),
                },
                {
                    "path": str(pass_log),
                    "kind": "compiler_pass_log",
                    "sha256": evaluate.file_sha256(pass_log),
                },
                {
                    "path": str(run_summary),
                    "kind": "benchmark_run_summary",
                    "sha256": evaluate.file_sha256(run_summary),
                },
                {
                    "path": str(type_mapping),
                    "kind": "type_mapping",
                    "sha256": evaluate.file_sha256(type_mapping),
                },
            ],
        },
    )
    return manifest


def write_mir_semantic_scope_runtime_audit_fixture(
    tmp: pathlib.Path,
    *,
    run_id: str,
    benchmark: str,
    type_id: int = 11,
    callsite: int = 4096,
    source_fingerprint: dict | None = None,
) -> pathlib.Path:
    events = tmp / f"{run_id}.events.jsonl"
    events.write_text(
        json.dumps(
            {
                "bytes": 640,
                "callsite": callsite,
                "compiler_site_id_stream_mode": "rustc-driver-mir-semantic-scope",
                "compiler_site_lowered": True,
                "compiler_site_replay": False,
                "count": 10,
                "deallocations": 10,
                "event": "typed_allocation_site",
                "module_id": evaluate.RUSTC_DRIVER_LOWERING_MODULE_ID,
                "policy_flags_seen": 3,
                "source": "std_bench_auto_metadata",
                "type_id": type_id,
                "type_id_basis": "compiler-assigned-allocation-site-object-type-id-rustc-driver-mir-semantic-scope",
                "typed": True,
            },
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )
    type_mapping = tmp / f"{run_id}.type-map.json"
    write_json(
        type_mapping,
        {
            "schema_version": 1,
            "allocation_sites": [
                {
                    "allocation_site_id": "mir:fixture::bench:bb0:stmt0",
                    "type_id": type_id,
                    "module_id": evaluate.RUSTC_DRIVER_LOWERING_MODULE_ID,
                    "policy_flags": 3,
                    "callsite": callsite,
                    "mir_function": "fixture::bench",
                    "source_span": "unialloc/benches/fixture.rs:1:1: 1:8 (#0)",
                    "semantic_object_type": "alloc::vec::Vec<u8>",
                    "type_id_basis": "rustc_middle_ty_destination_or_argument_heap_object_type",
                    "lowering_kind": "semantic_scope_enter_exit_rewrite",
                    "rewrite_status": "actual_semantic_scope_enter_exit_rewrite_applied",
                }
            ],
        },
    )
    audit = tmp / f"{run_id}.audit.json"
    write_json(
        audit,
        {
            "schema_version": 1,
            "source": "rustc-driver-mir-semantic-scope-std-bench-runtime-smoke-audit",
            "run_id": run_id,
            "evidence_source_fingerprint": copy.deepcopy(
                source_fingerprint
                if source_fingerprint is not None
                else evaluate.repository_source_fingerprint()
            ),
            "artifacts": {
                "runtime_events": str(events),
                "runtime_events_sha256": evaluate.file_sha256(events),
                "type_mapping": str(type_mapping),
                "type_mapping_sha256": evaluate.file_sha256(type_mapping),
                "pass_source": "tools/unialloc-rustc-pass/unialloc-rustc-mir-rewrite-dry-run.rs",
            },
            "toolchain": {"rust_toolchain": "nightly-2022-07-01"},
            "runtime": {
                "command": ["cargo", "bench", "-p", "unialloc", "--bench", "std_bench", benchmark],
                "returncode": 0,
            },
            "selected_target_benches": [benchmark],
            "summary": {
                "runtime_smoke_validated": True,
                "runtime_returncode": 0,
                "actual_semantic_scope_rewrite": True,
                "semantic_scope_rewrite_applied_count": 2,
                "direct_allocator_rewrite_requested": True,
                "direct_allocator_rewrite_validated": True,
                "direct_local_size_align_with_semantic_drop": True,
                "direct_size_align_local_pairing_validated": True,
                "std_bench_semantic_scope_candidate_count": 2,
                "semantic_scope_replacement_resolution_status": "resolved_unialloc_semantic_scope_enter_exit",
                "selected_benchmark_count": 1,
                "lowered_module_typed_allocation_site_event_count": 1,
                "typed_allocation_site_event_count": 1,
                "lowered_module_allocations": 10,
                "typed_allocations": 10,
                "fallback_allocations": 0,
                "lowered_module_type_id_bases": [
                    "compiler-assigned-allocation-site-object-type-id-rustc-driver-mir-semantic-scope"
                ],
                "lowered_module_compiler_site_id_stream_modes": ["rustc-driver-mir-semantic-scope"],
                "semantic_object_type_count": 1,
                "semantic_object_type_id_bases": [
                    "rustc_middle_ty_destination_or_argument_heap_object_type"
                ],
                "unknown_semantic_object_type_row_count": 0,
                "type_mapping_record_count": 1,
            },
            "target_rewrite": {
                "provider_override_installed": True,
                "compiler_pass": {
                    "kind": "rustc_driver_optimized_mir_provider_override",
                    "query_overridden": "optimized_mir",
                },
                "body_clone_returned_to_rustc": True,
                "actual_semantic_scope_rewrite_requested": True,
                "actual_semantic_scope_rewrite": True,
                "semantic_scope_rewrite_applied_count": 2,
                "semantic_scope_replacement_resolution_status": "resolved_unialloc_semantic_scope_enter_exit",
                "replacement_resolution_status": "resolved_unialloc_semantic_scope_enter_exit",
            },
        },
    )
    return audit


def tamper_mir_semantic_scope_runtime_artifact(
    audit_path: pathlib.Path,
    artifact_key: str,
) -> pathlib.Path:
    """Mutate a runtime artifact without refreshing its collection-time hash."""

    audit = json.loads(audit_path.read_text(encoding="utf-8"))
    artifact_path = pathlib.Path(audit["artifacts"][artifact_key])
    if artifact_key == "runtime_events":
        original = artifact_path.read_text(encoding="utf-8")
        artifact_path.write_text(original + original, encoding="utf-8")
    elif artifact_key == "type_mapping":
        payload = json.loads(artifact_path.read_text(encoding="utf-8"))
        payload["tampered_after_hash_capture"] = True
        write_json(artifact_path, payload)
    else:
        raise ValueError(f"unsupported runtime artifact key: {artifact_key}")
    return artifact_path


def write_mir_semantic_scope_runtime_timeout_fixture(
    tmp: pathlib.Path,
    *,
    run_id: str,
    benchmarks: list[str],
) -> pathlib.Path:
    audit = tmp / f"{run_id}.timeout.audit.json"
    write_json(
        audit,
        {
            "schema_version": 1,
            "source": "rustc-driver-mir-semantic-scope-std-bench-runtime-smoke-audit",
            "run_id": run_id,
            "artifacts": {},
            "runtime": {
                "command": ["cargo", "bench", "-p", "unialloc", "--bench", "std_bench", *benchmarks],
                "returncode": 124,
                "timed_out": True,
            },
            "selected_target_benches": benchmarks,
            "summary": {
                "runtime_smoke_validated": False,
                "runtime_returncode": 124,
                "actual_semantic_scope_rewrite": True,
                "semantic_scope_rewrite_applied_count": 0,
                "lowered_module_typed_allocation_site_event_count": 0,
                "lowered_module_allocations": 0,
                "semantic_scope_replacement_resolution_status": "resolved_unialloc_semantic_scope_enter_exit",
                "unknown_semantic_object_type_row_count": 0,
            },
        },
    )
    return audit


def source_fingerprint_fixture(digest_byte: str = "a") -> dict:
    return {
        "schema_version": evaluate.REPOSITORY_SOURCE_FINGERPRINT_SCHEMA_VERSION,
        "algorithm": "sha256",
        "source_digest": digest_byte * 64,
    }


def ready_mir_semantic_scope_compiler_audit(
    *,
    coverage: float = 99.91,
    runtime_audit_path: pathlib.Path | None = None,
) -> dict:
    runtime_audit_path = runtime_audit_path or (
        evaluate.RESULTS
        / "rustc_driver_mir_semantic_scope_std_bench_runtime_smoke_audit.json"
    )
    manifest_path = evaluate.RESULTS / "unit-ready-mir-semantic-scope.manifest.json"
    write_json(
        manifest_path,
        {
            "schema_version": 1,
            "runtime_audit": str(runtime_audit_path),
        },
    )
    return {
        "schema_version": 1,
        "source": "compiler-coverage-evidence-audit",
        "manifest": str(manifest_path),
        "type_id_basis": "compiler-assigned-allocation-site-object-type-id-rustc-driver-mir-semantic-scope",
        "blockers": [],
        "summary": {
            "benchmark_surface_status": "pass",
            "benchmark_surface_expected_count": 430,
            "benchmark_surface_observed_count": 430,
            "benchmark_surface_missing_count": 0,
            "claim_requested": True,
            "coverage_percent": coverage,
            "event_type_id_basis_status": "pass",
            "event_type_mapping_correlation_status": "pass",
            "ready_for_claim_grade_import": True,
            "type_mapping_status": "pass",
            "type_mapping_valid_record_count": 541,
        },
    }


def write_ready_mir_semantic_scope_runtime_companion(
    results: pathlib.Path,
    *,
    audit_path: pathlib.Path | None = None,
    run_id: str = "unit-runtime-companion",
    source_fingerprint: dict | None = None,
    artifacts: dict | None = None,
) -> pathlib.Path:
    path = audit_path or (
        results / "rustc_driver_mir_semantic_scope_std_bench_runtime_smoke_audit.json"
    )
    rewrite_path = (
        evaluate.RAW / run_id / "rewrites" / "std_bench.json"
    )
    write_json(
        rewrite_path,
        {
            "schema_version": 1,
            "source": "unit-rustc-driver-rewrite-map",
            "compiler_pass": {
                "kind": "rustc_driver_optimized_mir_provider_override",
                "query_overridden": "optimized_mir",
            },
            "summary": {
                "provider_override_installed": True,
                "body_clone_returned_to_rustc": True,
                "actual_semantic_scope_rewrite": True,
                "semantic_scope_rewrite_applied_count": 1,
            },
            "rewrite_candidates": [
                {
                    "rewrite_status": "actual_semantic_scope_enter_exit_rewrite_applied",
                    "replacement_symbol": "__unialloc_semantic_scope_enter_exit",
                    "source_span": "unialloc/benches/binary_heap.rs:9:42: 9:51",
                }
            ],
        },
    )
    audit = {
        "schema_version": 1,
        "source": "rustc-driver-mir-semantic-scope-std-bench-runtime-smoke-audit",
        "run_id": run_id,
        "summary": {
            "runtime_smoke_validated": True,
            "runtime_returncode": 0,
            "actual_semantic_scope_rewrite": True,
            "semantic_scope_rewrite_applied_count": 541,
            "direct_allocator_rewrite_requested": True,
            "direct_allocator_rewrite_validated": True,
            "direct_local_size_align_with_semantic_drop": True,
            "direct_size_align_local_pairing_validated": True,
            "runtime_full_surface_candidate_validated": True,
            "runtime_surface_full_surface_candidate": True,
            "runtime_surface_status": "full-surface-candidate",
            "canonical_expected_benchmark_count": 430,
            "canonical_missing_benchmark_count": 0,
            "selected_benchmark_count": 430,
            "lowered_module_typed_allocation_site_event_count": 239,
            "typed_allocation_site_event_count": 239,
            "lowered_module_allocations": 239,
            "typed_allocations": 239,
            "fallback_allocations": 0,
            "lowered_module_compiler_basis_present": True,
            "lowered_module_compiler_site_id_stream_modes": ["rustc-driver-mir-semantic-scope"],
            "lowered_module_type_id_bases": [
                "compiler-assigned-allocation-site-object-type-id-rustc-driver-mir-semantic-scope"
            ],
            "semantic_scope_replacement_resolution_status": "resolved_unialloc_semantic_scope_enter_exit",
            "unknown_semantic_object_type_row_count": 0,
            "type_mapping_record_count": 541,
        },
        "target_rewrite": {
            "provider_override_installed": True,
            "compiler_pass": {
                "kind": "rustc_driver_optimized_mir_provider_override",
                "query_overridden": "optimized_mir",
            },
            "body_clone_returned_to_rustc": True,
            "actual_semantic_scope_rewrite_requested": True,
            "actual_semantic_scope_rewrite": True,
            "semantic_scope_rewrite_applied_count": 541,
            "semantic_scope_replacement_resolution_status": "resolved_unialloc_semantic_scope_enter_exit",
            "replacement_resolution_status": "resolved_unialloc_semantic_scope_enter_exit",
            "path": str(rewrite_path),
        },
    }
    if source_fingerprint is not None:
        audit["evidence_source_fingerprint"] = copy.deepcopy(source_fingerprint)
    if artifacts is not None:
        audit["artifacts"] = copy.deepcopy(artifacts)
    write_json(path, audit)
    return path


def write_fresh_compiler_integrity_fixture(
    tmp: pathlib.Path,
    *,
    current: dict,
    basis: str = "compiler-assigned-allocation-site-object-type-id",
    fallback_count: int = 0,
) -> tuple[pathlib.Path, pathlib.Path, pathlib.Path, dict]:
    runtime_dir = tmp / "runtime"
    manifest_dir = tmp / "compiler"
    runtime_dir.mkdir()
    manifest_dir.mkdir()

    runtime_audit_path = write_mir_semantic_scope_runtime_audit_fixture(
        runtime_dir,
        run_id="fresh-integrity-runtime",
        benchmark="bench_a",
        source_fingerprint=current,
    )
    runtime_audit = json.loads(runtime_audit_path.read_text(encoding="utf-8"))
    write_ready_mir_semantic_scope_runtime_companion(
        evaluate.RESULTS,
        audit_path=runtime_audit_path,
        run_id="fresh-integrity-runtime",
        source_fingerprint=current,
        artifacts=runtime_audit["artifacts"],
    )

    if "rustc-driver-mir-semantic-scope" in basis:
        write_ready_mir_probe_companions(evaluate.RESULTS)
    manifest_path = write_manifest_fixture(
        manifest_dir,
        basis=basis,
        replay=False,
    )
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    if fallback_count:
        events_path = pathlib.Path(manifest["events"])
        with events_path.open("a", encoding="utf-8") as stream:
            stream.write(
                json.dumps(
                    {
                        "event": "fallback_allocations",
                        "typed": False,
                        "type_id": 0,
                        "count": fallback_count,
                        "bytes": fallback_count * 10,
                    },
                    sort_keys=True,
                )
                + "\n"
            )
        for item in manifest["evidence"]:
            if item.get("kind") == "coverage_events":
                item["sha256"] = evaluate.file_sha256(events_path)
    manifest["runtime_audit"] = str(runtime_audit_path)
    manifest["evidence_source_fingerprint"] = copy.deepcopy(current)
    write_json(manifest_path, manifest)

    saved_audit = evaluate.evidence_source_bound_payload(
        evaluate.build_compiler_coverage_audit(manifest_path),
        fingerprint=current,
    )
    audit_path = tmp / "compiler-coverage-evidence-audit.json"
    write_json(audit_path, saved_audit)
    return audit_path, manifest_path, runtime_audit_path, saved_audit


def write_probe_rewrite_map(
    *,
    run_id: str,
    source_span: str,
    summary: dict,
    candidate: dict,
) -> pathlib.Path:
    """Create a raw optimized_mir rewrite-map fixture under the active RAW root."""

    rewrite_path = evaluate.RAW / run_id / "rewrites" / f"{run_id}.json"
    write_json(
        rewrite_path,
        {
            "schema_version": 1,
            "source": "unit-rustc-driver-rewrite-map",
            "compiler_pass": {
                "kind": "rustc_driver_optimized_mir_provider_override",
                "query_overridden": "optimized_mir",
            },
            "summary": {
                "provider_override_installed": True,
                "body_clone_returned_to_rustc": True,
                **summary,
            },
            "rewrite_candidates": [
                {
                    "source_span": source_span,
                    **candidate,
                }
            ],
        },
    )
    return rewrite_path


def ready_semantic_metadata_validation_fields() -> dict:
    return {
        "semantic_metadata_validation": {
            "recovery_identity_matches": 7,
            "recovery_identity_mismatches": 0,
            "last_mismatch_requested_type_id": 0,
            "last_mismatch_recorded_type_id": 0,
            "last_mismatch_requested_module_id": 0,
            "last_mismatch_recorded_module_id": 0,
            "last_mismatch_requested_callsite": 0,
            "last_mismatch_recorded_callsite": 0,
        },
        "recovery_identity_matches": 7,
        "recovery_identity_mismatches": 0,
        "last_mismatch_requested_type_id": 0,
        "last_mismatch_recorded_type_id": 0,
        "last_mismatch_requested_module_id": 0,
        "last_mismatch_recorded_module_id": 0,
        "last_mismatch_requested_callsite": 0,
        "last_mismatch_recorded_callsite": 0,
    }


def ready_multi_owner_cross_thread_pairing_inputs() -> tuple[list[dict], list[dict], dict]:
    module_id = 0xC002DA0000000001
    vec_type_id = 0xC002DA0000000011
    arc_type_id = 0xC002DA0000000022
    common_mapping = {
        "mir_function": "cross_thread_escape_workload",
        "lowering_kind": "semantic_scope_enter_exit_rewrite",
        "rewrite_status": "actual_semantic_scope_enter_exit_rewrite_applied",
        "replacement_symbol": "__unialloc_semantic_scope_push_hints",
        "replacement_resolution_status": "resolved_unialloc_semantic_scope_push_hints_pop",
        "cross_thread_recovery_hint": True,
        "placement_hint": 0x8000,
        "placement_hint_basis": "auto_cross_thread_escape",
        "module_id": module_id,
    }
    type_mapping_rows = [
        {
            **common_mapping,
            "type_id": vec_type_id,
            "source_span": "unialloc/src/bin/rustc_driver_mir_cross_thread_hint_probe.rs:150:22: 150:44",
            "semantic_object_type": "std::vec::Vec<u64, std::alloc::Global>",
        },
        {
            **common_mapping,
            "type_id": arc_type_id,
            "source_span": "unialloc/src/bin/rustc_driver_mir_cross_thread_hint_probe.rs:156:25: 158:7",
            "semantic_object_type": "std::sync::Arc<[u64; 8], std::alloc::Global>",
        },
    ]
    rewrite_candidates = [
        {
            "mir_function": "cross_thread_escape_workload::{closure#1}",
            "lowering_kind": "semantic_scope_drop_multiple_heap_owners_skipped",
            "rewrite_status": "semantic_scope_drop_rewrite_skipped_multiple_heap_owners",
            "replacement_symbol": "__unialloc_semantic_scope_push_hints",
            "replacement_resolution_status": "rustc_middle_drop_multiple_heap_owners_not_lowered",
            "metadata_pairing_contract": "audit_only_multiple_heap_owner_drop_type",
            "semantic_object_type": (
                "multiple_heap_owners(std::sync::Arc<[u64; 8], std::alloc::Global>,"
                "std::vec::Vec<u64, std::alloc::Global>)"
            ),
            "destination_type": "Closure(cross_thread_escape_workload::{closure#1}, (Vec, Arc))",
            "source_span": "unialloc/src/bin/rustc_driver_mir_cross_thread_hint_probe.rs:176:5: 176:6",
            "allocation_site_id": "rustc-driver-mir-semantic-drop-multi-owner:unit",
            "module_id": module_id,
        }
    ]
    runtime_event = {
        "recovery_identity_mismatches": 0,
        "type_rows": [
            {
                "type_id": vec_type_id,
                "module_id": module_id,
                "allocations": 1,
                "deallocations": 1,
            },
            {
                "type_id": arc_type_id,
                "module_id": module_id,
                "allocations": 1,
                "deallocations": 1,
            },
        ],
    }
    return type_mapping_rows, rewrite_candidates, runtime_event


def mark_direct_allocator_probe_as_local_no_recovery(audit: dict) -> dict:
    """Mutate a direct allocator probe audit into the no-recovery local ABI case."""

    summary = audit["summary"]
    nested = summary.setdefault("semantic_metadata_validation", {})
    for container in (summary, nested):
        container["recovery_identity_matches"] = 0
        container["recovery_identity_mismatches"] = 0
        container["last_mismatch_requested_type_id"] = 0
        container["last_mismatch_recorded_type_id"] = 0
        container["last_mismatch_requested_module_id"] = 0
        container["last_mismatch_recorded_module_id"] = 0
        container["last_mismatch_requested_callsite"] = 0
        container["last_mismatch_recorded_callsite"] = 0
    summary["direct_local_no_recovery_metadata_abi_validated"] = True
    summary["semantic_metadata_recovery_identity_validated"] = True
    summary["semantic_metadata_recovery_identity_match_required"] = False
    return audit


def write_ready_mir_semantic_scope_probe_companion(results: pathlib.Path) -> pathlib.Path:
    path = results / "rustc_driver_mir_semantic_scope_probe_audit.json"
    rewrite_path = write_probe_rewrite_map(
        run_id="unit-semantic-scope-probe",
        source_span="unialloc/src/bin/rustc_driver_mir_semantic_scope_probe.rs:596:26: 596:66",
        summary={
            "actual_semantic_scope_rewrite": True,
            "semantic_scope_rewrite_applied_count": 9,
        },
        candidate={
            "rewrite_status": "actual_semantic_scope_enter_exit_rewrite_applied",
            "replacement_symbol": "__unialloc_semantic_scope_push",
            "lowering_kind": "semantic_scope_enter_exit_rewrite",
        },
    )
    write_json(
        path,
        {
            "schema_version": 1,
            "source": "rustc-driver-mir-semantic-scope-probe-audit",
            "run_id": "unit-semantic-scope-probe",
            "summary": {
                "runtime_probe_validated": True,
                "run_returncode": 0,
                "actual_semantic_scope_rewrite": True,
                "semantic_scope_rewrite_applied_count": 9,
                "semantic_scope_unwind_pop_inserted_count": 1,
                "semantic_scope_replacement_resolution_status": "resolved_unialloc_semantic_scope_enter_exit",
                "typed_allocations": 21,
                "typed_deallocations": 21,
                **ready_semantic_metadata_validation_fields(),
                "fallback_allocations": 0,
                "lowered_rows": 5,
                "semantic_scope_drop_unsolved_candidate_count": 0,
                "unsolved_drop_mapping_record_count": 0,
                "semantic_scope_drop_generic_type_parameter_skipped_count": 2,
                "generic_drop_mapping_record_count": 2,
                "unwind_scope_cleanup_validated": True,
                "unwind_scope_panic_observed": True,
                "post_unwind_direct_alloc_total_allocations": 1,
                "post_unwind_direct_alloc_typed_allocations": 0,
                "post_unwind_direct_alloc_fallback_allocations": 1,
                "post_unwind_direct_alloc_typed_deallocations": 0,
                "post_unwind_direct_alloc_fallback_deallocations": 1,
                "type_mapping_record_count": 30,
                "canonical_heap_family_type_mapping_record_counts": {
                    "vec": 3,
                    "vec_deque": 2,
                    "binary_heap": 2,
                    "btree_map": 2,
                    "btree_set": 2,
                    "linked_list": 2,
                    "hash_map": 2,
                    "hash_set": 2,
                    "string": 2,
                    "box": 1,
                    "rc": 2,
                    "arc": 2,
                    "pathbuf": 2,
                    "osstring": 2,
                    "cstring": 2,
                },
                "canonical_heap_family_missing": [],
                "std_collection_type_mapping_record_count": 17,
                "receiver_mutating_collection_type_mapping_record_count": 12,
                "receiver_mutating_collection_misattributed_record_count": 0,
                "vec_type_mapping_record_count": 3,
                "vec_deque_type_mapping_record_count": 2,
                "binary_heap_type_mapping_record_count": 2,
                "btree_map_type_mapping_record_count": 2,
                "btree_set_type_mapping_record_count": 2,
                "linked_list_type_mapping_record_count": 2,
                "hash_map_type_mapping_record_count": 2,
                "hash_set_type_mapping_record_count": 2,
                "string_type_mapping_record_count": 2,
                "box_type_mapping_record_count": 1,
                "rc_type_mapping_record_count": 2,
                "arc_type_mapping_record_count": 2,
                "pathbuf_type_mapping_record_count": 2,
                "osstring_type_mapping_record_count": 2,
                "cstring_type_mapping_record_count": 2,
                "hash_collection_type_mapping_record_count": 2,
                "hash_collection_owned_return_type_mapping_record_count": 2,
                "smart_pointer_type_mapping_record_count": 2,
                "smart_pointer_owned_return_type_mapping_record_count": 2,
                "owned_buffer_type_mapping_record_count": 2,
                "owned_buffer_return_type_mapping_record_count": 2,
                "c_string_type_mapping_record_count": 1,
                "c_string_return_type_mapping_record_count": 1,
                "destination_type_candidate_mapping_record_count": 1,
                "nested_heap_owner_type_mapping_record_count": 4,
                "nested_probe_return_type_mapping_record_count": 4,
                "no_wrapper_control_validated": True,
                "compiler_site_id_stream_mode": "rustc-driver-mir-semantic-scope",
                "type_id_basis": "compiler-assigned-allocation-site-object-type-id-rustc-driver-mir-semantic-scope",
            },
            "target_rewrite": {
                "provider_override_installed": True,
                "compiler_pass": {
                    "kind": "rustc_driver_optimized_mir_provider_override",
                    "query_overridden": "optimized_mir",
                },
                "body_clone_returned_to_rustc": True,
                "actual_semantic_scope_rewrite_requested": True,
                "actual_semantic_scope_rewrite": True,
                "semantic_scope_rewrite_applied_count": 9,
                "semantic_scope_replacement_resolution_status": "resolved_unialloc_semantic_scope_enter_exit",
                "replacement_resolution_status": "resolved_unialloc_semantic_scope_enter_exit",
                "path": str(rewrite_path),
            },
        },
    )
    return path


def write_ready_direct_allocator_mir_probe_companion(results: pathlib.Path) -> pathlib.Path:
    path = results / "rustc_driver_direct_allocator_mir_probe_audit.json"
    rewrite_path = write_probe_rewrite_map(
        run_id="unit-direct-allocator-probe",
        source_span="unialloc/src/bin/rustc_driver_direct_allocator_mir_probe.rs:71:23: 71:40",
        summary={
            "actual_allocator_call_replacement": True,
            "rewrite_applied_count": 5,
        },
        candidate={
            "rewrite_status": "actual_allocator_call_replacement_applied",
            "replacement_symbol": "__unialloc_alloc_layout_with_metadata_local",
            "lowering_kind": "direct_allocator_call_rewrite",
            "type_id": 0xC002DA7A,
            "module_id": evaluate.RUSTC_DRIVER_LOWERING_MODULE_ID,
            "callsite": 0xA110,
            "size_operand": 64,
            "align_operand": 8,
        },
    )
    write_json(
        path,
        {
            "schema_version": 1,
            "source": "rustc-driver-direct-allocator-mir-probe-audit",
            "run_id": "unit-direct-allocator-probe",
            "summary": {
                "runtime_probe_validated": True,
                "run_returncode": 0,
                "actual_allocator_call_replacement": True,
                "runtime_layout_provenance_validated": True,
                "runtime_layout_provenance_blockers": [],
                "direct_allocator_rewrite_applied_count": 5,
                "direct_layout_allocator_rewrite_applied_count": 2,
                "direct_heap_object_solved_rewrite_applied_count": 2,
                "direct_box_shallow_init_rewrite_applied_count": 1,
                "direct_layout_constructor_rewrite_applied_count": 1,
                "direct_layout_raw_value_constructor_rewrite_applied_count": 1,
                "direct_layout_runtime_array_constructor_rewrite_applied_count": 1,
                "direct_layout_transformer_rewrite_applied_count": 1,
                "direct_layout_result_option_passthrough_rewrite_applied_count": 1,
                "direct_layout_reconstructed_rewrite_applied_count": 1,
                "direct_layout_size_align_constructor_rewrite_applied_count": 1,
                "direct_layout_composite_rewrite_applied_count": 1,
                "direct_layout_packed_composite_rewrite_applied_count": 1,
                "direct_local_metadata_abi": True,
                "direct_local_size_align_with_semantic_drop": True,
                "direct_local_metadata_abi_rewrite_applied_count": 5,
                "direct_local_realloc_rewrite_applied_count": 1,
                "direct_local_dealloc_rewrite_applied_count": 1,
                "direct_local_size_align_alloc_rewrite_applied_count": 1,
                "direct_local_size_align_semantic_drop_scope_count": 1,
                "direct_size_align_recovery_backed_required_count": 0,
                "direct_local_size_align_metadata_contract_violation_count": 0,
                "direct_local_size_align_pairing_details": [
                    {
                        "type_id": 0xC002DA7A,
                        "semantic_object_type": "std::boxed::Box<[u8; 8]>",
                        "allocation_mir_functions": ["probe::allocate"],
                        "semantic_drop_mir_functions": ["probe::consume"],
                        "local_size_align_alloc_count": 1,
                        "semantic_drop_scope_count": 1,
                        "paired": True,
                    }
                ],
                "direct_local_size_align_pairing_gap_count": 0,
                "direct_size_align_alloc_recovery_contract_validated": True,
                "direct_size_align_local_pairing_validated": True,
                "direct_replacement_resolution_status": "resolved_unialloc_alloc_with_metadata_local",
                "manual_metadata_abi_calls": False,
                "typed_allocations": 8,
                "typed_deallocations": 8,
                **ready_semantic_metadata_validation_fields(),
                "fallback_allocations": 0,
                "lowered_rows": 12,
                "type_mapping_record_count": 12,
                "direct_box_shallow_init_type_mapping_record_count": 1,
                "direct_layout_constructor_type_mapping_record_count": 1,
                "direct_layout_raw_value_constructor_type_mapping_record_count": 1,
                "direct_layout_runtime_array_constructor_type_mapping_record_count": 1,
                "direct_layout_transformer_type_mapping_record_count": 1,
                "direct_layout_result_option_passthrough_type_mapping_record_count": 1,
                "direct_layout_reconstructed_type_mapping_record_count": 1,
                "direct_layout_size_align_constructor_type_mapping_record_count": 1,
                "direct_layout_composite_type_mapping_record_count": 1,
                "direct_layout_packed_composite_type_mapping_record_count": 1,
                "no_wrapper_control_validated": True,
                "compiler_site_id_stream_mode": "rustc-driver-direct-allocator-mir",
                "type_id_basis": "compiler-assigned-allocation-site-object-type-id-rustc-driver-direct-allocator-mir",
            },
            "runtime_layout_observation_rows": [
                {
                    "type_id": 0xC002DA7A,
                    "module_id": evaluate.RUSTC_DRIVER_LOWERING_MODULE_ID,
                    "callsite": 0xA110,
                    "allocations": 1,
                    "deallocations": 1,
                    "allocated_bytes": 64,
                    "observed_alloc_size": 64,
                    "observed_alloc_align": 8,
                    "observed_dealloc_size": 64,
                    "observed_dealloc_align": 8,
                }
            ],
            "target_rewrite": {
                "provider_override_installed": True,
                "body_clone_returned_to_rustc": True,
                "actual_allocator_call_replacement_requested": True,
                "actual_allocator_call_replacement": True,
                "rewrite_applied_count": 5,
                "direct_replacement_resolution_status": "resolved_unialloc_alloc_with_metadata_local",
                "replacement_resolution_status": "resolved_unialloc_alloc_with_metadata_local",
                "path": str(rewrite_path),
            },
        },
    )
    return path


def write_ready_mir_cross_thread_hint_probe_companion(results: pathlib.Path) -> pathlib.Path:
    path = results / "rustc_driver_mir_cross_thread_hint_probe_audit.json"
    rewrite_path = write_probe_rewrite_map(
        run_id="unit-cross-thread-hint-probe",
        source_span="unialloc/src/bin/rustc_driver_mir_cross_thread_hint_probe.rs:149:22: 149:44",
        summary={
            "actual_semantic_scope_rewrite": True,
            "semantic_scope_rewrite_applied_count": 9,
            "cross_thread_recovery_hint_count": 5,
        },
        candidate={
            "rewrite_status": "actual_semantic_scope_enter_exit_rewrite_applied",
            "replacement_symbol": "__unialloc_semantic_scope_push_hints",
            "lowering_kind": "semantic_scope_enter_exit_rewrite",
            "cross_thread_recovery_hint": True,
            "placement_hint": 32768,
            "placement_hint_basis": "auto_cross_thread_escape",
        },
    )
    mapping_rows, multi_owner_skip_rows, runtime_event = ready_multi_owner_cross_thread_pairing_inputs()
    rewrite_artifact = json.loads(rewrite_path.read_text(encoding="utf-8"))
    rewrite_artifact["rewrite_candidates"] = [*mapping_rows, *multi_owner_skip_rows]
    write_json(rewrite_path, rewrite_artifact)
    multi_owner_pairing = evaluate.rustc_driver_mir_cross_thread_multi_owner_recovery_pairing_summary(
        mapping_rows,
        multi_owner_skip_rows,
        runtime_event,
    )
    write_json(
        path,
        {
            "schema_version": 1,
            "source": "rustc-driver-mir-cross-thread-hint-probe-audit",
            "run_id": "unit-cross-thread-hint-probe",
            "summary": {
                "runtime_probe_validated": True,
                "run_returncode": 0,
                "actual_semantic_scope_rewrite": True,
                "semantic_scope_replacement_resolution_status": "resolved_unialloc_semantic_scope_push_hints_pop",
                "semantic_scope_resolution_status_ok": True,
                "semantic_scope_rewrite_applied_count": 9,
                "semantic_scope_drop_rewrite_applied_count": 2,
                "semantic_scope_drop_unsolved_candidate_count": 0,
                "cross_thread_recovery_hint_count": 5,
                "hinted_semantic_scope_rewrite_applied_count": 5,
                "total_allocations": 9,
                "typed_allocations": 7,
                "fallback_allocations": 2,
                "fallback_attribution_allocations": 2,
                "fallback_attribution_matches_stats": True,
                "fallback_attribution_raw_alloc_no_metadata": 2,
                "fallback_attribution_raw_alloc_no_metadata_bytes": 88,
                "fallback_attribution_raw_dealloc_no_metadata": 2,
                "fallback_trace_after_spawn_raw_alloc_no_metadata": 0,
                "fallback_trace_after_spawn_raw_alloc_no_metadata_bytes": 0,
                "fallback_trace_worker_start_raw_alloc_no_metadata": 2,
                "fallback_trace_worker_start_raw_alloc_no_metadata_bytes": 88,
                "typed_deallocations": 7,
                **ready_semantic_metadata_validation_fields(),
                "lowered_rows": 3,
                "lowered_deallocation_rows": 3,
                "arc_cross_thread_type_mapping_record_count": 2,
                **multi_owner_pairing,
                "non_escaping_vecdeque_cross_thread_hint_count": 0,
                "direct_allocator_candidate_count": 1,
                "non_escaping_direct_allocator_cross_thread_hint_count": 0,
                "type_mapping_record_count": 5,
                "drop_mapping_record_count": 2,
                "unsolved_drop_mapping_record_count": 0,
                "no_wrapper_control_validated": True,
                "compiler_site_id_stream_mode": "rustc-driver-mir-cross-thread-hint",
                "compiler_site_replay": False,
                "type_id_basis": "compiler-assigned-allocation-site-object-type-id-rustc-driver-mir-semantic-scope",
            },
            "runtime_event": runtime_event,
            "target_rewrite": {
                "provider_override_installed": True,
                "compiler_pass": {
                    "kind": "rustc_driver_optimized_mir_provider_override",
                    "query_overridden": "optimized_mir",
                },
                "body_clone_returned_to_rustc": True,
                "actual_semantic_scope_rewrite_requested": True,
                "actual_semantic_scope_rewrite": True,
                "cross_thread_recovery_hint_count": 5,
                "semantic_scope_rewrite_applied_count": 9,
                "semantic_scope_replacement_resolution_status": "resolved_unialloc_semantic_scope_push_hints_pop",
                "replacement_resolution_status": "resolved_unialloc_semantic_scope_push_hints_pop",
                "path": str(rewrite_path),
            },
        },
    )
    return path


def write_ready_mir_probe_companions(results: pathlib.Path) -> None:
    write_ready_mir_semantic_scope_probe_companion(results)
    write_ready_direct_allocator_mir_probe_companion(results)
    write_ready_mir_cross_thread_hint_probe_companion(results)


class CompilerCoverageClaimGradeGateTests(unittest.TestCase):
    def test_logged_subprocess_timeout_kills_process_group(self) -> None:
        if os.name == "nt":
            self.skipTest("process-group timeout behavior is POSIX-specific")
        with tempfile.TemporaryDirectory() as raw_tmp:
            tmp = pathlib.Path(raw_tmp)
            pid_file = tmp / "child.pid"
            script = tmp / "spawn_child.py"
            script.write_text(
                "\n".join(
                    [
                        "import pathlib, subprocess, sys, time",
                        "pid_file = pathlib.Path(sys.argv[1])",
                        "child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(30)'])",
                        "pid_file.write_text(str(child.pid), encoding='utf-8')",
                        "time.sleep(30)",
                    ]
                )
                + "\n",
                encoding="utf-8",
            )
            result = evaluate.run_logged_subprocess(
                tmp,
                "timeout-tree",
                [sys.executable, str(script), str(pid_file)],
                timeout=1,
            )
            self.assertEqual(result["returncode"], 124)
            self.assertTrue(result["timed_out"])
            self.assertTrue(result["process_group_started"])
            self.assertTrue(pid_file.exists())
            child_pid = int(pid_file.read_text(encoding="utf-8"))
            for _ in range(30):
                try:
                    os.kill(child_pid, 0)
                except ProcessLookupError:
                    break
                time.sleep(0.1)
            else:
                self.fail(f"timed-out subprocess child still exists: pid={child_pid}")

    def test_cycle_count_text_parser_requires_explicit_cycle_markers(self) -> None:
        noisy_log = "\n".join(
            [
                "[  123.456789] CPU: 4 PID: 99 rust_minimal: address 0xffff0000",
                "[  124.000001] rust_minimal: cycle: 1001",
                "[  125.000001] rust_minimal: cycles=1002",
                "[  126.000001] rust_minimal: duration_cycles: 1003",
            ]
        )
        self.assertEqual(
            evaluate.extract_cycle_count_samples(noisy_log),
            [1001, 1002, 1003],
        )
        self.assertEqual(
            evaluate.extract_cycle_count_samples(
                "[  123.456789] CPU: 4 PID: 99 rust_minimal: no cycle measurement"
            ),
            [],
        )

    def test_cycle_count_json_parser_accepts_structured_samples(self) -> None:
        self.assertEqual(
            evaluate.extract_cycle_count_samples(
                {
                    "cycle_count_samples": [
                        {"cycle_count": 7},
                        {"median_cycles": "8"},
                        9,
                    ]
                }
            ),
            [7, 8, 9],
        )

    def test_rust_for_linux_structured_cycle_markers_keep_benchmark_context(self) -> None:
        log = "\n".join(
            [
                "[  124.000001] rust_minimal: UNIALLOC_RFL_CYCLE_SAMPLE "
                "benchmark=bench_new median_cycles=1002 bytes=0",
                "[  125.000001] rust_minimal: UNIALLOC_RFL_CYCLE_SAMPLE "
                "benchmark=bench_with_capacity_1000 median_cycles=2003 bytes=4000",
            ]
        )
        self.assertEqual(evaluate.extract_cycle_count_samples(log), [1002, 2003])
        records = evaluate.extract_cycle_count_sample_records(log)
        self.assertEqual(
            records,
            [
                {
                    "benchmark": "bench_new",
                    "bytes": 0,
                    "cycle_count": 1002,
                    "source_marker": "UNIALLOC_RFL_CYCLE_SAMPLE",
                },
                {
                    "benchmark": "bench_with_capacity_1000",
                    "bytes": 4000,
                    "cycle_count": 2003,
                    "source_marker": "UNIALLOC_RFL_CYCLE_SAMPLE",
                },
            ],
        )

    def test_rust_for_linux_allocator_stats_parser_requires_explicit_stats_marker(self) -> None:
        log = "\n".join(
            [
                "[  124.000001] rust_minimal: cycle: 1002",
                "[  125.000001] rust_minimal: UniAlloc semantic bridge probe: "
                "dealloc_ok=true "
                "allocator_total_allocations=64 allocator_total_deallocations=61 "
                "allocator_typed_allocations=3 allocator_typed_deallocations=3 "
                "allocator_fallback_allocations=61 "
                "allocator_total_allocated_bytes=8192 allocator_typed_allocated_bytes=256 "
                "allocator_coverage_basis_points=468 "
                "allocator_type_stats_rows=1 allocator_type_stats_dropped_events=0 allocator_type_stats_probe_matched=true",
            ]
        )
        self.assertEqual(evaluate.extract_cycle_count_samples(log), [1002])
        stats = evaluate.extract_allocator_stats_samples(log)
        self.assertEqual(len(stats), 1)
        self.assertEqual(stats[0]["allocator_total_allocations"], 64)
        self.assertEqual(stats[0]["allocator_total_deallocations"], 61)
        self.assertEqual(stats[0]["allocator_type_stats_probe_matched"], 1)
        self.assertEqual(stats[0]["source_marker"], evaluate.RUST_FOR_LINUX_ALLOCATOR_STATS_MARKER)
        self.assertEqual(stats[0]["semantic_bridge_dealloc_ok"], 1)
        self.assertEqual(evaluate.rust_for_linux_allocator_stats_blockers(stats), [])
        self.assertEqual(
            evaluate.extract_allocator_stats_samples(
                "[  123.456789] allocator-ish log without explicit UniAlloc stats marker"
            ),
            [],
        )

    def test_rust_for_linux_allocator_stats_blockers_reject_missing_deallocs(self) -> None:
        blockers = evaluate.rust_for_linux_allocator_stats_blockers(
            [
                {
                    "allocator_total_allocations": 7,
                    "allocator_total_deallocations": 0,
                    "allocator_total_allocated_bytes": 128,
                }
            ]
        )
        joined = " | ".join(blockers)
        self.assertIn("no positive total deallocation counter", joined)
        self.assertIn("no allocator stats sample demonstrates positive typed semantic allocation", joined)
        self.assertIn("no allocator stats sample reports positive semantic type-stats rows", joined)
        self.assertIn("no allocator stats sample proves the semantic type-stats probe row matched", joined)

    def test_rust_for_linux_allocator_stats_blockers_reject_false_type_probe_match(self) -> None:
        blockers = evaluate.rust_for_linux_allocator_stats_blockers(
            [
                {
                    "allocator_total_allocations": 7,
                    "allocator_total_deallocations": 7,
                    "allocator_typed_allocations": 1,
                    "allocator_typed_deallocations": 1,
                    "allocator_total_allocated_bytes": 128,
                    "allocator_type_stats_rows": 1,
                    "allocator_type_stats_dropped_events": 0,
                    "allocator_type_stats_probe_matched": 0,
                }
            ]
        )
        self.assertIn(
            "no allocator stats sample proves the semantic type-stats probe row matched",
            " | ".join(blockers),
        )

    def test_rust_for_linux_allocator_stats_blockers_reject_dropped_type_events(self) -> None:
        sample = {
            "source_marker": evaluate.RUST_FOR_LINUX_ALLOCATOR_STATS_MARKER,
            "semantic_bridge_dealloc_ok": 1,
            "allocator_total_allocations": 7,
            "allocator_total_deallocations": 7,
            "allocator_typed_allocations": 1,
            "allocator_typed_deallocations": 1,
            "allocator_total_allocated_bytes": 128,
            "allocator_type_stats_rows": 1,
            "allocator_type_stats_dropped_events": 2,
            "allocator_type_stats_probe_matched": 1,
        }
        blockers = evaluate.rust_for_linux_allocator_stats_blockers([sample])
        self.assertIn(
            "dropped 2 semantic type-stats events",
            " | ".join(blockers),
        )

    def test_read_cycle_count_samples_rejects_noise_only_kernel_log(self) -> None:
        with tempfile.TemporaryDirectory() as raw_tmp:
            tmp = pathlib.Path(raw_tmp)
            log = tmp / "dmesg.log"
            log.write_text(
                "[  123.456789] CPU: 4 PID: 99 rust_minimal: loaded module 1234\n",
                encoding="utf-8",
            )
            samples, errors, raw = evaluate.read_cycle_count_samples(log)
        self.assertEqual(samples, [])
        self.assertIn("contains no parseable integer samples", " | ".join(errors))
        self.assertIsInstance(raw, str)

    def test_evaluate_write_json_uses_unique_atomic_temp_files_under_concurrency(self) -> None:
        with tempfile.TemporaryDirectory() as raw_tmp:
            tmp = pathlib.Path(raw_tmp)
            target = tmp / "shared-result.json"
            errors = []
            barrier = threading.Barrier(8)

            def writer(worker_id: int) -> None:
                try:
                    barrier.wait(timeout=5)
                    for iteration in range(20):
                        evaluate.write_json(
                            target,
                            {"worker": worker_id, "iteration": iteration},
                        )
                except BaseException as exc:  # pragma: no cover - assertion below reports detail
                    errors.append(exc)

            threads = [threading.Thread(target=writer, args=(idx,)) for idx in range(8)]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join(timeout=10)

            self.assertFalse(errors, [repr(error) for error in errors])
            self.assertTrue(target.exists())
            parsed = json.loads(target.read_text(encoding="utf-8"))
            self.assertIn("worker", parsed)
            self.assertIn("iteration", parsed)
            self.assertEqual(
                [],
                [entry.name for entry in tmp.iterdir() if entry.name.endswith(".tmp")],
            )

    def test_rustc_driver_rewrite_pruner_keeps_target_crate_and_removes_dependency_logs(self) -> None:
        with tempfile.TemporaryDirectory() as raw_tmp:
            tmp = pathlib.Path(raw_tmp)
            rewrites = tmp / "rewrites"
            rewrites.mkdir()
            std_bench = rewrites / "std_bench.json"
            dependency = rewrites / "serde.json"
            write_json(
                std_bench,
                {
                    "rustc_args": ["--crate-name", "std_bench"],
                    "summary": {"rewrite_candidate_count": 1},
                    "rewrite_candidates": [{"lowering_kind": "semantic_scope_enter_exit_rewrite"}],
                },
            )
            write_json(
                dependency,
                {
                    "rustc_args": ["--crate-name", "serde"],
                    "summary": {"rewrite_candidate_count": 0},
                    "rewrite_candidates": [],
                },
            )

            dry_run = evaluate.prune_rustc_driver_rewrite_audit_dir(
                rewrites,
                keep_crate_names=["std_bench"],
                manifest_out=tmp / "dry-run.json",
                dry_run=True,
            )
            self.assertTrue(std_bench.exists())
            self.assertTrue(dependency.exists())
            self.assertEqual(dry_run["removed_file_count"], 1)
            self.assertGreater(dry_run["removed_bytes"], 0)

            result = evaluate.prune_rustc_driver_rewrite_audit_dir(
                rewrites,
                keep_crate_names=["std_bench"],
                manifest_out=tmp / "prune.json",
            )
            self.assertTrue(std_bench.exists())
            self.assertFalse(dependency.exists())
            self.assertEqual(result["kept_file_count"], 1)
            self.assertEqual(result["removed_file_count"], 1)
            manifest = json.loads((tmp / "prune.json").read_text(encoding="utf-8"))
            self.assertEqual(manifest["keep_crate_names"], ["std_bench"])
            self.assertEqual(manifest["removed_file_count"], 1)

    def test_rust_for_linux_cycle_counts_content_blocker_requires_allocator_stats(self) -> None:
        with tempfile.TemporaryDirectory() as raw_tmp:
            tmp = pathlib.Path(raw_tmp)
            cycle_counts = tmp / "rust-for-linux-cycle-counts.json"
            evaluate.write_json(
                cycle_counts,
                {
                    "platform": "rust-for-linux",
                    "kind": "cycle_counts",
                    "passed": True,
                    "claim_grade": True,
                    "complete_for_claim": True,
                    "cycle_count_samples": [1001],
                    "sample_count": 1,
                },
            )
            blockers = evaluate.platform_evidence_content_blockers("cycle_counts", cycle_counts)
        self.assertIn("no UniAlloc allocator stats snapshot", " | ".join(blockers))

    def test_paper_collections_docker_driver_command_is_auditable(self) -> None:
        cell = {
            "dataset": "default_performance",
            "benchmark": "Collections",
            "allocator": "scudo",
            "variant_feature": None,
        }
        command = evaluate.paper_collections_docker_driver_command(
            cell,
            image="paper:latest",
            platform_name="linux/arm64",
            bench_filter="vec::bench_new",
            extra_features=["stats"],
            scudo_mode="ld-preload",
            scudo_runtime_library="/usr/lib/libclang_rt.scudo_standalone-test.so",
        )
        self.assertTrue(evaluate.command_targets_paper_collections_docker_driver(command))
        self.assertIn("evaluation/scripts/paper_collections_docker_driver.py", command)
        self.assertIn("--image", command)
        self.assertIn("paper:latest", command)
        self.assertIn("--platform", command)
        self.assertIn("linux/arm64", command)
        self.assertIn("--bench-filter", command)
        self.assertIn("--scudo-mode", command)
        self.assertIn("ld-preload", command)
        self.assertIn("--scudo-runtime-library", command)
        self.assertNotIn("paper_workload_placeholder.py", " ".join(command))

    def test_paper_collections_docker_driver_marks_only_historical_pin_paper_exact(self) -> None:
        repo_toolchain = "nightly-2026-06-11"
        with mock.patch.dict(
            os.environ,
            {paper_collections_docker_driver.RUST_TOOLCHAIN_ENV: ""},
            clear=False,
        ), mock.patch.object(
            paper_collections_docker_driver,
            "read_repo_rust_toolchain",
            return_value=repo_toolchain,
        ):
            repo_default = paper_collections_docker_driver.rust_toolchain_provenance(
                types.SimpleNamespace(rust_toolchain=None)
            )
            paper_exact = paper_collections_docker_driver.rust_toolchain_provenance(
                types.SimpleNamespace(rust_toolchain="nightly-2022-07-01")
            )

        self.assertEqual(repo_default["effective_toolchain"], repo_toolchain)
        self.assertTrue(repo_default["uses_repo_rust_toolchain"])
        self.assertFalse(repo_default["paper_exact_toolchain"])
        self.assertTrue(repo_default["source_accepted_newer_toolchain"])
        self.assertEqual(
            paper_exact["effective_toolchain"],
            paper_collections_docker_driver.PAPER_EXACT_RUST_TOOLCHAIN,
        )
        self.assertFalse(paper_exact["uses_repo_rust_toolchain"])
        self.assertTrue(paper_exact["paper_exact_toolchain"])
        self.assertFalse(paper_exact["source_accepted_newer_toolchain"])

    def test_paper_collections_docker_driver_helpers_preserve_fail_closed_contract(self) -> None:
        args = types.SimpleNamespace(
            dataset="default_performance",
            benchmark="Collections",
            allocator="ptmalloc",
            timeout=30,
            inner_timeout=None,
            variant_feature=None,
            bench_filter="vec::bench_new",
            extra_feature=["stats"],
            tcmalloc_lib_dir=None,
            cmake_bin=None,
            allow_host_allocator_mismatch=False,
            dry_run=True,
        )
        inner = paper_collections_docker_driver.inner_driver_args(args)
        self.assertEqual(inner[:2], ["python3", "evaluation/scripts/paper_workload_driver.py"])
        self.assertIn("--dry-run", inner)
        self.assertIn("--bench-filter", inner)
        self.assertIn("vec::bench_new", inner)
        self.assertEqual(
            paper_collections_docker_driver.last_json_object("noise\n{\"ok\": false}\n"),
            {"ok": False},
        )
        self.assertIsNone(paper_collections_docker_driver.last_json_object("noise only"))

    def test_paper_collections_docker_driver_uses_non_login_shell_for_container_path(self) -> None:
        args = types.SimpleNamespace(
            docker_bin="/usr/bin/docker",
            platform="linux/arm64",
            image="unialloc-collections-runner:nightly-2022-07-01",
            timeout=30,
            writable_mount=False,
        )
        with mock.patch.object(paper_collections_docker_driver, "run_command") as run_command:
            run_command.return_value = {
                "ok": True,
                "stdout": (
                    "UNAME=Linux-aarch64\n"
                    "ldd (Debian GLIBC 2.31-13+deb11u5) 2.31\n"
                    "/usr/bin/python3\n"
                    "Python 3.9.2\n"
                    "/usr/local/cargo/bin/cargo\n"
                    "cargo 1.64.0\n"
                    "/usr/bin/cmake\n"
                    "cmake version 3.18.4\n"
                ),
                "stderr": "",
            }
            probe = paper_collections_docker_driver.container_probe(args)
        command = run_command.call_args.args[0]
        self.assertIn("sh", command)
        shell_index = command.index("sh")
        self.assertEqual(command[shell_index + 1], "-c")
        self.assertNotIn("-lc", command)
        self.assertTrue(probe["ok"], probe)

    def test_paper_collections_docker_driver_parses_inner_stderr_json(self) -> None:
        args = types.SimpleNamespace(
            docker_bin="/usr/bin/docker",
            platform="linux/arm64",
            image="unialloc-collections-runner:nightly-2022-07-01",
            target_volume="unialloc-collections-target-nightly-2022-07-01",
            writable_mount=False,
            timeout=30,
            inner_timeout=None,
            dataset="default_performance",
            benchmark="Collections",
            allocator="scudo",
            variant_feature=None,
            bench_filter=None,
            extra_feature=[],
            tcmalloc_lib_dir=None,
            cmake_bin=None,
            allow_host_allocator_mismatch=False,
            dry_run=True,
        )
        stderr_record = {
            "ok": False,
            "source": "paper-workload-driver",
            "error": "scudo sanitizer runtime is not supported by the active Rust toolchain/target",
        }
        with mock.patch.object(paper_collections_docker_driver, "run_command") as run_command:
            run_command.return_value = {
                "ok": False,
                "exit_code": 2,
                "duration_seconds": 0.1,
                "stdout": "",
                "stderr": json.dumps(stderr_record, sort_keys=True) + "\n",
            }
            inner = paper_collections_docker_driver.run_inner_driver(args)
        self.assertFalse(inner["ok"])
        self.assertEqual(inner["record"]["error"], stderr_record["error"])
        self.assertEqual(inner["record"]["source"], "paper-workload-driver")

    def test_paper_collections_docker_driver_forwards_scudo_runtime_mode(self) -> None:
        args = types.SimpleNamespace(
            dataset="default_performance",
            benchmark="Collections",
            allocator="scudo",
            timeout=30,
            inner_timeout=None,
            variant_feature=None,
            bench_filter=None,
            extra_feature=[],
            tcmalloc_lib_dir=None,
            cmake_bin=None,
            scudo_mode="ld-preload",
            scudo_runtime_library="/usr/lib/llvm-16/lib/clang/16/lib/linux/libclang_rt.scudo_standalone-aarch64.so",
            allow_host_allocator_mismatch=False,
            dry_run=True,
        )
        inner = paper_collections_docker_driver.inner_driver_args(args)
        self.assertIn("--scudo-mode", inner)
        self.assertIn("ld-preload", inner)
        self.assertIn("--scudo-runtime-library", inner)
        self.assertIn(args.scudo_runtime_library, inner)

    def test_plan_docker_collections_dry_run_probe_is_recognized_without_running_local_driver(self) -> None:
        command = [
            sys.executable,
            "evaluation/scripts/paper_collections_docker_driver.py",
            "--dataset",
            "default_performance",
            "--benchmark",
            "Collections",
            "--allocator",
            "ptmalloc",
            "--image",
            "paper:latest",
        ]
        completed = types.SimpleNamespace(
            returncode=2,
            stdout="",
            stderr=json.dumps(
                {
                    "ok": False,
                    "source": "paper-collections-docker-driver",
                    "error": "Docker runner image is not ready for claim-grade Collections execution",
                },
                sort_keys=True,
            )
            + "\n",
        )
        with mock.patch.object(evaluate.subprocess, "run", return_value=completed) as run_mock:
            probe = evaluate.docker_collections_driver_dry_run_probe(
                command,
                {"cwd": "."},
                {
                    "dataset": "default_performance",
                    "benchmark": "Collections",
                    "allocator": "ptmalloc",
                },
                timeout=1,
            )
        self.assertIsNotNone(probe)
        assert probe is not None
        self.assertFalse(probe["ok"])
        self.assertIn("Docker Collections driver dry-run exited 2", probe["issue"])
        self.assertIn("--dry-run", run_mock.call_args.args[0])
        self.assertEqual(probe["record"]["source"], "paper-collections-docker-driver")

    def test_wrapper_manifest_can_route_blocked_collections_rule_to_docker_driver(self) -> None:
        contract = {
            "dataset": "default_performance",
            "benchmark": "Collections",
            "allocator": "ptmalloc",
            "variant_feature": None,
            "required_runs": 1,
            "timeout": 1800,
            "capability": {
                "kind": "host_blocked_allocator",
                "locally_runnable": False,
                "claim_grade_blocker": "Linux/glibc host required for ptmalloc paper evidence",
                "reason": "ptmalloc paper evidence requires a Linux/glibc host",
            },
        }
        rule = evaluate.paper_workload_wrapper_manifest_rule(
            contract,
            docker_collections_driver=True,
            docker_collections_image="paper:latest",
            docker_collections_platform="linux/arm64",
        )
        command = rule["command_template"]
        self.assertEqual(rule["capability_kind"], "docker_collections_driver")
        self.assertNotIn("claim_grade_blockers", rule["blocked_cell_contract"] if "blocked_cell_contract" in rule else {})
        self.assertNotIn("blocked_cell_contract", rule)

    def test_wrapper_manifest_embeds_scudo_runtime_options_for_scudo_rule(self) -> None:
        contract = {
            "dataset": "default_performance",
            "benchmark": "Collections",
            "allocator": "scudo",
            "variant_feature": None,
            "required_runs": 1,
            "timeout": 1800,
            "capability": {
                "kind": "toolchain_blocked_allocator",
                "locally_runnable": False,
                "claim_grade_blocker": "scudo sanitizer runtime/toolchain support required",
                "reason": "scudo sanitizer runtime is not supported by the active Rust toolchain/target",
            },
        }
        rule = evaluate.paper_workload_wrapper_manifest_rule(
            contract,
            docker_collections_driver=True,
            docker_collections_image="paper:latest",
            docker_collections_platform="linux/arm64",
            docker_collections_scudo_mode="ld-preload",
            docker_collections_scudo_runtime_library="/usr/lib/libclang_rt.scudo_standalone-test.so",
        )
        command = rule["command_template"]
        self.assertEqual(rule["capability_kind"], "docker_collections_driver")
        self.assertIn("--scudo-mode", command)
        self.assertIn("ld-preload", command)
        self.assertIn("--scudo-runtime-library", command)
        self.assertIn("/usr/lib/libclang_rt.scudo_standalone-test.so", command)
        self.assertNotIn("env", rule)
        self.assertTrue(evaluate.command_targets_paper_collections_docker_driver(command))
        self.assertIn("paper:latest", command)
        self.assertEqual(evaluate.wrapper_rule_issues(rule, contract), [])

    def test_default_plan_reports_disabled_local_collections_driver_without_false_positive_blocker(self) -> None:
        cell = {
            "dataset": "default_performance",
            "benchmark": "Collections",
            "allocator": "unialloc",
            "role": "paper",
            "category": "macro_single_thread",
            "variant_feature": "",
        }
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = pathlib.Path(tmp)
            with mock.patch.object(evaluate, "required_performance_sample_cells", return_value=([cell], [])):
                plan = evaluate.build_paper_performance_plan_skeleton(
                    {"methodology": {"runs_per_benchmark": 1}},
                    tmp_path,
                    dataset_names=["default_performance"],
                    runs=1,
                    timeout=30,
                    cwd=str(tmp_path),
                    collections_driver=False,
                )
                audit = evaluate.build_paper_performance_plan_audit(
                    {"methodology": {"runs_per_benchmark": 1}},
                    tmp_path,
                    plan,
                    dataset_names=["default_performance"],
                    default_runs=1,
                    required_runs=1,
                )

        workload = plan["workloads"][0]
        self.assertEqual(workload["driver_kind"], "fail-safe-placeholder")
        self.assertEqual(
            workload["claim_grade_blockers"],
            ["local Collections driver is available but was not enabled for this generated plan"],
        )
        self.assertNotIn("in-tree std_bench Collections driver is available", " ".join(workload["claim_grade_blockers"]))
        self.assertEqual(workload["blocked_cell_contract"]["status"], "local_collections_driver_not_enabled")
        self.assertIn("--collections-driver", " ".join(workload["blocked_cell_contract"]["required_remediation"]))
        self.assertEqual(audit["summary"]["invalid_workload_count"], 1, audit)

    def test_collections_driver_plan_uses_real_local_driver_command_for_supported_cells(self) -> None:
        cell = {
            "dataset": "default_performance",
            "benchmark": "Collections",
            "allocator": "unialloc",
            "role": "paper",
            "category": "macro_single_thread",
            "variant_feature": "",
        }
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = pathlib.Path(tmp)
            with mock.patch.object(evaluate, "required_performance_sample_cells", return_value=([cell], [])):
                plan = evaluate.build_paper_performance_plan_skeleton(
                    {"methodology": {"runs_per_benchmark": 1}},
                    tmp_path,
                    dataset_names=["default_performance"],
                    runs=1,
                    timeout=30,
                    cwd=str(tmp_path),
                    collections_driver=True,
                )
                audit = evaluate.build_paper_performance_plan_audit(
                    {"methodology": {"runs_per_benchmark": 1}},
                    tmp_path,
                    plan,
                    dataset_names=["default_performance"],
                    default_runs=1,
                    required_runs=1,
                )

        workload = plan["workloads"][0]
        self.assertEqual(workload["driver_kind"], "local-collections-driver")
        self.assertIn("evaluation/scripts/paper_workload_driver.py", workload["command"])
        self.assertNotIn("claim_grade_blockers", workload)
        self.assertEqual(audit["summary"]["invalid_workload_count"], 0, audit)
        self.assertTrue(audit["summary"]["ready_for_claim_grade_import"], audit)

    def test_collections_driver_plan_adds_compiler_site_replay_for_semantic_unialloc_cells(self) -> None:
        semantic_cell = {
            "dataset": "metadata_segregation",
            "benchmark": "Collections",
            "allocator": "unialloc",
            "role": "paper",
            "category": "micro",
            "variant_feature": "metadata_segregation",
        }
        baseline_cell = {**semantic_cell, "allocator": "tcmalloc"}
        mapping = "evaluation/raw/rustc-mir-real/type-mapping.json"

        command = evaluate.paper_collections_driver_command(
            semantic_cell,
            compiler_site_replay_type_mapping=mapping,
            compiler_site_id_mode="consuming-stream",
            compiler_site_replay_limit=128,
        )
        self.assertIn("--compiler-site-replay-type-mapping", command)
        self.assertIn(mapping, command)
        self.assertIn("--compiler-site-id-mode", command)
        self.assertIn("consuming-stream", command)
        self.assertIn("--compiler-site-recovery-scope", command)
        self.assertIn("thread-local", command)
        self.assertIn("--compiler-site-replay-limit", command)
        self.assertIn("128", command)

        baseline_command = evaluate.paper_collections_driver_command(
            baseline_cell,
            compiler_site_replay_type_mapping=mapping,
        )
        self.assertNotIn("--compiler-site-replay-type-mapping", baseline_command)

    def test_generate_plan_threads_compiler_site_replay_into_semantic_local_driver(self) -> None:
        cell = {
            "dataset": "type_isolation",
            "benchmark": "Collections",
            "allocator": "unialloc",
            "role": "paper",
            "category": "micro",
            "variant_feature": "type_isolation",
        }
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = pathlib.Path(tmp)
            with mock.patch.object(evaluate, "required_performance_sample_cells", return_value=([cell], [])):
                plan = evaluate.build_paper_performance_plan_skeleton(
                    {"methodology": {"runs_per_benchmark": 1}},
                    tmp_path,
                    dataset_names=["type_isolation"],
                    runs=1,
                    timeout=30,
                    cwd=str(tmp_path),
                    collections_driver=True,
                    compiler_site_replay_type_mapping="evaluation/raw/rustc-mir-real/type-mapping.json",
                    compiler_site_id_mode="cyclic-replay",
                )

        workload = plan["workloads"][0]
        self.assertEqual(workload["driver_kind"], "local-collections-driver")
        self.assertIn("--compiler-site-replay-type-mapping", workload["command"])
        self.assertIn("--compiler-site-id-mode", workload["command"])
        self.assertIn("--compiler-site-recovery-scope", workload["command"])
        self.assertEqual(workload["compiler_site_recovery_scope"], "thread-local")
        self.assertEqual(
            workload["compiler_site_replay_type_mapping"],
            "evaluation/raw/rustc-mir-real/type-mapping.json",
        )

    def test_performance_plan_capability_labels_user_accepted_newer_source_as_non_paper_exact(self) -> None:
        cell = {
            "dataset": "default_performance",
            "benchmark": "RPython",
            "allocator": "unialloc",
            "role": "paper",
            "category": "macro_single_thread",
            "variant_feature": "",
        }
        config_audit = {
            "configs": [
                {
                    "rule_id": "external-RPython",
                    "benchmarks": ["RPython"],
                    "datasets": ["default_performance"],
                    "categories": ["macro_single_thread"],
                    "claim_grade_runner_ready": True,
                    "runner_ready": True,
                    "adapter_script": "/tmp/rpython-adapter.py",
                    "adapter_active_config": "/tmp/rpython-config.json",
                    "resolved_real_workload_dir": "/tmp/rustpython",
                    "source_contract": {
                        "claim_grade_complete": True,
                        "exact_checkout_pin_complete": False,
                        "accepted_newer_checkout_pin_complete": True,
                        "paper_exact_claim_grade_complete": False,
                        "paper_exact_ref_complete": True,
                        "reproducible_snapshot_complete": True,
                        "source_provenance_class": evaluate.USER_ACCEPTED_NEWER_SOURCE_PROVENANCE_CLASS,
                    },
                }
            ]
        }
        capability = evaluate.paper_performance_cell_capability(
            cell,
            external_config_audit=config_audit,
        )

        self.assertEqual(capability["kind"], "external_adapter_claim_grade_runner")
        self.assertIn("accepted", capability["reason"])
        self.assertIn("non-paper-exact", capability["reason"])
        self.assertNotIn("with exact checkout", capability["reason"])
        self.assertTrue(capability["source_contract"]["accepted_newer_checkout_pin_complete"])
        self.assertFalse(capability["source_contract"]["paper_exact_claim_grade_complete"])
        self.assertEqual(
            capability["source_contract"]["source_provenance_class"],
            evaluate.USER_ACCEPTED_NEWER_SOURCE_PROVENANCE_CLASS,
        )

    def test_paper_performance_gap_plan_selects_only_missing_or_non_claim_grade_cells(self) -> None:
        with tempfile.TemporaryDirectory() as raw_tmp:
            tmp = pathlib.Path(raw_tmp)
            paper_dir = tmp / "paper"
            paper_dir.mkdir()
            (paper_dir / "default-perf.dat").write_text(
                "# # jemalloc ptmalloc\n"
                "1 \"Collections\" 1.0 1.1\n"
                "2 \"RRedis\" 1.0 1.1\n",
                encoding="utf-8",
            )
            cfg = {
                "paper_data": {"default_performance": "default-perf.dat"},
                "methodology": {"runs_per_benchmark": 6},
                "benchmarks": {"micro": ["Collections"], "macro": ["RRedis"]},
                "claims": [
                    {
                        "id": "C001",
                        "dataset": "default_performance",
                        "metric": "geomean_delta_percent_vs_unialloc",
                    }
                ],
            }
            required_cells, errors = evaluate.required_performance_sample_cells(
                cfg,
                paper_dir,
                ["default_performance"],
            )
            self.assertEqual(errors, [])
            workloads = [
                {
                    **cell,
                    "runs": 6,
                    "command": [
                        sys.executable,
                        "-c",
                        "import json; print(json.dumps({'seconds': 0.001}))",
                    ],
                    "cwd": ".",
                    "measurement": "stdout_json",
                    "time_field": "seconds",
                    "timeout": 30,
                }
                for cell in required_cells
            ]
            current_summary = {
                "datasets": {
                    "default_performance": {
                        "claim_grade": False,
                        "complete_for_claim": False,
                        "rows": [
                            {"benchmark": "Collections", "values": {"jemalloc": 1.0}},
                            {"benchmark": "RRedis", "values": {"jemalloc": 1.0, "ptmalloc": 1.0}},
                        ],
                        "scope": {
                            "complete_for_claim": False,
                            "sample_counts": {
                                "Collections": {"unialloc": 6, "jemalloc": 6, "ptmalloc": 0},
                                "RRedis": {"unialloc": 1, "jemalloc": 1, "ptmalloc": 1},
                            },
                            "sample_claim_grade_blockers": [
                                "RRedis/jemalloc: diagnostic-only wrapper evidence"
                            ],
                        },
                    }
                }
            }
            gap_plan = evaluate.build_paper_performance_gap_plan(
                cfg,
                paper_dir,
                {
                    "schema_version": 1,
                    "runs": 6,
                    "workloads": workloads,
                },
                current_summary,
                dataset_names=["default_performance"],
                required_runs=6,
                default_plan_runs=6,
                max_workloads=2,
                source_plan_path=tmp / "source-plan.json",
                gap_plan_path=tmp / "gap-plan.json",
            )
        labels = [target["label"] for target in gap_plan["gap_targets"]]
        self.assertEqual(
            labels,
            [
                "default_performance/Collections/ptmalloc",
                "default_performance/RRedis/unialloc",
                "default_performance/RRedis/jemalloc",
                "default_performance/RRedis/ptmalloc",
            ],
        )
        self.assertEqual(gap_plan["summary"]["gap_cell_count"], 4)
        self.assertEqual(gap_plan["summary"]["selected_workload_count"], 2)
        self.assertEqual(gap_plan["summary"]["blocked_gap_cell_count"], 0)
        selected = {
            f"{workload['dataset']}/{workload['benchmark']}/{workload['allocator']}": workload
            for workload in gap_plan["workloads"]
        }
        self.assertEqual(selected["default_performance/Collections/ptmalloc"]["runs"], 6)
        self.assertEqual(selected["default_performance/RRedis/unialloc"]["runs"], 5)
        blocked_jemalloc = next(
            target
            for target in gap_plan["gap_targets"]
            if target["label"] == "default_performance/RRedis/jemalloc"
        )
        self.assertEqual(blocked_jemalloc["runs_needed"], 6)
        self.assertIn(
            "diagnostic-only wrapper evidence",
            blocked_jemalloc["current_evidence"]["sample_claim_grade_blockers"],
        )
        self.assertIn("run-paper-performance-plan", gap_plan["run_commands"]["one_record_probe"])

    def test_paper_performance_gap_plan_blocks_invalid_source_plan_workloads(self) -> None:
        with tempfile.TemporaryDirectory() as raw_tmp:
            tmp = pathlib.Path(raw_tmp)
            paper_dir = tmp / "paper"
            paper_dir.mkdir()
            (paper_dir / "default-perf.dat").write_text(
                "# # jemalloc\n1 \"Collections\" 1.0\n",
                encoding="utf-8",
            )
            cfg = {
                "paper_data": {"default_performance": "default-perf.dat"},
                "methodology": {"runs_per_benchmark": 6},
                "benchmarks": {"micro": ["Collections"]},
            }
            source_plan = {
                "runs": 6,
                "workloads": [
                    {
                        "dataset": "default_performance",
                        "benchmark": "Collections",
                        "allocator": "unialloc",
                        "runs": 6,
                        "command": [
                            "python3",
                            "evaluation/scripts/paper_workload_placeholder.py",
                            "--dataset",
                            "default_performance",
                        ],
                        "cwd": ".",
                        "measurement": "stdout_json",
                        "time_field": "seconds",
                    }
                ],
            }
            gap_plan = evaluate.build_paper_performance_gap_plan(
                cfg,
                paper_dir,
                source_plan,
                {"datasets": {}},
                dataset_names=["default_performance"],
                required_runs=6,
                default_plan_runs=6,
                source_plan_path=tmp / "source-plan.json",
                gap_plan_path=tmp / "gap-plan.json",
            )
        self.assertEqual(gap_plan["summary"]["selected_workload_count"], 0)
        self.assertGreaterEqual(gap_plan["summary"]["blocked_gap_cell_count"], 1)
        self.assertIn("fail-safe placeholder", gap_plan["blocked_targets"][0]["blocked_reason"])

    def test_paper_performance_gap_plan_round_robin_truncation_keeps_later_datasets(self) -> None:
        candidates = [
            {"dataset": "default_performance", "benchmark": f"d{i}", "allocator": "unialloc"}
            for i in range(3)
        ] + [
            {"dataset": "type_isolation", "benchmark": f"t{i}", "allocator": "unialloc"}
            for i in range(3)
        ] + [
            {"dataset": "metadata_segregation", "benchmark": f"m{i}", "allocator": "unialloc"}
            for i in range(3)
        ]
        selected = evaluate.select_gap_workload_candidates(
            candidates,
            max_workloads=5,
            selection_strategy="dataset-round-robin",
        )
        self.assertEqual(
            [workload["dataset"] for workload in selected],
            [
                "default_performance",
                "type_isolation",
                "metadata_segregation",
                "default_performance",
                "type_isolation",
            ],
        )
        paper_order = evaluate.select_gap_workload_candidates(
            candidates,
            max_workloads=5,
            selection_strategy="paper-order",
        )
        self.assertEqual(
            [workload["dataset"] for workload in paper_order],
            [
                "default_performance",
                "default_performance",
                "default_performance",
                "type_isolation",
                "type_isolation",
            ],
        )

    def test_paper_performance_gap_plan_local_fast_first_defers_docker(self) -> None:
        candidates = [
            {
                "dataset": "default_performance",
                "benchmark": "Collections",
                "allocator": "ptmalloc",
                "command": [
                    "python3",
                    "evaluation/scripts/paper_collections_docker_driver.py",
                    "--allocator",
                    "ptmalloc",
                ],
            },
            {
                "dataset": "type_isolation",
                "benchmark": "Collections",
                "allocator": "tcmalloc",
                "command": [
                    "python3",
                    "evaluation/scripts/paper_workload_driver.py",
                    "--allocator",
                    "tcmalloc",
                ],
            },
            {
                "dataset": "default_performance",
                "benchmark": "RJS-Compiler",
                "allocator": "unialloc",
                "command": [
                    "python3",
                    "evaluation/scripts/paper_external_workload.py",
                    "--allocator",
                    "unialloc",
                ],
            },
        ]

        selected = evaluate.select_gap_workload_candidates(
            candidates,
            max_workloads=2,
            selection_strategy="local-fast-first",
        )

        self.assertEqual(
            [(workload["benchmark"], workload["allocator"]) for workload in selected],
            [
                ("Collections", "tcmalloc"),
                ("RJS-Compiler", "unialloc"),
            ],
        )

    def test_platform_workload_runtime_ready_requires_real_matching_events(self) -> None:
        event = {
            "source": "platform_allocator_workload",
            "passed": True,
            "allocator": "UniAlloc",
            "global_allocator": "UniAlloc",
            "global_allocator_active": True,
            "allocator_backend": "page_heap_thread_cache",
            "system_backend": "darwin_mmap",
            "thread_local_backend": "pthread_key_destructor",
            "thread_local_key_ready": True,
            "thread_local_save_failures": 0,
            "allocator_stats_recording_active": True,
            "unialloc_allocator_activity_observed": True,
            "allocator_allocation_counters_consistent": True,
            "allocator_deallocation_counters_consistent": True,
            "allocator_allocated_byte_counters_consistent": True,
            "allocator_coverage_basis_points_valid": True,
            "allocator_total_allocations": 7,
            "allocator_typed_allocations": 0,
            "allocator_fallback_allocations": 7,
            "allocator_live_allocations": 0,
            "allocator_total_deallocations": 7,
            "allocator_typed_deallocations": 0,
            "allocator_fallback_deallocations": 7,
            "allocator_total_allocated_bytes": 4096,
            "allocator_typed_allocated_bytes": 0,
            "allocator_fallback_allocated_bytes": 4096,
            "allocator_coverage_basis_points": 0,
            "target_os": "macos",
            "target_arch": "aarch64",
            "threads": 4,
            "iters": 64,
            "max_len": 1024,
            "duration_ns": 12345,
            "checksum": 99,
        }

        def record_for(parsed_event: dict) -> dict:
            return {
                "passed": True,
                "parsed_event_valid": True,
                "parsed_event": parsed_event,
                "command": [
                    "platform_allocator_workload",
                    "--threads",
                    "4",
                    "--iters",
                    "64",
                    "--max-len",
                    "1024",
                ],
            }

        records = [record_for(event)]
        events = [event]
        ready, blockers = evaluate.platform_workload_runtime_ready(
            records=records,
            events=events,
            requested_samples=1,
            expected_threads=4,
            expected_iters=64,
            expected_max_len=1024,
            expected_target_os="macos",
        )
        self.assertTrue(ready, blockers)

        not_ready, blockers = evaluate.platform_workload_runtime_ready(
            records=records,
            events=[{**event, "threads": 2}],
            requested_samples=1,
            expected_threads=4,
            expected_iters=64,
            expected_max_len=1024,
            expected_target_os="macos",
        )
        self.assertFalse(not_ready)
        self.assertIn("threads does not match", " | ".join(blockers))

        unstable_ready, unstable_blockers = evaluate.platform_workload_runtime_ready(
            records=[
                record_for(event),
                record_for({**event, "checksum": 100}),
            ],
            events=[
                event,
                {**event, "checksum": 100},
            ],
            requested_samples=2,
            expected_threads=4,
            expected_iters=64,
            expected_max_len=1024,
            expected_target_os="macos",
        )
        self.assertFalse(unstable_ready)
        self.assertIn("checksums are not stable", " | ".join(unstable_blockers))

        inactive_allocator_ready, inactive_allocator_blockers = evaluate.platform_workload_runtime_ready(
            records=[record_for({**event, "global_allocator_active": False, "thread_local_key_ready": False})],
            events=[{**event, "global_allocator_active": False, "thread_local_key_ready": False}],
            requested_samples=1,
            expected_threads=4,
            expected_iters=64,
            expected_max_len=1024,
            expected_target_os="macos",
        )
        self.assertFalse(inactive_allocator_ready)
        inactive_joined = " | ".join(inactive_allocator_blockers)
        self.assertIn("active UniAlloc global allocator", inactive_joined)
        self.assertIn("thread-local destructor key is not ready", inactive_joined)

        no_allocator_activity_ready, no_allocator_activity_blockers = evaluate.platform_workload_runtime_ready(
            records=[record_for({**event, "unialloc_allocator_activity_observed": False})],
            events=[{**event, "unialloc_allocator_activity_observed": False}],
            requested_samples=1,
            expected_threads=4,
            expected_iters=64,
            expected_max_len=1024,
            expected_target_os="macos",
        )
        self.assertFalse(no_allocator_activity_ready)
        self.assertIn(
            "allocation/deallocation activity",
            " | ".join(no_allocator_activity_blockers),
        )

        missing_consistency_ready, missing_consistency_blockers = evaluate.platform_workload_runtime_ready(
            records=[
                record_for(
                    {
                        k: v
                        for k, v in event.items()
                        if k != "allocator_allocation_counters_consistent"
                    }
                )
            ],
            events=[
                {
                    k: v
                    for k, v in event.items()
                    if k != "allocator_allocation_counters_consistent"
                }
            ],
            requested_samples=1,
            expected_threads=4,
            expected_iters=64,
            expected_max_len=1024,
            expected_target_os="macos",
        )
        self.assertFalse(missing_consistency_ready)
        self.assertIn(
            "consistent allocation counters",
            " | ".join(missing_consistency_blockers),
        )

        bad_dealloc_ready, bad_dealloc_blockers = evaluate.platform_workload_runtime_ready(
            records=[
                record_for(
                    {
                        **event,
                        "allocator_total_deallocations": 7,
                        "allocator_typed_deallocations": 1,
                        "allocator_fallback_deallocations": 7,
                    }
                )
            ],
            events=[
                {
                    **event,
                    "allocator_total_deallocations": 7,
                    "allocator_typed_deallocations": 1,
                    "allocator_fallback_deallocations": 7,
                }
            ],
            requested_samples=1,
            expected_threads=4,
            expected_iters=64,
            expected_max_len=1024,
            expected_target_os="macos",
        )
        self.assertFalse(bad_dealloc_ready)
        self.assertIn(
            "deallocation counters do not add up",
            " | ".join(bad_dealloc_blockers),
        )

        bad_bytes_ready, bad_bytes_blockers = evaluate.platform_workload_runtime_ready(
            records=[record_for({**event, "allocator_fallback_allocated_bytes": 4095})],
            events=[{**event, "allocator_fallback_allocated_bytes": 4095}],
            requested_samples=1,
            expected_threads=4,
            expected_iters=64,
            expected_max_len=1024,
            expected_target_os="macos",
        )
        self.assertFalse(bad_bytes_ready)
        self.assertIn(
            "allocated-byte counters do not add up",
            " | ".join(bad_bytes_blockers),
        )

        missing_live_ready, missing_live_blockers = evaluate.platform_workload_runtime_ready(
            records=[record_for({k: v for k, v in event.items() if k != "allocator_live_allocations"})],
            events=[{k: v for k, v in event.items() if k != "allocator_live_allocations"}],
            requested_samples=1,
            expected_threads=4,
            expected_iters=64,
            expected_max_len=1024,
            expected_target_os="macos",
        )
        self.assertFalse(missing_live_ready)
        self.assertIn("allocator_live_allocations", " | ".join(missing_live_blockers))

        detached_ready, detached_blockers = evaluate.platform_workload_runtime_ready(
            records=[record_for(event)],
            events=[{**event, "checksum": 101}],
            requested_samples=1,
            expected_threads=4,
            expected_iters=64,
            expected_max_len=1024,
            expected_target_os="macos",
        )
        self.assertFalse(detached_ready)
        self.assertIn("parsed_event is detached", " | ".join(detached_blockers))

    def test_collect_platform_runtime_workload_samples_attaches_parsed_events(self) -> None:
        event = {
            "source": "platform_allocator_workload",
            "passed": True,
            "allocator": "UniAlloc",
            "global_allocator": "UniAlloc",
            "global_allocator_active": True,
            "allocator_backend": "page_heap_thread_cache",
            "system_backend": "test_mmap",
            "thread_local_backend": "pthread_key_destructor",
            "thread_local_key_ready": True,
            "thread_local_save_failures": 0,
            "target_os": "macos",
            "target_arch": "aarch64",
            "threads": 1,
            "iters": 2,
            "max_len": 3,
            "duration_ns": 4,
            "checksum": 5,
        }
        with tempfile.TemporaryDirectory() as raw_tmp:
            tmp = pathlib.Path(raw_tmp)
            records, events, logs = evaluate.collect_platform_runtime_workload_samples(
                out_dir=tmp,
                label_prefix="runtime-sample",
                command=[
                    sys.executable,
                    "-c",
                    "import json; print(json.dumps(%r))" % event,
                ],
                timeout=30,
                sample_count=2,
            )
            self.assertEqual(len(records), 2)
            self.assertEqual(events, [event, event])
            self.assertEqual(len(logs), 2)
            self.assertTrue(all(record["parsed_event_valid"] for record in records))
            self.assertTrue(all(path.exists() for path in logs))

    def test_collect_imported_platform_runtime_workload_samples_attaches_sha_and_event(self) -> None:
        event = {
            "source": "platform_allocator_workload",
            "passed": True,
            "allocator": "UniAlloc",
            "global_allocator": "UniAlloc",
            "global_allocator_active": True,
            "allocator_backend": "page_heap_thread_cache",
            "system_backend": "windows_virtual_alloc",
            "thread_local_backend": "fls_callback",
            "thread_local_key_ready": True,
            "thread_local_save_failures": 0,
            "target_os": "windows",
            "target_family": "windows",
            "target_arch": "x86_64",
            "threads": 2,
            "iters": 16,
            "max_len": 256,
            "duration_ns": 1234,
            "checksum": 9876,
        }
        with tempfile.TemporaryDirectory() as raw_tmp:
            tmp = pathlib.Path(raw_tmp)
            imported = tmp / "windows-runtime-stdout.log"
            imported.write_text(
                "Windows runner banner\n" + json.dumps(event, sort_keys=True) + "\n",
                encoding="utf-8",
            )
            records, events, logs = evaluate.collect_imported_platform_runtime_workload_samples(
                out_dir=tmp,
                label_prefix="windows-runtime-import",
                artifact_paths=[imported],
            )
            self.assertEqual(events, [event])
            self.assertEqual(records[0]["parsed_event"], event)
            self.assertTrue(records[0]["parsed_event_valid"])
            self.assertEqual(records[0]["source_artifact"], str(imported))
            self.assertRegex(records[0]["source_artifact_sha256"], r"^[0-9a-f]{64}$")
            self.assertTrue(logs[0].exists())
            artifacts = evaluate.platform_runtime_run_artifacts(records, logs)
            self.assertTrue(artifacts[0]["external_runtime_import"])
            self.assertEqual(artifacts[0]["source_artifact"], str(imported))
            self.assertEqual(
                artifacts[0]["source_artifact_sha256"],
                records[0]["source_artifact_sha256"],
            )

    def test_run_platform_command_applies_extra_env(self) -> None:
        with tempfile.TemporaryDirectory() as raw_tmp:
            tmp = pathlib.Path(raw_tmp)
            result = evaluate.run_platform_command(
                tmp,
                "extra-env",
                [sys.executable, "-c", "import os; print(os.environ.get('UNIALLOC_TEST_ENV'))"],
                timeout=30,
                extra_env={"UNIALLOC_TEST_ENV": "present"},
            )
            stdout = pathlib.Path(result["stdout"]).read_text(encoding="utf-8")
        self.assertTrue(result["passed"], result)
        self.assertEqual(stdout.strip(), "present")
        self.assertEqual(result["env_overrides"]["UNIALLOC_TEST_ENV"], "present")

    def test_windows_linker_env_key_normalizes_rust_target(self) -> None:
        self.assertEqual(
            evaluate.windows_linker_env_key("x86_64-pc-windows-gnu"),
            "CARGO_TARGET_X86_64_PC_WINDOWS_GNU_LINKER",
        )

    def test_windows_linker_selection_fails_closed_for_bad_explicit_path(self) -> None:
        with tempfile.TemporaryDirectory() as raw_tmp:
            missing = pathlib.Path(raw_tmp) / "missing-linker"
            selection = evaluate.windows_linker_selection(
                "x86_64-pc-windows-gnu",
                str(missing),
            )
        self.assertFalse(selection["available"])
        self.assertEqual(selection["source"], "--windows-linker")
        self.assertIn("not executable", " | ".join(selection["diagnostics"]))

    def test_windows_link_failure_blockers_classify_missing_import_libraries(self) -> None:
        blockers = evaluate.windows_link_failure_blockers(
            "\n".join(
                [
                    "lld: error: unable to find library -ladvapi32",
                    "lld: error: unable to find library -lkernel32",
                ]
            )
        )
        self.assertIn("missing Windows import/runtime libraries", " | ".join(blockers))
        self.assertIn("-ladvapi32", " | ".join(blockers))

    def test_windows_runtime_runner_selection_respects_explicit_executable(self) -> None:
        with tempfile.TemporaryDirectory() as raw_tmp:
            runner = pathlib.Path(raw_tmp) / "fake-wine"
            runner.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
            runner.chmod(0o755)
            selection = evaluate.windows_runtime_runner_selection(str(runner))
        self.assertTrue(selection["available"], selection)
        self.assertEqual(selection["source"], "--windows-runtime-runner")
        self.assertEqual(selection["mode"], "runner")
        self.assertEqual(selection["runner"], str(runner.resolve()))
        self.assertEqual(selection["command_prefix"], [str(runner.resolve())])
        self.assertEqual(evaluate.windows_runtime_command_prefix(selection), [str(runner.resolve())])

    def test_windows_runtime_runner_selection_allows_native_windows_host(self) -> None:
        with mock.patch.object(evaluate.platform, "system", return_value="Windows"), mock.patch.object(
            evaluate.shutil, "which", return_value=None
        ):
            selection = evaluate.windows_runtime_runner_selection()
        self.assertTrue(selection["available"], selection)
        self.assertEqual(selection["source"], "native-host")
        self.assertEqual(selection["mode"], "native")
        self.assertIsNone(selection["runner"])
        self.assertEqual(selection["command_prefix"], [])
        self.assertEqual(evaluate.windows_runtime_command_prefix(selection), [])

    def test_windows_runtime_runner_selection_fails_closed_without_host_or_wine(self) -> None:
        with mock.patch.object(evaluate.platform, "system", return_value="Darwin"), mock.patch.object(
            evaluate.shutil, "which", return_value=None
        ):
            selection = evaluate.windows_runtime_runner_selection()
        self.assertFalse(selection["available"], selection)
        self.assertEqual(selection["mode"], "runner")
        self.assertIn("no native Windows host", " | ".join(selection["diagnostics"]))
        self.assertEqual(evaluate.windows_runtime_command_prefix(selection), [])

    def test_windows_runtime_runner_selection_can_use_ready_repo_docker_wine_runner(self) -> None:
        with tempfile.TemporaryDirectory() as raw_tmp:
            tmp = pathlib.Path(raw_tmp)
            runner = tmp / "tools" / "windows" / "docker-wine-runner.sh"
            runner.parent.mkdir(parents=True)
            runner.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
            runner.chmod(0o755)
            with mock.patch.object(evaluate, "ROOT", tmp), mock.patch.object(
                evaluate.platform, "system", return_value="Darwin"
            ), mock.patch.object(evaluate.shutil, "which", return_value=None), mock.patch.object(
                evaluate,
                "docker_image_status",
                return_value={
                    "image": "unialloc-windows-wine-runner:trixie",
                    "available": True,
                    "docker": "/usr/bin/docker",
                    "image_id": "sha256:test",
                    "diagnostics": [],
                },
            ):
                selection = evaluate.windows_runtime_runner_selection()
        self.assertTrue(selection["available"], selection)
        self.assertEqual(selection["source"], "repo-docker-wine-runner")
        self.assertEqual(selection["runner"], str(runner.resolve()))
        self.assertEqual(evaluate.windows_runtime_command_prefix(selection), [str(runner.resolve())])
        self.assertEqual(selection["docker_image"]["image_id"], "sha256:test")

    def test_platform_command_artifact_preserves_command_log_and_env(self) -> None:
        with tempfile.TemporaryDirectory() as raw_tmp:
            tmp = pathlib.Path(raw_tmp)
            log = tmp / "windows-runtime-workload-check.log"
            log.write_text("real command log\n", encoding="utf-8")
            artifact = evaluate.platform_command_artifact(
                {
                    "label": "windows-runtime-workload-check",
                    "command": ["cargo", "check"],
                    "exit_code": 0,
                    "passed": True,
                    "stdout": str(tmp / "stdout.txt"),
                    "stderr": str(tmp / "stderr.txt"),
                    "env_overrides": {"CARGO_TARGET_X86_64_PC_WINDOWS_GNU_LINKER": "/tool"},
                },
                log=log,
            )
        self.assertEqual(artifact["label"], "windows-runtime-workload-check")
        self.assertEqual(artifact["command"], ["cargo", "check"])
        self.assertTrue(artifact["passed"])
        self.assertEqual(artifact["log"], str(log))
        self.assertIn("CARGO_TARGET_X86_64_PC_WINDOWS_GNU_LINKER", artifact["env_overrides"])

    def test_parse_last_json_stdout_event_returns_last_structured_line(self) -> None:
        with tempfile.TemporaryDirectory() as raw_tmp:
            tmp = pathlib.Path(raw_tmp)
            stdout = tmp / "stdout.txt"
            stdout.write_text(
                "noise\n{\"passed\": false}\n{\"passed\": true, \"init_api\": \"unialloc_fixed_heap_try_init\"}\n",
                encoding="utf-8",
            )
            event = evaluate.parse_last_json_stdout_event({"stdout": str(stdout)})
        self.assertIsNotNone(event)
        assert event is not None
        self.assertTrue(event["passed"])
        self.assertEqual(event["init_api"], "unialloc_fixed_heap_try_init")

    def test_rust_for_linux_unialloc_source_contract_requires_real_bridge(self) -> None:
        with tempfile.TemporaryDirectory() as raw_tmp:
            module_dir = pathlib.Path(raw_tmp)
            (module_dir / "rust_bench.rs").write_text(
                """
                mod unialloc_bridge;
                fn init() {
                    let ready = unialloc_bridge::ensure_initialized();
                    if !ready {
                        pr_alert!("UniAlloc fixed-heap bridge initialization failed");
                        return Err(Error::EINVAL);
                    }
                    run_semantic_bridge_probe();
                    unialloc_bridge::reset_semantic_stats();
                    if let Some(stats) = unialloc_bridge::semantic_stats_snapshot() {
                        pr_info!("allocator_total_deallocations={} allocator_typed_allocations={} allocator_typed_deallocations={} allocator_type_stats_rows={} allocator_type_stats_dropped_events={} allocator_type_stats_probe_matched=true", stats.total_deallocations, stats.typed_allocations, stats.typed_deallocations, 1, stats.semantic_type_stats_dropped_events);
                    }
                    let _ = unialloc_bridge::semantic_fallback_attribution_snapshot_abi();
                    if let Some(fallback) = unialloc_bridge::semantic_fallback_attribution_snapshot() {
                        pr_info!("raw_alloc_no_metadata={} realloc_recorded_old_metadata_new_allocations={}", fallback.raw_alloc_no_metadata, fallback.realloc_recorded_old_metadata_new_allocations);
                    }
                    let _ = unialloc_bridge::semantic_metadata_validation_snapshot_abi();
                    if let Some(validation) = unialloc_bridge::semantic_metadata_validation_snapshot() {
                        pr_info!("recovery_identity_matches={} recovery_identity_mismatches={} last_mismatch_requested_type_id={} last_mismatch_recorded_type_id={}", validation.recovery_identity_matches, validation.recovery_identity_mismatches, validation.last_mismatch_requested_type_id, validation.last_mismatch_recorded_type_id);
                    }
                    pr_info!("UNIALLOC_CONSTRAINED_BOOT_SAMPLE platform=rust-for-linux boot_cycle=1 allocator_type_stats_dropped_events=0 c_abi_probe_passed=true");
                }
                fn run_semantic_bridge_probe() {
                    pr_info!("allocator_typed_allocations=1 allocator_typed_deallocations=1 allocator_type_stats_rows=1 allocator_type_stats_dropped_events=0 allocator_type_stats_probe_matched=true");
                }
                """,
                encoding="utf-8",
            )
            (module_dir / "unialloc_bridge.rs").write_text(
                """
                use core::alloc::{GlobalAlloc, Layout};
                use core::mem::MaybeUninit;
                const UNIALLOC_SEMANTIC_STATS_SNAPSHOT_ABI_VERSION: u32 = 3;
                const UNIALLOC_SEMANTIC_TYPE_STATS_SNAPSHOT_ABI_VERSION: u32 = 2;
                const UNIALLOC_SEMANTIC_FALLBACK_ATTRIBUTION_SNAPSHOT_ABI_VERSION: u32 = 1;
                const UNIALLOC_SEMANTIC_METADATA_VALIDATION_SNAPSHOT_ABI_VERSION: u32 = 1;
                const UNIALLOC_CONSTRAINED_BOOT_SAMPLE_ABI_VERSION: u32 = 2;
                const UNIALLOC_FLAG_TYPE_ISOLATED: u32 = 1 << 0;
                #[repr(C)]
                pub struct SemanticStatsSnapshot {
                    pub total_allocations: usize,
                    pub typed_allocations: usize,
                    pub fallback_allocations: usize,
                    pub total_allocated_bytes: usize,
                    pub typed_allocated_bytes: usize,
                    pub fallback_allocated_bytes: usize,
                    pub typed_deallocations: usize,
                    pub fallback_deallocations: usize,
                    pub policy_flags_seen: u32,
                    pub last_type_id: u64,
                    pub coverage_basis_points: usize,
                    pub typed_cache_hits: usize,
                    pub typed_cache_inserts: usize,
                    pub typed_cache_bypasses: usize,
                    pub delayed_free_enqueues: usize,
                    pub delayed_free_flushes: usize,
                    pub metadata_pac_auth_signs: usize,
                    pub metadata_pac_auth_verifications: usize,
                    pub metadata_pac_auth_failures: usize,
                    pub metadata_pac_software_fallback_signs: usize,
                    pub metadata_pac_software_fallback_verifications: usize,
                    pub metadata_pac_software_fallback_failures: usize,
                    pub total_deallocations: usize,
                    pub semantic_type_stats_dropped_events: usize,
                }
                #[repr(C)]
                pub struct SemanticTypeStatsSnapshot {
                    pub type_id: u64,
                    pub module_id: u64,
                    pub callsite: u64,
                    pub allocations: usize,
                    pub allocated_bytes: usize,
                    pub deallocations: usize,
                    pub cache_hits: usize,
                    pub cache_inserts: usize,
                    pub cache_bypasses: usize,
                    pub observed_alloc_size: usize,
                    pub observed_alloc_align: usize,
                    pub observed_dealloc_size: usize,
                    pub observed_dealloc_align: usize,
                    pub policy_flags_seen: u32,
                }
                #[repr(C)]
                pub struct SemanticFallbackAttributionSnapshot {
                    pub raw_alloc_no_metadata: usize,
                    pub raw_alloc_no_metadata_bytes: usize,
                    pub raw_dealloc_no_metadata: usize,
                    pub raw_realloc_no_metadata: usize,
                    pub raw_realloc_no_metadata_bytes: usize,
                    pub raw_realloc_moved_dealloc_no_metadata: usize,
                    pub realloc_recorded_old_metadata_new_allocations: usize,
                    pub realloc_recorded_old_metadata_new_allocation_bytes: usize,
                }
                #[repr(C)]
                pub struct SemanticMetadataValidationSnapshot {
                    pub recovery_identity_matches: usize,
                    pub recovery_identity_mismatches: usize,
                    pub last_mismatch_requested_type_id: u64,
                    pub last_mismatch_recorded_type_id: u64,
                    pub last_mismatch_requested_module_id: u64,
                    pub last_mismatch_recorded_module_id: u64,
                    pub last_mismatch_requested_callsite: u64,
                    pub last_mismatch_recorded_callsite: u64,
                }
                pub struct CAbiProbeReport {
                    pub passed: bool,
                }
                #[repr(C)]
                pub struct ConstrainedBootSample {
                    pub boot_cycle: usize,
                    pub fixed_heap_ready: bool,
                    pub c_abi_invoked: bool,
                    pub c_abi_ready_after_init: bool,
                    pub c_abi_round_trips: usize,
                    pub c_abi_invalid_layouts_rejected: usize,
                    pub c_abi_over_page_alignment_checked: bool,
                    pub c_abi_over_page_alignment: usize,
                    pub allocator_total_allocations: usize,
                    pub allocator_total_deallocations: usize,
                    pub allocator_typed_allocations: usize,
                    pub allocator_typed_deallocations: usize,
                    pub allocator_fallback_allocations: usize,
                    pub allocator_fallback_deallocations: usize,
                    pub allocator_total_allocated_bytes: usize,
                    pub allocator_typed_allocated_bytes: usize,
                    pub allocator_fallback_allocated_bytes: usize,
                    pub allocator_coverage_basis_points: usize,
                    pub allocator_type_stats_rows: usize,
                    pub allocator_type_stats_dropped_events: usize,
                    pub allocator_type_stats_probe_matched: bool,
                }
                #[repr(align(4096))]
                struct Heap([u8; UNIALLOC_KERNEL_HEAP_BYTES]);
                const UNIALLOC_KERNEL_HEAP_BYTES: usize = 4096;
                #[global_allocator]
                static A: UniAllocKernelGlobal = UniAllocKernelGlobal;
                struct UniAllocKernelGlobal;
                unsafe impl GlobalAlloc for UniAllocKernelGlobal {
                    unsafe fn alloc(&self, layout: Layout) -> *mut u8 {
                        unialloc_alloc(layout.size(), layout.align())
                    }
                    unsafe fn dealloc(&self, ptr: *mut u8, layout: Layout) {
                        unialloc_dealloc(ptr, layout.size(), layout.align())
                    }
                    unsafe fn realloc(&self, ptr: *mut u8, layout: Layout, new_size: usize) -> *mut u8 {
                        unialloc_realloc(ptr, layout.size(), layout.align(), new_size)
                    }
                }
                extern "C" {
                    fn unialloc_fixed_heap_try_init(a: usize, b: usize, c: usize) -> bool;
                    fn unialloc_fixed_heap_try_extend(a: usize, b: usize) -> bool;
                    fn unialloc_alloc(size: usize, align: usize) -> *mut u8;
                    fn unialloc_dealloc(ptr: *mut u8, size: usize, align: usize);
                    fn unialloc_realloc(ptr: *mut u8, old_size: usize, old_align: usize, new_size: usize) -> *mut u8;
                    fn __unialloc_semantic_stats_snapshot_abi_version() -> u32;
                    fn __unialloc_semantic_stats_snapshot_size() -> usize;
                    fn __unialloc_alloc_with_metadata(size: usize, align: usize, type_id: u64, module_id: u64, flags: u32, callsite: u64) -> *mut u8;
                    fn __unialloc_dealloc_with_metadata(ptr: *mut u8, size: usize, align: usize, type_id: u64, module_id: u64, flags: u32, callsite: u64) -> bool;
                    fn __unialloc_semantic_stats_snapshot_checked(out: *mut SemanticStatsSnapshot, out_size: usize) -> bool;
                    fn __unialloc_semantic_stats_reset();
                    fn __unialloc_semantic_type_stats_snapshot_abi_version() -> u32;
                    fn __unialloc_semantic_type_stats_snapshot_record_size() -> usize;
                    fn __unialloc_semantic_type_stats_snapshot_checked(out: *mut SemanticTypeStatsSnapshot, len: usize, record_size: usize) -> usize;
                    fn __unialloc_semantic_fallback_attribution_snapshot_abi_version() -> u32;
                    fn __unialloc_semantic_fallback_attribution_snapshot_size() -> usize;
                    fn __unialloc_semantic_fallback_attribution_snapshot_checked(out: *mut SemanticFallbackAttributionSnapshot, out_size: usize) -> bool;
                    fn __unialloc_semantic_metadata_validation_snapshot_abi_version() -> u32;
                    fn __unialloc_semantic_metadata_validation_snapshot_size() -> usize;
                    fn __unialloc_semantic_metadata_validation_snapshot_checked(out: *mut SemanticMetadataValidationSnapshot, out_size: usize) -> bool;
                    fn __unialloc_constrained_boot_sample_abi_version() -> u32;
                    fn __unialloc_constrained_boot_sample_size() -> usize;
                    fn __unialloc_constrained_boot_sample_checked(out: *mut ConstrainedBootSample, out_size: usize) -> bool;
                }
                fn try_extend_reserved(size: usize) -> bool {
                    unsafe { unialloc_fixed_heap_try_extend(size, 4096) }
                }
                fn reset_semantic_stats() {
                    unsafe { __unialloc_semantic_stats_reset() }
                }
                unsafe fn semantic_alloc_with_metadata(size: usize, align: usize, type_id: u64, module_id: u64, flags: u32, callsite: u64) -> *mut u8 {
                    __unialloc_alloc_with_metadata(size, align, type_id, module_id, flags, callsite)
                }
                unsafe fn semantic_dealloc_with_metadata(ptr: *mut u8, size: usize, align: usize, type_id: u64, module_id: u64, flags: u32, callsite: u64) -> bool {
                    __unialloc_dealloc_with_metadata(ptr, size, align, type_id, module_id, flags, callsite)
                }
                fn semantic_type_stats_snapshot(out: &mut [SemanticTypeStatsSnapshot]) -> Option<usize> {
                    let _ = __unialloc_semantic_type_stats_snapshot_abi_version;
                    let _ = __unialloc_semantic_type_stats_snapshot_record_size;
                    Some(unsafe { __unialloc_semantic_type_stats_snapshot_checked(out.as_mut_ptr(), out.len(), core::mem::size_of::<SemanticTypeStatsSnapshot>()) })
                }
                fn semantic_fallback_attribution_snapshot_abi() -> (u32, usize) {
                    unsafe {
                        (
                            __unialloc_semantic_fallback_attribution_snapshot_abi_version(),
                            __unialloc_semantic_fallback_attribution_snapshot_size(),
                        )
                    }
                }
                fn semantic_fallback_attribution_snapshot() -> Option<SemanticFallbackAttributionSnapshot> {
                    let _ = semantic_fallback_attribution_snapshot_abi();
                    let mut out = MaybeUninit::<SemanticFallbackAttributionSnapshot>::uninit();
                    let copied = unsafe {
                        __unialloc_semantic_fallback_attribution_snapshot_checked(out.as_mut_ptr(), core::mem::size_of::<SemanticFallbackAttributionSnapshot>())
                    };
                    if copied {
                        Some(unsafe { out.assume_init() })
                    } else {
                        None
                    }
                }
                fn semantic_metadata_validation_snapshot_abi() -> (u32, usize) {
                    unsafe {
                        (
                            __unialloc_semantic_metadata_validation_snapshot_abi_version(),
                            __unialloc_semantic_metadata_validation_snapshot_size(),
                        )
                    }
                }
                fn semantic_metadata_validation_snapshot() -> Option<SemanticMetadataValidationSnapshot> {
                    let _ = semantic_metadata_validation_snapshot_abi();
                    let mut out = MaybeUninit::<SemanticMetadataValidationSnapshot>::uninit();
                    let copied = unsafe {
                        __unialloc_semantic_metadata_validation_snapshot_checked(out.as_mut_ptr(), core::mem::size_of::<SemanticMetadataValidationSnapshot>())
                    };
                    if copied {
                        Some(unsafe { out.assume_init() })
                    } else {
                        None
                    }
                }
                fn semantic_stats_snapshot() -> Option<SemanticStatsSnapshot> {
                    let mut out = MaybeUninit::<SemanticStatsSnapshot>::uninit();
                    let copied = unsafe {
                        __unialloc_semantic_stats_snapshot_checked(out.as_mut_ptr(), core::mem::size_of::<SemanticStatsSnapshot>())
                    };
                    if copied {
                        Some(unsafe { out.assume_init() })
                    } else {
                        None
                    }
                }
                fn constrained_boot_sample(c_abi: CAbiProbeReport) -> Option<ConstrainedBootSample> {
                    let _ = c_abi.passed;
                    let _ = __unialloc_constrained_boot_sample_abi_version;
                    let _ = __unialloc_constrained_boot_sample_size;
                    let mut out = MaybeUninit::<ConstrainedBootSample>::uninit();
                    let copied = unsafe {
                        __unialloc_constrained_boot_sample_checked(out.as_mut_ptr(), core::mem::size_of::<ConstrainedBootSample>())
                    };
                    if copied {
                        Some(unsafe { out.assume_init() })
                    } else {
                        None
                    }
                }
                """,
                encoding="utf-8",
            )
            (module_dir / "unialloc_kernel_ffi.h").write_text(
                "#define UNIALLOC_SEMANTIC_STATS_SNAPSHOT_ABI_VERSION 3u\n"
                "#define UNIALLOC_SEMANTIC_TYPE_STATS_SNAPSHOT_ABI_VERSION 2u\n"
                "#define UNIALLOC_SEMANTIC_FALLBACK_ATTRIBUTION_SNAPSHOT_ABI_VERSION 1u\n"
                "#define UNIALLOC_SEMANTIC_METADATA_VALIDATION_SNAPSHOT_ABI_VERSION 1u\n"
                "#define UNIALLOC_CONSTRAINED_BOOT_SAMPLE_ABI_VERSION 2u\n"
                "#define UNIALLOC_FLAG_TYPE_ISOLATED (1u << 0)\n"
                "typedef struct UniallocSemanticStatsSnapshot {\n"
                "size_t total_allocations; size_t typed_allocations; size_t fallback_allocations; size_t total_allocated_bytes; size_t typed_allocated_bytes; size_t fallback_allocated_bytes; size_t typed_deallocations; size_t fallback_deallocations; uint32_t policy_flags_seen; uint64_t last_type_id; size_t coverage_basis_points; size_t typed_cache_hits; size_t typed_cache_inserts; size_t typed_cache_bypasses; size_t delayed_free_enqueues; size_t delayed_free_flushes; size_t metadata_pac_auth_signs; size_t metadata_pac_auth_verifications; size_t metadata_pac_auth_failures; size_t metadata_pac_software_fallback_signs; size_t metadata_pac_software_fallback_verifications; size_t metadata_pac_software_fallback_failures; size_t total_deallocations; size_t semantic_type_stats_dropped_events;\n"
                "} UniallocSemanticStatsSnapshot;\n"
                "typedef struct UniallocSemanticTypeStatsSnapshot { uint64_t type_id; uint64_t module_id; uint64_t callsite; size_t allocations; size_t allocated_bytes; size_t deallocations; size_t cache_hits; size_t cache_inserts; size_t cache_bypasses; size_t observed_alloc_size; size_t observed_alloc_align; size_t observed_dealloc_size; size_t observed_dealloc_align; uint32_t policy_flags_seen; } UniallocSemanticTypeStatsSnapshot;\n"
                "typedef struct UniallocSemanticFallbackAttributionSnapshot { size_t raw_alloc_no_metadata; size_t raw_alloc_no_metadata_bytes; size_t raw_dealloc_no_metadata; size_t raw_realloc_no_metadata; size_t raw_realloc_no_metadata_bytes; size_t raw_realloc_moved_dealloc_no_metadata; size_t realloc_recorded_old_metadata_new_allocations; size_t realloc_recorded_old_metadata_new_allocation_bytes; } UniallocSemanticFallbackAttributionSnapshot;\n"
                "typedef struct UniallocSemanticMetadataValidationSnapshot { size_t recovery_identity_matches; size_t recovery_identity_mismatches; uint64_t last_mismatch_requested_type_id; uint64_t last_mismatch_recorded_type_id; uint64_t last_mismatch_requested_module_id; uint64_t last_mismatch_recorded_module_id; uint64_t last_mismatch_requested_callsite; uint64_t last_mismatch_recorded_callsite; } UniallocSemanticMetadataValidationSnapshot;\n"
                "typedef struct UniallocConstrainedBootSample { size_t boot_cycle; bool fixed_heap_ready; bool c_abi_invoked; bool c_abi_ready_after_init; size_t c_abi_round_trips; size_t c_abi_invalid_layouts_rejected; bool c_abi_over_page_alignment_checked; size_t c_abi_over_page_alignment; size_t allocator_total_allocations; size_t allocator_total_deallocations; size_t allocator_typed_allocations; size_t allocator_typed_deallocations; size_t allocator_fallback_allocations; size_t allocator_fallback_deallocations; size_t allocator_total_allocated_bytes; size_t allocator_typed_allocated_bytes; size_t allocator_fallback_allocated_bytes; size_t allocator_coverage_basis_points; size_t allocator_type_stats_rows; size_t allocator_type_stats_dropped_events; bool allocator_type_stats_probe_matched; } UniallocConstrainedBootSample;\n"
                "bool unialloc_fixed_heap_try_init(size_t,size_t,size_t);\n"
                "bool unialloc_fixed_heap_try_extend(size_t,size_t);\n"
                "void *unialloc_alloc(size_t,size_t);\n"
                "void *unialloc_realloc(void *,size_t,size_t,size_t);\n"
                "void *__unialloc_alloc_with_metadata(size_t,size_t,uint64_t,uint64_t,uint32_t,uint64_t);\n"
                "bool __unialloc_dealloc_with_metadata(void *,size_t,size_t,uint64_t,uint64_t,uint32_t,uint64_t);\n"
                "uint32_t __unialloc_semantic_stats_snapshot_abi_version(void);\n"
                "size_t __unialloc_semantic_stats_snapshot_size(void);\n"
                "bool __unialloc_semantic_stats_snapshot_checked(UniallocSemanticStatsSnapshot *,size_t);\n"
                "bool __unialloc_semantic_stats_snapshot(UniallocSemanticStatsSnapshot *);\n"
                "void __unialloc_semantic_stats_reset(void);\n"
                "uint32_t __unialloc_semantic_type_stats_snapshot_abi_version(void);\n"
                "size_t __unialloc_semantic_type_stats_snapshot_record_size(void);\n"
                "size_t __unialloc_semantic_type_stats_snapshot_checked(UniallocSemanticTypeStatsSnapshot *,size_t,size_t);\n"
                "size_t __unialloc_semantic_type_stats_snapshot(UniallocSemanticTypeStatsSnapshot *,size_t);\n"
                "uint32_t __unialloc_semantic_fallback_attribution_snapshot_abi_version(void);\n"
                "size_t __unialloc_semantic_fallback_attribution_snapshot_size(void);\n"
                "bool __unialloc_semantic_fallback_attribution_snapshot_checked(UniallocSemanticFallbackAttributionSnapshot *,size_t);\n"
                "bool __unialloc_semantic_fallback_attribution_snapshot(UniallocSemanticFallbackAttributionSnapshot *);\n"
                "uint32_t __unialloc_semantic_metadata_validation_snapshot_abi_version(void);\n"
                "size_t __unialloc_semantic_metadata_validation_snapshot_size(void);\n"
                "bool __unialloc_semantic_metadata_validation_snapshot_checked(UniallocSemanticMetadataValidationSnapshot *,size_t);\n"
                "bool __unialloc_semantic_metadata_validation_snapshot(UniallocSemanticMetadataValidationSnapshot *);\n"
                "uint32_t __unialloc_constrained_boot_sample_abi_version(void);\n"
                "size_t __unialloc_constrained_boot_sample_size(void);\n"
                "bool __unialloc_constrained_boot_sample_checked(UniallocConstrainedBootSample *,size_t);\n",
                encoding="utf-8",
            )
            (module_dir / "Makefile").write_text(
                "UNIALLOC_RLIB := target/libunialloc.rlib\n"
                "UNIALLOC_FEATURES := fixed_heap,allow_mem_leak,stats\n"
                "cargo build -p unialloc --lib --no-default-features --features $(UNIALLOC_FEATURES)\n"
                "make RUSTFLAGS_MODULE='--extern unialloc=$(UNIALLOC_RLIB) -Ldependency=target/deps'\n",
                encoding="utf-8",
            )
            audit = evaluate.rust_for_linux_unialloc_source_contract(module_dir)
        self.assertTrue(audit["passed"], audit.get("blockers"))
        self.assertTrue(audit["checks"]["bridge_declares_global_allocator"])
        self.assertTrue(audit["checks"]["bridge_snapshots_semantic_stats_checked_abi"])
        self.assertTrue(audit["checks"]["bridge_exposes_semantic_type_metadata_abi"])
        self.assertTrue(audit["checks"]["bridge_snapshots_semantic_type_stats_checked_abi"])
        self.assertTrue(audit["checks"]["bridge_snapshots_semantic_fallback_attribution_checked_abi"])
        self.assertTrue(audit["checks"]["bridge_snapshots_semantic_metadata_validation_checked_abi"])
        self.assertTrue(audit["checks"]["bridge_semantic_snapshot_layouts_match_allocator"])
        self.assertTrue(audit["checks"]["header_semantic_snapshot_layouts_match_allocator"])
        self.assertTrue(audit["checks"]["bridge_constrained_boot_abi_version_matches_allocator"])
        self.assertTrue(audit["checks"]["bridge_constrained_boot_layout_matches_allocator"])
        self.assertTrue(audit["checks"]["header_constrained_boot_abi_version_matches_allocator"])
        self.assertTrue(audit["checks"]["header_constrained_boot_layout_matches_allocator"])
        self.assertTrue(audit["checks"]["module_reports_semantic_type_stats_probe"])
        self.assertTrue(audit["checks"]["module_reports_semantic_fallback_attribution"])
        self.assertTrue(audit["checks"]["module_reports_semantic_metadata_validation"])
        self.assertTrue(audit["checks"]["makefile_builds_rlib_dependency"])
        self.assertTrue(audit["checks"]["makefile_passes_rlib_to_final_module_crate"])
        self.assertTrue(audit["checks"]["module_resets_and_reports_allocator_stats"])
        self.assertTrue(audit["checks"]["header_exports_semantic_stats_checked_abi"])
        self.assertTrue(audit["checks"]["header_exports_semantic_type_metadata_abi"])
        self.assertTrue(audit["checks"]["header_exports_semantic_type_stats_checked_abi"])
        self.assertTrue(audit["checks"]["header_exports_semantic_fallback_attribution_checked_abi"])
        self.assertTrue(audit["checks"]["header_exports_semantic_metadata_validation_checked_abi"])
        self.assertTrue(audit["checks"]["makefile_enables_allocator_stats_feature"])
        self.assertTrue(audit["checks"]["module_resets_and_reports_allocator_stats"])
        self.assertTrue(audit["checks"]["header_exports_semantic_stats_checked_abi"])
        self.assertTrue(audit["checks"]["header_exports_semantic_type_metadata_abi"])
        self.assertTrue(audit["checks"]["header_exports_semantic_type_stats_checked_abi"])
        self.assertTrue(audit["checks"]["header_exports_semantic_fallback_attribution_checked_abi"])
        self.assertTrue(audit["checks"]["header_exports_semantic_metadata_validation_checked_abi"])
        self.assertTrue(audit["checks"]["makefile_enables_allocator_stats_feature"])

    def test_real_kernel_ffi_header_exports_size_negotiated_semantic_stats_abi(self) -> None:
        source = (
            ROOT / "kernel" / "kernel-modules" / "benchmarking" / "unialloc_kernel_ffi.h"
        ).read_text(encoding="utf-8")
        self.assertIn("typedef struct UniallocSemanticStatsSnapshot", source)
        self.assertIn("UNIALLOC_SEMANTIC_STATS_SNAPSHOT_ABI_VERSION 3u", source)
        self.assertIn("semantic_type_stats_dropped_events", source)
        self.assertIn("UNIALLOC_CONSTRAINED_BOOT_SAMPLE_ABI_VERSION 2u", source)
        self.assertIn("uint32_t policy_flags_seen", source)
        self.assertIn("uint64_t last_type_id", source)
        self.assertIn("uint32_t __unialloc_semantic_stats_snapshot_abi_version", source)
        self.assertIn("size_t total_deallocations", source)
        self.assertIn("__unialloc_semantic_stats_snapshot_abi_version", source)
        self.assertIn("__unialloc_semantic_stats_snapshot_size", source)
        self.assertIn("__unialloc_semantic_stats_snapshot_checked", source)
        self.assertIn("__unialloc_semantic_stats_snapshot(", source)
        self.assertIn("__unialloc_semantic_stats_reset", source)
        self.assertIn("UNIALLOC_SEMANTIC_TYPE_STATS_SNAPSHOT_ABI_VERSION 2u", source)
        self.assertIn("typedef struct UniallocSemanticTypeStatsSnapshot", source)
        self.assertIn("__unialloc_alloc_with_metadata", source)
        self.assertIn("__unialloc_dealloc_with_metadata", source)
        self.assertIn("__unialloc_semantic_type_stats_snapshot_abi_version", source)
        self.assertIn("__unialloc_semantic_type_stats_snapshot_record_size", source)
        self.assertIn("__unialloc_semantic_type_stats_snapshot_checked", source)
        self.assertIn("UNIALLOC_SEMANTIC_FALLBACK_ATTRIBUTION_SNAPSHOT_ABI_VERSION 1u", source)
        self.assertIn("typedef struct UniallocSemanticFallbackAttributionSnapshot", source)
        self.assertIn("__unialloc_semantic_fallback_attribution_snapshot_abi_version", source)
        self.assertIn("__unialloc_semantic_fallback_attribution_snapshot_size", source)
        self.assertIn("__unialloc_semantic_fallback_attribution_snapshot_checked", source)
        self.assertIn("__unialloc_semantic_fallback_attribution_snapshot(", source)
        self.assertIn("UNIALLOC_SEMANTIC_METADATA_VALIDATION_SNAPSHOT_ABI_VERSION 1u", source)
        self.assertIn("typedef struct UniallocSemanticMetadataValidationSnapshot", source)
        self.assertIn("__unialloc_semantic_metadata_validation_snapshot_abi_version", source)
        self.assertIn("__unialloc_semantic_metadata_validation_snapshot_size", source)
        self.assertIn("__unialloc_semantic_metadata_validation_snapshot_checked", source)
        self.assertIn("__unialloc_semantic_metadata_validation_snapshot(", source)

    def test_real_rust_for_linux_module_satisfies_unialloc_source_contract(self) -> None:
        module_dir = ROOT / "kernel" / "kernel-modules" / "benchmarking"
        audit = evaluate.rust_for_linux_unialloc_source_contract(module_dir)
        self.assertTrue(audit["passed"], audit.get("blockers"))
        self.assertTrue(audit["checks"]["bridge_snapshots_semantic_stats_checked_abi"])
        self.assertTrue(audit["checks"]["bridge_exposes_semantic_type_metadata_abi"])
        self.assertTrue(audit["checks"]["bridge_snapshots_semantic_type_stats_checked_abi"])
        self.assertTrue(audit["checks"]["bridge_snapshots_semantic_fallback_attribution_checked_abi"])
        self.assertTrue(audit["checks"]["bridge_snapshots_semantic_metadata_validation_checked_abi"])
        self.assertTrue(audit["checks"]["bridge_semantic_snapshot_layouts_match_allocator"])
        self.assertTrue(audit["checks"]["header_semantic_snapshot_layouts_match_allocator"])
        self.assertTrue(audit["checks"]["bridge_constrained_boot_abi_version_matches_allocator"])
        self.assertTrue(audit["checks"]["bridge_constrained_boot_layout_matches_allocator"])
        self.assertTrue(audit["checks"]["header_constrained_boot_abi_version_matches_allocator"])
        self.assertTrue(audit["checks"]["header_constrained_boot_layout_matches_allocator"])
        self.assertTrue(audit["checks"]["module_reports_semantic_type_stats_probe"])
        self.assertTrue(audit["checks"]["module_reports_semantic_fallback_attribution"])
        self.assertTrue(audit["checks"]["module_reports_semantic_metadata_validation"])
        self.assertTrue(audit["checks"]["makefile_builds_rlib_dependency"])
        self.assertTrue(audit["checks"]["makefile_passes_rlib_to_final_module_crate"])

    def test_rust_for_linux_source_contract_rejects_stale_type_stats_abi_version(self) -> None:
        source_dir = ROOT / "kernel" / "kernel-modules" / "benchmarking"
        with tempfile.TemporaryDirectory() as raw_tmp:
            module_dir = pathlib.Path(raw_tmp)
            for name in ("rust_bench.rs", "unialloc_bridge.rs", "unialloc_kernel_ffi.h", "Makefile"):
                (module_dir / name).write_text(
                    (source_dir / name).read_text(encoding="utf-8"), encoding="utf-8"
                )

            bridge = module_dir / "unialloc_bridge.rs"
            bridge.write_text(
                bridge.read_text(encoding="utf-8").replace(
                    "UNIALLOC_SEMANTIC_TYPE_STATS_SNAPSHOT_ABI_VERSION: u32 = 2",
                    "UNIALLOC_SEMANTIC_TYPE_STATS_SNAPSHOT_ABI_VERSION: u32 = 1",
                ),
                encoding="utf-8",
            )

            audit = evaluate.rust_for_linux_unialloc_source_contract(module_dir)

        self.assertFalse(audit["passed"])
        self.assertFalse(
            audit["checks"]["bridge_semantic_snapshot_abi_versions_match_allocator"]
        )
        self.assertTrue(audit["checks"]["header_semantic_snapshot_abi_versions_match_allocator"])

    def test_rust_for_linux_makefile_feature_parser_ignores_comments(self) -> None:
        makefile = """
        # stats appears here but must not satisfy the feature gate.
        UNIALLOC_FEATURES ?= fixed_heap,allow_mem_leak # stats
        cargo rustc --features $(UNIALLOC_FEATURES)
        """
        self.assertEqual(
            evaluate.makefile_unialloc_feature_set(makefile),
            {"fixed_heap", "allow_mem_leak"},
        )
        self.assertFalse(evaluate.makefile_enables_unialloc_rfl_features(makefile))

        continued = """
        UNIALLOC_FEATURES ?= fixed_heap,\\
          allow_mem_leak,stats
        """
        self.assertTrue(evaluate.makefile_enables_unialloc_rfl_features(continued))

    def test_rust_for_linux_unialloc_source_contract_rejects_default_allocator_smoke(self) -> None:
        with tempfile.TemporaryDirectory() as raw_tmp:
            module_dir = pathlib.Path(raw_tmp)
            (module_dir / "rust_bench.rs").write_text("use alloc::vec::Vec;\n", encoding="utf-8")
            (module_dir / "Makefile").write_text("obj-m += rust_bench.o\n", encoding="utf-8")
            audit = evaluate.rust_for_linux_unialloc_source_contract(module_dir)
        self.assertFalse(audit["passed"])
        self.assertIn("missing source contract checks", "; ".join(audit["blockers"]))

    def test_staticlib_symbol_audit_requires_unialloc_c_abi_symbols(self) -> None:
        text = "\n".join(evaluate.RUST_FOR_LINUX_UNIALLOC_C_ABI_SYMBOLS)
        audit = evaluate.staticlib_symbol_audit(text, pathlib.Path("/tmp/libunialloc.a"))
        self.assertFalse(audit["passed"])
        self.assertFalse(audit["missing_symbols"])
        with tempfile.NamedTemporaryFile() as tmp:
            ready = evaluate.staticlib_symbol_audit(text, pathlib.Path(tmp.name))
        self.assertTrue(ready["passed"])

    def test_constrained_platform_smoke_builds_unialloc_staticlib_contract(self) -> None:
        source = (ROOT / "evaluation" / "scripts" / "evaluate.py").read_text(encoding="utf-8")
        self.assertEqual(
            evaluate.CONSTRAINED_PLATFORM_UNIALLOC_STATICLIB_FEATURES,
            "fixed_heap,allow_mem_leak,stats",
        )
        self.assertIn("constrained}-unialloc-staticlib-build", source)
        self.assertIn("constrained}-unialloc-integration-audit.json", source)
        self.assertIn("unialloc_required_c_abi_symbols", source)
        self.assertIn("constrained_staticlib_ready", source)
        self.assertIn("constrained}-boot-sample-producer-target-check", source)
        self.assertIn("boot_sample_producer_target_ready", source)
        self.assertIn("boot_sample_producer_target_cleanup", source)
        self.assertIn("diagnostic_platform_evidence_object", source)

    def test_windows_linker_selection_can_use_repo_zig_wrapper(self) -> None:
        with mock.patch.object(evaluate.shutil, "which", return_value=None), mock.patch.object(
            evaluate,
            "executable_version_probe",
            return_value=(True, "clang version test"),
        ):
            selection = evaluate.windows_linker_selection("x86_64-pc-windows-gnu")
        self.assertTrue(selection["available"])
        self.assertEqual(selection["source"], "repo-tools")
        self.assertTrue(selection["linker"].endswith("zig-rustlink-x86_64-pc-windows-gnu.sh"))
        self.assertTrue(any(item.get("source") == "repo-tools" for item in selection["candidates"]))

    def test_platform_runtime_run_artifacts_preserve_log_and_parsed_event(self) -> None:
        with tempfile.TemporaryDirectory() as raw_tmp:
            tmp = pathlib.Path(raw_tmp)
            log = tmp / "macos-runtime-workload-1.log"
            log.write_text("runtime command log\n", encoding="utf-8")
            event = {
                "source": "platform_allocator_workload",
                "passed": True,
                "threads": 2,
                "iters": 8,
                "max_len": 256,
                "checksum": 123,
                "duration_ns": 999,
            }
            artifacts = evaluate.platform_runtime_run_artifacts(
                [
                    {
                        "label": "macos-runtime-workload-1",
                        "command": ["cargo", "run"],
                        "exit_code": 0,
                        "passed": True,
                        "parsed_event_valid": True,
                        "parsed_event": event,
                    }
                ],
                [log],
            )
        self.assertEqual(len(artifacts), 1)
        self.assertEqual(artifacts[0]["log"], str(log))
        self.assertTrue(artifacts[0]["parsed_event_valid"])
        self.assertEqual(artifacts[0]["parsed_event"], event)

    def test_write_platform_command_log_handles_skipped_command_record(self) -> None:
        with tempfile.TemporaryDirectory() as raw_tmp:
            tmp = pathlib.Path(raw_tmp)
            log = evaluate.write_platform_command_log(
                tmp,
                "windows-runtime-workload-build",
                {
                    "label": "windows-runtime-workload-build",
                    "command": ["cargo", "build", "--target", "x86_64-pc-windows-gnu"],
                    "env_overrides": {},
                    "exit_code": None,
                    "passed": False,
                    "error": "Windows target linker is unavailable; executable build/link was not attempted",
                },
            )
            text = log.read_text(encoding="utf-8")
        self.assertIn("windows-runtime-workload-build", text)
        self.assertIn("cargo build --target x86_64-pc-windows-gnu", text)
        self.assertIn("passed: False", text)
        self.assertIn("error: Windows target linker is unavailable", text)

    def test_recognized_boot_emulator_rejects_uri_metadata_for_local_execution(self) -> None:
        self.assertFalse(
            evaluate.recognized_boot_emulator(
                "https://example.invalid/qemu",
                {"kind": "uri", "usable": True},
            )
        )

    def test_windows_runtime_workload_blockers_are_actionable_and_fail_closed(self) -> None:
        with tempfile.TemporaryDirectory() as raw_tmp:
            tmp = pathlib.Path(raw_tmp)
            stderr = tmp / "windows-runtime-workload-build.stderr.txt"
            stderr.write_text(
                "\n".join(
                    [
                        "lld: error: unable to find library -ladvapi32",
                        "lld: error: unable to find library -lkernel32",
                    ]
                ),
                encoding="utf-8",
            )
            blockers = evaluate.windows_runtime_workload_blockers(
                runtime_check_rec={"passed": True},
                runtime_build_rec={"passed": False, "stderr": str(stderr)},
                windows_linker={"diagnostics": ["missing x86_64-w64-mingw32-gcc linker"]},
                windows_runner_selection={
                    "available": False,
                    "diagnostics": ["no native Windows host or Wine/wine64 runner available"],
                },
                windows_exe=None,
                runtime_can_execute=False,
                runtime_import_requested=False,
                runtime_passed=False,
                runtime_validation_blockers=[],
            )
        joined = " | ".join(blockers)
        self.assertIn("executable build/link failed", joined)
        self.assertIn("missing x86_64-w64-mingw32-gcc linker", joined)
        self.assertIn("missing Windows import/runtime libraries", joined)
        self.assertIn("no native Windows host or Wine/wine64 runner available", joined)

    def test_windows_runtime_import_mode_uses_validation_blockers_not_missing_wine(self) -> None:
        blockers = evaluate.windows_runtime_workload_blockers(
            runtime_check_rec={"passed": True},
            runtime_build_rec={"passed": True},
            windows_linker={"diagnostics": []},
            windows_runner_selection={
                "available": False,
                "diagnostics": ["no native Windows host or Wine/wine64 runner available"],
            },
            windows_exe=pathlib.Path("platform_allocator_workload.exe"),
            runtime_can_execute=False,
            runtime_import_requested=True,
            runtime_passed=False,
            runtime_validation_blockers=["runtime event 1 target_family is not 'windows'"],
        )
        joined = " | ".join(blockers)
        self.assertIn("target_family is not 'windows'", joined)
        self.assertNotIn("no native Windows host", joined)

    def test_imported_rust_for_linux_kernel_build_log_preserves_sha_and_pattern_gate(self) -> None:
        with tempfile.TemporaryDirectory() as raw_tmp:
            tmp = pathlib.Path(raw_tmp)
            build_log = tmp / "rfl-kernel-build.log"
            build_log.write_text(
                "\n".join(
                    [
                        "  MODPOST Module.symvers",
                        "  LD [M] /work/kernel/kernel-modules/benchmarking/rust_bench.ko",
                        "  BTF [M] /work/kernel/kernel-modules/benchmarking/rust_bench.ko",
                    ]
                )
                + "\n",
                encoding="utf-8",
            )
            record, log, passed, blockers = evaluate.collect_imported_rust_for_linux_kernel_build_log(
                out_dir=tmp,
                artifact_path=build_log,
                success_pattern=r"re:LD \[M\].*rust_bench\.ko",
            )
            artifact = evaluate.platform_command_artifact(record, log=log)
        self.assertTrue(passed, blockers)
        self.assertTrue(record["external_kernel_build_import"])
        self.assertRegex(record["source_artifact_sha256"], r"^[0-9a-f]{64}$")
        self.assertTrue(record["success_pattern_matched"])
        self.assertIn("rust_bench", record["matched_log_markers"])
        self.assertEqual(record["kernel_build_provenance_blockers"], [])
        self.assertIn("modpost", record["kernel_build_provenance"]["matched_kernel_context_markers"])
        self.assertIn("rust_bench.ko", record["kernel_build_provenance"]["matched_module_build_markers"])
        self.assertEqual(artifact["source_artifact_sha256"], record["source_artifact_sha256"])
        self.assertTrue(artifact["external_kernel_build_import"])

    def test_imported_rust_for_linux_kernel_build_log_rejects_unrelated_success_log(self) -> None:
        with tempfile.TemporaryDirectory() as raw_tmp:
            tmp = pathlib.Path(raw_tmp)
            build_log = tmp / "unrelated-build.log"
            build_log.write_text("BUILD SUCCESS\n", encoding="utf-8")
            _record, _log, passed, blockers = evaluate.collect_imported_rust_for_linux_kernel_build_log(
                out_dir=tmp,
                artifact_path=build_log,
                success_pattern="BUILD SUCCESS",
            )
        self.assertFalse(passed)
        self.assertIn("required module markers", " | ".join(blockers))

    def test_imported_rust_for_linux_kernel_build_log_rejects_arbitrary_rust_bench_success(self) -> None:
        with tempfile.TemporaryDirectory() as raw_tmp:
            tmp = pathlib.Path(raw_tmp)
            build_log = tmp / "fake-rfl-build.log"
            build_log.write_text(
                "BUILD SUCCESS\nrust_bench test fixture passed\n",
                encoding="utf-8",
            )
            record, _log, passed, blockers = evaluate.collect_imported_rust_for_linux_kernel_build_log(
                out_dir=tmp,
                artifact_path=build_log,
                success_pattern="BUILD SUCCESS",
            )
        self.assertFalse(passed)
        joined = " | ".join(blockers)
        self.assertIn("lacks Linux kernel/Kbuild provenance markers", joined)
        self.assertIn("lacks rust_bench kernel module compile/link markers", joined)
        self.assertTrue(record["success_pattern_matched"])
        self.assertIn("rust_bench", record["matched_log_markers"])

    def test_imported_constrained_boot_log_preserves_sha_and_pattern_gate(self) -> None:
        with tempfile.TemporaryDirectory() as raw_tmp:
            tmp = pathlib.Path(raw_tmp)
            boot_log = tmp / "blogos-boot.log"
            boot_log.write_text(
                "BlogOS boot\n"
                "UniAlloc fixed_heap initialized\n"
                "UNIALLOC_BOOT_OK\n"
                f"{evaluate.CONSTRAINED_BOOT_PROVENANCE_MARKER} "
                "platform=blogos "
                f"image_sha256={'1' * 64} "
                f"boot_config_sha256={'2' * 64} "
                "emulator=qemu-system-x86_64 "
                "emulator_version=8.2.0\n"
                f"{evaluate.CONSTRAINED_BOOT_SAMPLE_MARKER} "
                "platform=blogos boot_cycle=1 fixed_heap_ready=true "
                "c_abi_invoked=true c_abi_ready_after_init=true "
                "c_abi_round_trips=2 c_abi_invalid_layouts_rejected=3 "
                "c_abi_over_page_alignment_checked=true c_abi_over_page_alignment=8192 "
                "allocator_total_allocations=4 allocator_total_deallocations=4 "
                "allocator_typed_allocations=1 allocator_typed_deallocations=1 "
                "allocator_fallback_allocations=3 allocator_fallback_deallocations=3 "
                "allocator_total_allocated_bytes=4096 allocator_typed_allocated_bytes=64 "
                "allocator_fallback_allocated_bytes=4032 allocator_coverage_basis_points=2500 "
                "allocator_type_stats_rows=1 allocator_type_stats_dropped_events=0 allocator_type_stats_probe_matched=true "
                "c_abi_probe_passed=true\n",
                encoding="utf-8",
            )
            record, log, passed, blockers = evaluate.collect_imported_constrained_boot_log(
                out_dir=tmp,
                platform_name="blogos",
                display_name="BlogOS",
                artifact_path=boot_log,
                success_pattern="UNIALLOC_BOOT_OK",
                required_log_markers=["blogos", "unialloc"],
            )
            artifact = evaluate.platform_command_artifact(record, log=log)
            imported_source = pathlib.Path(record["imported_source_artifact"])
            self.assertTrue(imported_source.exists())
            self.assertEqual(imported_source.read_text(encoding="utf-8"), boot_log.read_text(encoding="utf-8"))
        self.assertTrue(passed, blockers)
        self.assertTrue(record["external_boot_log_import"])
        self.assertRegex(record["source_artifact_sha256"], r"^[0-9a-f]{64}$")
        self.assertRegex(record["imported_source_artifact_sha256"], r"^[0-9a-f]{64}$")
        self.assertTrue(record["success_pattern_matched"])
        self.assertEqual(record["missing_required_log_markers"], [])
        self.assertEqual(record["boot_provenance_record_blockers"], [])
        self.assertCountEqual(record["matched_log_markers"], ["blogos", "unialloc"])
        self.assertEqual(artifact["source_artifact_sha256"], record["source_artifact_sha256"])
        self.assertEqual(
            artifact["imported_source_artifact_sha256"],
            record["imported_source_artifact_sha256"],
        )
        self.assertTrue(artifact["external_boot_log_import"])
        self.assertEqual(artifact["missing_required_log_markers"], [])

    def test_imported_constrained_boot_log_rejects_unrelated_success_log(self) -> None:
        with tempfile.TemporaryDirectory() as raw_tmp:
            tmp = pathlib.Path(raw_tmp)
            boot_log = tmp / "unrelated-success.log"
            boot_log.write_text(
                "platform_allocator_workload passed\nUniAlloc runtime event valid\n",
                encoding="utf-8",
            )
            record, _log, passed, blockers = evaluate.collect_imported_constrained_boot_log(
                out_dir=tmp,
                platform_name="blogos",
                display_name="BlogOS",
                artifact_path=boot_log,
                success_pattern="platform_allocator_workload",
                required_log_markers=["blogos", "unialloc"],
            )
        self.assertFalse(passed)
        self.assertTrue(record["success_pattern_matched"])
        self.assertIn("blogos", record["missing_required_log_markers"])
        self.assertIn("missing required markers", " | ".join(blockers))

    def test_platform_audit_requires_declared_sha256_for_claim_grade_artifacts(self) -> None:
        with tempfile.TemporaryDirectory() as raw_tmp:
            tmp = pathlib.Path(raw_tmp)
            artifact = tmp / "windows-run-summary.json"
            write_json(
                artifact,
                {
                    "passed": True,
                    "claim_grade": True,
                    "complete_for_claim": True,
                    "command_executed": True,
                },
            )
            entry = {
                "target": "Windows real target",
                "passed": True,
                "claim_grade": True,
                "complete_for_claim": True,
                "host": {"system": "Windows"},
                "target_triple": "x86_64-pc-windows-gnu",
                "rust_target": "x86_64-pc-windows-gnu",
                "platform_target": "Windows 10 or newer target host",
                "os_version": "Windows fixture",
                "target_metadata": {"runtime_workload": "platform_allocator_workload"},
                "evidence": [{"path": artifact.name, "kind": "run_summary"}],
            }
            audit = evaluate.audit_platform_entry("windows", entry, tmp)
        self.assertFalse(audit["ready_for_claim_grade_import"])
        joined = " | ".join(audit["evidence_content_blockers"])
        self.assertIn("missing declared sha256", joined)

    def test_platform_audit_rejects_mismatched_declared_sha256(self) -> None:
        with tempfile.TemporaryDirectory() as raw_tmp:
            tmp = pathlib.Path(raw_tmp)
            artifact = tmp / "macos-performance-data.json"
            write_json(
                artifact,
                {
                    "passed": True,
                    "claim_grade": True,
                    "complete_for_claim": True,
                    "sample_count": 1,
                    "metric": "allocator_workload_runtime",
                },
            )
            entry = {
                "target": "macOS real target",
                "passed": True,
                "claim_grade": True,
                "complete_for_claim": True,
                "host": {"system": "Darwin"},
                "target_triple": "aarch64-apple-darwin",
                "rust_target": "aarch64-apple-darwin",
                "platform_target": "macOS Apple Silicon target host",
                "os_version": "macOS fixture",
                "target_metadata": {"runtime_workload": "platform_allocator_workload"},
                "evidence": [
                    {
                        "path": artifact.name,
                        "kind": "performance_data",
                        "sha256": "0" * 64,
                    }
                ],
            }
            audit = evaluate.audit_platform_entry("macos", entry, tmp)
        self.assertFalse(audit["ready_for_claim_grade_import"])
        joined = " | ".join(audit["evidence_content_blockers"])
        self.assertIn("declared sha256 does not match", joined)

    def test_platform_preflight_audit_is_written_without_results_import(self) -> None:
        old_results = evaluate.RESULTS
        with tempfile.TemporaryDirectory() as raw_tmp:
            tmp = pathlib.Path(raw_tmp)
            results = tmp / "results"
            results.mkdir()
            matrix_path = tmp / "platform-matrix.generated.json"
            matrix = {
                name: {
                    "target": info["target"],
                    "passed": False,
                    "claim_grade": False,
                    "complete_for_claim": False,
                    "evidence": [],
                    "notes": "unit smoke only",
                }
                for name, info in evaluate.PLATFORM_SMOKE_TARGETS.items()
            }
            write_json(matrix_path, matrix)
            try:
                evaluate.RESULTS = results
                audit, audit_path = evaluate.write_platform_matrix_preflight_audit(
                    matrix_path,
                    matrix,
                    update_results=False,
                )
                audit_exists = audit_path.exists()
                results_audit_exists = (results / "platform_matrix_audit.json").exists()
            finally:
                evaluate.RESULTS = old_results
        self.assertTrue(audit_exists)
        self.assertFalse(results_audit_exists)
        self.assertFalse(audit["summary"]["ready_for_claim_grade_import"])
        self.assertEqual(
            audit_path.name,
            "platform-matrix-preflight-audit.json",
        )

    def test_partial_platform_refresh_preserves_unselected_evidence_paths(self) -> None:
        old_results = evaluate.RESULTS
        with tempfile.TemporaryDirectory() as raw_tmp:
            tmp = pathlib.Path(raw_tmp)
            previous_raw = tmp / "old-platform-run"
            new_raw = tmp / "new-platform-run"
            results = tmp / "results"
            previous_raw.mkdir()
            new_raw.mkdir()
            results.mkdir()

            build_log = previous_raw / "windows-build.log"
            build_log.write_text("cargo build passed\n", encoding="utf-8")
            run_summary = previous_raw / "windows-run-summary.json"
            write_json(
                run_summary,
                {
                    "passed": True,
                    "claim_grade": True,
                    "complete_for_claim": True,
                    "command_executed": True,
                },
            )
            performance = previous_raw / "windows-performance-data.json"
            write_json(
                performance,
                {
                    "passed": True,
                    "claim_grade": True,
                    "complete_for_claim": True,
                    "metric": "platform_allocator_workload_duration_ns",
                    "sample_count": 1,
                    "samples": [123],
                },
            )
            windows_entry = {
                "target": "Windows runtime workload target",
                "passed": True,
                "claim_grade": True,
                "complete_for_claim": True,
                "evidence_source_fingerprint": evaluate.repository_source_fingerprint(),
                "host": {"system": "Windows"},
                "target_triple": "x86_64-pc-windows-gnu",
                "rust_target": "x86_64-pc-windows-gnu",
                "platform_target": "Windows 10 or newer target host",
                "os_version": "Windows fixture",
                "target_metadata": {"runtime_workload": "platform_allocator_workload"},
                "notes": "Windows runtime workload evidence from previous refresh.",
                "methodology": "cross-target build plus runtime workload sample",
                "evidence": [
                    {
                        "path": build_log.name,
                        "kind": "build_log",
                        "sha256": evaluate.file_sha256(build_log),
                    },
                    {
                        "path": run_summary.name,
                        "kind": "run_summary",
                        "sha256": evaluate.file_sha256(run_summary),
                    },
                    {
                        "path": performance.name,
                        "kind": "performance_data",
                        "sha256": evaluate.file_sha256(performance),
                    },
                ],
                "verified_evidence": [
                    str(build_log.resolve()),
                    str(run_summary.resolve()),
                    str(performance.resolve()),
                ],
                "platform_claim_blockers": ["stale audit field should not be reused"],
            }
            write_json(
                results / "platform_matrix.json",
                {
                    "windows": windows_entry,
                    "_meta": {
                        "source_matrix": str(previous_raw / "platform-matrix.generated.json"),
                    },
                },
            )
            matrix = {
                "macos": {
                    "target": "macOS refreshed in this unit fixture",
                    "passed": False,
                    "claim_grade": False,
                    "complete_for_claim": False,
                    "evidence": [],
                }
            }
            try:
                evaluate.RESULTS = results
                evaluate.preserve_unselected_platform_matrix_entries(matrix, ["macos"])
            finally:
                evaluate.RESULTS = old_results

            preserved = matrix["windows"]
            self.assertTrue(preserved["preserved_from_previous_platform_matrix"])
            self.assertNotIn("verified_evidence", preserved)
            self.assertNotIn("platform_claim_blockers", preserved)
            preserved_paths = [item["path"] for item in preserved["evidence"]]
            self.assertEqual(preserved_paths, [str(build_log.resolve()), str(run_summary.resolve()), str(performance.resolve())])
            audit = evaluate.audit_platform_entry("windows", preserved, new_raw)
            self.assertTrue(audit["ready_for_claim_grade_import"], audit["blockers"])

    def test_partial_platform_refresh_uses_best_raw_claim_grade_when_current_is_smoke_only(self) -> None:
        old_results = evaluate.RESULTS
        with tempfile.TemporaryDirectory() as raw_tmp:
            tmp = pathlib.Path(raw_tmp)
            raw_root = tmp / "raw"
            best_raw = raw_root / "best-windows-run"
            old_raw = raw_root / "old-smoke-run"
            new_raw = raw_root / "new-platform-run"
            results = tmp / "results"
            best_raw.mkdir(parents=True)
            old_raw.mkdir(parents=True)
            new_raw.mkdir(parents=True)
            results.mkdir()

            build_log = best_raw / "windows-build.log"
            build_log.write_text("cargo build passed\n", encoding="utf-8")
            run_summary = best_raw / "windows-run-summary.json"
            write_json(
                run_summary,
                {
                    "passed": True,
                    "claim_grade": True,
                    "complete_for_claim": True,
                    "command_executed": True,
                },
            )
            performance = best_raw / "windows-performance-data.json"
            write_json(
                performance,
                {
                    "passed": True,
                    "claim_grade": True,
                    "complete_for_claim": True,
                    "metric": "platform_allocator_workload_duration_ns",
                    "sample_count": 1,
                    "samples": [123],
                },
            )
            windows_entry = {
                "target": "Windows runtime workload target",
                "passed": True,
                "claim_grade": True,
                "complete_for_claim": True,
                "evidence_source_fingerprint": evaluate.repository_source_fingerprint(),
                "host": {"system": "Windows"},
                "target_triple": "x86_64-pc-windows-gnu",
                "rust_target": "x86_64-pc-windows-gnu",
                "platform_target": "Windows 10 or newer target host",
                "os_version": "Windows fixture",
                "target_metadata": {"runtime_workload": "platform_allocator_workload"},
                "notes": "Windows runtime workload evidence from raw refresh.",
                "methodology": "cross-target build plus runtime workload sample",
                "evidence": [
                    {
                        "path": build_log.name,
                        "kind": "build_log",
                        "sha256": evaluate.file_sha256(build_log),
                    },
                    {
                        "path": run_summary.name,
                        "kind": "run_summary",
                        "sha256": evaluate.file_sha256(run_summary),
                    },
                    {
                        "path": performance.name,
                        "kind": "performance_data",
                        "sha256": evaluate.file_sha256(performance),
                    },
                ],
            }
            raw_matrix = {"windows": windows_entry}
            raw_matrix_path = best_raw / "platform-matrix.generated.json"
            write_json(raw_matrix_path, raw_matrix)
            preflight = evaluate.build_platform_matrix_audit(
                {
                    "claims": [
                        {
                            "id": "C007-cross-platform-retargeting",
                            "platforms": ["windows"],
                        }
                    ]
                },
                raw_matrix_path,
                raw_matrix,
            )
            self.assertTrue(
                preflight["platforms"]["windows"]["ready_for_claim_grade_import"],
                preflight["platforms"]["windows"]["blockers"],
            )
            write_json(best_raw / "platform-matrix-preflight-audit.json", preflight)

            write_json(
                results / "platform_matrix.json",
                {
                    "windows": {
                        "target": "Old smoke-only Windows target",
                        "passed": True,
                        "claim_grade": False,
                        "complete_for_claim": False,
                        "evidence_verified": False,
                        "evidence": [],
                    },
                    "_meta": {
                        "source_matrix": str(old_raw / "platform-matrix.generated.json"),
                    },
                },
            )
            matrix = {
                "macos": {
                    "target": "macOS refreshed in this unit fixture",
                    "passed": False,
                    "claim_grade": False,
                    "complete_for_claim": False,
                    "evidence": [],
                }
            }
            try:
                evaluate.RESULTS = results
                evaluate.preserve_unselected_platform_matrix_entries(
                    matrix,
                    ["macos"],
                    raw_root=raw_root,
                )
            finally:
                evaluate.RESULTS = old_results

            preserved = matrix["windows"]
            self.assertTrue(preserved["preserved_from_best_raw_platform_matrix"])
            self.assertEqual(
                preserved["preserved_source_matrix"],
                str(raw_matrix_path),
            )
            preserved_paths = [item["path"] for item in preserved["evidence"]]
            self.assertEqual(
                preserved_paths,
                [
                    str(build_log.resolve()),
                    str(run_summary.resolve()),
                    str(performance.resolve()),
                ],
            )
            audit = evaluate.audit_platform_entry("windows", preserved, new_raw)
            self.assertTrue(audit["ready_for_claim_grade_import"], audit["blockers"])

    def test_partial_platform_refresh_does_not_reuse_selected_missing_platform(self) -> None:
        old_results = evaluate.RESULTS
        with tempfile.TemporaryDirectory() as raw_tmp:
            tmp = pathlib.Path(raw_tmp)
            previous_raw = tmp / "old-platform-run"
            results = tmp / "results"
            previous_raw.mkdir()
            results.mkdir()

            old_log = previous_raw / "windows-build.log"
            old_log.write_text("old windows evidence\n", encoding="utf-8")
            write_json(
                results / "platform_matrix.json",
                {
                    "windows": {
                        "target": "Old Windows target",
                        "passed": True,
                        "claim_grade": True,
                        "complete_for_claim": True,
                        "evidence": [{"path": old_log.name, "kind": "build_log"}],
                        "verified_evidence": [str(old_log.resolve())],
                    },
                    "_meta": {
                        "source_matrix": str(previous_raw / "platform-matrix.generated.json"),
                    },
                },
            )
            matrix = {}
            try:
                evaluate.RESULTS = results
                evaluate.preserve_unselected_platform_matrix_entries(
                    matrix,
                    ["windows"],
                    raw_root=tmp,
                )
            finally:
                evaluate.RESULTS = old_results

            self.assertFalse(matrix["windows"]["passed"])
            self.assertFalse(matrix["windows"]["claim_grade"])
            self.assertEqual(matrix["windows"]["evidence"], [])
            self.assertIn("Not selected", matrix["macos"]["notes"])

    def test_exact_dynamic_compiler_basis_can_pass_claim_gate(self) -> None:
        with tempfile.TemporaryDirectory() as raw_tmp:
            tmp = pathlib.Path(raw_tmp)
            manifest = write_manifest_fixture(
                tmp,
                basis="compiler-assigned-allocation-site-object-type-id",
                replay=False,
            )
            audit = evaluate.build_compiler_coverage_audit(manifest)
        self.assertTrue(audit["summary"]["ready_for_claim_grade_import"], audit["blockers"])

    def test_fresh_compiler_coverage_integrity_rechecks_manifest_evidence_bytes(self) -> None:
        with tempfile.TemporaryDirectory() as raw_tmp:
            tmp = pathlib.Path(raw_tmp)
            results = tmp / "results"
            results.mkdir()
            current = source_fingerprint_fixture()
            with temporary_eval_results_and_raw(results):
                audit_path, manifest_path, _runtime_audit_path, _saved_audit = (
                    write_fresh_compiler_integrity_fixture(tmp, current=current)
                )
                manifest = json.loads(manifest_path.read_text(encoding="utf-8"))

                before = evaluate.fresh_compiler_coverage_integrity_status(
                    audit_path,
                    current=current,
                )
                pass_log = pathlib.Path(
                    next(
                        item["path"]
                        for item in manifest["evidence"]
                        if item.get("kind") == "compiler_pass_log"
                    )
                )
                pass_log.write_text(
                    pass_log.read_text(encoding="utf-8") + "tampered after saved audit\n",
                    encoding="utf-8",
                )
                after = evaluate.fresh_compiler_coverage_integrity_status(
                    audit_path,
                    current=current,
                )

        self.assertTrue(before["ready"], before["blockers"])
        self.assertFalse(after["ready"])
        self.assertIn("declared sha256 does not match", " | ".join(after["blockers"]))

    def test_fresh_compiler_coverage_integrity_rejects_saved_coverage_mismatch(self) -> None:
        with tempfile.TemporaryDirectory() as raw_tmp:
            tmp = pathlib.Path(raw_tmp)
            results = tmp / "results"
            results.mkdir()
            current = source_fingerprint_fixture("b")
            with temporary_eval_results_and_raw(results):
                audit_path, _manifest_path, _runtime_audit_path, saved_audit = (
                    write_fresh_compiler_integrity_fixture(
                        tmp,
                        current=current,
                        fallback_count=25,
                    )
                )
                self.assertEqual(saved_audit["summary"]["coverage_percent"], 80.0)
                saved_audit["summary"]["coverage_percent"] = 99.9
                write_json(audit_path, saved_audit)

                status = evaluate.fresh_compiler_coverage_integrity_status(
                    audit_path,
                    current=current,
                )

        self.assertFalse(status["ready"])
        self.assertEqual(status["fresh_audit_summary"]["coverage_percent"], 80.0)
        self.assertIn("coverage_percent", " | ".join(status["blockers"]))

    def test_fresh_compiler_coverage_integrity_rejects_saved_type_basis_mismatch(self) -> None:
        mir_basis = (
            "compiler-assigned-allocation-site-object-type-id-"
            "rustc-driver-mir-semantic-scope"
        )
        generic_basis = "compiler-assigned-allocation-site-object-type-id"
        with tempfile.TemporaryDirectory() as raw_tmp:
            tmp = pathlib.Path(raw_tmp)
            results = tmp / "results"
            results.mkdir()
            current = source_fingerprint_fixture("c")
            with temporary_eval_results_and_raw(results):
                audit_path, _manifest_path, _runtime_audit_path, saved_audit = (
                    write_fresh_compiler_integrity_fixture(
                        tmp,
                        current=current,
                        basis=mir_basis,
                    )
                )
                saved_audit["type_id_basis"] = generic_basis
                saved_audit["summary"]["event_type_id_bases"] = [generic_basis]
                saved_audit["summary"]["type_id_basis"] = generic_basis
                write_json(audit_path, saved_audit)

                status = evaluate.fresh_compiler_coverage_integrity_status(
                    audit_path,
                    current=current,
                )

        self.assertFalse(status["ready"])
        self.assertEqual(
            status["fresh_claim_critical_snapshot"]["event_type_id_bases"],
            [mir_basis],
        )
        joined = " | ".join(status["blockers"])
        self.assertIn("type_id_basis", joined)
        self.assertIn("event_type_id_bases", joined)

    def test_fresh_compiler_coverage_integrity_rejects_runtime_semantic_flag_tamper(self) -> None:
        with tempfile.TemporaryDirectory() as raw_tmp:
            tmp = pathlib.Path(raw_tmp)
            results = tmp / "results"
            results.mkdir()
            current = source_fingerprint_fixture("d")
            with temporary_eval_results_and_raw(results):
                audit_path, _manifest_path, runtime_audit_path, _saved_audit = (
                    write_fresh_compiler_integrity_fixture(tmp, current=current)
                )
                before = evaluate.fresh_compiler_coverage_integrity_status(
                    audit_path,
                    current=current,
                )
                runtime_audit = json.loads(runtime_audit_path.read_text(encoding="utf-8"))
                runtime_audit["summary"]["runtime_smoke_validated"] = False
                runtime_audit["summary"]["actual_semantic_scope_rewrite"] = False
                write_json(runtime_audit_path, runtime_audit)
                after = evaluate.fresh_compiler_coverage_integrity_status(
                    audit_path,
                    current=current,
                )

        self.assertTrue(before["ready"], before["blockers"])
        self.assertFalse(after["ready"])
        joined = " | ".join(after["blockers"])
        self.assertIn("runtime semantic gate", joined)
        self.assertIn("runtime smoke is not validated", joined)

    def test_compiler_claim_uses_manifest_runtime_instead_of_fixed_results_companion(self) -> None:
        mir_basis = (
            "compiler-assigned-allocation-site-object-type-id-"
            "rustc-driver-mir-semantic-scope"
        )
        with tempfile.TemporaryDirectory() as raw_tmp:
            tmp = pathlib.Path(raw_tmp)
            results = tmp / "results"
            results.mkdir()
            current = source_fingerprint_fixture("e")
            with temporary_eval_results_and_raw(results):
                _audit_path, manifest_path, runtime_a_path, saved_audit = (
                    write_fresh_compiler_integrity_fixture(
                        tmp,
                        current=current,
                        basis=mir_basis,
                    )
                )
                runtime_b_path = write_ready_mir_semantic_scope_runtime_companion(
                    results,
                    run_id="fixed-results-runtime-b",
                )
                runtime_b = json.loads(runtime_b_path.read_text(encoding="utf-8"))
                runtime_b["summary"]["runtime_smoke_validated"] = False
                write_json(runtime_b_path, runtime_b)

                ready = evaluate.compiler_coverage_claim_grade_status(
                    saved_audit,
                    threshold=72.17,
                )

                runtime_a = json.loads(runtime_a_path.read_text(encoding="utf-8"))
                runtime_a["summary"]["runtime_smoke_validated"] = False
                write_json(runtime_a_path, runtime_a)
                write_ready_mir_semantic_scope_runtime_companion(
                    results,
                    run_id="fixed-results-runtime-b-restored",
                )
                rejected = evaluate.compiler_coverage_claim_grade_status(
                    saved_audit,
                    threshold=72.17,
                )

        self.assertEqual(pathlib.Path(saved_audit["manifest"]).resolve(), manifest_path.resolve())
        self.assertTrue(ready["ready"], ready["blockers"])
        self.assertEqual(
            pathlib.Path(ready["runtime_companion_path"]).resolve(),
            runtime_a_path.resolve(),
        )
        self.assertNotEqual(
            pathlib.Path(ready["runtime_companion_path"]).resolve(),
            runtime_b_path.resolve(),
        )
        self.assertFalse(rejected["ready"])
        self.assertEqual(
            pathlib.Path(rejected["runtime_companion_path"]).resolve(),
            runtime_a_path.resolve(),
        )
        self.assertIn("runtime smoke is not validated", " | ".join(rejected["blockers"]))

    def test_cyclic_replay_basis_cannot_pass_claim_gate(self) -> None:
        with tempfile.TemporaryDirectory() as raw_tmp:
            tmp = pathlib.Path(raw_tmp)
            manifest = write_manifest_fixture(
                tmp,
                basis="compiler-assigned-allocation-site-object-type-id-cyclic-replay",
                replay=True,
            )
            audit = evaluate.build_compiler_coverage_audit(manifest)
        self.assertFalse(audit["summary"]["ready_for_claim_grade_import"])
        joined = " | ".join(audit["blockers"])
        self.assertIn("replay/bridge", joined)
        self.assertIn("compiler_site_replay=true", joined)

    def test_compiler_coverage_claim_grade_status_accepts_full_surface_mir_audit(self) -> None:
        with tempfile.TemporaryDirectory() as raw_tmp:
            results = pathlib.Path(raw_tmp) / "results"
            results.mkdir()
            with temporary_eval_results_and_raw(results):
                write_ready_mir_semantic_scope_runtime_companion(results)
                write_ready_mir_probe_companions(results)
                status = evaluate.compiler_coverage_claim_grade_status(
                    ready_mir_semantic_scope_compiler_audit(coverage=99.91),
                    threshold=72.17,
                )
        self.assertTrue(status["ready"], status["blockers"])
        self.assertEqual(status["observed"], 99.91)
        self.assertTrue(status["runtime_companion"]["ready"], status["runtime_companion"])
        self.assertTrue(status["mir_probe_companions"]["ready"], status["mir_probe_companions"])

    def test_mir_runtime_claim_gate_promotes_only_full_surface_with_ready_companions(self) -> None:
        gate = evaluate.rustc_driver_mir_semantic_scope_runtime_claim_gate(
            runtime_smoke_validated=True,
            runtime_full_surface_candidate_validated=True,
            runtime_surface_blockers=[],
            target_rewrite={
                "provider_override_installed": True,
                "compiler_pass": {
                    "kind": "rustc_driver_optimized_mir_provider_override",
                    "query_overridden": "optimized_mir",
                },
                "body_clone_returned_to_rustc": True,
                "actual_semantic_scope_rewrite": True,
            },
            target_rewrite_summary={},
            actual_semantic_scope_rewrite=True,
            semantic_scope_rewrite_applied_count=3,
            missing_runtime_pairs_from_mapping=[],
            direct_allocator_rewrite_requested=True,
            direct_allocator_rewrite_validated=True,
            direct_local_size_align_with_semantic_drop_requested=True,
            direct_size_align_local_pairing_validated=True,
            mir_probe_companions={"ready": True, "blockers": []},
        )
        self.assertTrue(gate["runtime_claim_ready"], gate)
        self.assertTrue(gate["ready_for_claim_grade_import"], gate)
        self.assertEqual(gate["blockers"], [])

        blocked = evaluate.rustc_driver_mir_semantic_scope_runtime_claim_gate(
            runtime_smoke_validated=True,
            runtime_full_surface_candidate_validated=True,
            runtime_surface_blockers=[],
            target_rewrite={
                "provider_override_installed": True,
                "compiler_pass": {
                    "kind": "rustc_driver_optimized_mir_provider_override",
                    "query_overridden": "optimized_mir",
                },
                "body_clone_returned_to_rustc": True,
                "actual_semantic_scope_rewrite": True,
            },
            target_rewrite_summary={},
            actual_semantic_scope_rewrite=True,
            semantic_scope_rewrite_applied_count=3,
            missing_runtime_pairs_from_mapping=[],
            direct_allocator_rewrite_requested=True,
            direct_allocator_rewrite_validated=True,
            direct_local_size_align_with_semantic_drop_requested=True,
            direct_size_align_local_pairing_validated=True,
            mir_probe_companions={"ready": False, "blockers": ["missing probe"]},
        )
        self.assertFalse(blocked["runtime_claim_ready"], blocked)
        self.assertIn("MIR probe companion blocker: missing probe", blocked["blockers"])


    def test_mir_runtime_claim_gate_requires_explicit_compiler_pass_query(self) -> None:
        base = dict(
            runtime_smoke_validated=True,
            runtime_full_surface_candidate_validated=True,
            runtime_surface_blockers=[],
            target_rewrite_summary={},
            actual_semantic_scope_rewrite=True,
            semantic_scope_rewrite_applied_count=3,
            missing_runtime_pairs_from_mapping=[],
            direct_allocator_rewrite_requested=True,
            direct_allocator_rewrite_validated=True,
            direct_local_size_align_with_semantic_drop_requested=True,
            direct_size_align_local_pairing_validated=True,
            mir_probe_companions={"ready": True, "blockers": []},
        )
        missing = evaluate.rustc_driver_mir_semantic_scope_runtime_claim_gate(
            **base,
            target_rewrite={
                "provider_override_installed": True,
                "body_clone_returned_to_rustc": True,
                "actual_semantic_scope_rewrite": True,
            },
        )
        self.assertFalse(missing["runtime_claim_ready"], missing)
        self.assertIn("target rewrite is missing compiler_pass provider-override evidence", missing["blockers"])

        wrong_kind = evaluate.rustc_driver_mir_semantic_scope_runtime_claim_gate(
            **base,
            target_rewrite={
                "provider_override_installed": True,
                "compiler_pass": {"kind": "rustc_mir_dry_run", "query_overridden": "optimized_mir"},
                "body_clone_returned_to_rustc": True,
                "actual_semantic_scope_rewrite": True,
            },
        )
        self.assertFalse(wrong_kind["runtime_claim_ready"], wrong_kind)
        self.assertIn(
            "target rewrite compiler_pass is not rustc_driver_optimized_mir_provider_override",
            wrong_kind["blockers"],
        )

        wrong_query = evaluate.rustc_driver_mir_semantic_scope_runtime_claim_gate(
            **base,
            target_rewrite={
                "provider_override_installed": True,
                "compiler_pass": {
                    "kind": "rustc_driver_optimized_mir_provider_override",
                    "query_overridden": "mir_built",
                },
                "body_clone_returned_to_rustc": True,
                "actual_semantic_scope_rewrite": True,
            },
        )
        self.assertFalse(wrong_query["runtime_claim_ready"], wrong_query)
        self.assertIn(
            "target rewrite compiler_pass does not identify optimized_mir as the overridden rustc query",
            wrong_query["blockers"],
        )

    def test_mir_runtime_claim_gate_requires_direct_modes_requested_for_claim(self) -> None:
        base = dict(
            runtime_smoke_validated=True,
            runtime_full_surface_candidate_validated=True,
            runtime_surface_blockers=[],
            target_rewrite={
                "provider_override_installed": True,
                "compiler_pass": {
                    "kind": "rustc_driver_optimized_mir_provider_override",
                    "query_overridden": "optimized_mir",
                },
                "body_clone_returned_to_rustc": True,
                "actual_semantic_scope_rewrite": True,
            },
            target_rewrite_summary={},
            actual_semantic_scope_rewrite=True,
            semantic_scope_rewrite_applied_count=3,
            missing_runtime_pairs_from_mapping=[],
            direct_allocator_rewrite_validated=True,
            direct_size_align_local_pairing_validated=True,
            mir_probe_companions={"ready": True, "blockers": []},
        )
        gate = evaluate.rustc_driver_mir_semantic_scope_runtime_claim_gate(
            **base,
            direct_allocator_rewrite_requested=False,
            direct_local_size_align_with_semantic_drop_requested=False,
        )
        self.assertFalse(gate["runtime_claim_ready"], gate)
        joined = " | ".join(gate["blockers"])
        self.assertIn("direct allocator rewrite mode was not requested", joined)
        self.assertIn("direct local size/align semantic-drop mode was not requested", joined)

    def test_mir_runtime_claim_gate_keeps_slice_smoke_non_claim_grade(self) -> None:
        gate = evaluate.rustc_driver_mir_semantic_scope_runtime_claim_gate(
            runtime_smoke_validated=True,
            runtime_full_surface_candidate_validated=False,
            runtime_surface_blockers=["missing canonical benchmark"],
            target_rewrite={
                "provider_override_installed": True,
                "compiler_pass": {
                    "kind": "rustc_driver_optimized_mir_provider_override",
                    "query_overridden": "optimized_mir",
                },
                "body_clone_returned_to_rustc": True,
                "actual_semantic_scope_rewrite": True,
            },
            target_rewrite_summary={},
            actual_semantic_scope_rewrite=True,
            semantic_scope_rewrite_applied_count=3,
            missing_runtime_pairs_from_mapping=[],
            direct_allocator_rewrite_requested=True,
            direct_allocator_rewrite_validated=True,
            direct_local_size_align_with_semantic_drop_requested=True,
            direct_size_align_local_pairing_validated=True,
            mir_probe_companions={"ready": True, "blockers": []},
        )
        self.assertFalse(gate["runtime_claim_ready"], gate)
        joined = " | ".join(gate["blockers"])
        self.assertIn("selected/slice smoke is coverage evidence only", joined)
        self.assertIn("missing canonical benchmark", joined)

    def test_mir_semantic_scope_probe_requires_each_heap_family(self) -> None:
        with tempfile.TemporaryDirectory() as raw_tmp:
            results = pathlib.Path(raw_tmp) / "results"
            results.mkdir()
            with temporary_eval_results_and_raw(results):
                audit_path = write_ready_mir_semantic_scope_probe_companion(results)
                audit = json.loads(audit_path.read_text(encoding="utf-8"))
                summary = audit["summary"]
                summary["canonical_heap_family_type_mapping_record_counts"]["binary_heap"] = 0
                summary["binary_heap_type_mapping_record_count"] = 0
                status = evaluate.rustc_driver_mir_semantic_scope_probe_companion_status(audit)
        self.assertFalse(status["ready"])
        joined = " | ".join(status["blockers"])
        self.assertIn("binary_heap heap-object type mapping", joined)

    def test_mir_semantic_scope_probe_requires_receiver_owner_attribution(self) -> None:
        with tempfile.TemporaryDirectory() as raw_tmp:
            results = pathlib.Path(raw_tmp) / "results"
            results.mkdir()
            with temporary_eval_results_and_raw(results):
                audit_path = write_ready_mir_semantic_scope_probe_companion(results)
                audit = json.loads(audit_path.read_text(encoding="utf-8"))

                summary = audit["summary"]
                summary["receiver_mutating_collection_type_mapping_record_count"] = 0
                status = evaluate.rustc_driver_mir_semantic_scope_probe_companion_status(audit)
        self.assertFalse(status["ready"])
        self.assertIn(
            "receiver-mutating collection heap-owner attribution",
            " | ".join(status["blockers"]),
        )

        with tempfile.TemporaryDirectory() as raw_tmp:
            results = pathlib.Path(raw_tmp) / "results"
            results.mkdir()
            with temporary_eval_results_and_raw(results):
                audit_path = write_ready_mir_semantic_scope_probe_companion(results)
                audit = json.loads(audit_path.read_text(encoding="utf-8"))
                summary = audit["summary"]
                summary["receiver_mutating_collection_type_mapping_record_count"] = 12
                summary["receiver_mutating_collection_misattributed_record_count"] = 1
                status = evaluate.rustc_driver_mir_semantic_scope_probe_companion_status(audit)
        self.assertFalse(status["ready"])
        self.assertIn("misattributed", " | ".join(status["blockers"]))

    def test_mir_semantic_scope_probe_requires_unwind_cleanup(self) -> None:
        with tempfile.TemporaryDirectory() as raw_tmp:
            results = pathlib.Path(raw_tmp) / "results"
            results.mkdir()
            with temporary_eval_results_and_raw(results):
                audit_path = write_ready_mir_semantic_scope_probe_companion(results)
                audit = json.loads(audit_path.read_text(encoding="utf-8"))
                summary = audit["summary"]
                summary["semantic_scope_unwind_pop_inserted_count"] = 0
                summary["unwind_scope_cleanup_validated"] = False
                summary["post_unwind_direct_alloc_typed_allocations"] = 1
                summary["post_unwind_direct_alloc_fallback_allocations"] = 0
                status = evaluate.rustc_driver_mir_semantic_scope_probe_companion_status(audit)
        self.assertFalse(status["ready"])
        joined = " | ".join(status["blockers"])
        self.assertIn("cleanup/unwind pop insertion", joined)
        self.assertIn("cleanup pop on panic unwind", joined)
        self.assertIn("leaked typed metadata", joined)

    def test_mir_semantic_scope_probe_requires_generic_drop_classification(self) -> None:
        with tempfile.TemporaryDirectory() as raw_tmp:
            results = pathlib.Path(raw_tmp) / "results"
            results.mkdir()
            with temporary_eval_results_and_raw(results):
                audit_path = write_ready_mir_semantic_scope_probe_companion(results)
                audit = json.loads(audit_path.read_text(encoding="utf-8"))
                summary = audit["summary"]
                summary["semantic_scope_drop_unsolved_candidate_count"] = 1
                summary["unsolved_drop_mapping_record_count"] = 1
                summary["semantic_scope_drop_generic_type_parameter_skipped_count"] = 0
                status = evaluate.rustc_driver_mir_semantic_scope_probe_companion_status(audit)
        self.assertFalse(status["ready"])
        joined = " | ".join(status["blockers"])
        self.assertIn("unsolved non-generic Drop", joined)
        self.assertIn("generic Drop<T> cleanup", joined)

    def test_mir_cross_thread_hint_probe_requires_real_hint_recovery(self) -> None:
        with tempfile.TemporaryDirectory() as raw_tmp:
            results = pathlib.Path(raw_tmp) / "results"
            results.mkdir()
            with temporary_eval_results_and_raw(results):
                audit_path = write_ready_mir_cross_thread_hint_probe_companion(results)
                audit = json.loads(audit_path.read_text(encoding="utf-8"))
                status = evaluate.rustc_driver_mir_cross_thread_hint_probe_companion_status(audit)
                self.assertTrue(status["ready"], status["blockers"])
                summary = audit["summary"]
                summary["cross_thread_recovery_hint_count"] = 0
                summary["hinted_semantic_scope_rewrite_applied_count"] = 0
                summary["lowered_deallocation_rows"] = 0
                summary["no_wrapper_control_validated"] = False
                summary["fallback_attribution_matches_stats"] = False
                summary["non_escaping_vecdeque_cross_thread_hint_count"] = 1
                summary["non_escaping_direct_allocator_cross_thread_hint_count"] = 1
                summary["multi_owner_cross_thread_recovery_pairing_validated"] = False
                status = evaluate.rustc_driver_mir_cross_thread_hint_probe_companion_status(audit)
        self.assertFalse(status["ready"])
        joined = " | ".join(status["blockers"])
        self.assertIn("cross-thread recovery hints", joined)
        self.assertIn("hinted semantic-scope rewrites", joined)
        self.assertIn("lowered deallocation type rows", joined)
        self.assertIn("no-wrapper negative control", joined)
        self.assertIn("runtime fallback attribution", joined)
        self.assertIn("non-escaping VecDeque", joined)
        self.assertIn("non-escaping direct allocator rows", joined)
        self.assertIn("worker-drop recovery pairing", joined)

    def test_mir_cross_thread_multi_owner_pairing_accepts_bound_arc_and_vec(self) -> None:
        mapping_rows, rewrite_candidates, runtime_event = ready_multi_owner_cross_thread_pairing_inputs()
        summary = evaluate.rustc_driver_mir_cross_thread_multi_owner_recovery_pairing_summary(
            mapping_rows,
            rewrite_candidates,
            runtime_event,
        )
        self.assertTrue(
            summary["multi_owner_cross_thread_recovery_pairing_validated"],
            summary["multi_owner_cross_thread_recovery_pairing_blockers"],
        )
        self.assertNotEqual(
            summary["multi_owner_cross_thread_vec_type_id"],
            summary["multi_owner_cross_thread_arc_type_id"],
        )

    def test_mir_cross_thread_multi_owner_pairing_accepts_local_hint_pair(self) -> None:
        mapping_rows, rewrite_candidates, runtime_event = ready_multi_owner_cross_thread_pairing_inputs()
        for row in mapping_rows:
            row["replacement_symbol"] = "__unialloc_semantic_scope_push_hints_local"
            row["replacement_resolution_status"] = (
                "resolved_unialloc_semantic_scope_push_hints_local_pop"
            )
        rewrite_candidates[0]["replacement_symbol"] = "__unialloc_semantic_scope_push_hints_local"
        summary = evaluate.rustc_driver_mir_cross_thread_multi_owner_recovery_pairing_summary(
            mapping_rows,
            rewrite_candidates,
            runtime_event,
        )
        self.assertTrue(
            summary["multi_owner_cross_thread_recovery_pairing_validated"],
            summary["multi_owner_cross_thread_recovery_pairing_blockers"],
        )

    def test_mir_cross_thread_multi_owner_pairing_rejects_unbound_evidence(self) -> None:
        base_mapping, base_rewrites, base_runtime = ready_multi_owner_cross_thread_pairing_inputs()

        missing_dealloc = (copy.deepcopy(base_mapping), copy.deepcopy(base_rewrites), copy.deepcopy(base_runtime))
        missing_dealloc[2]["type_rows"][1]["deallocations"] = 0

        overcounted_alloc = (copy.deepcopy(base_mapping), copy.deepcopy(base_rewrites), copy.deepcopy(base_runtime))
        overcounted_alloc[2]["type_rows"][0]["allocations"] = 2

        tampered_vec_id = (copy.deepcopy(base_mapping), copy.deepcopy(base_rewrites), copy.deepcopy(base_runtime))
        tampered_vec_id[0][0]["type_id"] ^= 0x100

        tampered_arc_symbol = (copy.deepcopy(base_mapping), copy.deepcopy(base_rewrites), copy.deepcopy(base_runtime))
        tampered_arc_symbol[0][1]["replacement_symbol"] = "__unialloc_semantic_scope_push"

        missing_arc_symbol = (copy.deepcopy(base_mapping), copy.deepcopy(base_rewrites), copy.deepcopy(base_runtime))
        missing_arc_symbol[0][1].pop("replacement_symbol")

        cross_paired_arc_symbol = (
            copy.deepcopy(base_mapping),
            copy.deepcopy(base_rewrites),
            copy.deepcopy(base_runtime),
        )
        cross_paired_arc_symbol[0][1]["replacement_symbol"] = (
            "__unialloc_semantic_scope_push_hints_local"
        )

        missing_skip = (copy.deepcopy(base_mapping), [], copy.deepcopy(base_runtime))

        tampered_skip = (copy.deepcopy(base_mapping), copy.deepcopy(base_rewrites), copy.deepcopy(base_runtime))
        tampered_skip[1][0]["rewrite_status"] = "actual_semantic_scope_drop_rewrite_applied"

        missing_skip_symbol = (copy.deepcopy(base_mapping), copy.deepcopy(base_rewrites), copy.deepcopy(base_runtime))
        missing_skip_symbol[1][0].pop("replacement_symbol")

        tampered_skip_symbol = (copy.deepcopy(base_mapping), copy.deepcopy(base_rewrites), copy.deepcopy(base_runtime))
        tampered_skip_symbol[1][0]["replacement_symbol"] = "__unialloc_semantic_scope_push"

        duplicate_skip = (copy.deepcopy(base_mapping), copy.deepcopy(base_rewrites), copy.deepcopy(base_runtime))
        duplicate_skip[1].append(copy.deepcopy(duplicate_skip[1][0]))

        colliding_ids = (copy.deepcopy(base_mapping), copy.deepcopy(base_rewrites), copy.deepcopy(base_runtime))
        colliding_ids[0][1]["type_id"] = colliding_ids[0][0]["type_id"]

        for label, inputs in (
            ("missing Arc deallocation", missing_dealloc),
            ("two Vec allocations for one deallocation", overcounted_alloc),
            ("tampered Vec type id", tampered_vec_id),
            ("tampered Arc replacement symbol", tampered_arc_symbol),
            ("missing Arc replacement symbol", missing_arc_symbol),
            ("cross-paired Arc replacement symbol and resolution", cross_paired_arc_symbol),
            ("missing multi-owner skip", missing_skip),
            ("tampered multi-owner skip", tampered_skip),
            ("missing multi-owner skip replacement symbol", missing_skip_symbol),
            ("tampered multi-owner skip replacement symbol", tampered_skip_symbol),
            ("duplicate multi-owner skip", duplicate_skip),
            ("colliding Arc/Vec ids", colliding_ids),
        ):
            with self.subTest(case=label):
                summary = evaluate.rustc_driver_mir_cross_thread_multi_owner_recovery_pairing_summary(*inputs)
                self.assertFalse(summary["multi_owner_cross_thread_recovery_pairing_validated"])
                self.assertTrue(summary["multi_owner_cross_thread_recovery_pairing_blockers"])

    def test_mir_cross_thread_companion_rejects_summary_without_raw_runtime(self) -> None:
        with tempfile.TemporaryDirectory() as raw_tmp:
            results = pathlib.Path(raw_tmp) / "results"
            results.mkdir()
            with temporary_eval_results_and_raw(results):
                audit_path = write_ready_mir_cross_thread_hint_probe_companion(results)
                audit = json.loads(audit_path.read_text(encoding="utf-8"))
                audit.pop("runtime_event")
                status = evaluate.rustc_driver_mir_cross_thread_hint_probe_companion_status(audit)
        self.assertFalse(status["ready"])
        self.assertIn("raw runtime_event is unavailable", " | ".join(status["blockers"]))

    def test_mir_cross_thread_companion_rejects_summary_tamper_against_raw(self) -> None:
        with tempfile.TemporaryDirectory() as raw_tmp:
            results = pathlib.Path(raw_tmp) / "results"
            results.mkdir()
            with temporary_eval_results_and_raw(results):
                audit_path = write_ready_mir_cross_thread_hint_probe_companion(results)
                audit = json.loads(audit_path.read_text(encoding="utf-8"))
                audit["summary"]["multi_owner_cross_thread_vec_type_id"] ^= 0x100
                status = evaluate.rustc_driver_mir_cross_thread_hint_probe_companion_status(audit)
        self.assertFalse(status["ready"])
        self.assertIn("does not match recomputed raw evidence", " | ".join(status["blockers"]))

    def test_mir_probe_companions_gate_recovery_identity_mismatches(self) -> None:
        cases = (
            (
                write_ready_mir_semantic_scope_probe_companion,
                evaluate.rustc_driver_mir_semantic_scope_probe_companion_status,
            ),
            (
                write_ready_direct_allocator_mir_probe_companion,
                evaluate.rustc_driver_direct_allocator_mir_probe_companion_status,
            ),
            (
                write_ready_mir_cross_thread_hint_probe_companion,
                evaluate.rustc_driver_mir_cross_thread_hint_probe_companion_status,
            ),
        )
        for writer, status_fn in cases:
            with self.subTest(probe=writer.__name__):
                with tempfile.TemporaryDirectory() as raw_tmp:
                    results = pathlib.Path(raw_tmp) / "results"
                    results.mkdir()
                    with temporary_eval_results_and_raw(results):
                        audit_path = writer(results)
                        audit = json.loads(audit_path.read_text(encoding="utf-8"))
                        summary = audit["summary"]
                        summary["recovery_identity_mismatches"] = 1
                        summary["last_mismatch_requested_type_id"] = 0xC002
                        summary["last_mismatch_recorded_type_id"] = 0xDA7A
                        status = status_fn(audit)
                self.assertFalse(status["ready"])
                self.assertIn("recovery identity mismatches", " | ".join(status["blockers"]))

    def test_mir_probe_companions_require_recovery_identity_reporting(self) -> None:
        with tempfile.TemporaryDirectory() as raw_tmp:
            results = pathlib.Path(raw_tmp) / "results"
            results.mkdir()
            with temporary_eval_results_and_raw(results):
                audit_path = write_ready_mir_semantic_scope_probe_companion(results)
                audit = json.loads(audit_path.read_text(encoding="utf-8"))
                summary = audit["summary"]
                for key in evaluate.SEMANTIC_METADATA_VALIDATION_KEYS:
                    summary.pop(key, None)
                summary.pop("semantic_metadata_validation", None)
                status = evaluate.rustc_driver_mir_semantic_scope_probe_companion_status(audit)
        self.assertFalse(status["ready"])
        self.assertIn("recovery-identity validation", " | ".join(status["blockers"]))

    def test_direct_allocator_probe_status_accepts_proved_local_no_recovery_metadata_abi(self) -> None:
        with tempfile.TemporaryDirectory() as raw_tmp:
            results = pathlib.Path(raw_tmp) / "results"
            results.mkdir()
            with temporary_eval_results_and_raw(results):
                audit_path = write_ready_direct_allocator_mir_probe_companion(results)
                audit = mark_direct_allocator_probe_as_local_no_recovery(
                    json.loads(audit_path.read_text(encoding="utf-8"))
                )
                status = evaluate.rustc_driver_direct_allocator_mir_probe_companion_status(audit)
        self.assertTrue(status["ready"], status["blockers"])

    def test_direct_allocator_probe_status_rejects_zero_recovery_matches_without_local_no_recovery_proof(self) -> None:
        with tempfile.TemporaryDirectory() as raw_tmp:
            results = pathlib.Path(raw_tmp) / "results"
            results.mkdir()
            with temporary_eval_results_and_raw(results):
                audit_path = write_ready_direct_allocator_mir_probe_companion(results)
                audit = mark_direct_allocator_probe_as_local_no_recovery(
                    json.loads(audit_path.read_text(encoding="utf-8"))
                )
                audit["summary"]["direct_local_dealloc_rewrite_applied_count"] = 0
                audit["summary"]["direct_local_no_recovery_metadata_abi_validated"] = False
                audit["summary"]["semantic_metadata_recovery_identity_validated"] = False
                audit["summary"]["semantic_metadata_recovery_identity_match_required"] = True
                status = evaluate.rustc_driver_direct_allocator_mir_probe_companion_status(audit)
        self.assertFalse(status["ready"])
        self.assertIn(
            "did not observe any matching semantic metadata recovery identity",
            " | ".join(status["blockers"]),
        )

    def test_direct_allocator_probe_status_rejects_zero_recovery_matches_when_recovery_backed_rows_remain(self) -> None:
        with tempfile.TemporaryDirectory() as raw_tmp:
            results = pathlib.Path(raw_tmp) / "results"
            results.mkdir()
            with temporary_eval_results_and_raw(results):
                audit_path = write_ready_direct_allocator_mir_probe_companion(results)
                audit = mark_direct_allocator_probe_as_local_no_recovery(
                    json.loads(audit_path.read_text(encoding="utf-8"))
                )
                audit["summary"]["direct_size_align_recovery_backed_required_count"] = 1
                audit["summary"]["direct_conservative_size_align_alloc_rewrite_applied_count"] = 1
                audit["summary"]["direct_local_no_recovery_metadata_abi_validated"] = False
                audit["summary"]["semantic_metadata_recovery_identity_validated"] = False
                audit["summary"]["semantic_metadata_recovery_identity_match_required"] = True
                status = evaluate.rustc_driver_direct_allocator_mir_probe_companion_status(audit)
        self.assertFalse(status["ready"])
        self.assertIn(
            "did not observe any matching semantic metadata recovery identity",
            " | ".join(status["blockers"]),
        )

    def test_mir_probe_companions_require_raw_provider_override_rewrite_maps(self) -> None:
        with tempfile.TemporaryDirectory() as raw_tmp:
            results = pathlib.Path(raw_tmp) / "results"
            results.mkdir()
            with temporary_eval_results_and_raw(results):
                cases = [
                    (
                        write_ready_mir_semantic_scope_probe_companion,
                        evaluate.rustc_driver_mir_semantic_scope_probe_companion_status,
                        "semantic-scope MIR probe",
                    ),
                    (
                        write_ready_direct_allocator_mir_probe_companion,
                        evaluate.rustc_driver_direct_allocator_mir_probe_companion_status,
                        "direct allocator MIR probe",
                    ),
                    (
                        write_ready_mir_cross_thread_hint_probe_companion,
                        evaluate.rustc_driver_mir_cross_thread_hint_probe_companion_status,
                        "cross-thread MIR hint probe",
                    ),
                ]
                for writer, status_fn, label in cases:
                    with self.subTest(label=label):
                        audit_path = writer(results)
                        audit = json.loads(audit_path.read_text(encoding="utf-8"))
                        self.assertTrue(status_fn(audit)["ready"], label)

                        audit_without_target = json.loads(json.dumps(audit))
                        audit_without_target.pop("target_rewrite", None)
                        status = status_fn(audit_without_target)
                        self.assertFalse(status["ready"])
                        self.assertIn(
                            "target_rewrite provider-override evidence",
                            " | ".join(status["blockers"]),
                        )

                        rewrite_path = pathlib.Path(audit["target_rewrite"]["path"])
                        rewrite_artifact = json.loads(rewrite_path.read_text(encoding="utf-8"))
                        outside_raw = pathlib.Path(raw_tmp) / f"{label.replace(' ', '-')}-outside.json"
                        write_json(outside_raw, rewrite_artifact)
                        outside_audit = json.loads(json.dumps(audit))
                        outside_audit["target_rewrite"]["path"] = str(outside_raw)
                        status = status_fn(outside_audit)
                        self.assertFalse(status["ready"])
                        self.assertIn("not under evaluation/raw", " | ".join(status["blockers"]))

                        bad_kind_path = rewrite_path.with_name(rewrite_path.stem + "-bad-kind.json")
                        rewrite_artifact["compiler_pass"]["kind"] = "hand_written_fixture"
                        write_json(bad_kind_path, rewrite_artifact)
                        bad_kind_audit = json.loads(json.dumps(audit))
                        bad_kind_audit["target_rewrite"]["path"] = str(bad_kind_path)
                        status = status_fn(bad_kind_audit)
                        self.assertFalse(status["ready"])
                        self.assertIn("optimized_mir provider override", " | ".join(status["blockers"]))

    def test_compiler_coverage_claim_grade_status_requires_runtime_companion(self) -> None:
        old_results = evaluate.RESULTS
        with tempfile.TemporaryDirectory() as raw_tmp:
            results = pathlib.Path(raw_tmp) / "results"
            results.mkdir()
            try:
                evaluate.RESULTS = results
                status = evaluate.compiler_coverage_claim_grade_status(
                    ready_mir_semantic_scope_compiler_audit(coverage=99.91),
                    threshold=72.17,
                )
            finally:
                evaluate.RESULTS = old_results
        self.assertFalse(status["ready"])
        joined = " | ".join(status["blockers"])
        self.assertIn("compiler runtime companion blocker", joined)
        self.assertIn("compiler coverage manifest runtime audit could not be resolved", joined)

    def test_mir_semantic_scope_runtime_companion_requires_target_rewrite_evidence(self) -> None:
        with tempfile.TemporaryDirectory() as raw_tmp:
            results = pathlib.Path(raw_tmp) / "results"
            results.mkdir()
            with temporary_eval_results_and_raw(results):
                audit_path = write_ready_mir_semantic_scope_runtime_companion(results)
                audit = json.loads(audit_path.read_text(encoding="utf-8"))

                status = evaluate.rustc_driver_mir_semantic_scope_runtime_companion_status(audit)
                self.assertTrue(status["ready"], status["blockers"])

                audit_without_target = dict(audit)
                audit_without_target.pop("target_rewrite", None)
                status = evaluate.rustc_driver_mir_semantic_scope_runtime_companion_status(audit_without_target)
                self.assertFalse(status["ready"])
                joined = " | ".join(status["blockers"])
                self.assertIn("target_rewrite provider-override evidence", joined)

                audit_bad_target = dict(audit)
                audit_bad_target["target_rewrite"] = {
                    **audit["target_rewrite"],
                    "provider_override_installed": False,
                    "body_clone_returned_to_rustc": False,
                    "semantic_scope_rewrite_applied_count": 0,
                    "semantic_scope_replacement_resolution_status": "not_requested_dry_run",
                    "replacement_resolution_status": "not_requested_dry_run",
                }
                status = evaluate.rustc_driver_mir_semantic_scope_runtime_companion_status(audit_bad_target)
                self.assertFalse(status["ready"])
                joined = " | ".join(status["blockers"])
                self.assertIn("provider override", joined)
                self.assertIn("cloned MIR body", joined)
                self.assertIn("target_rewrite reports no applied semantic-scope rewrites", joined)

                audit_missing_artifact = dict(audit)
                audit_missing_artifact["target_rewrite"] = {
                    **audit["target_rewrite"],
                    "path": str(results.parent / "raw" / "missing-rewrite-map.json"),
                }
                status = evaluate.rustc_driver_mir_semantic_scope_runtime_companion_status(
                    audit_missing_artifact
                )
                self.assertFalse(status["ready"])
                joined = " | ".join(status["blockers"])
                self.assertIn("raw rewrite-map artifact is missing", joined)

                rewrite_path = pathlib.Path(audit["target_rewrite"]["path"])
                rewrite_artifact = json.loads(rewrite_path.read_text(encoding="utf-8"))
                rewrite_artifact["rewrite_candidates"][0][
                    "source_span"
                ] = "unialloc/examples/std_vec_bench.rs:1:1: 1:10"
                bad_source_path = rewrite_path.with_name("std_bench_bad_source.json")
                write_json(bad_source_path, rewrite_artifact)
                audit_bad_source = dict(audit)
                audit_bad_source["target_rewrite"] = {
                    **audit["target_rewrite"],
                    "path": str(bad_source_path),
                }
                status = evaluate.rustc_driver_mir_semantic_scope_runtime_companion_status(
                    audit_bad_source
                )
                self.assertFalse(status["ready"])
                joined = " | ".join(status["blockers"])
                self.assertIn("no applied candidates from unialloc/benches/", joined)

    def test_mir_semantic_scope_runtime_companion_requires_explicit_compiler_pass_query(self) -> None:
        with tempfile.TemporaryDirectory() as raw_tmp:
            results = pathlib.Path(raw_tmp) / "results"
            results.mkdir()
            with temporary_eval_results_and_raw(results):
                audit_path = write_ready_mir_semantic_scope_runtime_companion(results)
                audit = json.loads(audit_path.read_text(encoding="utf-8"))

                cases = {
                    "missing": ({}, "missing compiler_pass provider-override evidence"),
                    "wrong_kind": (
                        {"kind": "rustc_mir_dry_run", "query_overridden": "optimized_mir"},
                        "compiler_pass is not rustc_driver_optimized_mir_provider_override",
                    ),
                    "wrong_query": (
                        {
                            "kind": "rustc_driver_optimized_mir_provider_override",
                            "query_overridden": "mir_built",
                        },
                        "compiler_pass does not identify optimized_mir",
                    ),
                }
                for name, (compiler_pass, expected) in cases.items():
                    with self.subTest(name=name):
                        bad = dict(audit)
                        bad_target = dict(audit["target_rewrite"])
                        if compiler_pass:
                            bad_target["compiler_pass"] = compiler_pass
                        else:
                            bad_target.pop("compiler_pass", None)
                        bad["target_rewrite"] = bad_target
                        status = evaluate.rustc_driver_mir_semantic_scope_runtime_companion_status(bad)
                        self.assertFalse(status["ready"], status)
                        self.assertIn(expected, " | ".join(status["blockers"]))

    def test_mir_semantic_scope_runtime_companion_requires_direct_claim_modes(self) -> None:
        with tempfile.TemporaryDirectory() as raw_tmp:
            results = pathlib.Path(raw_tmp) / "results"
            results.mkdir()
            with temporary_eval_results_and_raw(results):
                audit_path = write_ready_mir_semantic_scope_runtime_companion(results)
                audit = json.loads(audit_path.read_text(encoding="utf-8"))
                summary = dict(audit["summary"])
                summary.pop("direct_allocator_rewrite_requested")
                summary["direct_allocator_rewrite_validated"] = False
                summary["direct_local_size_align_with_semantic_drop"] = False
                summary["direct_size_align_local_pairing_validated"] = False
                audit["summary"] = summary
                status = evaluate.rustc_driver_mir_semantic_scope_runtime_companion_status(audit)
        self.assertFalse(status["ready"], status)
        joined = " | ".join(status["blockers"])
        self.assertIn("direct allocator rewrite mode was not requested", joined)
        self.assertIn("direct allocator rewrite validation is not satisfied", joined)
        self.assertIn("direct local size/align semantic-drop mode was not requested", joined)
        self.assertIn("direct size/align local ABI pairing validation is not satisfied", joined)

    def test_compiler_coverage_claim_grade_status_requires_mir_probe_companions(self) -> None:
        with tempfile.TemporaryDirectory() as raw_tmp:
            results = pathlib.Path(raw_tmp) / "results"
            results.mkdir()
            with temporary_eval_results_and_raw(results):
                write_ready_mir_semantic_scope_runtime_companion(results)
                status = evaluate.compiler_coverage_claim_grade_status(
                    ready_mir_semantic_scope_compiler_audit(coverage=99.91),
                    threshold=72.17,
                )
        self.assertFalse(status["ready"])
        joined = " | ".join(status["blockers"])
        self.assertIn("compiler MIR probe companion blocker", joined)
        self.assertIn("missing evaluation/results/rustc_driver_mir_semantic_scope_probe_audit.json", joined)
        self.assertIn("missing evaluation/results/rustc_driver_direct_allocator_mir_probe_audit.json", joined)
        self.assertIn("missing evaluation/results/rustc_driver_mir_cross_thread_hint_probe_audit.json", joined)

    def test_compiler_coverage_claim_grade_status_rechecks_current_receiver_owner_probe(self) -> None:
        with tempfile.TemporaryDirectory() as raw_tmp:
            results = pathlib.Path(raw_tmp) / "results"
            results.mkdir()
            with temporary_eval_results_and_raw(results):
                write_ready_mir_semantic_scope_runtime_companion(results)
                write_ready_mir_probe_companions(results)
                embedded_companions = evaluate.rustc_driver_mir_probe_companion_status()
                semantic_audit_path = results / "rustc_driver_mir_semantic_scope_probe_audit.json"
                semantic_audit = json.loads(semantic_audit_path.read_text(encoding="utf-8"))
                semantic_audit["summary"].pop(
                    "receiver_mutating_collection_type_mapping_record_count",
                    None,
                )
                semantic_audit["summary"]["receiver_mutating_collection_misattributed_record_count"] = 1
                write_json(semantic_audit_path, semantic_audit)
                compiler_audit = ready_mir_semantic_scope_compiler_audit(coverage=99.91)
                compiler_audit["rustc_driver_mir_probe_companions"] = embedded_companions
                status = evaluate.compiler_coverage_claim_grade_status(
                    compiler_audit,
                    threshold=72.17,
                )
        self.assertFalse(status["ready"])
        joined = " | ".join(status["blockers"])
        self.assertIn("receiver-mutating collection heap-owner attribution", joined)
        self.assertIn("misattributed", joined)

    def test_compiler_coverage_audit_requires_mir_probe_companions_for_semantic_scope_claim(self) -> None:
        old_results = evaluate.RESULTS
        with tempfile.TemporaryDirectory() as raw_tmp:
            tmp = pathlib.Path(raw_tmp)
            results = tmp / "results"
            results.mkdir()
            manifest = write_manifest_fixture(
                tmp,
                basis="compiler-assigned-allocation-site-object-type-id-rustc-driver-mir-semantic-scope",
                replay=False,
            )
            try:
                evaluate.RESULTS = results
                audit = evaluate.build_compiler_coverage_audit(manifest)
            finally:
                evaluate.RESULTS = old_results
        self.assertFalse(audit["summary"]["ready_for_claim_grade_import"])
        self.assertFalse(audit["summary"]["rustc_driver_mir_probe_companions_ready"])
        joined = " | ".join(audit["blockers"])
        self.assertIn("compiler MIR probe companion blocker", joined)
        self.assertIn("missing evaluation/results/rustc_driver_mir_semantic_scope_probe_audit.json", joined)
        self.assertIn("missing evaluation/results/rustc_driver_mir_cross_thread_hint_probe_audit.json", joined)

    def test_c002_claim_uses_ready_compiler_audit_over_legacy_coverage_and_gap_diagnostics(self) -> None:
        claim = {
            "id": "C002-type-coverage",
            "description": "Compiler-assisted extraction covers standard Rust alloc benchmarks.",
            "metric": "coverage_percent",
            "threshold": {"gte": 72.17},
            "required_for_overclaim": True,
        }
        with tempfile.TemporaryDirectory() as raw_tmp:
            results = pathlib.Path(raw_tmp) / "results"
            results.mkdir()
            with temporary_eval_results_and_raw(results):
                write_json(
                    results / "coverage_summary.json",
                    {
                        "coverage_percent": 99.98,
                        "evidence_verified": True,
                        "claim_grade": False,
                        "type_id_basis": "compiler-assigned-allocation-site-object-type-id",
                        "claim_grade_blockers": ["legacy selected-slice coverage import"],
                    },
                )
                write_json(
                    results / "compiler_coverage_evidence_audit.json",
                    ready_mir_semantic_scope_compiler_audit(coverage=99.91),
                )
                write_ready_mir_semantic_scope_runtime_companion(results)
                write_ready_mir_probe_companions(results)
                write_json(
                    results / "compiler_dynamic_attribution_gap_audit.json",
                    {
                        "summary": {
                            "current_mir_semantic_scope_runtime_evidence_ready": True,
                            "observed_source_claim_ready": True,
                            "benchmark_suite_claim_ready": True,
                            "remaining_gap_count": 7,
                        },
                        "remaining_gaps": [
                            {
                                "id": "legacy-preflight-gap",
                                "description": "Older replay/preflight diagnostic gap.",
                            }
                        ],
                    },
                )
                result = evaluate.evaluate_claim(claim, {"source": "current"}, "current")
        self.assertEqual(result["status"], "pass", result["evidence"])
        self.assertEqual(result["observed"], 99.91)
        evidence = " | ".join(result["evidence"])
        self.assertIn("accepted current claim-grade compiler coverage audit", evidence)
        self.assertIn("accepted real rustc_driver MIR semantic-scope runtime companion", evidence)
        self.assertIn("accepted small rustc_driver MIR probe companions", evidence)
        self.assertIn("cross_thread_recovery_hints=5", evidence)
        self.assertIn("remaining_legacy_gap_count=7", evidence)
        self.assertNotIn("dynamic attribution gap: legacy-preflight-gap", evidence)

    def test_c002_claim_can_use_ready_compiler_audit_without_legacy_coverage_summary(self) -> None:
        claim = {
            "id": "C002-type-coverage",
            "description": "Compiler-assisted extraction covers standard Rust alloc benchmarks.",
            "metric": "coverage_percent",
            "threshold": {"gte": 72.17},
            "required_for_overclaim": True,
        }
        with tempfile.TemporaryDirectory() as raw_tmp:
            results = pathlib.Path(raw_tmp) / "results"
            results.mkdir()
            with temporary_eval_results_and_raw(results):
                write_json(
                    results / "compiler_coverage_evidence_audit.json",
                    ready_mir_semantic_scope_compiler_audit(coverage=99.5),
                )
                write_ready_mir_semantic_scope_runtime_companion(results)
                write_ready_mir_probe_companions(results)
                result = evaluate.evaluate_claim(claim, {"source": "current"}, "current")
        self.assertEqual(result["status"], "pass", result["evidence"])
        self.assertEqual(result["observed"], 99.5)
        evidence = " | ".join(result["evidence"])
        self.assertIn("coverage_summary is absent", evidence)
        self.assertIn("runtime companion", evidence)
        self.assertIn("MIR probe companions", evidence)

    def test_std_bench_runtime_target_selection_supports_full_surface_shards(self) -> None:
        benches = [
            "aaa_semantic_auto_metadata_enable",
            "binary_heap::bench_push",
            "btree::map::insert_rand_100",
            "vec_deque::bench_grow_1025",
            "zzz_semantic_auto_metadata_report",
        ]
        selection = evaluate.select_std_bench_runtime_targets(
            benches,
            full_surface=True,
            shard_index=1,
            shard_count=2,
        )
        self.assertEqual(selection["selection_mode"], "full-surface-shard")
        self.assertEqual(
            selection["selected_target_benches"],
            ["btree::map::insert_rand_100"],
        )
        self.assertIn("aaa_semantic_auto_metadata_enable", selection["selected_benches"])
        self.assertIn("zzz_semantic_auto_metadata_report", selection["selected_benches"])
        self.assertIn("binary_heap::bench_push", selection["derived_skips"])

    def test_std_bench_runtime_target_selection_keeps_pattern_smoke_deterministic(self) -> None:
        benches = [
            "aaa_semantic_auto_metadata_enable",
            "binary_heap::bench_push",
            "btree::map::insert_rand_100",
            "vec_deque::bench_grow_1025",
            "zzz_semantic_auto_metadata_report",
        ]
        selection = evaluate.select_std_bench_runtime_targets(
            benches,
            bench_patterns=["btree::map::insert"],
            max_benchmarks=1,
        )
        self.assertEqual(selection["selection_mode"], "pattern-smoke")
        self.assertEqual(selection["selected_target_benches"], ["btree::map::insert_rand_100"])
        self.assertEqual(selection["max_benchmarks"], 1)
        self.assertNotIn("btree::map::insert_rand_100", selection["derived_skips"])

    def test_std_bench_runtime_target_selection_can_offset_inside_shard(self) -> None:
        benches = [
            "aaa_semantic_auto_metadata_enable",
            "bench_0",
            "bench_1",
            "bench_2",
            "bench_3",
            "bench_4",
            "zzz_semantic_auto_metadata_report",
        ]
        selection = evaluate.select_std_bench_runtime_targets(
            benches,
            full_surface=True,
            shard_index=0,
            shard_count=2,
            shard_offset=1,
            max_benchmarks=1,
        )
        self.assertEqual(selection["selection_mode"], "full-surface-shard-offset-capped")
        self.assertEqual(selection["selected_target_benches"], ["bench_2"])
        self.assertEqual(selection["shard_offset"], 1)
        self.assertIn("bench_0", selection["derived_skips"])
        self.assertNotIn("bench_2", selection["derived_skips"])

    def test_std_bench_runtime_target_selection_accepts_exact_benchmarks(self) -> None:
        benches = [
            "aaa_semantic_auto_metadata_enable",
            "btree::set::clone_100",
            "btree::set::clone_100_and_remove_half",
            "zzz_semantic_auto_metadata_report",
        ]
        selection = evaluate.select_std_bench_runtime_targets(
            benches,
            explicit_benchmarks=["btree::set::clone_100_and_remove_half"],
            bench_patterns=["btree::set::clone_100"],
        )
        self.assertEqual(selection["selection_mode"], "explicit-benchmarks")
        self.assertEqual(
            selection["selected_target_benches"],
            ["btree::set::clone_100_and_remove_half"],
        )
        self.assertIn("btree::set::clone_100", selection["derived_skips"])

    def test_std_bench_runtime_target_selection_rejects_unknown_exact_benchmark(self) -> None:
        with self.assertRaisesRegex(ValueError, "unknown std_bench target"):
            evaluate.select_std_bench_runtime_targets(
                ["aaa_semantic_auto_metadata_enable", "bench_a", "zzz_semantic_auto_metadata_report"],
                explicit_benchmarks=["bench_missing"],
            )

    def test_load_std_bench_benchmark_requests_reads_json_object(self) -> None:
        with tempfile.TemporaryDirectory() as raw_tmp:
            path = pathlib.Path(raw_tmp) / "benchmarks.json"
            write_json(path, {"missing_benchmarks": ["bench_a", "bench_b"]})
            benches = evaluate.load_std_bench_benchmark_requests(
                ["bench_c,bench_d"],
                [str(path)],
            )
        self.assertEqual(benches, ["bench_c", "bench_d", "bench_a", "bench_b"])

    def test_load_std_bench_recommended_batch_requests_reads_plan_index(self) -> None:
        with tempfile.TemporaryDirectory() as raw_tmp:
            path = pathlib.Path(raw_tmp) / "plan.json"
            write_json(
                path,
                {
                    "recommended_shards": [
                        {"missing_benchmarks": ["bench_a", "bench_b"]},
                        {"missing_benchmarks": ["bench_c", "bench_d"]},
                    ]
                },
            )
            benches = evaluate.load_std_bench_recommended_batch_requests([str(path)], batch_index=1)
        self.assertEqual(benches, ["bench_c", "bench_d"])

    def test_load_std_bench_recommended_batch_requests_reads_consecutive_range(self) -> None:
        with tempfile.TemporaryDirectory() as raw_tmp:
            path = pathlib.Path(raw_tmp) / "plan.json"
            write_json(
                path,
                {
                    "recommended_shards": [
                        {"missing_benchmarks": ["bench_a", "bench_b"]},
                        {"missing_benchmarks": ["bench_c", "bench_d"]},
                        {"missing_benchmarks": ["bench_e", "bench_f"]},
                    ]
                },
            )
            benches = evaluate.load_std_bench_recommended_batch_requests(
                [str(path)],
                batch_index=1,
                batch_count=2,
            )
        self.assertEqual(benches, ["bench_c", "bench_d", "bench_e", "bench_f"])

    def test_std_bench_adaptive_batches_are_bounded_and_deduped(self) -> None:
        batches = evaluate.std_bench_adaptive_batches(
            ["bench_a,bench_b", "bench_a", "bench_c", "bench_d"],
            batch_size=2,
            max_batches=1,
        )
        self.assertEqual(batches, [["bench_a", "bench_b"]])

    def test_std_bench_adaptive_batches_reject_bad_size(self) -> None:
        with self.assertRaisesRegex(ValueError, "batch-size"):
            evaluate.std_bench_adaptive_batches(["bench_a"], batch_size=0)

    def test_std_bench_adaptive_child_run_id_is_short_and_unique(self) -> None:
        parent = "rustc-driver-mir-semantic-scope-std-bench-runtime-adaptive-" + "x" * 120
        first = evaluate.std_bench_adaptive_child_run_id(
            parent,
            "b0",
            ["btree::set::clone_10k_and_pop_all"],
        )
        second = evaluate.std_bench_adaptive_child_run_id(
            parent,
            "b0",
            ["btree::set::difference_random_10k_vs_10k"],
        )
        self.assertLessEqual(len(first), 96)
        self.assertLessEqual(len(second), 96)
        self.assertNotEqual(first, second)
        self.assertIn("-n1-", first)

    def test_adaptive_runtime_refresh_steps_are_fail_closed_except_claim_check(self) -> None:
        steps = evaluate.adaptive_runtime_refresh_steps(
            "unit-refresh",
            exact_batch_size=3,
            max_new_shards=2,
            timeout=77,
        )
        self.assertEqual([step["name"] for step in steps], [
            "plan-runtime-shards",
            "merge-runtime-shards",
            "package-runtime",
            "audit-dynamic-gap",
            "claim-check-current",
        ])
        self.assertTrue(steps[-1]["allow_nonzero"])
        self.assertFalse(any(step["allow_nonzero"] for step in steps[:-1]))
        command_text = " ".join(" ".join(step["command"]) for step in steps)
        self.assertIn("--exact-batch-size 3", command_text)
        self.assertIn("--max-new-shards 2", command_text)
        self.assertIn("--recommendation-family-burst 2", command_text)
        self.assertIn("--source current", command_text)

    def test_adaptive_runtime_refresh_steps_forward_planner_recommendation_knobs(self) -> None:
        steps = evaluate.adaptive_runtime_refresh_steps(
            "unit-refresh",
            exact_batch_size=3,
            max_new_shards=2,
            timeout=77,
            recommendation_max_estimated_cost=120,
            recommendation_family_burst=0,
        )
        plan_command = next(step["command"] for step in steps if step["name"] == "plan-runtime-shards")
        command_text = " ".join(plan_command)
        self.assertIn("--recommendation-max-estimated-cost 120", command_text)
        self.assertIn("--recommendation-family-burst 0", command_text)
        self.assertIn("--recommendation-batch-family-cap 0", command_text)

    def test_family_capped_exact_batches_defer_dominant_family_when_alternates_exist(self) -> None:
        ordered = [
            "vec::bench_clone_from_01_0100_1000",
            "vec::bench_clone_from_01_1000_0100",
            "vec::bench_clone_from_01_1000_1000",
            "vec::bench_clone_from_10_0010_0010",
            "vec::bench_clone_from_10_0010_0100",
            "vec::bench_clone_from_10_0100_0010",
            "vec::bench_in_place_recycle",
            "slice::starts_with_diff_one_element_at_end",
            "slice::rotate_tiny_by1",
            "linked_list::bench_iter_mut",
        ]
        batches, diagnostics = evaluate.family_capped_std_bench_exact_batches(
            ordered,
            batch_size=6,
            max_per_family_per_batch=2,
        )
        self.assertTrue(diagnostics["enabled"])
        self.assertTrue(diagnostics["changed"])
        self.assertEqual(
            batches[0],
            [
                "vec::bench_clone_from_01_0100_1000",
                "vec::bench_clone_from_01_1000_0100",
                "vec::bench_in_place_recycle",
                "slice::starts_with_diff_one_element_at_end",
                "slice::rotate_tiny_by1",
                "linked_list::bench_iter_mut",
            ],
        )
        first_batch_families = evaluate.std_bench_recommendation_family_summary(batches[0])[
            "benchmark_family_counts"
        ]
        self.assertEqual(first_batch_families["vec::bench_clone_from_*"], 2)
        self.assertEqual(diagnostics["output_first_batch_family_counts"][0], first_batch_families)

    def test_bounded_digest_run_id_preserves_suffix_and_uniqueness(self) -> None:
        long_prefix = "rustc-driver-mir-semantic-scope-std-bench-runtime-adaptive-" + "x" * 160
        first = evaluate.bounded_digest_run_id(long_prefix, "package", "salt-a", limit=72)
        second = evaluate.bounded_digest_run_id(long_prefix, "package", "salt-b", limit=72)
        self.assertLessEqual(len(first), 72)
        self.assertLessEqual(len(second), 72)
        self.assertIn("package", first)
        self.assertNotEqual(first, second)

    def test_compact_adaptive_refresh_step_summary_drops_large_fields(self) -> None:
        compact = evaluate.compact_adaptive_refresh_step_summary(
            "audit-dynamic-gap",
            {
                "compiler_audit_ready_for_claim_grade_import": False,
                "benchmark_surface_missing_count": 12,
                "remaining_gap_count": 3,
                "very_large_nested_field": {"x": list(range(100))},
            },
        )
        self.assertEqual(
            compact,
            {
                "compiler_audit_ready_for_claim_grade_import": False,
                "benchmark_surface_missing_count": 12,
                "remaining_gap_count": 3,
            },
        )

    def test_libtest_exact_filter_args_avoid_prefix_skip_false_negative(self) -> None:
        args = evaluate.libtest_exact_filter_args(
            [
                "aaa_semantic_auto_metadata_enable",
                "btree::set::clone_100_and_remove_half",
                "zzz_semantic_auto_metadata_report",
            ]
        )
        self.assertEqual(args[0], "--exact")
        self.assertIn("btree::set::clone_100_and_remove_half", args)
        self.assertNotIn("--skip", args)
        self.assertNotIn("btree::set::clone_100", args)

    def test_test_once_libtest_args_force_single_threaded_sentinels(self) -> None:
        args = evaluate.std_bench_runtime_libtest_args(
            [
                "aaa_semantic_auto_metadata_enable",
                "vec::bench_from_slice_0000",
                "zzz_semantic_auto_metadata_report",
            ],
            libtest_mode="test-once",
        )
        self.assertEqual(args[:2], ["--nocapture", "--test-threads=1"])
        self.assertIn("--exact", args)
        self.assertIn("aaa_semantic_auto_metadata_enable", args)
        self.assertIn("zzz_semantic_auto_metadata_report", args)

    def test_bench_libtest_args_preserve_historical_parallel_bench_mode(self) -> None:
        args = evaluate.std_bench_runtime_libtest_args(
            ["vec::bench_from_slice_0000"],
            libtest_mode="bench",
        )
        self.assertEqual(args[:2], ["--nocapture", "--exact"])
        self.assertNotIn("--test-threads=1", args)

    def test_normalize_std_bench_libtest_mode_accepts_coverage_aliases(self) -> None:
        self.assertEqual(evaluate.normalize_std_bench_libtest_mode("coverage"), "test-once")
        self.assertEqual(evaluate.normalize_std_bench_libtest_mode("cargo-bench"), "bench")
        with self.assertRaisesRegex(ValueError, "--libtest-mode"):
            evaluate.normalize_std_bench_libtest_mode("mock")

    def test_purge_cargo_bench_target_artifacts_can_cover_debug_test_profile(self) -> None:
        with tempfile.TemporaryDirectory() as raw_tmp:
            target = pathlib.Path(raw_tmp) / "target"
            release_dep = target / "release" / "deps" / "std_bench-release"
            debug_dep = target / "debug" / "deps" / "std_bench-debug"
            debug_fingerprint = target / "debug" / ".fingerprint" / "std_bench-debug"
            debug_incremental = target / "debug" / "incremental" / "std_bench-debug-incremental"
            unrelated = target / "debug" / "deps" / "other_bench-debug"
            for path in [release_dep, debug_dep, debug_fingerprint, unrelated]:
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text("x", encoding="utf-8")
            debug_incremental.mkdir(parents=True, exist_ok=True)
            (debug_incremental / "work-product").write_text("x", encoding="utf-8")
            result = evaluate.purge_cargo_bench_target_artifacts(
                target,
                "std_bench",
                profiles=("release", "debug"),
            )
            self.assertEqual(result["returncode"], 0)
            self.assertFalse(release_dep.exists())
            self.assertFalse(debug_dep.exists())
            self.assertFalse(debug_fingerprint.exists())
            self.assertFalse(debug_incremental.exists())
            self.assertTrue(unrelated.exists())
            self.assertEqual(result["profiles"], ["release", "debug"])

    def test_parse_jsonl_events_accepts_libtest_inline_json_output(self) -> None:
        with tempfile.TemporaryDirectory() as raw_tmp:
            path = pathlib.Path(raw_tmp) / "stdout.txt"
            path.write_text(
                "\n".join(
                    [
                        "test zzz_semantic_auto_metadata_report ... {\"event\":\"typed_allocation_site\",\"count\":1}",
                        "{\"event\":\"fallback_allocations\",\"count\":2} ok",
                    ]
                )
                + "\n",
                encoding="utf-8",
            )
            events = evaluate.parse_jsonl_events(path)
        self.assertEqual([event["event"] for event in events], [
            "typed_allocation_site",
            "fallback_allocations",
        ])
        self.assertEqual([event["count"] for event in events], [1, 2])

    def test_parse_jsonl_events_ignores_directory_stdout_path(self) -> None:
        with tempfile.TemporaryDirectory() as raw_tmp:
            self.assertEqual(evaluate.parse_jsonl_events(pathlib.Path(raw_tmp)), [])

    def test_parse_bench_stdout_ignores_directory_stdout_path(self) -> None:
        with tempfile.TemporaryDirectory() as raw_tmp:
            self.assertEqual(evaluate.parse_bench_stdout(pathlib.Path(raw_tmp)), [])

    def test_coverage_stats_keeps_deallocation_only_type_rows(self) -> None:
        with tempfile.TemporaryDirectory() as raw_tmp:
            events = pathlib.Path(raw_tmp) / "events.jsonl"
            rows = [
                {
                    "source": "std_bench_auto_metadata",
                    "event": "typed_allocation_site",
                    "typed": True,
                    "type_id": 7,
                    "callsite": 11,
                    "count": 2,
                    "bytes": 64,
                    "deallocations": 1,
                },
                {
                    "source": "std_bench_auto_metadata",
                    "event": "typed_allocation_site",
                    "typed": True,
                    "type_id": 7,
                    "callsite": 99,
                    "count": 0,
                    "bytes": 0,
                    "deallocations": 3,
                },
                {
                    "source": "std_bench_auto_metadata",
                    "event": "fallback_allocations",
                    "typed": False,
                    "type_id": 0,
                    "count": 1,
                    "bytes": 16,
                    "deallocations": 1,
                },
            ]
            events.write_text(
                "".join(json.dumps(row, sort_keys=True) + "\n" for row in rows),
                encoding="utf-8",
            )
            stats = evaluate.parse_coverage_events_stats(events)

        self.assertTrue(stats["valid"], stats["parse_errors"])
        self.assertEqual(stats["total_allocations"], 3)
        self.assertEqual(stats["typed_allocations"], 2)
        self.assertEqual(stats["total_deallocations"], 5)
        self.assertEqual(stats["typed_deallocations"], 4)
        self.assertEqual(stats["per_type_event_count"], 2)
        self.assertEqual(stats["per_type_event_deallocations"], 4)
        self.assertIn(
            {"type_id": 7, "callsite": 99},
            stats["per_type_event_type_id_callsite_pairs"],
        )

    def test_coverage_stats_extracts_hugepage_mmap_observability_without_polluting_coverage(self) -> None:
        with tempfile.TemporaryDirectory() as raw_tmp:
            events = pathlib.Path(raw_tmp) / "events.jsonl"
            rows = [
                {
                    "source": "std_bench_auto_metadata",
                    "event": "typed_allocation_site",
                    "typed": True,
                    "type_id": 7,
                    "callsite": 11,
                    "count": 3,
                    "bytes": 96,
                    "deallocations": 2,
                },
                {
                    "source": "std_bench_auto_metadata",
                    "event": "fallback_allocations",
                    "typed": False,
                    "type_id": 0,
                    "count": 2,
                    "bytes": 64,
                    "deallocations": 2,
                },
                {
                    "source": "std_bench_auto_metadata",
                    "event": "hugepage_mmap_stats",
                    "feature_hugepage": True,
                    "attempts": 4,
                    "successes": 0,
                    "fallbacks": 4,
                    "aligned_fallbacks": 3,
                    "advised_fallbacks": 1,
                    "fallback_failures": 0,
                },
                {
                    "source": "std_bench_auto_metadata",
                    "event": "hugepage_metadata_side_cache_probe",
                    "feature_hugepage": True,
                    "attempted": True,
                    "allocated": True,
                    "reused": True,
                    "preexisting_allocated": False,
                    "preexisting_mapping_backing": "unallocated",
                    "cache_hits_delta": 1,
                    "cache_inserts_delta": 2,
                    "cache_bypasses_delta": 1,
                    "mmap_attempts_delta": 1,
                    "mmap_successes_delta": 0,
                    "mmap_fallbacks_delta": 1,
                    "mmap_aligned_fallbacks_delta": 1,
                    "mmap_advised_fallbacks_delta": 0,
                    "mmap_fallback_failures_delta": 0,
                    "mapping_backing": "ordinary-fallback-aligned",
                },
                {
                    "source": "std_bench_auto_metadata",
                    "event": "metadata_segregation_side_cache_probe",
                    "feature_metadata_segregation": True,
                    "attempted": True,
                    "allocated": True,
                    "reused": True,
                    "side_cache_inline_occupied_after": True,
                    "side_cache_occupied_entries_after": 1,
                    "side_cache_occupied_buckets_after": 0,
                    "side_cache_corrupt_buckets_after": 0,
                    "side_cache_hot_bucket_active_after": False,
                    "cache_hits_delta": 1,
                    "cache_inserts_delta": 2,
                    "cache_bypasses_delta": 1,
                },
                {
                    "source": "std_bench_auto_metadata",
                    "event": "pac_metadata_auth_probe",
                    "feature_pac": True,
                    "target_arch_aarch64": True,
                    "attempted": True,
                    "pac_probe_uses_allocated_object": True,
                    "backend_observation_consistent": True,
                    "context_binding_active": True,
                    "software_fallback_active": False,
                    "pac_probe_key": "ia",
                    "pac_probe_active_key": "ia",
                    "pac_probe_key_matrix": [
                        {
                            "key": "ia",
                            "available": True,
                            "signed_changed": True,
                            "strip_roundtrip": True,
                            "correct_context_roundtrip": True,
                            "wrong_context_rejected": True,
                        }
                    ],
                    "pac_probe_signed_changed": True,
                    "pac_probe_strip_roundtrip": True,
                    "pac_probe_correct_context_roundtrip": True,
                    "pac_probe_wrong_context_rejected": True,
                    "allocated": True,
                    "reused": True,
                    "cache_hits_delta": 1,
                    "cache_inserts_delta": 2,
                    "cache_bypasses_delta": 1,
                    "pac_signs_delta": 2,
                    "pac_verifications_delta": 1,
                    "pac_failures_delta": 0,
                    "pac_software_fallback_signs_delta": 0,
                    "pac_software_fallback_verifications_delta": 0,
                    "pac_software_fallback_failures_delta": 0,
                },
            ]
            events.write_text(
                "".join(json.dumps(row, sort_keys=True) + "\n" for row in rows),
                encoding="utf-8",
            )
            stats = evaluate.parse_coverage_events_stats(events)

        self.assertTrue(stats["valid"], stats["parse_errors"])
        self.assertEqual(stats["total_allocations"], 5)
        self.assertEqual(stats["typed_allocations"], 3)
        self.assertEqual(
            stats["hugepage_mmap_stats"],
            {
                "attempts": 4,
                "successes": 0,
                "fallbacks": 4,
                "aligned_fallbacks": 3,
                "advised_fallbacks": 1,
                "fallback_failures": 0,
                "event_count": 1,
                "platform_error_stage": "none",
                "platform_error_stages": [],
                "platform_error_code": 0,
                "platform_error_name": "none",
            },
        )
        self.assertEqual(
            stats["hugepage_metadata_side_cache_probe"],
            {
                "event_count": 1,
                "attempted": 1,
                "allocated": 1,
                "reused": 1,
                "cache_hits_delta": 1,
                "cache_inserts_delta": 2,
                "cache_bypasses_delta": 1,
                "mmap_attempts_delta": 1,
                "mmap_successes_delta": 0,
                "mmap_fallbacks_delta": 1,
                "mmap_aligned_fallbacks_delta": 1,
                "mmap_advised_fallbacks_delta": 0,
                "mmap_fallback_failures_delta": 0,
                "preexisting_allocated": 0,
                "preexisting_mapping_backings": ["unallocated"],
                "side_cache_allocated": 0,
                "side_cache_addresses": [],
                "side_cache_mapping_size": 0,
                "side_cache_mapping_sizes": [],
                "mapping_backings": ["ordinary-fallback-aligned"],
                "linux_smaps_attempted": 0,
                "linux_smaps_found": 0,
                "linux_smaps_kernel_page_size_kb": 0,
                "linux_smaps_mmu_page_size_kb": 0,
                "linux_smaps_anon_huge_pages_kb": 0,
                "linux_smaps_vmflags_ht": False,
                "linux_smaps_vmflags_hg": False,
                "linux_smaps_hugepage_backed": False,
                "mmap_platform_error_stages": [],
                "mmap_platform_error_code": 0,
                "mmap_platform_error_name": "none",
                "feature_enabled": 1,
                "hugepage_backed": False,
                "fallback_backed": True,
                "aligned_fallback_backed": True,
            },
        )
        self.assertEqual(
            stats["metadata_segregation_side_cache_probe"],
            {
                "event_count": 1,
                "attempted": 1,
                "allocated": 1,
                "reused": 1,
                "cache_hits_delta": 1,
                "cache_inserts_delta": 2,
                "cache_bypasses_delta": 1,
                "feature_enabled": 1,
                "side_cache_inline_occupied_after": 1,
                "side_cache_occupied_entries_after": 1,
                "side_cache_occupied_buckets_after": 0,
                "side_cache_corrupt_buckets_after": 0,
                "side_cache_hot_bucket_active_after": 0,
            },
        )
        self.assertEqual(
            stats["pac_metadata_auth_probe"],
            {
                "event_count": 1,
                "attempted": 1,
                "context_binding_active": True,
                "software_fallback_active": False,
                "pac_probe_uses_allocated_object": True,
                "backend_observation_consistent": True,
                "pac_probe_key": "ia",
                "pac_probe_active_key": "ia",
                "pac_probe_keys": ["ia"],
                "pac_probe_active_keys": ["ia"],
                "pac_probe_key_matrix": [
                    {
                        "key": "ia",
                        "available": True,
                        "signed_changed": True,
                        "strip_roundtrip": True,
                        "correct_context_roundtrip": True,
                        "wrong_context_rejected": True,
                    }
                ],
                "pac_probe_signed_changed": True,
                "pac_probe_strip_roundtrip": True,
                "pac_probe_correct_context_roundtrip": True,
                "pac_probe_wrong_context_rejected": True,
                "allocated": 1,
                "reused": 1,
                "cache_hits_delta": 1,
                "cache_inserts_delta": 2,
                "cache_bypasses_delta": 1,
                "pac_signs_delta": 2,
                "pac_verifications_delta": 1,
                "pac_failures_delta": 0,
                "pac_software_fallback_signs_delta": 0,
                "pac_software_fallback_verifications_delta": 0,
                "pac_software_fallback_failures_delta": 0,
                "feature_enabled": 1,
            },
        )
        self.assertTrue(
            evaluate.hugepage_metadata_side_cache_validated(
                "bench_ourself,stats,hugepage", stats
            )
        )
        self.assertTrue(
            evaluate.metadata_segregation_side_cache_validated(
                "bench_ourself,stats,metadata_segregation", stats
            )
        )
        self.assertTrue(evaluate.pac_metadata_auth_validated("bench_ourself,stats,pac", stats))
        self.assertFalse(
            evaluate.pac_metadata_auth_software_fallback_validated(
                "bench_ourself,stats,pac", stats
            )
        )
        self.assertIn(
            "hugepage mmap attempts fell back to 2MiB-aligned ordinary mmap",
            evaluate.hugepage_mmap_claim_blockers("bench_ourself,stats,hugepage", stats),
        )

    def test_hugepage_side_cache_validation_accepts_prewarmed_backing(self) -> None:
        with tempfile.TemporaryDirectory() as raw_tmp:
            events = pathlib.Path(raw_tmp) / "events.jsonl"
            rows = [
                {
                    "source": "std_bench_auto_metadata",
                    "event": "typed_allocation_site",
                    "typed": True,
                    "type_id": 17,
                    "callsite": 23,
                    "count": 1,
                    "bytes": 32,
                    "deallocations": 1,
                },
                {
                    "source": "std_bench_auto_metadata",
                    "event": "hugepage_metadata_side_cache_probe",
                    "feature_hugepage": True,
                    "attempted": True,
                    "allocated": True,
                    "reused": True,
                    "preexisting_allocated": True,
                    "preexisting_mapping_backing": "ordinary-fallback-aligned",
                    "mapping_backing": "ordinary-fallback-aligned",
                    "cache_hits_delta": 1,
                    "cache_inserts_delta": 2,
                    "cache_bypasses_delta": 1,
                    "mmap_attempts_delta": 0,
                    "mmap_successes_delta": 0,
                    "mmap_fallbacks_delta": 0,
                    "mmap_aligned_fallbacks_delta": 0,
                    "mmap_advised_fallbacks_delta": 0,
                    "mmap_fallback_failures_delta": 0,
                },
            ]
            events.write_text(
                "".join(json.dumps(row, sort_keys=True) + "\n" for row in rows),
                encoding="utf-8",
            )
            stats = evaluate.parse_coverage_events_stats(events)

        self.assertTrue(stats["valid"], stats["parse_errors"])
        self.assertEqual(
            stats["hugepage_metadata_side_cache_probe"]["preexisting_allocated"],
            1,
        )
        self.assertEqual(
            stats["hugepage_metadata_side_cache_probe"]["preexisting_mapping_backings"],
            ["ordinary-fallback-aligned"],
        )
        self.assertTrue(
            evaluate.hugepage_metadata_side_cache_validated(
                "bench_ourself,stats,hugepage", stats
            )
        )

    def test_feature_probe_validation_requires_event_feature_flag(self) -> None:
        with tempfile.TemporaryDirectory() as raw_tmp:
            events = pathlib.Path(raw_tmp) / "events.jsonl"
            rows = [
                {
                    "source": "std_bench_auto_metadata",
                    "event": "typed_allocation_site",
                    "typed": True,
                    "type_id": 17,
                    "callsite": 23,
                    "count": 1,
                    "bytes": 32,
                    "deallocations": 1,
                },
                {
                    "source": "std_bench_auto_metadata",
                    "event": "metadata_segregation_side_cache_probe",
                    "feature_metadata_segregation": False,
                    "attempted": True,
                    "allocated": True,
                    "reused": True,
                    "cache_hits_delta": 1,
                    "cache_inserts_delta": 2,
                    "cache_bypasses_delta": 1,
                },
                {
                    "source": "std_bench_auto_metadata",
                    "event": "pac_metadata_auth_probe",
                    "feature_pac": False,
                    "attempted": True,
                    "pac_probe_uses_allocated_object": True,
                    "backend_observation_consistent": True,
                    "context_binding_active": False,
                    "software_fallback_active": True,
                    "allocated": True,
                    "reused": True,
                    "cache_hits_delta": 1,
                    "cache_inserts_delta": 2,
                    "cache_bypasses_delta": 1,
                    "pac_signs_delta": 0,
                    "pac_verifications_delta": 0,
                    "pac_failures_delta": 0,
                    "pac_software_fallback_signs_delta": 2,
                    "pac_software_fallback_verifications_delta": 1,
                    "pac_software_fallback_failures_delta": 0,
                },
                {
                    "source": "std_bench_auto_metadata",
                    "event": "hugepage_metadata_side_cache_probe",
                    "feature_hugepage": False,
                    "attempted": True,
                    "allocated": True,
                    "reused": True,
                    "preexisting_allocated": True,
                    "preexisting_mapping_backing": "ordinary-fallback-aligned",
                    "mapping_backing": "ordinary-fallback-aligned",
                    "cache_hits_delta": 1,
                    "cache_inserts_delta": 2,
                    "cache_bypasses_delta": 1,
                    "mmap_attempts_delta": 0,
                    "mmap_successes_delta": 0,
                    "mmap_fallbacks_delta": 0,
                    "mmap_aligned_fallbacks_delta": 0,
                    "mmap_advised_fallbacks_delta": 0,
                    "mmap_fallback_failures_delta": 0,
                },
            ]
            events.write_text(
                "".join(json.dumps(row, sort_keys=True) + "\n" for row in rows),
                encoding="utf-8",
            )
            stats = evaluate.parse_coverage_events_stats(events)

        self.assertTrue(stats["valid"], stats["parse_errors"])
        self.assertEqual(stats["metadata_segregation_side_cache_probe"]["feature_enabled"], 0)
        self.assertFalse(
            evaluate.metadata_segregation_side_cache_validated(
                "bench_ourself,stats,metadata_segregation", stats
            )
        )
        self.assertEqual(stats["pac_metadata_auth_probe"]["feature_enabled"], 0)
        self.assertFalse(
            evaluate.pac_metadata_auth_software_fallback_validated(
                "bench_ourself,stats,pac", stats
            )
        )
        self.assertEqual(stats["hugepage_metadata_side_cache_probe"]["feature_enabled"], 0)
        self.assertFalse(
            evaluate.hugepage_metadata_side_cache_validated(
                "bench_ourself,stats,hugepage", stats
            )
        )

    def test_pac_metadata_auth_software_fallback_is_observable_but_not_claim_grade(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            events = pathlib.Path(tmpdir) / "events.jsonl"
            rows = [
                {
                    "source": "std_bench_auto_metadata",
                    "event": "typed_allocations",
                    "typed": True,
                    "count": 1,
                    "bytes": 64,
                    "deallocations": 1,
                    "metadata_pac_auth_signs": 0,
                    "metadata_pac_auth_verifications": 0,
                    "metadata_pac_auth_failures": 0,
                    "metadata_pac_software_fallback_signs": 1,
                    "metadata_pac_software_fallback_verifications": 1,
                    "metadata_pac_software_fallback_failures": 0,
                },
                {
                    "source": "std_bench_auto_metadata",
                    "event": "pac_metadata_auth_probe",
                    "feature_pac": True,
                    "target_arch_aarch64": True,
                    "attempted": True,
                    "pac_probe_uses_allocated_object": True,
                    "backend_observation_consistent": True,
                    "context_binding_active": False,
                    "software_fallback_active": True,
                    "allocated": True,
                    "reused": True,
                    "cache_hits_delta": 1,
                    "cache_inserts_delta": 2,
                    "cache_bypasses_delta": 0,
                    "pac_signs_delta": 0,
                    "pac_verifications_delta": 0,
                    "pac_failures_delta": 0,
                    "pac_software_fallback_signs_delta": 2,
                    "pac_software_fallback_verifications_delta": 1,
                    "pac_software_fallback_failures_delta": 0,
                },
            ]
            events.write_text(
                "".join(json.dumps(row, sort_keys=True) + "\n" for row in rows),
                encoding="utf-8",
            )
            stats = evaluate.parse_coverage_events_stats(events)

        self.assertTrue(stats["valid"], stats["parse_errors"])
        self.assertFalse(evaluate.pac_metadata_auth_validated("bench_ourself,stats,pac", stats))
        self.assertTrue(
            evaluate.pac_metadata_auth_software_fallback_validated(
                "bench_ourself,stats,pac", stats
            )
        )
        self.assertEqual(
            stats["runtime_policy_stats"]["metadata_pac_software_fallback_signs"], 1
        )

    def claim_grade_direct_pac_audit_fixture(self, run_id: str = "pac-direct-arm64e") -> dict:
        """Claim-grade direct allocator PAC evidence used by C006 gate tests."""

        return {
            "source": "pac-metadata-direct-probe",
            "run_id": run_id,
            "summary": {
                "probe_validated": True,
                "claim_grade": True,
                "allocator_validated": True,
                "full_allocator_side_cache_validated": True,
                "metadata_pac_negative_auth_requires_signal_probe": True,
                "hardware_pac_validated": True,
                "software_fallback_validated": False,
                "context_binding_active": True,
                "software_fallback_active": False,
                "pac_probe_uses_allocated_object": True,
                "backend_observation_consistent": True,
                "pac_probe_active_key": "da",
                "pac_probe_signed_changed": True,
                "pac_probe_wrong_context_rejected": True,
                "typed_cache_inserts": 2,
                "typed_cache_hits": 1,
                "pac_signs": 3,
                "pac_verifications": 2,
                "pac_failures": 0,
                "pac_software_fallback_signs": 0,
                "pac_software_fallback_verifications": 0,
                "pac_software_fallback_failures": 0,
            },
        }

    def arm64e_signal_pac_abi_audit_fixture(self, *, by_signal: bool = True) -> dict:
        """PAC ABI negative-control evidence for destructive arm64e auth failures."""

        return {
            "source": "pac-hardware-abi-probe",
            "run_id": "pac-abi-arm64e-signal",
            "summary": {
                "arm64_raw_pac_noop": True,
                "arm64e_sign_correct_auth": True,
                "arm64e_wrong_context_rejected": by_signal,
                "arm64e_wrong_context_rejected_by_signal": by_signal,
                "hardware_pac_context_binding_observed": True,
                "rust_arm64e_target_listed": True,
                "rust_arm64e_std_installed": False,
                "rust_arm64e_target_available": False,
            },
        }

    def test_claim_check_runtime_gate_accepts_direct_arm64e_pac_over_std_fallback(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            raw = pathlib.Path(tmpdir) / "raw"
            run = raw / "pac-fallback"
            run.mkdir(parents=True)
            write_json(
                run / "std-bench-auto-summary.json",
                {
                    "run_id": "pac-fallback",
                    "features": "bench_ourself,stats,pac",
                    "pac_metadata_auth_validated": False,
                    "pac_metadata_auth_software_fallback_validated": True,
                    "pac_metadata_auth_probe": {
                        "event_count": 1,
                        "context_binding_active": False,
                        "software_fallback_active": True,
                    },
                },
            )
            old_raw = evaluate.RAW
            evaluate.RAW = raw
            try:
                with mock.patch.object(
                    evaluate,
                    "latest_pac_metadata_direct_probe_audit",
                    return_value=(
                        pathlib.Path("pac-metadata-direct-probe-audit.json"),
                        self.claim_grade_direct_pac_audit_fixture(),
                    ),
                ), mock.patch.object(
                    evaluate,
                    "latest_pac_arm64e_build_std_direct_probe_audit",
                    return_value=(None, {}),
                ), mock.patch.object(
                    evaluate,
                    "latest_pac_hardware_abi_probe_audit",
                    return_value=(
                        pathlib.Path("pac-hardware-abi-probe-audit.json"),
                        self.arm64e_signal_pac_abi_audit_fixture(),
                    ),
                ):
                    result = evaluate.evaluate_claim(
                        {
                            "id": "C006-pac-cost",
                            "description": "PAC cost",
                            "metric": "slowdown_percent_vs_baselines",
                            "dataset": "pac_authentication",
                            "threshold": {"between": [1.0, 3.0]},
                        },
                        {
                            "datasets": {
                                "pac_authentication": {
                                    "path": "unit-pac",
                                    "claim_grade": True,
                                    "geomean": 1.0 / 1.02,
                                    "count": 1,
                                    "scope": {},
                                }
                            }
                        },
                        "current",
                    )
            finally:
                evaluate.RAW = old_raw

        self.assertEqual(result["status"], "pass")
        self.assertIn(str(run / "std-bench-auto-summary.json"), result["evidence"])
        self.assertIn("pac-metadata-direct-probe-audit.json", result["evidence"])
        self.assertIn("pac-hardware-abi-probe-audit.json", result["evidence"])
        self.assertTrue(
            any("runtime feature gate satisfied by direct arm64e" in evidence for evidence in result["evidence"]),
            result["evidence"],
        )
        self.assertFalse(
            any("PAC runtime probe used software fallback" in evidence for evidence in result["evidence"]),
            result["evidence"],
        )

    def test_claim_check_runtime_gate_requires_arm64e_signal_negative_for_direct_pac(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            raw = pathlib.Path(tmpdir) / "raw"
            run = raw / "pac-fallback"
            run.mkdir(parents=True)
            write_json(
                run / "std-bench-auto-summary.json",
                {
                    "run_id": "pac-fallback",
                    "features": "bench_ourself,stats,pac",
                    "pac_metadata_auth_validated": False,
                    "pac_metadata_auth_software_fallback_validated": True,
                    "pac_metadata_auth_probe": {
                        "event_count": 1,
                        "context_binding_active": False,
                        "software_fallback_active": True,
                    },
                },
            )
            old_raw = evaluate.RAW
            evaluate.RAW = raw
            try:
                with mock.patch.object(
                    evaluate,
                    "latest_pac_metadata_direct_probe_audit",
                    return_value=(
                        pathlib.Path("pac-metadata-direct-probe-audit.json"),
                        self.claim_grade_direct_pac_audit_fixture(),
                    ),
                ), mock.patch.object(
                    evaluate,
                    "latest_pac_arm64e_build_std_direct_probe_audit",
                    return_value=(None, {}),
                ), mock.patch.object(
                    evaluate,
                    "latest_pac_hardware_abi_probe_audit",
                    return_value=(
                        pathlib.Path("pac-hardware-abi-probe-audit.json"),
                        self.arm64e_signal_pac_abi_audit_fixture(by_signal=False),
                    ),
                ):
                    result = evaluate.evaluate_claim(
                        {
                            "id": "C006-pac-cost",
                            "description": "PAC cost",
                            "metric": "slowdown_percent_vs_baselines",
                            "dataset": "pac_authentication",
                            "threshold": {"between": [1.0, 3.0]},
                        },
                        {
                            "datasets": {
                                "pac_authentication": {
                                    "path": "unit-pac",
                                    "claim_grade": True,
                                    "geomean": 1.0 / 1.02,
                                    "count": 1,
                                    "scope": {},
                                }
                            }
                        },
                        "current",
                    )
            finally:
                evaluate.RAW = old_raw

        self.assertEqual(result["status"], "missing")
        self.assertTrue(
            any("destructive arm64e negative-auth signal evidence" in evidence for evidence in result["evidence"]),
            result["evidence"],
        )

    def test_claim_check_runtime_gate_rejects_pac_software_fallback(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            raw = pathlib.Path(tmpdir) / "raw"
            run = raw / "pac-fallback"
            run.mkdir(parents=True)
            write_json(
                run / "std-bench-auto-summary.json",
                {
                    "run_id": "pac-fallback",
                    "features": "bench_ourself,stats,pac",
                    "pac_metadata_auth_validated": False,
                    "pac_metadata_auth_software_fallback_validated": True,
                    "pac_metadata_auth_probe": {
                        "event_count": 1,
                        "context_binding_active": False,
                        "software_fallback_active": True,
                    },
                },
            )
            old_raw = evaluate.RAW
            evaluate.RAW = raw
            try:
                direct_pac = {
                    "source": "pac-metadata-direct-probe",
                    "run_id": "pac-direct-fallback",
                    "summary": {
                        "probe_validated": True,
                        "claim_grade": False,
                        "allocator_validated": True,
                        "hardware_pac_validated": False,
                        "software_fallback_validated": True,
                        "context_binding_active": False,
                        "software_fallback_active": True,
                        "pac_probe_active_key": "none",
                        "pac_probe_signed_changed": False,
                        "pac_probe_wrong_context_rejected": False,
                        "pac_probe_key_matrix": [
                            {
                                "key": "ib",
                                "available": False,
                                "wrong_context_rejected": False,
                            }
                        ],
                        "typed_cache_inserts": 1,
                        "typed_cache_hits": 1,
                        "pac_signs": 0,
                        "pac_verifications": 0,
                        "pac_failures": 0,
                        "pac_software_fallback_signs": 2,
                        "pac_software_fallback_verifications": 1,
                        "pac_software_fallback_failures": 0,
                    },
                }
                build_std_direct_pac = {
                    "source": "pac-metadata-direct-probe",
                    "run_id": "pac-arm64e-build-std-signal",
                    "summary": {
                        "probe_validated": False,
                        "claim_grade": False,
                        "allocator_validated": False,
                        "hardware_pac_validated": False,
                        "build_std_requested": True,
                        "build_std_crates": "std,panic_abort",
                        "rust_target": "arm64e-apple-darwin",
                        "exit_code": -11,
                        "cargo_target_cleaned": True,
                        "cargo_target_removed_bytes": 4096,
                        "typed_cache_inserts": 0,
                        "typed_cache_hits": 0,
                    },
                }
                with mock.patch.object(
                    evaluate,
                    "latest_pac_metadata_direct_probe_audit",
                    return_value=(
                        pathlib.Path("pac-metadata-direct-probe-audit.json"),
                        direct_pac,
                    ),
                ), mock.patch.object(
                    evaluate,
                    "latest_pac_arm64e_build_std_direct_probe_audit",
                    return_value=(
                        pathlib.Path("pac-arm64e-build-std-direct-probe-audit.json"),
                        build_std_direct_pac,
                    ),
                ), mock.patch.object(
                    evaluate,
                    "latest_pac_hardware_abi_probe_audit",
                    return_value=(None, {}),
                ):
                    result = evaluate.evaluate_claim(
                        {
                            "id": "C006-pac-cost",
                            "description": "PAC cost",
                            "metric": "slowdown_percent_vs_baselines",
                            "dataset": "pac_authentication",
                            "threshold": {"between": [1.0, 3.0]},
                        },
                        {
                            "datasets": {
                                "pac_authentication": {
                                    "path": "unit-pac",
                                    "claim_grade": True,
                                    "geomean": 1.0 / 1.02,
                                    "count": 1,
                                    "scope": {},
                                }
                            }
                        },
                        "current",
                    )
            finally:
                evaluate.RAW = old_raw

        self.assertEqual(result["status"], "missing")
        self.assertIn(str(run / "std-bench-auto-summary.json"), result["evidence"])
        self.assertIn("pac-metadata-direct-probe-audit.json", result["evidence"])
        self.assertIn("pac-arm64e-build-std-direct-probe-audit.json", result["evidence"])
        self.assertTrue(
            any("software fallback" in evidence for evidence in result["evidence"]),
            result["evidence"],
        )
        self.assertTrue(
            any("build-std direct probe" in evidence for evidence in result["evidence"]),
            result["evidence"],
        )
        self.assertTrue(
            any("terminated by signal 11" in evidence for evidence in result["evidence"]),
            result["evidence"],
        )

    def test_rust_arm64e_target_listed_without_std_is_not_buildable(self) -> None:
        self.assertTrue(
            evaluate.rust_target_list_contains_arm64e(
                "aarch64-apple-darwin\narm64e-apple-darwin\nx86_64-apple-darwin\n"
            )
        )
        no_std = evaluate.rustup_arm64e_std_status(
            "aarch64-apple-darwin (installed)\nrust-std-x86_64-apple-darwin (installed)\n"
        )
        self.assertFalse(no_std["available"])
        self.assertFalse(no_std["installed"])
        installed = evaluate.rustup_arm64e_std_status(
            "arm64e-apple-darwin (installed)\naarch64-apple-darwin (installed)\n"
        )
        self.assertTrue(installed["available"])
        self.assertTrue(installed["installed"])

    def test_pac_direct_probe_records_arm64e_missing_std_blocker(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            results = pathlib.Path(tmpdir) / "results"
            with temporary_eval_results_and_raw(results) as raw:

                def fake_platform_run(out_dir, label, cmd, timeout, *, extra_env=None):
                    self.assertEqual(label, "pac-metadata-direct-probe")
                    self.assertIn("--target", cmd)
                    self.assertIn("arm64e-apple-darwin", cmd)
                    target_dir = pathlib.Path((extra_env or {})["CARGO_TARGET_DIR"])
                    target_dir.mkdir(parents=True, exist_ok=True)
                    (target_dir / "partial.o").write_bytes(b"z" * 23)
                    stdout_path = pathlib.Path(out_dir) / f"{label}.stdout.txt"
                    stderr_path = pathlib.Path(out_dir) / f"{label}.stderr.txt"
                    stdout_path.write_text("", encoding="utf-8")
                    stderr_path.write_text(
                        "error[E0463]: can't find crate for `core`\n"
                        "= note: the `arm64e-apple-darwin` target may not be installed\n",
                        encoding="utf-8",
                    )
                    return {
                        "command": cmd,
                        "stdout": str(stdout_path),
                        "stderr": str(stderr_path),
                        "exit_code": 101,
                        "passed": False,
                    }

                args = types.SimpleNamespace(
                    run_id="pac-arm64e-missing-std-unit",
                    output_dir=None,
                    toolchain="nightly",
                    features="pac,stats",
                    target="arm64e-apple-darwin",
                    timeout=60,
                    no_update_results=True,
                )
                with mock.patch.object(evaluate.shutil, "which", return_value="/usr/bin/cargo"), mock.patch.object(
                    evaluate, "run_platform_command", side_effect=fake_platform_run
                ), contextlib.redirect_stdout(io.StringIO()):
                    rc = evaluate.collect_pac_metadata_direct_probe(args)
                audit = json.loads(
                    (raw / "pac-arm64e-missing-std-unit" / "pac-metadata-direct-probe-audit.json").read_text(
                        encoding="utf-8"
                    )
                )

        self.assertEqual(rc, 1)
        self.assertEqual(audit["rust_toolchain"], "nightly")
        self.assertEqual(audit["rust_target"], "arm64e-apple-darwin")
        self.assertTrue(audit["summary"]["cargo_target_cleaned"])
        self.assertEqual(audit["summary"]["cargo_target_removed_bytes"], 23)
        self.assertTrue(
            any("lacks an installed standard library" in blocker for blocker in audit["blockers"]),
            audit["blockers"],
        )

    def test_pac_direct_probe_build_std_records_explicit_build_from_source_attempt(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            results = pathlib.Path(tmpdir) / "results"
            with temporary_eval_results_and_raw(results) as raw:

                def fake_platform_run(out_dir, label, cmd, timeout, *, extra_env=None):
                    self.assertEqual(label, "pac-arm64e-std-runtime-preflight")
                    self.assertIn("-Z", cmd)
                    self.assertIn("build-std=std,panic_abort", cmd)
                    self.assertIn("--target", cmd)
                    self.assertIn("arm64e-apple-darwin", cmd)
                    target_dir = pathlib.Path((extra_env or {})["CARGO_TARGET_DIR"])
                    target_dir.mkdir(parents=True, exist_ok=True)
                    (target_dir / "partial.o").write_bytes(b"z" * 17)
                    stdout_path = pathlib.Path(out_dir) / f"{label}.stdout.txt"
                    stderr_path = pathlib.Path(out_dir) / f"{label}.stderr.txt"
                    stdout_path.write_text("", encoding="utf-8")
                    stderr_path.write_text(
                        "error[E0463]: can't find crate for `core`\n"
                        "= note: the `arm64e-apple-darwin` target may not be installed\n",
                        encoding="utf-8",
                    )
                    return {
                        "command": cmd,
                        "stdout": str(stdout_path),
                        "stderr": str(stderr_path),
                        "exit_code": 101,
                        "passed": False,
                    }

                args = types.SimpleNamespace(
                    run_id="pac-arm64e-build-std-unit",
                    output_dir=None,
                    toolchain="nightly",
                    features="pac,stats",
                    target="arm64e-apple-darwin",
                    build_std=True,
                    build_std_crates="std,panic_abort",
                    timeout=60,
                    no_update_results=True,
                )
                with mock.patch.object(evaluate.shutil, "which", return_value="/usr/bin/cargo"), mock.patch.object(
                    evaluate, "run_platform_command", side_effect=fake_platform_run
                ), contextlib.redirect_stdout(io.StringIO()):
                    rc = evaluate.collect_pac_metadata_direct_probe(args)
                audit = json.loads(
                    (raw / "pac-arm64e-build-std-unit" / "pac-metadata-direct-probe-audit.json").read_text(
                        encoding="utf-8"
                    )
                )

        self.assertEqual(rc, 1)
        self.assertTrue(audit["build_std_requested"])
        self.assertEqual(audit["build_std_crates"], "std,panic_abort")
        self.assertTrue(audit["summary"]["build_std_requested"])
        self.assertFalse(audit["summary"]["arm64e_std_runtime_preflight_passed"])
        self.assertTrue(audit["summary"]["skipped_allocator_probe_due_to_preflight"])
        self.assertEqual(audit["summary"]["cargo_target_removed_bytes"], 17)
        joined = " | ".join(audit["blockers"])
        self.assertIn("even with -Zbuild-std=std,panic_abort", joined)
        self.assertNotIn("without build-std evidence", joined)

    def test_pac_direct_probe_nostd_runtime_is_non_claim_grade_without_side_cache(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            results = pathlib.Path(tmpdir) / "results"
            with temporary_eval_results_and_raw(results) as raw:

                def fake_platform_run(out_dir, label, cmd, timeout, *, extra_env=None):
                    self.assertEqual(label, "pac-metadata-direct-probe")
                    self.assertIn("pac_metadata_probe_nostd", cmd)
                    self.assertEqual(cmd[cmd.index("--example") + 1], "pac_metadata_probe_nostd")
                    target_dir = pathlib.Path((extra_env or {})["CARGO_TARGET_DIR"])
                    target_dir.mkdir(parents=True, exist_ok=True)
                    (target_dir / "partial.o").write_bytes(b"z" * 19)
                    stdout_path = pathlib.Path(out_dir) / f"{label}.stdout.txt"
                    stderr_path = pathlib.Path(out_dir) / f"{label}.stderr.txt"
                    stdout_path.write_text(
                        json.dumps(
                            {
                                "source": "pac_metadata_probe",
                                "runtime": "nostd",
                                "passed": True,
                                "claim_grade": False,
                                "allocator_validated": True,
                                "hardware_pac_validated": True,
                                "software_fallback_validated": False,
                                "context_binding_active": True,
                                "software_fallback_active": False,
                                "pac_probe_uses_allocated_object": True,
                                "backend_observation_consistent": True,
                                "full_allocator_side_cache_validated": False,
                                "metadata_pac_negative_auth_requires_signal_probe": True,
                                "pac_probe_active_key": "da",
                                "pac_probe_signed_changed": True,
                                "pac_probe_wrong_context_rejected": True,
                                "typed_cache_inserts": 0,
                                "typed_cache_hits": 0,
                                "pac_signs": 1,
                                "pac_verifications": 3,
                                "pac_failures": 0,
                            },
                            sort_keys=True,
                        )
                        + "\n",
                        encoding="utf-8",
                    )
                    stderr_path.write_text("", encoding="utf-8")
                    return {
                        "command": cmd,
                        "stdout": str(stdout_path),
                        "stderr": str(stderr_path),
                        "exit_code": 0,
                        "passed": True,
                    }

                args = types.SimpleNamespace(
                    run_id="pac-arm64e-nostd-unit",
                    output_dir=None,
                    toolchain="nightly",
                    features="pac,stats",
                    target="arm64e-apple-darwin",
                    runtime="nostd",
                    build_std=True,
                    build_std_crates="std,panic_abort",
                    timeout=60,
                    no_update_results=True,
                )
                with mock.patch.object(evaluate.shutil, "which", return_value="/usr/bin/cargo"), mock.patch.object(
                    evaluate, "run_platform_command", side_effect=fake_platform_run
                ), contextlib.redirect_stdout(io.StringIO()):
                    rc = evaluate.collect_pac_metadata_direct_probe(args)
                audit = json.loads(
                    (raw / "pac-arm64e-nostd-unit" / "pac-metadata-direct-probe-audit.json").read_text(
                        encoding="utf-8"
                    )
                )

        self.assertEqual(rc, 0)
        self.assertEqual(audit["runtime"], "nostd")
        self.assertEqual(audit["summary"]["runtime"], "nostd")
        self.assertTrue(audit["summary"]["probe_validated"])
        self.assertTrue(audit["summary"]["hardware_pac_validated"])
        self.assertFalse(audit["summary"]["claim_grade"])
        self.assertFalse(audit["summary"]["full_allocator_side_cache_validated"])
        joined = " | ".join(audit["blockers"])
        self.assertIn("did not validate the full allocator type-cache side-cache", joined)
        self.assertIn("observed no typed-cache inserts", joined)
        self.assertEqual(audit["summary"]["cargo_target_removed_bytes"], 19)

    def test_pac_direct_probe_signal_exit_is_explicit_blocker(self) -> None:
        blockers = evaluate.pac_metadata_direct_probe_blockers(
            {
                "source": "pac-metadata-direct-probe",
                "summary": {
                    "probe_validated": False,
                    "allocator_validated": False,
                    "typed_cache_inserts": 0,
                    "typed_cache_hits": 0,
                    "hardware_pac_validated": False,
                    "exit_code": -11,
                },
            }
        )
        self.assertTrue(
            any("terminated by signal 11" in blocker for blocker in blockers),
            blockers,
        )

    def test_latest_pac_direct_probe_prefers_hardware_claim_grade_over_newer_fallback(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = pathlib.Path(tmpdir)
            results = root / "results"
            raw = root / "raw"
            hardware_path = results / "pac_metadata_direct_probe_audit.json"
            fallback_path = raw / "software-fallback" / "pac-metadata-direct-probe-audit.json"
            write_json(
                hardware_path,
                {
                    "source": "pac-metadata-direct-probe",
                    "run_id": "hardware-pac-claim-grade",
                    "summary": {
                        "probe_validated": True,
                        "claim_grade": True,
                        "allocator_validated": True,
                        "hardware_pac_validated": True,
                        "software_fallback_validated": False,
                        "context_binding_active": True,
                        "software_fallback_active": False,
                        "pac_probe_active_key": "ib",
                        "pac_probe_signed_changed": True,
                        "pac_probe_wrong_context_rejected": True,
                        "typed_cache_inserts": 2,
                        "typed_cache_hits": 1,
                        "pac_signs": 2,
                        "pac_verifications": 1,
                        "pac_failures": 0,
                        "pac_software_fallback_signs": 0,
                        "pac_software_fallback_verifications": 0,
                        "pac_software_fallback_failures": 0,
                    },
                },
            )
            write_json(
                fallback_path,
                {
                    "source": "pac-metadata-direct-probe",
                    "run_id": "newer-software-fallback",
                    "summary": {
                        "probe_validated": True,
                        "claim_grade": False,
                        "allocator_validated": True,
                        "hardware_pac_validated": False,
                        "software_fallback_validated": True,
                        "context_binding_active": False,
                        "software_fallback_active": True,
                        "pac_probe_active_key": "none",
                        "pac_probe_signed_changed": False,
                        "pac_probe_wrong_context_rejected": False,
                        "typed_cache_inserts": 2,
                        "typed_cache_hits": 1,
                        "pac_signs": 0,
                        "pac_verifications": 0,
                        "pac_failures": 0,
                        "pac_software_fallback_signs": 2,
                        "pac_software_fallback_verifications": 1,
                        "pac_software_fallback_failures": 0,
                    },
                },
            )
            future = time.time() + 60
            os.utime(fallback_path, (future, future))
            old_results, old_raw = evaluate.RESULTS, evaluate.RAW
            evaluate.RESULTS, evaluate.RAW = results, raw
            try:
                selected_path, selected_audit = evaluate.latest_pac_metadata_direct_probe_audit()
            finally:
                evaluate.RESULTS, evaluate.RAW = old_results, old_raw

        self.assertEqual(selected_path, hardware_path)
        self.assertEqual(selected_audit.get("run_id"), "hardware-pac-claim-grade")

    def test_claim_check_runtime_gate_accepts_metadata_segregation_probe(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            raw = pathlib.Path(tmpdir) / "raw"
            run = raw / "metadata-probe"
            run.mkdir(parents=True)
            write_json(
                run / "std-bench-auto-summary.json",
                {
                    "run_id": "metadata-probe",
                    "features": "bench_ourself,stats,metadata_segregation",
                    "metadata_segregation_side_cache_validated": True,
                    "metadata_segregation_side_cache_probe": {"event_count": 1},
                },
            )
            old_raw = evaluate.RAW
            evaluate.RAW = raw
            try:
                result = evaluate.evaluate_claim(
                    {
                        "id": "C004-metadata-segregation-speedup",
                        "description": "metadata speedup",
                        "metric": "geomean_speedup_percent_vs_baselines",
                        "dataset": "metadata_segregation",
                        "threshold": {"gte": 4.0},
                    },
                    {
                        "datasets": {
                            "metadata_segregation": {
                                "path": "unit-metadata",
                                "claim_grade": True,
                                "speedup_percent_vs_baselines": 5.0,
                                "count": 1,
                                "scope": {},
                            }
                        }
                    },
                    "current",
                )
            finally:
                evaluate.RAW = old_raw

        self.assertEqual(result["status"], "pass", result["evidence"])
        self.assertIn(str(run / "std-bench-auto-summary.json"), result["evidence"])
        self.assertFalse(
            any("claim-grade blocker" in evidence for evidence in result["evidence"]),
            result["evidence"],
        )

    def test_hugepage_direct_probe_summary_separates_request_and_compact_mapping_alignment(self) -> None:
        event = {
            "passed": True,
            "claim_grade": False,
            "hugepage_backed": False,
            "side_cache_allocated": True,
            "backing": "ordinary-fallback",
            "mmap_attempts": 5,
            "mmap_successes": 0,
            "mmap_fallbacks": 5,
            "mmap_aligned_fallbacks": 5,
            "mmap_advised_fallbacks": 0,
            "mmap_fallback_failures": 0,
            "last_error_stage": "macos-superpage-unsupported",
            "last_error_code": 4,
            "last_error_name": "KERN_INVALID_ARGUMENT",
            "reused_same_ptr": True,
            "typed_cache_inserts": 4,
            "typed_cache_hits": 2,
            # Compact fallback remaps the final metadata side-cache below 2MiB
            # to reduce memory footprint.  The hugepage-sized request still used
            # the aligned fallback path, so these two fields must stay separate.
            "side_cache_mapping_size": 49152,
            "fallback_alignment": 2097152,
            "aligned_fallback_supported": True,
            "side_cache_mapping_aligned_fallback_supported": False,
        }

        summary = evaluate.hugepage_metadata_direct_probe_summary(
            event, {"passed": True, "exit_code": 0}
        )

        self.assertTrue(summary["probe_validated"])
        self.assertEqual(summary["side_cache_mapping_size"], 49152)
        self.assertTrue(summary["aligned_fallback_supported"])
        self.assertFalse(summary["side_cache_mapping_aligned_fallback_supported"])
        blockers = evaluate.hugepage_metadata_direct_probe_blockers(
            {"source": evaluate.HUGEPAGE_METADATA_DIRECT_PROBE_SOURCE, "summary": summary}
        )
        self.assertEqual(len(blockers), 1)
        self.assertIn("did not observe real hugepage backing", blockers[0])

    def test_claim_check_runtime_gate_requires_hugepage_backing_success(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            raw = pathlib.Path(tmpdir) / "raw"
            run = raw / "hugepage-fallback"
            run.mkdir(parents=True)
            write_json(
                run / "std-bench-auto-summary.json",
                {
                    "run_id": "hugepage-fallback",
                    "features": "bench_ourself,stats,hugepage",
                    "hugepage_metadata_side_cache_validated": True,
                    "hugepage_acceleration_observed": False,
                    "hugepage_metadata_side_cache_probe": {
                        "event_count": 1,
                        "mmap_successes_delta": 0,
                        "mmap_fallbacks_delta": 1,
                    },
                },
            )
            old_raw = evaluate.RAW
            evaluate.RAW = raw
            try:
                with mock.patch.object(
                    evaluate,
                    "latest_hugepage_metadata_direct_probe_audit",
                    return_value=(None, {}),
                ):
                    result = evaluate.evaluate_claim(
                        {
                            "id": "C005-hugepage-metadata-speedup",
                            "description": "hugepage speedup",
                            "metric": "geomean_speedup_percent_vs_baselines",
                            "dataset": "hugepage_metadata",
                            "threshold": {"between": [2.0, 4.0]},
                        },
                        {
                            "datasets": {
                                "hugepage_metadata": {
                                    "path": "unit-hugepage",
                                    "claim_grade": True,
                                    "speedup_percent_vs_baselines": 3.0,
                                    "count": 1,
                                    "scope": {},
                                }
                            }
                        },
                        "current",
                    )
            finally:
                evaluate.RAW = old_raw

        self.assertEqual(result["status"], "missing")
        self.assertTrue(
            any("successful hugepage mmap backing" in evidence for evidence in result["evidence"]),
            result["evidence"],
        )

    def test_claim_check_hugepage_direct_probe_can_supersede_stale_fallback_probe(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            raw = pathlib.Path(tmpdir) / "raw"
            run = raw / "hugepage-fallback"
            run.mkdir(parents=True)
            write_json(
                run / "std-bench-auto-summary.json",
                {
                    "run_id": "hugepage-fallback",
                    "features": "bench_ourself,stats,hugepage",
                    "hugepage_metadata_side_cache_validated": True,
                    "hugepage_acceleration_observed": False,
                    "hugepage_metadata_side_cache_probe": {
                        "event_count": 1,
                        "mmap_successes_delta": 0,
                        "mmap_fallbacks_delta": 1,
                    },
                    "hugepage_claim_grade_blockers": [
                        "hugepage mmap attempts fell back to 2MiB-aligned ordinary mmap"
                    ],
                },
            )
            direct_path = raw / "hugepage-direct" / "hugepage-metadata-direct-probe-audit.json"
            direct_audit = {
                "source": "hugepage-metadata-direct-probe",
                "run_id": "hugepage-direct",
                "summary": {
                    "probe_validated": True,
                    "claim_grade": True,
                    "hugepage_backed": True,
                    "side_cache_allocated": True,
                    "backing": "ordinary-fallback-aligned-with-hugepage-advice",
                    "mmap_attempts": 1,
                    "mmap_successes": 0,
                    "mmap_fallbacks": 1,
                    "mmap_aligned_fallbacks": 1,
                    "mmap_advised_fallbacks": 1,
                    "last_error_stage": "linux-hugetlb-mmap",
                    "last_error_code": 22,
                    "last_error_name": "EINVAL",
                    "linux_smaps_mapping_found": True,
                    "linux_smaps_kernel_page_kb": 4,
                    "linux_smaps_mmu_page_kb": 4,
                    "linux_smaps_anon_huge_kb": 4096,
                    "linux_smaps_thp_eligible": True,
                    "linux_smaps_vmflags_ht": False,
                    "linux_smaps_vmflags_hg": True,
                    "linux_smaps_vmflags_nh": False,
                    "linux_prefaulted_pages": 512,
                    "typed_cache_inserts": 2,
                    "typed_cache_hits": 1,
                    "exit_code": 0,
                },
            }
            old_raw = evaluate.RAW
            evaluate.RAW = raw
            try:
                with mock.patch.object(
                    evaluate,
                    "latest_hugepage_metadata_direct_probe_audit",
                    return_value=(direct_path, direct_audit),
                ):
                    result = evaluate.evaluate_claim(
                        {
                            "id": "C005-hugepage-metadata-speedup",
                            "description": "hugepage speedup",
                            "metric": "geomean_speedup_percent_vs_baselines",
                            "dataset": "hugepage_metadata",
                            "threshold": {"between": [2.0, 4.0]},
                        },
                        {
                            "datasets": {
                                "hugepage_metadata": {
                                    "path": "unit-hugepage",
                                    "claim_grade": True,
                                    "speedup_percent_vs_baselines": 3.0,
                                    "count": 1,
                                    "scope": {},
                                }
                            }
                        },
                        "current",
                    )
            finally:
                evaluate.RAW = old_raw

        self.assertEqual(result["status"], "pass", result["evidence"])
        self.assertTrue(
            any("direct hugepage metadata allocator/PAL probe" in evidence for evidence in result["evidence"]),
            result["evidence"],
        )
        self.assertFalse(
            any("hugepage mmap attempts fell back" in evidence for evidence in result["evidence"]),
            result["evidence"],
        )

    def test_latest_hugepage_direct_probe_prefers_claim_grade_over_newer_fallback(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = pathlib.Path(tmpdir)
            results = root / "results"
            raw = root / "raw"
            claim_grade_path = results / "hugepage_metadata_direct_probe_audit.json"
            fallback_path = raw / "macos-fallback" / "hugepage-metadata-direct-probe-audit.json"
            write_json(
                claim_grade_path,
                {
                    "source": "hugepage-metadata-direct-probe",
                    "run_id": "linux-thp-claim-grade",
                    "summary": {
                        "probe_validated": True,
                        "claim_grade": True,
                        "hugepage_backed": True,
                        "side_cache_allocated": True,
                        "backing": "ordinary-fallback-aligned-with-hugepage-advice",
                        "mmap_attempts": 5,
                        "mmap_successes": 0,
                        "mmap_fallbacks": 5,
                        "mmap_aligned_fallbacks": 5,
                        "mmap_advised_fallbacks": 5,
                        "typed_cache_inserts": 2,
                        "typed_cache_hits": 1,
                        "linux_smaps_mapping_found": True,
                        "linux_smaps_kernel_page_kb": 4,
                        "linux_smaps_mmu_page_kb": 4,
                        "linux_smaps_anon_huge_kb": 4096,
                        "linux_smaps_thp_eligible": True,
                        "linux_smaps_vmflags_ht": False,
                        "linux_smaps_vmflags_hg": True,
                        "linux_smaps_vmflags_nh": False,
                        "linux_prefaulted_pages": 512,
                    },
                },
            )
            write_json(
                fallback_path,
                {
                    "source": "hugepage-metadata-direct-probe",
                    "run_id": "newer-macos-fallback",
                    "summary": {
                        "probe_validated": True,
                        "claim_grade": False,
                        "hugepage_backed": False,
                        "side_cache_allocated": True,
                        "backing": "ordinary-fallback-aligned",
                        "mmap_attempts": 5,
                        "mmap_successes": 0,
                        "mmap_fallbacks": 5,
                        "mmap_aligned_fallbacks": 5,
                        "typed_cache_inserts": 2,
                        "typed_cache_hits": 1,
                        "linux_smaps_mapping_found": False,
                        "linux_smaps_kernel_page_kb": 0,
                        "linux_smaps_mmu_page_kb": 0,
                        "linux_smaps_anon_huge_kb": 0,
                    },
                },
            )
            future = time.time() + 60
            os.utime(fallback_path, (future, future))
            old_results, old_raw = evaluate.RESULTS, evaluate.RAW
            evaluate.RESULTS, evaluate.RAW = results, raw
            try:
                selected_path, selected_audit = evaluate.latest_hugepage_metadata_direct_probe_audit()
            finally:
                evaluate.RESULTS, evaluate.RAW = old_results, old_raw

        self.assertEqual(selected_path, claim_grade_path)
        self.assertEqual(selected_audit.get("run_id"), "linux-thp-claim-grade")

    def test_hugepage_mmap_claim_blockers_accept_real_success(self) -> None:
        stats = {
            "hugepage_mmap_stats": {
                "attempts": 3,
                "successes": 1,
                "fallbacks": 2,
                "aligned_fallbacks": 2,
                "advised_fallbacks": 0,
                "fallback_failures": 0,
                "event_count": 1,
            }
        }
        self.assertEqual(
            evaluate.hugepage_mmap_claim_blockers("bench_ourself,stats,hugepage", stats),
            [],
        )

    def test_mir_semantic_scope_runtime_shard_aggregate_detects_full_surface_union(self) -> None:
        old_surface = evaluate.latest_complete_std_bench_surface
        evaluate.latest_complete_std_bench_surface = lambda: {
            "status": "present",
            "benchmarks": ["bench_a", "bench_b"],
            "source_summary": "unit-surface",
            "run_id": "unit",
            "bench_count": 2,
            "blockers": [],
        }
        try:
            with tempfile.TemporaryDirectory() as raw_tmp:
                tmp = pathlib.Path(raw_tmp)
                audit_a = write_mir_semantic_scope_runtime_audit_fixture(
                    tmp,
                    run_id="shard-a",
                    benchmark="bench_a",
                )
                audit_b = write_mir_semantic_scope_runtime_audit_fixture(
                    tmp,
                    run_id="shard-b",
                    benchmark="bench_b",
                )
                aggregate = evaluate.aggregate_rustc_driver_mir_semantic_scope_runtime_audits(
                    [audit_a, audit_b],
                    out_dir=tmp,
                    run_id="aggregate",
                )
        finally:
            evaluate.latest_complete_std_bench_surface = old_surface
        summary = aggregate["summary"]
        self.assertTrue(summary["runtime_smoke_validated"], summary["blockers"])
        self.assertTrue(summary["runtime_full_surface_candidate_validated"], summary["blockers"])
        self.assertEqual(summary["selected_benchmark_count"], 2)
        self.assertEqual(summary["canonical_missing_benchmark_count"], 0)

    def test_mir_semantic_scope_runtime_shard_aggregate_keeps_incomplete_union_non_claim(self) -> None:
        old_surface = evaluate.latest_complete_std_bench_surface
        evaluate.latest_complete_std_bench_surface = lambda: {
            "status": "present",
            "benchmarks": ["bench_a", "bench_b"],
            "source_summary": "unit-surface",
            "run_id": "unit",
            "bench_count": 2,
            "blockers": [],
        }
        try:
            with tempfile.TemporaryDirectory() as raw_tmp:
                tmp = pathlib.Path(raw_tmp)
                audit_a = write_mir_semantic_scope_runtime_audit_fixture(
                    tmp,
                    run_id="shard-a",
                    benchmark="bench_a",
                )
                aggregate = evaluate.aggregate_rustc_driver_mir_semantic_scope_runtime_audits(
                    [audit_a],
                    out_dir=tmp,
                    run_id="aggregate",
                )
        finally:
            evaluate.latest_complete_std_bench_surface = old_surface
        summary = aggregate["summary"]
        self.assertTrue(summary["runtime_smoke_validated"], summary["blockers"])
        self.assertFalse(summary["runtime_full_surface_candidate_validated"])
        self.assertEqual(summary["canonical_missing_benchmark_count"], 1)
        self.assertIn("complete canonical std_bench surface", " | ".join(summary["blockers"]))

    def test_mir_semantic_scope_runtime_shard_aggregate_rejects_tampered_artifacts(self) -> None:
        old_surface = evaluate.latest_complete_std_bench_surface
        evaluate.latest_complete_std_bench_surface = lambda: {
            "status": "present",
            "benchmarks": ["bench_a", "bench_b"],
            "source_summary": "unit-surface",
            "run_id": "unit",
            "bench_count": 2,
            "blockers": [],
        }
        try:
            for artifact_key in ("runtime_events", "type_mapping"):
                with self.subTest(artifact_key=artifact_key):
                    with tempfile.TemporaryDirectory() as raw_tmp:
                        tmp = pathlib.Path(raw_tmp)
                        source_fingerprint = evaluate.repository_source_fingerprint()
                        audit_a = write_mir_semantic_scope_runtime_audit_fixture(
                            tmp,
                            run_id=f"shard-a-{artifact_key}",
                            benchmark="bench_a",
                            source_fingerprint=source_fingerprint,
                        )
                        audit_b = write_mir_semantic_scope_runtime_audit_fixture(
                            tmp,
                            run_id=f"shard-b-{artifact_key}",
                            benchmark="bench_b",
                            source_fingerprint=source_fingerprint,
                        )
                        tamper_mir_semantic_scope_runtime_artifact(audit_a, artifact_key)
                        aggregate = evaluate.aggregate_rustc_driver_mir_semantic_scope_runtime_audits(
                            [audit_a, audit_b],
                            out_dir=tmp,
                            run_id=f"aggregate-{artifact_key}",
                        )

                    summary = aggregate["summary"]
                    self.assertFalse(summary["runtime_smoke_validated"])
                    self.assertFalse(summary["runtime_full_surface_candidate_validated"])
                    self.assertIn(
                        f"artifacts.{artifact_key} sha256 mismatch",
                        " | ".join(summary["blockers"]),
                    )
        finally:
            evaluate.latest_complete_std_bench_surface = old_surface

    def test_mir_semantic_scope_runtime_shard_aggregate_enforces_source_fingerprint_identity(self) -> None:
        old_surface = evaluate.latest_complete_std_bench_surface
        evaluate.latest_complete_std_bench_surface = lambda: {
            "status": "present",
            "benchmarks": ["bench_a", "bench_b"],
            "source_summary": "unit-surface",
            "run_id": "unit",
            "bench_count": 2,
            "blockers": [],
        }
        try:
            current = evaluate.repository_source_fingerprint()
            stale = copy.deepcopy(current)
            digest = str(current["source_digest"])
            stale["source_digest"] = ("0" if digest[0] != "0" else "1") + digest[1:]
            metadata_variant = copy.deepcopy(current)
            metadata_variant["head_commit"] = "f" * 40
            metadata_variant["working_tree_dirty"] = not bool(current.get("working_tree_dirty"))
            metadata_variant["working_tree_status_line_count"] = 999999

            cases = (
                (
                    "metadata-only-difference",
                    current,
                    metadata_variant,
                    True,
                    None,
                ),
                (
                    "mixed-source-digest",
                    current,
                    stale,
                    False,
                    "runtime shard audits do not share one explicit repository source fingerprint",
                ),
                (
                    "shared-stale-source-digest",
                    stale,
                    stale,
                    False,
                    "runtime shard source binding: artifact repository source digest does not match the current working tree",
                ),
            )
            for case_name, fingerprint_a, fingerprint_b, expected_valid, expected_blocker in cases:
                with self.subTest(case=case_name):
                    with tempfile.TemporaryDirectory() as raw_tmp:
                        tmp = pathlib.Path(raw_tmp)
                        audit_a = write_mir_semantic_scope_runtime_audit_fixture(
                            tmp,
                            run_id=f"shard-a-{case_name}",
                            benchmark="bench_a",
                            source_fingerprint=fingerprint_a,
                        )
                        audit_b = write_mir_semantic_scope_runtime_audit_fixture(
                            tmp,
                            run_id=f"shard-b-{case_name}",
                            benchmark="bench_b",
                            source_fingerprint=fingerprint_b,
                        )
                        aggregate = evaluate.aggregate_rustc_driver_mir_semantic_scope_runtime_audits(
                            [audit_a, audit_b],
                            out_dir=tmp,
                            run_id=f"aggregate-{case_name}",
                        )

                    summary = aggregate["summary"]
                    self.assertEqual(
                        summary["runtime_smoke_validated"],
                        expected_valid,
                        summary["blockers"],
                    )
                    self.assertEqual(
                        summary["runtime_full_surface_candidate_validated"],
                        expected_valid,
                        summary["blockers"],
                    )
                    if expected_blocker is None:
                        self.assertEqual(
                            aggregate["evidence_source_fingerprint"]["source_digest"],
                            current["source_digest"],
                        )
                    else:
                        self.assertIn(expected_blocker, " | ".join(summary["blockers"]))
        finally:
            evaluate.latest_complete_std_bench_surface = old_surface

    def test_mir_semantic_scope_runtime_shard_merge_can_read_compatible_plan(self) -> None:
        old_surface = evaluate.latest_complete_std_bench_surface
        evaluate.latest_complete_std_bench_surface = lambda: {
            "status": "present",
            "benchmarks": ["bench_a", "bench_b"],
            "source_summary": "unit-surface",
            "run_id": "unit",
            "bench_count": 2,
            "blockers": [],
        }
        try:
            with tempfile.TemporaryDirectory() as raw_tmp:
                tmp = pathlib.Path(raw_tmp)
                audit_a = write_mir_semantic_scope_runtime_audit_fixture(
                    tmp,
                    run_id="shard-a",
                    benchmark="bench_a",
                )
                audit_b = write_mir_semantic_scope_runtime_audit_fixture(
                    tmp,
                    run_id="shard-b",
                    benchmark="bench_b",
                )
                plan = tmp / "plan.json"
                write_json(
                    plan,
                    {
                        "schema_version": 1,
                        "compatible_runtime_audits": [
                            {"path": str(audit_a)},
                            {"path": str(audit_b)},
                        ],
                    },
                )
                output_dir = tmp / "aggregate-from-plan"
                args = type(
                    "Args",
                    (),
                    {
                        "runtime_smoke_audit_paths": [],
                        "runtime_smoke_audits": [],
                        "plan_json_paths": [str(plan)],
                        "run_id": "aggregate-from-plan",
                        "output_dir": str(output_dir),
                        "no_update_results": True,
                    },
                )()
                with contextlib.redirect_stdout(io.StringIO()):
                    rc = evaluate.merge_rustc_driver_mir_semantic_scope_std_bench_runtime_shards(args)
                aggregate = json.loads(
                    (output_dir / "rustc-driver-mir-semantic-scope-std-bench-runtime-aggregate-audit.json").read_text(
                        encoding="utf-8"
                    )
                )
        finally:
            evaluate.latest_complete_std_bench_surface = old_surface
        self.assertEqual(rc, 0)
        self.assertEqual(aggregate["summary"]["selected_benchmark_count"], 2)
        self.assertTrue(aggregate["summary"]["runtime_full_surface_candidate_validated"])

    def test_mir_semantic_scope_runtime_shard_merge_skips_results_update_when_not_claim_ready(self) -> None:
        old_surface = evaluate.latest_complete_std_bench_surface
        evaluate.latest_complete_std_bench_surface = lambda: {
            "status": "present",
            "benchmarks": ["bench_a", "bench_b"],
            "source_summary": "unit-surface",
            "run_id": "unit",
            "bench_count": 2,
            "blockers": [],
        }
        try:
            with tempfile.TemporaryDirectory() as raw_tmp:
                tmp = pathlib.Path(raw_tmp)
                results = tmp / "results"
                results.mkdir()
                with temporary_eval_results_and_raw(results):
                    write_ready_mir_probe_companions(results)
                    audit_a = write_mir_semantic_scope_runtime_audit_fixture(
                        tmp,
                        run_id="shard-a",
                        benchmark="bench_a",
                    )
                    output_dir = tmp / "aggregate-incomplete"
                    args = type(
                        "Args",
                        (),
                        {
                            "runtime_smoke_audit_paths": [str(audit_a)],
                            "runtime_smoke_audits": [],
                            "plan_json_paths": [],
                            "run_id": "aggregate-incomplete",
                            "output_dir": str(output_dir),
                            "no_update_results": False,
                        },
                    )()
                    stdout = io.StringIO()
                    with contextlib.redirect_stdout(stdout):
                        rc = evaluate.merge_rustc_driver_mir_semantic_scope_std_bench_runtime_shards(args)
                    payload = json.loads(stdout.getvalue())
                    results_audit = results / "rustc_driver_mir_semantic_scope_std_bench_runtime_smoke_audit.json"
        finally:
            evaluate.latest_complete_std_bench_surface = old_surface
        self.assertEqual(rc, 0)
        self.assertFalse(results_audit.exists())
        self.assertIsNone(payload["results_audit"])
        self.assertIn("aggregate is not claim-ready", payload["results_update_skipped_reason"])

    def test_mir_semantic_scope_runtime_shard_merge_blocks_missing_or_wrong_compiler_pass(self) -> None:
        old_surface = evaluate.latest_complete_std_bench_surface
        evaluate.latest_complete_std_bench_surface = lambda: {
            "status": "present",
            "benchmarks": ["bench_a", "bench_b"],
            "source_summary": "unit-surface",
            "run_id": "unit",
            "bench_count": 2,
            "blockers": [],
        }
        try:
            cases = {
                "missing": None,
                "wrong_kind": {"kind": "rustc_mir_dry_run", "query_overridden": "optimized_mir"},
                "wrong_query": {
                    "kind": "rustc_driver_optimized_mir_provider_override",
                    "query_overridden": "mir_built",
                },
            }
            for name, compiler_pass in cases.items():
                with self.subTest(name=name):
                    with tempfile.TemporaryDirectory() as raw_tmp:
                        tmp = pathlib.Path(raw_tmp)
                        results = tmp / "results"
                        results.mkdir()
                        with temporary_eval_results_and_raw(results):
                            write_ready_mir_probe_companions(results)
                            audit_a = write_mir_semantic_scope_runtime_audit_fixture(
                                tmp,
                                run_id=f"shard-a-{name}",
                                benchmark="bench_a",
                            )
                            audit_b = write_mir_semantic_scope_runtime_audit_fixture(
                                tmp,
                                run_id=f"shard-b-{name}",
                                benchmark="bench_b",
                            )
                            bad_audit = json.loads(audit_a.read_text(encoding="utf-8"))
                            if compiler_pass is None:
                                bad_audit["target_rewrite"].pop("compiler_pass", None)
                            else:
                                bad_audit["target_rewrite"]["compiler_pass"] = compiler_pass
                            write_json(audit_a, bad_audit)
                            output_dir = tmp / f"aggregate-{name}"
                            args = type(
                                "Args",
                                (),
                                {
                                    "runtime_smoke_audit_paths": [str(audit_a), str(audit_b)],
                                    "runtime_smoke_audits": [],
                                    "plan_json_paths": [],
                                    "run_id": f"aggregate-{name}",
                                    "output_dir": str(output_dir),
                                    "no_update_results": False,
                                },
                            )()
                            stdout = io.StringIO()
                            with contextlib.redirect_stdout(stdout):
                                rc = evaluate.merge_rustc_driver_mir_semantic_scope_std_bench_runtime_shards(args)
                            payload = json.loads(stdout.getvalue())
                            results_audit = results / "rustc_driver_mir_semantic_scope_std_bench_runtime_smoke_audit.json"
                    self.assertEqual(rc, 1)
                    self.assertFalse(results_audit.exists())
                    self.assertIsNone(payload["results_audit"])
                    self.assertIn("aggregate is not claim-ready", payload["results_update_skipped_reason"])
                    self.assertIn("compiler_pass", " | ".join(payload["claim_update_gate"]["blockers"]))
        finally:
            evaluate.latest_complete_std_bench_surface = old_surface

    def test_mir_semantic_scope_runtime_shard_merge_blocks_missing_direct_claim_modes(self) -> None:
        old_surface = evaluate.latest_complete_std_bench_surface
        evaluate.latest_complete_std_bench_surface = lambda: {
            "status": "present",
            "benchmarks": ["bench_a", "bench_b"],
            "source_summary": "unit-surface",
            "run_id": "unit",
            "bench_count": 2,
            "blockers": [],
        }
        try:
            with tempfile.TemporaryDirectory() as raw_tmp:
                tmp = pathlib.Path(raw_tmp)
                results = tmp / "results"
                results.mkdir()
                with temporary_eval_results_and_raw(results):
                    write_ready_mir_probe_companions(results)
                    audit_a = write_mir_semantic_scope_runtime_audit_fixture(
                        tmp,
                        run_id="shard-a-direct-missing",
                        benchmark="bench_a",
                    )
                    audit_b = write_mir_semantic_scope_runtime_audit_fixture(
                        tmp,
                        run_id="shard-b-direct-missing",
                        benchmark="bench_b",
                    )
                    bad_audit = json.loads(audit_a.read_text(encoding="utf-8"))
                    bad_audit["summary"].pop("direct_allocator_rewrite_requested", None)
                    bad_audit["summary"]["direct_allocator_rewrite_validated"] = False
                    bad_audit["summary"]["direct_local_size_align_with_semantic_drop"] = False
                    bad_audit["summary"]["direct_size_align_local_pairing_validated"] = False
                    write_json(audit_a, bad_audit)
                    output_dir = tmp / "aggregate-direct-missing"
                    args = type(
                        "Args",
                        (),
                        {
                            "runtime_smoke_audit_paths": [str(audit_a), str(audit_b)],
                            "runtime_smoke_audits": [],
                            "plan_json_paths": [],
                            "run_id": "aggregate-direct-missing",
                            "output_dir": str(output_dir),
                            "no_update_results": False,
                        },
                    )()
                    stdout = io.StringIO()
                    with contextlib.redirect_stdout(stdout):
                        rc = evaluate.merge_rustc_driver_mir_semantic_scope_std_bench_runtime_shards(args)
                    payload = json.loads(stdout.getvalue())
                    results_audit = results / "rustc_driver_mir_semantic_scope_std_bench_runtime_smoke_audit.json"
        finally:
            evaluate.latest_complete_std_bench_surface = old_surface
        self.assertEqual(rc, 1)
        self.assertFalse(results_audit.exists())
        self.assertIsNone(payload["results_audit"])
        joined = " | ".join(payload["claim_update_gate"]["blockers"])
        self.assertIn("aggregate is not claim-ready", payload["results_update_skipped_reason"])
        self.assertIn("direct allocator rewrite mode was not requested", joined)
        self.assertIn("direct local size/align semantic-drop mode was not requested", joined)

    def test_mir_semantic_scope_runtime_aggregate_gate_rejects_self_attesting_summary_with_bad_source_audits(self) -> None:
        old_surface = evaluate.latest_complete_std_bench_surface
        evaluate.latest_complete_std_bench_surface = lambda: {
            "status": "present",
            "benchmarks": ["bench_a", "bench_b"],
            "source_summary": "unit-surface",
            "run_id": "unit",
            "bench_count": 2,
            "blockers": [],
        }
        try:
            with tempfile.TemporaryDirectory() as raw_tmp:
                tmp = pathlib.Path(raw_tmp)
                results = tmp / "results"
                results.mkdir()
                with temporary_eval_results_and_raw(results):
                    write_ready_mir_probe_companions(results)
                    audit_a = write_mir_semantic_scope_runtime_audit_fixture(
                        tmp,
                        run_id="shard-a-self-attest",
                        benchmark="bench_a",
                    )
                    audit_b = write_mir_semantic_scope_runtime_audit_fixture(
                        tmp,
                        run_id="shard-b-self-attest",
                        benchmark="bench_b",
                    )
                    aggregate = evaluate.aggregate_rustc_driver_mir_semantic_scope_runtime_audits(
                        [audit_a, audit_b],
                        out_dir=tmp,
                        run_id="aggregate-self-attest",
                    )
                    summary = aggregate["summary"]
                    summary.update(
                        {
                            "runtime_smoke_validated": True,
                            "runtime_aggregate_validated": True,
                            "runtime_full_surface_candidate_validated": True,
                            "runtime_surface_blocker_count": 0,
                            "canonical_missing_benchmark_count": 0,
                            "target_rewrite_compiler_pass_validated": True,
                            "direct_allocator_rewrite_requested": True,
                            "direct_allocator_rewrite_validated": True,
                            "direct_local_size_align_with_semantic_drop": True,
                            "direct_size_align_local_pairing_validated": True,
                            "blockers": [],
                        }
                    )
                    aggregate["source_audits"][0].update(
                        {
                            "target_rewrite_compiler_pass_validated": False,
                            "direct_allocator_rewrite_requested": False,
                            "direct_allocator_rewrite_validated": False,
                            "direct_local_size_align_with_semantic_drop": False,
                            "direct_size_align_local_pairing_validated": False,
                        }
                    )

                    companion = evaluate.rustc_driver_mir_semantic_scope_runtime_companion_status(aggregate)
                    gate = evaluate.rustc_driver_mir_semantic_scope_runtime_aggregate_claim_update_gate(
                        aggregate,
                        mir_probe_companions={"ready": True, "blockers": []},
                    )

                    output_dir = tmp / "aggregate-self-attest-output"
                    args = type(
                        "Args",
                        (),
                        {
                            "runtime_smoke_audit_paths": [str(audit_a), str(audit_b)],
                            "runtime_smoke_audits": [],
                            "plan_json_paths": [],
                            "run_id": "aggregate-self-attest-output",
                            "output_dir": str(output_dir),
                            "no_update_results": False,
                        },
                    )()
                    stdout = io.StringIO()
                    with mock.patch.object(
                        evaluate,
                        "aggregate_rustc_driver_mir_semantic_scope_runtime_audits",
                        return_value=aggregate,
                    ):
                        with contextlib.redirect_stdout(stdout):
                            rc = evaluate.merge_rustc_driver_mir_semantic_scope_std_bench_runtime_shards(args)
                    payload = json.loads(stdout.getvalue())
                    results_audit = results / "rustc_driver_mir_semantic_scope_std_bench_runtime_smoke_audit.json"
        finally:
            evaluate.latest_complete_std_bench_surface = old_surface

        self.assertFalse(companion["ready"], companion)
        self.assertFalse(gate["ready"], gate)
        self.assertFalse(gate["runtime_companion_ready"], gate)
        joined_gate_blockers = " | ".join(gate["blockers"])
        self.assertIn("aggregate runtime companion/source blocker", joined_gate_blockers)
        self.assertIn("source audits without optimized_mir compiler_pass evidence", joined_gate_blockers)
        self.assertIn("source audits without requested+validated direct allocator", joined_gate_blockers)
        self.assertEqual(rc, 0)
        self.assertFalse(results_audit.exists())
        self.assertIsNone(payload["results_audit"])
        self.assertFalse(payload["claim_update_gate"]["ready"])
        self.assertIn("aggregate is not claim-ready", payload["results_update_skipped_reason"])

    def test_mir_semantic_scope_runtime_shard_merge_updates_results_when_claim_ready(self) -> None:
        old_surface = evaluate.latest_complete_std_bench_surface
        evaluate.latest_complete_std_bench_surface = lambda: {
            "status": "present",
            "benchmarks": ["bench_a", "bench_b"],
            "source_summary": "unit-surface",
            "run_id": "unit",
            "bench_count": 2,
            "blockers": [],
        }
        try:
            with tempfile.TemporaryDirectory() as raw_tmp:
                tmp = pathlib.Path(raw_tmp)
                results = tmp / "results"
                results.mkdir()
                with temporary_eval_results_and_raw(results):
                    write_ready_mir_probe_companions(results)
                    audit_a = write_mir_semantic_scope_runtime_audit_fixture(
                        tmp,
                        run_id="shard-a",
                        benchmark="bench_a",
                    )
                    audit_b = write_mir_semantic_scope_runtime_audit_fixture(
                        tmp,
                        run_id="shard-b",
                        benchmark="bench_b",
                    )
                    output_dir = tmp / "aggregate-ready"
                    args = type(
                        "Args",
                        (),
                        {
                            "runtime_smoke_audit_paths": [str(audit_a), str(audit_b)],
                            "runtime_smoke_audits": [],
                            "plan_json_paths": [],
                            "run_id": "aggregate-ready",
                            "output_dir": str(output_dir),
                            "no_update_results": False,
                        },
                    )()
                    stdout = io.StringIO()
                    with contextlib.redirect_stdout(stdout):
                        rc = evaluate.merge_rustc_driver_mir_semantic_scope_std_bench_runtime_shards(args)
                    payload = json.loads(stdout.getvalue())
                    results_audit = results / "rustc_driver_mir_semantic_scope_std_bench_runtime_smoke_audit.json"
                    updated = json.loads(results_audit.read_text(encoding="utf-8"))
        finally:
            evaluate.latest_complete_std_bench_surface = old_surface
        self.assertEqual(rc, 0)
        self.assertTrue(payload["claim_update_gate"]["ready"], payload)
        self.assertEqual(payload["results_audit"], str(results_audit))
        self.assertTrue(updated["summary"]["runtime_full_surface_candidate_validated"])

    def test_mir_semantic_scope_claim_grade_package_is_fail_closed_but_can_pass_full_surface(self) -> None:
        old_surface = evaluate.latest_complete_std_bench_surface
        old_results = evaluate.RESULTS
        evaluate.latest_complete_std_bench_surface = lambda: {
            "status": "present",
            "benchmarks": ["bench_a", "bench_b"],
            "source_summary": "unit-surface",
            "run_id": "unit",
            "bench_count": 2,
            "blockers": [],
        }
        try:
            with tempfile.TemporaryDirectory() as raw_tmp:
                tmp = pathlib.Path(raw_tmp)
                results = tmp / "results"
                results.mkdir()
                with temporary_eval_results_and_raw(results):
                    write_ready_mir_probe_companions(results)
                    audit_a = write_mir_semantic_scope_runtime_audit_fixture(
                        tmp,
                        run_id="shard-a",
                        benchmark="bench_a",
                    )
                    audit_b = write_mir_semantic_scope_runtime_audit_fixture(
                        tmp,
                        run_id="shard-b",
                        benchmark="bench_b",
                    )
                    aggregate = evaluate.aggregate_rustc_driver_mir_semantic_scope_runtime_audits(
                        [audit_a, audit_b],
                        out_dir=tmp,
                        run_id="aggregate",
                    )
                    aggregate_path = tmp / "aggregate.audit.json"
                    write_json(aggregate_path, aggregate)
                    output_dir = tmp / "package"
                    args = type(
                        "Args",
                        (),
                        {
                            "runtime_smoke_audit": str(aggregate_path),
                            "run_id": "package",
                            "output_dir": str(output_dir),
                            "manifest_out": None,
                            "update_compiler_coverage_result": False,
                            "no_update_results": True,
                            "claim_grade": True,
                        },
                    )()
                    with contextlib.redirect_stdout(io.StringIO()):
                        rc = evaluate.package_rustc_driver_mir_semantic_scope_std_bench_runtime_manifest(args)
                    import_audit = json.loads(
                        (output_dir / "rustc-driver-mir-semantic-scope-std-bench-runtime-import-audit.json").read_text(
                            encoding="utf-8"
                        )
                    )
        finally:
            evaluate.latest_complete_std_bench_surface = old_surface
            evaluate.RESULTS = old_results
        self.assertEqual(rc, 0, import_audit.get("blockers"))
        self.assertTrue(import_audit["summary"]["claim_grade"])
        self.assertTrue(import_audit["summary"]["compiler_audit_ready_for_claim_grade_import"])

    def test_mir_semantic_scope_claim_grade_package_rechecks_source_before_publication(self) -> None:
        old_surface = evaluate.latest_complete_std_bench_surface
        evaluate.latest_complete_std_bench_surface = lambda: {
            "status": "present",
            "benchmarks": ["bench_a", "bench_b"],
            "source_summary": "unit-surface",
            "run_id": "unit",
            "bench_count": 2,
            "blockers": [],
        }
        try:
            with tempfile.TemporaryDirectory() as raw_tmp:
                tmp = pathlib.Path(raw_tmp)
                results = tmp / "results"
                results.mkdir()
                stable_source = source_fingerprint_fixture("f")
                changed_source = source_fingerprint_fixture("0")
                with temporary_eval_results_and_raw(results):
                    write_ready_mir_probe_companions(results)
                    audit_a = write_mir_semantic_scope_runtime_audit_fixture(
                        tmp,
                        run_id="publication-source-shard-a",
                        benchmark="bench_a",
                        source_fingerprint=stable_source,
                    )
                    audit_b = write_mir_semantic_scope_runtime_audit_fixture(
                        tmp,
                        run_id="publication-source-shard-b",
                        benchmark="bench_b",
                        source_fingerprint=stable_source,
                    )
                    aggregate = evaluate.aggregate_rustc_driver_mir_semantic_scope_runtime_audits(
                        [audit_a, audit_b],
                        out_dir=tmp,
                        run_id="publication-source-aggregate",
                    )
                    aggregate_path = tmp / "publication-source-aggregate.audit.json"
                    write_json(aggregate_path, aggregate)
                    output_dir = tmp / "publication-source-package"
                    args = type(
                        "Args",
                        (),
                        {
                            "runtime_smoke_audit": str(aggregate_path),
                            "run_id": "publication-source-package",
                            "output_dir": str(output_dir),
                            "manifest_out": None,
                            "update_compiler_coverage_result": False,
                            "no_update_results": True,
                            "claim_grade": True,
                        },
                    )()
                    with mock.patch.object(
                        evaluate,
                        "repository_source_fingerprint",
                        side_effect=[stable_source, stable_source, changed_source],
                    ):
                        with contextlib.redirect_stdout(io.StringIO()):
                            rc = evaluate.package_rustc_driver_mir_semantic_scope_std_bench_runtime_manifest(
                                args
                            )
                    import_audit = json.loads(
                        (
                            output_dir
                            / "rustc-driver-mir-semantic-scope-std-bench-runtime-import-audit.json"
                        ).read_text(encoding="utf-8")
                    )
        finally:
            evaluate.latest_complete_std_bench_surface = old_surface

        self.assertEqual(rc, 1)
        self.assertFalse(import_audit["summary"]["publication_ready"])
        self.assertFalse(import_audit["summary"]["claim_grade"])
        self.assertFalse(import_audit["summary"]["complete_for_claim"])
        self.assertFalse(
            import_audit["summary"]["mir_semantic_scope_runtime_import_preflight_validated"]
        )
        self.assertIn(
            "repository source changed before runtime evidence publication",
            " | ".join(import_audit["blockers"]),
        )

    def test_mir_semantic_scope_claim_grade_package_rejects_tampered_runtime_artifacts(self) -> None:
        old_surface = evaluate.latest_complete_std_bench_surface
        evaluate.latest_complete_std_bench_surface = lambda: {
            "status": "present",
            "benchmarks": ["bench_a", "bench_b"],
            "source_summary": "unit-surface",
            "run_id": "unit",
            "bench_count": 2,
            "blockers": [],
        }
        try:
            for artifact_key in ("runtime_events", "type_mapping"):
                with self.subTest(artifact_key=artifact_key):
                    with tempfile.TemporaryDirectory() as raw_tmp:
                        tmp = pathlib.Path(raw_tmp)
                        results = tmp / "results"
                        results.mkdir()
                        with temporary_eval_results_and_raw(results):
                            write_ready_mir_probe_companions(results)
                            source_fingerprint = evaluate.repository_source_fingerprint()
                            audit_a = write_mir_semantic_scope_runtime_audit_fixture(
                                tmp,
                                run_id=f"shard-a-{artifact_key}",
                                benchmark="bench_a",
                                source_fingerprint=source_fingerprint,
                            )
                            audit_b = write_mir_semantic_scope_runtime_audit_fixture(
                                tmp,
                                run_id=f"shard-b-{artifact_key}",
                                benchmark="bench_b",
                                source_fingerprint=source_fingerprint,
                            )
                            aggregate = evaluate.aggregate_rustc_driver_mir_semantic_scope_runtime_audits(
                                [audit_a, audit_b],
                                out_dir=tmp,
                                run_id=f"aggregate-{artifact_key}",
                            )
                            aggregate_path = tmp / f"aggregate-{artifact_key}.audit.json"
                            write_json(aggregate_path, aggregate)
                            tamper_mir_semantic_scope_runtime_artifact(
                                aggregate_path,
                                artifact_key,
                            )
                            output_dir = tmp / f"package-{artifact_key}"
                            args = type(
                                "Args",
                                (),
                                {
                                    "runtime_smoke_audit": str(aggregate_path),
                                    "run_id": f"package-{artifact_key}",
                                    "output_dir": str(output_dir),
                                    "manifest_out": None,
                                    "update_compiler_coverage_result": False,
                                    "no_update_results": True,
                                    "claim_grade": True,
                                },
                            )()
                            with contextlib.redirect_stdout(io.StringIO()):
                                rc = evaluate.package_rustc_driver_mir_semantic_scope_std_bench_runtime_manifest(args)
                            import_audit = json.loads(
                                (
                                    output_dir
                                    / "rustc-driver-mir-semantic-scope-std-bench-runtime-import-audit.json"
                                ).read_text(encoding="utf-8")
                            )

                    self.assertEqual(rc, 1)
                    self.assertFalse(import_audit["summary"]["claim_grade"])
                    self.assertFalse(
                        import_audit["summary"]["mir_semantic_scope_runtime_import_preflight_validated"]
                    )
                    self.assertIn(
                        f"runtime artifact integrity: artifacts.{artifact_key} sha256 mismatch",
                        " | ".join(import_audit["blockers"]),
                    )
        finally:
            evaluate.latest_complete_std_bench_surface = old_surface

    def test_mir_semantic_scope_runtime_shard_planner_recommends_offsets(self) -> None:
        old_surface = evaluate.latest_complete_std_bench_surface
        evaluate.latest_complete_std_bench_surface = lambda: {
            "status": "present",
            "benchmarks": ["bench_0", "bench_1", "bench_2", "bench_3"],
            "source_summary": "unit-surface",
            "run_id": "unit",
            "bench_count": 4,
            "blockers": [],
        }
        try:
            with tempfile.TemporaryDirectory() as raw_tmp:
                tmp = pathlib.Path(raw_tmp)
                audit_0 = write_mir_semantic_scope_runtime_audit_fixture(
                    tmp,
                    run_id="shard-0",
                    benchmark="bench_0",
                )
                output_dir = tmp / "plan"
                args = type(
                    "Args",
                    (),
                    {
                        "runtime_smoke_audit_paths": [str(audit_0)],
                        "run_id": "plan",
                        "output_dir": str(output_dir),
                        "shard_count": 2,
                        "max_benchmarks": 1,
                        "max_new_shards": 3,
                        "timeout": 123,
                        "no_discover_existing": True,
                        "no_update_results": True,
                    },
                )()
                with contextlib.redirect_stdout(io.StringIO()):
                    rc = evaluate.plan_rustc_driver_mir_semantic_scope_runtime_shards(args)
                plan = json.loads(
                    (output_dir / "rustc-driver-mir-semantic-scope-std-bench-runtime-shard-plan.json").read_text(
                        encoding="utf-8"
                    )
                )
        finally:
            evaluate.latest_complete_std_bench_surface = old_surface
        self.assertEqual(rc, 0)
        self.assertEqual(plan["summary"]["covered_benchmark_count"], 1)
        self.assertEqual(plan["summary"]["missing_benchmark_count"], 3)
        commands = " | ".join(item["command_text"] for item in plan["recommended_shards"])
        self.assertIn("--shard-index 1", commands)
        self.assertIn("--shard-offset 1", commands)

    def test_mir_semantic_scope_runtime_shard_planner_can_emit_exact_batches(self) -> None:
        old_surface = evaluate.latest_complete_std_bench_surface
        evaluate.latest_complete_std_bench_surface = lambda: {
            "status": "present",
            "benchmarks": ["bench_0", "bench_1", "bench_2", "bench_3", "bench_4"],
            "source_summary": "unit-surface",
            "run_id": "unit",
            "bench_count": 5,
            "blockers": [],
        }
        try:
            with tempfile.TemporaryDirectory() as raw_tmp:
                tmp = pathlib.Path(raw_tmp)
                audit_0 = write_mir_semantic_scope_runtime_audit_fixture(
                    tmp,
                    run_id="shard-0",
                    benchmark="bench_0",
                )
                output_dir = tmp / "plan"
                args = type(
                    "Args",
                    (),
                    {
                        "runtime_smoke_audit_paths": [str(audit_0)],
                        "run_id": "plan",
                        "output_dir": str(output_dir),
                        "shard_count": 2,
                        "max_benchmarks": 1,
                        "max_new_shards": 2,
                        "timeout": 123,
                        "no_discover_existing": True,
                        "no_update_results": True,
                        "compatible_group_strategy": "latest",
                        "recommendation_mode": "exact-batches",
                        "exact_batch_size": 2,
                    },
                )()
                with contextlib.redirect_stdout(io.StringIO()):
                    rc = evaluate.plan_rustc_driver_mir_semantic_scope_runtime_shards(args)
                plan = json.loads(
                    (output_dir / "rustc-driver-mir-semantic-scope-std-bench-runtime-shard-plan.json").read_text(
                        encoding="utf-8"
                    )
                )
        finally:
            evaluate.latest_complete_std_bench_surface = old_surface
        self.assertEqual(rc, 0)
        self.assertEqual(plan["summary"]["recommendation_mode"], "exact-batches")
        self.assertEqual(plan["summary"]["recommended_benchmark_count"], 4)
        commands = " | ".join(item["command_text"] for item in plan["recommended_commands"])
        self.assertIn("--benchmark-plan-json", commands)
        self.assertIn("--recommended-batch-index 0", commands)
        self.assertIn("--recommended-batch-index 1", commands)
        self.assertNotIn("--benchmarks", commands)
        self.assertNotIn("--shard-index", commands)

    def test_adaptive_request_loader_can_use_refreshed_plan_batch(self) -> None:
        with tempfile.TemporaryDirectory() as raw_tmp:
            tmp = pathlib.Path(raw_tmp)
            plan = tmp / "plan.json"
            write_json(
                plan,
                {
                    "recommended_commands": [
                        {"missing_benchmarks": ["bench_a", "bench_b"]},
                        {"missing_benchmarks": ["bench_c"]},
                    ]
                },
            )
            args = type(
                "Args",
                (),
                {
                    "benchmarks": [],
                    "benchmark_files": [],
                    "benchmark_plan_jsons": [],
                    "recommended_batch_index": 0,
                    "recommended_batch_count": 1,
                },
            )()
            loaded = evaluate.load_adaptive_mir_semantic_scope_benchmark_request(
                args,
                benchmark_plan_jsons=[plan],
                recommended_batch_index=1,
                recommended_batch_count=1,
            )
        self.assertEqual(loaded, ["bench_c"])

    def test_parse_optional_positive_int_rejects_zero(self) -> None:
        self.assertEqual(evaluate.parse_optional_positive_int("3", name="--rounds"), 3)
        self.assertIsNone(evaluate.parse_optional_positive_int(None, name="--rounds"))
        with self.assertRaises(ValueError):
            evaluate.parse_optional_positive_int(0, name="--rounds")

    def test_current_mir_semantic_scope_gap_is_primary_over_legacy_replay_wording(self) -> None:
        observed_source = {
            "kind": "rustc-driver-mir-semantic-scope-selected-runtime",
            "compiler_instrumented": True,
            "claim_grade": False,
            "complete_for_claim": False,
            "coverage_surface_complete": False,
            "compiler_site_replay": False,
            "compiler_site_id_stream_mode": "rustc-driver-mir-semantic-scope",
            "selected_benchmarks": ["bench_0", "bench_1"],
            "runtime_surface": {
                "status": "slice-or-incomplete",
                "expected_benchmark_count": 430,
                "observed_benchmark_count": 117,
                "missing_expected_benchmark_count": 313,
                "expected_benchmarks": ["bench_" + str(idx) for idx in range(430)],
            },
        }
        compiler_summary = {
            "ready_for_claim_grade_import": False,
            "event_valid": True,
            "event_type_id_bases": [
                "compiler-assigned-allocation-site-object-type-id-rustc-driver-mir-semantic-scope"
            ],
            "event_type_id_basis_status": "pass",
            "event_type_mapping_correlation_status": "pass",
            "type_mapping_status": "pass",
            "type_mapping_valid_record_count": 541,
            "benchmark_surface_expected_count": 430,
            "benchmark_surface_observed_count": 117,
            "benchmark_surface_missing_count": 313,
        }
        runtime_summary = {
            "runtime_smoke_validated": True,
            "runtime_returncode": 0,
            "runtime_full_surface_candidate_validated": False,
            "selected_benchmark_count": 117,
            "canonical_expected_benchmark_count": 430,
            "canonical_missing_benchmark_count": 313,
            "actual_semantic_scope_rewrite": True,
            "semantic_scope_rewrite_applied_count": 541,
            "semantic_scope_replacement_resolution_status": "resolved_unialloc_semantic_scope_enter_exit",
            "lowered_module_typed_allocation_site_event_count": 128,
            "lowered_module_allocations": 52938622,
            "lowered_module_compiler_site_id_stream_modes": ["rustc-driver-mir-semantic-scope"],
            "unknown_semantic_object_type_row_count": 0,
        }
        gap = evaluate.current_mir_semantic_scope_runtime_surface_gap(
            observed_source=observed_source,
            compiler_summary=compiler_summary,
            runtime_summary=runtime_summary,
            runtime_smoke_validated=True,
            legacy_replay_event_bases=[
                "compiler-assigned-allocation-site-object-type-id-cyclic-replay"
            ],
            exact_dynamic_preflight={"kind": "std_bench_slice"},
        )
        self.assertIsNotNone(gap)
        assert gap is not None
        self.assertEqual(gap["id"], "mir_semantic_scope_full_surface_c002_runtime_missing")
        self.assertIn(
            "real rustc_driver optimized_mir semantic-scope selected runtime covering 117/430",
            gap["description"],
        )
        self.assertIn("legacy replay remains only a preflight/reference basis", gap["description"])
        self.assertNotIn("runtime events still come from a cyclic replay bridge", gap["description"])
        self.assertEqual(
            gap["evidence"]["legacy_replay_event_bases"],
            ["compiler-assigned-allocation-site-object-type-id-cyclic-replay"],
        )
        self.assertNotIn(
            "expected_benchmarks",
            gap["evidence"]["observed_benchmark_source"]["runtime_surface"],
        )
        self.assertEqual(
            gap["evidence"]["observed_benchmark_source"]["selected_benchmark_count"],
            2,
        )

    def test_final_mir_semantic_scope_runtime_ready_overrides_legacy_replay(self) -> None:
        observed_source = {
            "kind": "rustc-driver-mir-semantic-scope-final-c002-runtime",
            "compiler_instrumented": True,
            "claim_grade": True,
            "complete_for_claim": True,
            "coverage_surface_complete": True,
            "compiler_site_replay": False,
            "compiler_site_id_stream_mode": "rustc-driver-mir-semantic-scope",
        }
        compiler_summary = {
            "ready_for_claim_grade_import": True,
            "event_type_id_basis_status": "pass",
            "event_type_mapping_correlation_status": "pass",
            "benchmark_surface_status": "pass",
            "benchmark_surface_expected_count": 430,
            "benchmark_surface_observed_count": 430,
            "benchmark_surface_missing_count": 0,
        }
        runtime_summary = {
            "runtime_smoke_validated": True,
            "runtime_returncode": 0,
            "runtime_full_surface_candidate_validated": True,
            "runtime_surface_full_surface_candidate": True,
            "canonical_missing_benchmark_count": 0,
            "actual_semantic_scope_rewrite": True,
            "semantic_scope_rewrite_applied_count": 541,
            "lowered_module_typed_allocation_site_event_count": 239,
            "unknown_semantic_object_type_row_count": 0,
            "lowered_module_compiler_site_id_stream_modes": ["rustc-driver-mir-semantic-scope"],
        }
        self.assertTrue(evaluate.compiler_benchmark_surface_complete_for_claim(compiler_summary))
        self.assertTrue(
            evaluate.current_mir_semantic_scope_final_c002_runtime_ready(
                observed_source=observed_source,
                compiler_summary=compiler_summary,
                runtime_summary=runtime_summary,
                runtime_smoke_validated=True,
                compiler_audit_ready=True,
                observed_source_claim_ready=True,
                manifest_requested_claim=True,
                suite_claim_ready=True,
            )
        )
        self.assertIsNone(
            evaluate.current_mir_semantic_scope_runtime_surface_gap(
                observed_source=observed_source,
                compiler_summary=compiler_summary,
                runtime_summary=runtime_summary,
                runtime_smoke_validated=True,
                legacy_replay_event_bases=[
                    "compiler-assigned-allocation-site-object-type-id-cyclic-replay"
                ],
                exact_dynamic_preflight={"kind": "std_bench_slice"},
            )
        )

    def test_observed_source_gap_wording_distinguishes_real_mir_runtime_from_bridge(self) -> None:
        description = evaluate.observed_benchmark_source_not_final_c002_description(
            {
                "kind": "rustc-driver-mir-semantic-scope-selected-runtime",
                "compiler_site_replay": False,
            }
        )
        self.assertIn("real rustc_driver MIR semantic-scope runtime evidence", description)
        self.assertNotIn("bridge/reference", description)

    def test_dynamic_attribution_gap_priority_puts_current_mir_surface_gap_first(self) -> None:
        ranked = evaluate.prioritized_dynamic_attribution_gaps(
            [
                {"id": "rustc_driver_mir_actual_rewrite_runtime_smoke_not_full_surface_claim"},
                {"id": "upstream_audit_blocker"},
                {"id": "mir_semantic_scope_full_surface_c002_runtime_missing"},
                {"id": "observed_benchmark_source_not_final_c002_run"},
            ]
        )
        self.assertEqual(
            [gap["id"] for gap in ranked],
            [
                "mir_semantic_scope_full_surface_c002_runtime_missing",
                "observed_benchmark_source_not_final_c002_run",
                "rustc_driver_mir_actual_rewrite_runtime_smoke_not_full_surface_claim",
                "upstream_audit_blocker",
            ],
        )

    def test_mir_semantic_scope_runtime_shard_planner_can_prioritize_by_pair_gain(self) -> None:
        old_surface = evaluate.latest_complete_std_bench_surface
        evaluate.latest_complete_std_bench_surface = lambda: {
            "status": "present",
            "benchmarks": ["bench_0", "bench_1", "bench_2", "bench_3"],
            "source_summary": "unit-surface",
            "run_id": "unit",
            "bench_count": 4,
            "blockers": [],
        }
        try:
            with tempfile.TemporaryDirectory() as raw_tmp:
                tmp = pathlib.Path(raw_tmp)
                audit_0 = write_mir_semantic_scope_runtime_audit_fixture(
                    tmp,
                    run_id="shard-0",
                    benchmark="bench_0",
                    type_id=101,
                    callsite=1001,
                )
                audit = json.loads(audit_0.read_text(encoding="utf-8"))
                type_mapping_path = pathlib.Path(audit["artifacts"]["type_mapping"])
                write_json(
                    type_mapping_path,
                    {
                        "schema_version": 1,
                        "allocation_sites": [
                            {
                                "allocation_site_id": "mir:bench_0:bb0:stmt0",
                                "type_id": 101,
                                "module_id": evaluate.RUSTC_DRIVER_LOWERING_MODULE_ID,
                                "policy_flags": 3,
                                "callsite": 1001,
                                "mir_function": "bench_0",
                                "source_span": "unit.rs:1:1: 1:8 (#0)",
                                "semantic_object_type": "alloc::vec::Vec<u8>",
                                "type_id_basis": "rustc_middle_ty_destination_or_argument_heap_object_type",
                                "lowering_kind": "semantic_scope_enter_exit_rewrite",
                                "rewrite_status": "actual_semantic_scope_enter_exit_rewrite_applied",
                            },
                            {
                                "allocation_site_id": "mir:bench_1:bb0:stmt0",
                                "type_id": 102,
                                "module_id": evaluate.RUSTC_DRIVER_LOWERING_MODULE_ID,
                                "policy_flags": 3,
                                "callsite": 1002,
                                "mir_function": "bench_1",
                                "source_span": "unit.rs:2:1: 2:8 (#0)",
                                "semantic_object_type": "alloc::vec::Vec<u16>",
                                "type_id_basis": "rustc_middle_ty_destination_or_argument_heap_object_type",
                                "lowering_kind": "semantic_scope_enter_exit_rewrite",
                                "rewrite_status": "actual_semantic_scope_enter_exit_rewrite_applied",
                            },
                            {
                                "allocation_site_id": "mir:bench_2:bb0:stmt0",
                                "type_id": 103,
                                "module_id": evaluate.RUSTC_DRIVER_LOWERING_MODULE_ID,
                                "policy_flags": 3,
                                "callsite": 1003,
                                "mir_function": "bench_2",
                                "source_span": "unit.rs:3:1: 3:8 (#0)",
                                "semantic_object_type": "alloc::vec::Vec<u32>",
                                "type_id_basis": "rustc_middle_ty_destination_or_argument_heap_object_type",
                                "lowering_kind": "semantic_scope_enter_exit_rewrite",
                                "rewrite_status": "actual_semantic_scope_enter_exit_rewrite_applied",
                            },
                            {
                                "allocation_site_id": "mir:bench_2:bb1:stmt0",
                                "type_id": 104,
                                "module_id": evaluate.RUSTC_DRIVER_LOWERING_MODULE_ID,
                                "policy_flags": 3,
                                "callsite": 1004,
                                "mir_function": "bench_2",
                                "source_span": "unit.rs:4:1: 4:8 (#0)",
                                "semantic_object_type": "alloc::vec::Vec<u64>",
                                "type_id_basis": "rustc_middle_ty_destination_or_argument_heap_object_type",
                                "lowering_kind": "semantic_scope_enter_exit_rewrite",
                                "rewrite_status": "actual_semantic_scope_enter_exit_rewrite_applied",
                            },
                        ],
                    },
                )
                audit["artifacts"]["type_mapping_sha256"] = evaluate.file_sha256(type_mapping_path)
                write_json(audit_0, audit)
                output_dir = tmp / "plan"
                args = type(
                    "Args",
                    (),
                    {
                        "runtime_smoke_audit_paths": [str(audit_0)],
                        "run_id": "plan",
                        "output_dir": str(output_dir),
                        "shard_count": 2,
                        "max_benchmarks": 1,
                        "max_new_shards": 1,
                        "timeout": 123,
                        "no_discover_existing": True,
                        "no_update_results": True,
                        "compatible_group_strategy": "latest",
                        "recommendation_mode": "exact-batches",
                        "recommendation_priority": "mir-pair-gain",
                        "exact_batch_size": 2,
                    },
                )()
                with contextlib.redirect_stdout(io.StringIO()):
                    rc = evaluate.plan_rustc_driver_mir_semantic_scope_runtime_shards(args)
                plan = json.loads(
                    (output_dir / "rustc-driver-mir-semantic-scope-std-bench-runtime-shard-plan.json").read_text(
                        encoding="utf-8"
                    )
                )
        finally:
            evaluate.latest_complete_std_bench_surface = old_surface
        self.assertEqual(rc, 0)
        self.assertEqual(plan["summary"]["recommendation_priority"], "mir-pair-gain")
        self.assertEqual(plan["recommendation_missing_benchmarks"][:3], ["bench_2", "bench_1", "bench_3"])
        self.assertEqual(plan["recommended_commands"][0]["missing_benchmarks"], ["bench_2", "bench_1"])
        diagnostics = plan["recommendation_priority_diagnostics"]
        self.assertEqual(diagnostics["status"], "available")
        self.assertEqual(diagnostics["positive_gain_benchmark_count"], 2)
        self.assertEqual(diagnostics["top_pair_gain_benchmarks"][0]["benchmark"], "bench_2")
        self.assertEqual(diagnostics["top_pair_gain_benchmarks"][0]["uncovered_pair_gain"], 2)

    def test_mir_pair_gain_priority_uses_runtime_cost_only_as_tie_breaker(self) -> None:
        with tempfile.TemporaryDirectory() as raw_tmp:
            tmp = pathlib.Path(raw_tmp)
            audit_path = write_mir_semantic_scope_runtime_audit_fixture(
                tmp,
                run_id="cost-tie",
                benchmark="bench_observed",
                type_id=101,
                callsite=1001,
            )
            audit = json.loads(audit_path.read_text(encoding="utf-8"))
            type_mapping_path = pathlib.Path(audit["artifacts"]["type_mapping"])
            write_json(
                type_mapping_path,
                {
                    "schema_version": 1,
                    "allocation_sites": [
                        {
                            "allocation_site_id": "mir:observed:bb0:stmt0",
                            "type_id": 101,
                            "module_id": evaluate.RUSTC_DRIVER_LOWERING_MODULE_ID,
                            "policy_flags": 3,
                            "callsite": 1001,
                            "mir_function": "bench_observed",
                            "source_span": "unit.rs:1:1: 1:8 (#0)",
                            "semantic_object_type": "alloc::vec::Vec<u8>",
                            "type_id_basis": "rustc_middle_ty_destination_or_argument_heap_object_type",
                            "lowering_kind": "semantic_scope_enter_exit_rewrite",
                            "rewrite_status": "actual_semantic_scope_enter_exit_rewrite_applied",
                        },
                        {
                            "allocation_site_id": "mir:huge:bb0:stmt0",
                            "type_id": 201,
                            "module_id": evaluate.RUSTC_DRIVER_LOWERING_MODULE_ID,
                            "policy_flags": 3,
                            "callsite": 2001,
                            "mir_function": "slice::rotate_huge_by1234577_big",
                            "source_span": "unit.rs:2:1: 2:8 (#0)",
                            "semantic_object_type": "alloc::vec::Vec<u64>",
                            "type_id_basis": "rustc_middle_ty_destination_or_argument_heap_object_type",
                            "lowering_kind": "semantic_scope_enter_exit_rewrite",
                            "rewrite_status": "actual_semantic_scope_enter_exit_rewrite_applied",
                        },
                        {
                            "allocation_site_id": "mir:tiny:bb0:stmt0",
                            "type_id": 202,
                            "module_id": evaluate.RUSTC_DRIVER_LOWERING_MODULE_ID,
                            "policy_flags": 3,
                            "callsite": 2002,
                            "mir_function": "slice::rotate_tiny_half_plus_one",
                            "source_span": "unit.rs:3:1: 3:8 (#0)",
                            "semantic_object_type": "alloc::vec::Vec<u64>",
                            "type_id_basis": "rustc_middle_ty_destination_or_argument_heap_object_type",
                            "lowering_kind": "semantic_scope_enter_exit_rewrite",
                            "rewrite_status": "actual_semantic_scope_enter_exit_rewrite_applied",
                        },
                    ],
                },
            )
            audit["artifacts"]["type_mapping_sha256"] = evaluate.file_sha256(type_mapping_path)
            write_json(audit_path, audit)
            prioritized, diagnostics = evaluate.prioritize_mir_semantic_scope_missing_benchmarks_by_pair_gain(
                [
                    "slice::rotate_huge_by1234577_big",
                    "slice::rotate_tiny_half_plus_one",
                ],
                [{"path": str(audit_path)}],
                [
                    "slice::rotate_huge_by1234577_big",
                    "slice::rotate_tiny_half_plus_one",
                ],
            )
        self.assertEqual(
            prioritized[:2],
            ["slice::rotate_tiny_half_plus_one", "slice::rotate_huge_by1234577_big"],
        )
        top = diagnostics["top_pair_gain_benchmarks"]
        self.assertEqual(top[0]["benchmark"], "slice::rotate_tiny_half_plus_one")
        self.assertEqual(top[0]["uncovered_pair_gain"], top[1]["uncovered_pair_gain"])
        self.assertLess(top[0]["estimated_runtime_cost"], top[1]["estimated_runtime_cost"])
        self.assertGreater(
            evaluate.std_bench_estimated_runtime_cost("slice::sort_unstable_by_key_lexicographic"),
            120,
        )

    def test_runtime_shard_planner_cost_filter_keeps_missing_fail_closed(self) -> None:
        old_surface = evaluate.latest_complete_std_bench_surface
        evaluate.latest_complete_std_bench_surface = lambda: {
            "status": "present",
            "benchmarks": [
                "slice::rotate_tiny_half_plus_one",
                "slice::rotate_huge_by1234577_big",
                "vec::bench_transmute",
            ],
            "source_summary": "unit-surface",
            "run_id": "unit",
            "bench_count": 3,
            "blockers": [],
        }
        try:
            with tempfile.TemporaryDirectory() as raw_tmp:
                tmp = pathlib.Path(raw_tmp)
                output_dir = tmp / "plan"
                args = type(
                    "Args",
                    (),
                    {
                        "runtime_smoke_audit_paths": [],
                        "run_id": "cost-filter-plan",
                        "output_dir": str(output_dir),
                        "shard_count": 2,
                        "max_benchmarks": 1,
                        "max_new_shards": 2,
                        "timeout": 123,
                        "no_discover_existing": True,
                        "no_update_results": True,
                        "compatible_group_strategy": "latest",
                        "recommendation_mode": "exact-batches",
                        "recommendation_priority": "surface-order",
                        "recommendation_max_estimated_cost": 120,
                        "exact_batch_size": 4,
                        "include_blocked_benchmarks": False,
                    },
                )()
                with contextlib.redirect_stdout(io.StringIO()):
                    rc = evaluate.plan_rustc_driver_mir_semantic_scope_runtime_shards(args)
                plan = json.loads(
                    (output_dir / "rustc-driver-mir-semantic-scope-std-bench-runtime-shard-plan.json").read_text(
                        encoding="utf-8"
                    )
                )
        finally:
            evaluate.latest_complete_std_bench_surface = old_surface
        self.assertEqual(rc, 0)
        self.assertEqual(plan["summary"]["missing_benchmark_count"], 3)
        self.assertEqual(plan["summary"]["recommendation_cost_filter_enabled"], True)
        self.assertEqual(plan["summary"]["recommendation_cost_filtered_benchmark_count"], 1)
        self.assertEqual(
            plan["recommendation_missing_benchmarks"],
            ["slice::rotate_tiny_half_plus_one", "vec::bench_transmute"],
        )
        self.assertEqual(
            plan["recommendation_cost_filter"]["filtered_benchmarks"][0]["benchmark"],
            "slice::rotate_huge_by1234577_big",
        )
        self.assertEqual(
            plan["recommended_commands"][0]["missing_benchmarks"],
            ["slice::rotate_tiny_half_plus_one", "vec::bench_transmute"],
        )
        self.assertEqual(
            set(plan["recommended_commands"][0]["estimated_runtime_costs"]),
            {"slice::rotate_tiny_half_plus_one", "vec::bench_transmute"},
        )
        self.assertEqual(
            plan["recommended_commands"][0]["benchmark_family_counts"],
            {"slice::rotate_*": 1, "vec::*": 1},
        )
        self.assertEqual(
            plan["summary"]["recommended_benchmark_family_counts"],
            {"slice::rotate_*": 1, "vec::*": 1},
        )
        self.assertNotIn("slice::rotate_huge_by1234577_big", plan["recommended_commands"][0]["missing_benchmarks"])

    def test_recommendation_family_uses_template_then_namespace_fallback(self) -> None:
        self.assertEqual(
            evaluate.std_bench_recommendation_family("vec::bench_clone_from_01_0010_0010"),
            "vec::bench_clone_from_*",
        )
        self.assertEqual(
            evaluate.std_bench_recommendation_family("btree::map::insert_seq_100"),
            "btree::map::*",
        )
        self.assertEqual(
            evaluate.std_bench_recommendation_family("btree::set::intersection_100_neg_vs_100_pos"),
            "btree::set::*",
        )
        self.assertEqual(
            evaluate.std_bench_recommendation_family("binary_heap::bench_push"),
            "binary_heap::*",
        )

    def test_recommendation_family_diversity_preserves_pair_gain_tiers(self) -> None:
        ordered = [
            "vec::bench_clone_from_01_0010_0010",
            "vec::bench_clone_from_01_0010_0100",
            "vec::bench_clone_from_01_0100_0010",
            "vec::bench_clone_from_01_0100_0100",
            "vec::bench_dedup_new_100",
            "vec::bench_dedup_new_1000",
            "slice::rotate_medium_by1",
            "vec::bench_from_iter_0000",
            "linked_list::bench_iter_mut",
        ]
        pair_gain = {bench: 3 for bench in ordered[:7]}
        pair_gain.update({bench: 2 for bench in ordered[7:]})
        diversified, diagnostics = evaluate.diversify_std_bench_recommendations_by_gain_tier(
            ordered,
            pair_gain_by_benchmark=pair_gain,
            max_per_family_per_cycle=2,
        )
        self.assertTrue(diagnostics["enabled"])
        self.assertTrue(diagnostics["changed"])
        self.assertEqual(
            diversified[:7],
            [
                "vec::bench_clone_from_01_0010_0010",
                "vec::bench_clone_from_01_0010_0100",
                "vec::bench_dedup_new_100",
                "vec::bench_dedup_new_1000",
                "slice::rotate_medium_by1",
                "vec::bench_clone_from_01_0100_0010",
                "vec::bench_clone_from_01_0100_0100",
            ],
        )
        self.assertEqual(diversified[7:], ["vec::bench_from_iter_0000", "linked_list::bench_iter_mut"])
        self.assertEqual(diagnostics["tier_count"], 2)
        self.assertEqual(diagnostics["reported_tier_count"], 2)
        self.assertEqual(diagnostics["truncated_tier_count"], 0)

    def test_mir_semantic_scope_runtime_shard_planner_skips_timed_out_benchmarks(self) -> None:
        old_surface = evaluate.latest_complete_std_bench_surface
        evaluate.latest_complete_std_bench_surface = lambda: {
            "status": "present",
            "benchmarks": ["bench_0", "bench_1_slow", "bench_2", "bench_3"],
            "source_summary": "unit-surface",
            "run_id": "unit",
            "bench_count": 4,
            "blockers": [],
        }
        try:
            with tempfile.TemporaryDirectory() as raw_tmp:
                tmp = pathlib.Path(raw_tmp)
                audit_0 = write_mir_semantic_scope_runtime_audit_fixture(
                    tmp,
                    run_id="shard-0",
                    benchmark="bench_0",
                )
                timeout_audit = write_mir_semantic_scope_runtime_timeout_fixture(
                    tmp,
                    run_id="slow-timeout",
                    benchmarks=["bench_1_slow"],
                )
                output_dir = tmp / "plan"
                args = type(
                    "Args",
                    (),
                    {
                        "runtime_smoke_audit_paths": [str(audit_0), str(timeout_audit)],
                        "run_id": "plan",
                        "output_dir": str(output_dir),
                        "shard_count": 2,
                        "max_benchmarks": 1,
                        "max_new_shards": 2,
                        "timeout": 123,
                        "no_discover_existing": True,
                        "no_update_results": True,
                        "compatible_group_strategy": "latest",
                        "recommendation_mode": "exact-batches",
                        "exact_batch_size": 1,
                        "include_blocked_benchmarks": False,
                    },
                )()
                with contextlib.redirect_stdout(io.StringIO()):
                    rc = evaluate.plan_rustc_driver_mir_semantic_scope_runtime_shards(args)
                plan = json.loads(
                    (output_dir / "rustc-driver-mir-semantic-scope-std-bench-runtime-shard-plan.json").read_text(
                        encoding="utf-8"
                    )
                )
        finally:
            evaluate.latest_complete_std_bench_surface = old_surface
        self.assertEqual(rc, 0)
        self.assertEqual(plan["summary"]["missing_benchmark_count"], 3)
        self.assertEqual(plan["summary"]["timeout_blocked_benchmark_count"], 1)
        self.assertEqual(plan["summary"]["next_missing_benchmark"], "bench_1_slow")
        self.assertEqual(plan["summary"]["next_recommended_missing_benchmark"], "bench_2")
        self.assertIn("bench_1_slow", plan["timeout_blocked_benchmarks"])
        recommended = [
            bench
            for item in plan["recommended_shards"]
            for bench in item.get("missing_benchmarks", [])
        ]
        self.assertEqual(recommended, ["bench_2", "bench_3"])
        self.assertNotIn("bench_1_slow", recommended)


class StdBenchSourceCorrectnessAuditTests(unittest.TestCase):
    def test_direct_allocator_local_metadata_abi_statuses_are_gate_known(self) -> None:
        expected = {
            "resolved_unialloc_alloc_with_metadata_local",
            "resolved_unialloc_alloc_with_metadata_hints_local",
            "resolved_unialloc_alloc_layout_with_metadata_local",
            "resolved_unialloc_alloc_layout_with_metadata_hints_local",
            "resolved_unialloc_alloc_zeroed_layout_with_metadata_local",
            "resolved_unialloc_alloc_zeroed_layout_with_metadata_hints_local",
            "resolved_unialloc_realloc_layout_with_metadata_local",
            "resolved_unialloc_realloc_layout_with_metadata_hints_local",
            "resolved_unialloc_dealloc_layout_with_metadata_local",
            "resolved_unialloc_dealloc_layout_with_metadata_hints_local",
        }
        self.assertTrue(expected.issubset(evaluate.RUSTC_DRIVER_DIRECT_ALLOCATOR_RESOLVED_STATUSES))

    def test_direct_allocator_local_metadata_abi_keeps_size_align_recovery_backed(self) -> None:
        pass_source = (ROOT / "tools" / "unialloc-rustc-pass" / "unialloc-rustc-mir-rewrite-dry-run.rs").read_text(
            encoding="utf-8"
        )
        self.assertNotIn(
            '(DirectAllocatorCallKind::SizeAlignAlloc, false, true) => {\n'
            '            "__unialloc_alloc_with_metadata_local"',
            pass_source,
        )
        self.assertNotIn(
            '(DirectAllocatorCallKind::SizeAlignAlloc, true, true) => {\n'
            '            "__unialloc_alloc_with_metadata_hints_local"',
            pass_source,
        )
        self.assertIn(
            '(DirectAllocatorCallKind::SizeAlignAlloc, false, _) => "__unialloc_alloc_with_metadata"',
            pass_source,
        )
        self.assertIn(
            '(DirectAllocatorCallKind::SizeAlignAlloc, true, _) => {\n'
            '            "__unialloc_alloc_with_metadata_hints"',
            pass_source,
        )
        self.assertIn(
            '"recovery_backed_size_align_alloc_unpaired_dealloc"',
            pass_source,
        )
        self.assertIn(
            "direct_local_size_align_metadata_contract_violation_count",
            pass_source,
        )
        evaluate_source = (ROOT / "evaluation" / "scripts" / "evaluate.py").read_text(
            encoding="utf-8"
        )
        self.assertIn(
            "direct_size_align_alloc_recovery_contract_validated",
            evaluate_source,
        )
        self.assertIn(
            "RUSTC_DRIVER_DIRECT_ALLOCATOR_CONSERVATIVE_SIZE_ALIGN_ALLOC_SYMBOLS",
            evaluate_source,
        )
        self.assertIn("direct_allocator_rewrite_requested", evaluate_source)
        self.assertIn("--direct-allocator-rewrite", evaluate_source)
        self.assertIn("runtime_full_surface_candidate_validated", evaluate_source)
        self.assertIn("results_update_skipped", evaluate_source)

    def test_direct_allocator_size_align_local_pairing_contract_is_fail_closed(self) -> None:
        pass_source = (ROOT / "tools" / "unialloc-rustc-pass" / "unialloc-rustc-mir-rewrite-dry-run.rs").read_text(
            encoding="utf-8"
        )
        self.assertIn("--unialloc-direct-local-size-align-with-semantic-drop", pass_source)
        self.assertIn("UNIALLOC_DIRECT_LOCAL_SIZE_ALIGN_WITH_SEMANTIC_DROP", pass_source)
        self.assertIn("local_metadata_abi_semantic_drop_scope", pass_source)
        self.assertIn("semantic_scope_drop_active_metadata", pass_source)
        self.assertIn("mir-heap-object-type-v1", pass_source)
        self.assertIn("direct_local_size_align_pairing_details", pass_source)
        self.assertIn("direct_local_size_align_pairing_gap_count", pass_source)

        rows = [
            {
                "replacement_symbol": "__unialloc_alloc_with_metadata_local",
                "replacement_resolution_status": "resolved_unialloc_alloc_with_metadata_local",
                "metadata_pairing_contract": "local_metadata_abi_semantic_drop_scope",
            },
            {
                "replacement_symbol": "__unialloc_alloc_with_metadata_hints_local",
                "replacement_resolution_status": "resolved_unialloc_alloc_with_metadata_hints_local",
                "metadata_pairing_contract": "missing_or_wrong_contract",
            },
            {
                "replacement_symbol": "__unialloc_alloc_with_metadata",
                "replacement_resolution_status": "resolved_unialloc_alloc_with_metadata",
                "metadata_pairing_contract": "recovery_backed_size_align_alloc_unpaired_dealloc",
            },
        ]
        self.assertEqual(
            evaluate.rustc_driver_direct_allocator_local_size_align_contract_violation_count(rows),
            1,
        )
        alloc_rows = [
            {
                "mir_function": "probe::paired",
                "type_id": 123,
                "semantic_object_type": "std::boxed::Box<[u64; 16]>",
            },
            {
                "mir_function": "probe::unpaired",
                "type_id": 456,
                "semantic_object_type": "std::boxed::Box<[u32; 4]>",
            },
        ]
        drop_rows = [
            {
                "mir_function": "probe::paired",
                "type_id": 123,
                "semantic_object_type": "std::boxed::Box<[u64; 16]>",
            }
        ]
        gaps = evaluate.rustc_driver_direct_allocator_local_size_align_pairing_gaps(
            local_size_align_alloc_rows=alloc_rows,
            semantic_drop_scope_rows=drop_rows,
        )
        details = evaluate.rustc_driver_direct_allocator_local_size_align_pairing_details(
            local_size_align_alloc_rows=alloc_rows,
            semantic_drop_scope_rows=drop_rows,
        )
        self.assertEqual(len(details), 2)
        self.assertIn("paired", details[0])
        self.assertEqual(len(gaps), 1)
        self.assertEqual(gaps[0]["type_id"], 456)
        self.assertEqual(gaps[0]["allocation_mir_functions"], ["probe::unpaired"])
        cross_function_gaps = evaluate.rustc_driver_direct_allocator_local_size_align_pairing_gaps(
            local_size_align_alloc_rows=[
                {
                    "mir_function": "probe::allocate",
                    "type_id": 789,
                    "semantic_object_type": "std::boxed::Box<[u8; 8]>",
                }
            ],
            semantic_drop_scope_rows=[
                {
                    "mir_function": "probe::consume",
                    "type_id": 789,
                    "semantic_object_type": "std::boxed::Box<[u8; 8]>",
                }
            ],
        )
        self.assertEqual(cross_function_gaps, [])

    def test_direct_allocator_probe_status_rejects_local_size_align_pairing_gap(self) -> None:
        with tempfile.TemporaryDirectory() as raw_tmp:
            results = pathlib.Path(raw_tmp) / "results"
            results.mkdir()
            with temporary_eval_results_and_raw(results):
                audit_path = write_ready_direct_allocator_mir_probe_companion(results)
                audit = json.loads(audit_path.read_text(encoding="utf-8"))
                audit["summary"]["direct_local_size_align_pairing_gap_count"] = 1
                status = evaluate.rustc_driver_direct_allocator_mir_probe_companion_status(audit)
        self.assertFalse(status["ready"])
        self.assertIn(
            "direct allocator MIR probe reports unpaired local size/align heap-object identities",
            " | ".join(status["blockers"]),
        )

    def test_direct_allocator_probe_status_rejects_missing_local_size_align_pairing_details(self) -> None:
        with tempfile.TemporaryDirectory() as raw_tmp:
            results = pathlib.Path(raw_tmp) / "results"
            results.mkdir()
            with temporary_eval_results_and_raw(results):
                audit_path = write_ready_direct_allocator_mir_probe_companion(results)
                audit = json.loads(audit_path.read_text(encoding="utf-8"))
                audit["summary"].pop("direct_local_size_align_pairing_details")
                status = evaluate.rustc_driver_direct_allocator_mir_probe_companion_status(audit)
        self.assertFalse(status["ready"])
        self.assertIn(
            "direct allocator MIR probe does not report per-identity local size/align pairing details",
            " | ".join(status["blockers"]),
        )

    def test_direct_allocator_probe_status_rejects_missing_runtime_layout_observations(self) -> None:
        with tempfile.TemporaryDirectory() as raw_tmp:
            results = pathlib.Path(raw_tmp) / "results"
            results.mkdir()
            with temporary_eval_results_and_raw(results):
                audit_path = write_ready_direct_allocator_mir_probe_companion(results)
                audit = json.loads(audit_path.read_text(encoding="utf-8"))
                audit.pop("runtime_layout_observation_rows")
                status = evaluate.rustc_driver_direct_allocator_mir_probe_companion_status(audit)
        self.assertFalse(status["ready"])
        self.assertIn("runtime layout observation rows", " | ".join(status["blockers"]))

    def test_direct_allocator_probe_status_rejects_zero_or_mismatched_runtime_layout_observations(self) -> None:
        with tempfile.TemporaryDirectory() as raw_tmp:
            results = pathlib.Path(raw_tmp) / "results"
            results.mkdir()
            with temporary_eval_results_and_raw(results):
                audit_path = write_ready_direct_allocator_mir_probe_companion(results)
                audit = json.loads(audit_path.read_text(encoding="utf-8"))
                audit["runtime_layout_observation_rows"][0]["observed_alloc_size"] = 0
                status_zero = evaluate.rustc_driver_direct_allocator_mir_probe_companion_status(audit)

                audit = json.loads(audit_path.read_text(encoding="utf-8"))
                audit["runtime_layout_observation_rows"][0]["observed_dealloc_align"] = 16
                status_dealloc_side_ignored_for_alloc = (
                    evaluate.rustc_driver_direct_allocator_mir_probe_companion_status(audit)
                )

                audit = json.loads(audit_path.read_text(encoding="utf-8"))
                audit["runtime_layout_observation_rows"][0]["observed_alloc_align"] = 16
                status_mismatch = evaluate.rustc_driver_direct_allocator_mir_probe_companion_status(audit)
        self.assertFalse(status_zero["ready"])
        self.assertTrue(status_dealloc_side_ignored_for_alloc["ready"], status_dealloc_side_ignored_for_alloc["blockers"])
        self.assertFalse(status_mismatch["ready"])
        self.assertIn("does not match raw alloc size/align operands", " | ".join(status_zero["blockers"]))
        self.assertIn("does not match raw alloc size/align operands", " | ".join(status_mismatch["blockers"]))

    def test_direct_allocator_probe_status_rejects_pass_rows_without_size_align_operands(self) -> None:
        with tempfile.TemporaryDirectory() as raw_tmp:
            results = pathlib.Path(raw_tmp) / "results"
            results.mkdir()
            with temporary_eval_results_and_raw(results):
                audit_path = write_ready_direct_allocator_mir_probe_companion(results)
                audit = json.loads(audit_path.read_text(encoding="utf-8"))
                rewrite_path = pathlib.Path(audit["target_rewrite"]["path"])
                rewrite = json.loads(rewrite_path.read_text(encoding="utf-8"))
                rewrite["rewrite_candidates"][0].pop("size_operand")
                rewrite["rewrite_candidates"][0].pop("align_operand")
                write_json(rewrite_path, rewrite)
                status = evaluate.rustc_driver_direct_allocator_mir_probe_companion_status(audit)
        self.assertFalse(status["ready"])
        self.assertIn("no applied direct allocator row with concrete size/align operands", " | ".join(status["blockers"]))

    def test_direct_allocator_probe_status_checks_every_applied_metadata_abi_row(self) -> None:
        with tempfile.TemporaryDirectory() as raw_tmp:
            results = pathlib.Path(raw_tmp) / "results"
            results.mkdir()
            with temporary_eval_results_and_raw(results):
                audit_path = write_ready_direct_allocator_mir_probe_companion(results)
                audit = json.loads(audit_path.read_text(encoding="utf-8"))
                rewrite_path = pathlib.Path(audit["target_rewrite"]["path"])
                rewrite = json.loads(rewrite_path.read_text(encoding="utf-8"))
                second = dict(rewrite["rewrite_candidates"][0])
                second.update(
                    {
                        "source_span": "unialloc/src/bin/rustc_driver_direct_allocator_mir_probe.rs:99:1: 99:8",
                        "type_id": 0xC002DA7B,
                        "callsite": 0xA111,
                        "size_operand": 128,
                        "align_operand": 16,
                    }
                )
                rewrite["rewrite_candidates"].append(second)
                write_json(rewrite_path, rewrite)
                status_missing_runtime = evaluate.rustc_driver_direct_allocator_mir_probe_companion_status(audit)

                second.pop("size_operand")
                second.pop("align_operand")
                rewrite["rewrite_candidates"][1] = second
                write_json(rewrite_path, rewrite)
                status_missing_operands = evaluate.rustc_driver_direct_allocator_mir_probe_companion_status(audit)

        self.assertFalse(status_missing_runtime["ready"])
        self.assertIn("no runtime layout observation matching", " | ".join(status_missing_runtime["blockers"]))
        self.assertFalse(status_missing_operands["ready"])
        self.assertIn("lacks non-empty size/align operands", " | ".join(status_missing_operands["blockers"]))

    def test_direct_allocator_probe_status_rejects_dealloc_row_with_only_alloc_observation(self) -> None:
        with tempfile.TemporaryDirectory() as raw_tmp:
            results = pathlib.Path(raw_tmp) / "results"
            results.mkdir()
            with temporary_eval_results_and_raw(results):
                audit_path = write_ready_direct_allocator_mir_probe_companion(results)
                audit = json.loads(audit_path.read_text(encoding="utf-8"))
                rewrite_path = pathlib.Path(audit["target_rewrite"]["path"])
                rewrite = json.loads(rewrite_path.read_text(encoding="utf-8"))
                rewrite["rewrite_candidates"][0]["replacement_symbol"] = (
                    "__unialloc_dealloc_layout_with_metadata_local"
                )
                write_json(rewrite_path, rewrite)
                audit["runtime_layout_observation_rows"][0].update(
                    {
                        "allocations": 1,
                        "allocated_bytes": 64,
                        "observed_alloc_size": 64,
                        "observed_alloc_align": 8,
                        "deallocations": 0,
                        "observed_dealloc_size": 0,
                        "observed_dealloc_align": 0,
                    }
                )
                status = evaluate.rustc_driver_direct_allocator_mir_probe_companion_status(audit)

        self.assertFalse(status["ready"])
        blockers = " | ".join(status["blockers"])
        self.assertIn("raw dealloc size/align operands", blockers)
        self.assertIn("unmatched or invalid runtime layout provenance rows", blockers)

    def test_direct_allocator_probe_status_checks_numeric_operands_on_kind_specific_side(self) -> None:
        with tempfile.TemporaryDirectory() as raw_tmp:
            results = pathlib.Path(raw_tmp) / "results"
            results.mkdir()
            with temporary_eval_results_and_raw(results):
                audit_path = write_ready_direct_allocator_mir_probe_companion(results)
                audit = json.loads(audit_path.read_text(encoding="utf-8"))
                rewrite_path = pathlib.Path(audit["target_rewrite"]["path"])
                rewrite = json.loads(rewrite_path.read_text(encoding="utf-8"))

                alloc_audit = copy.deepcopy(audit)
                alloc_audit["runtime_layout_observation_rows"][0]["observed_alloc_size"] = 63
                alloc_audit["runtime_layout_observation_rows"][0]["observed_dealloc_size"] = 64
                status_alloc_mismatch = evaluate.rustc_driver_direct_allocator_mir_probe_companion_status(
                    alloc_audit
                )

                dealloc_rewrite = copy.deepcopy(rewrite)
                dealloc_rewrite["rewrite_candidates"][0]["replacement_symbol"] = (
                    "__unialloc_dealloc_layout_with_metadata_local"
                )
                write_json(rewrite_path, dealloc_rewrite)
                dealloc_audit = copy.deepcopy(audit)
                dealloc_audit["runtime_layout_observation_rows"][0]["observed_alloc_size"] = 63
                dealloc_audit["runtime_layout_observation_rows"][0]["observed_dealloc_size"] = 64
                status_dealloc_alloc_side_ignored = (
                    evaluate.rustc_driver_direct_allocator_mir_probe_companion_status(dealloc_audit)
                )
                dealloc_audit["runtime_layout_observation_rows"][0]["observed_dealloc_size"] = 63
                status_dealloc_mismatch = evaluate.rustc_driver_direct_allocator_mir_probe_companion_status(
                    dealloc_audit
                )

                realloc_rewrite = copy.deepcopy(rewrite)
                realloc_rewrite["rewrite_candidates"][0]["replacement_symbol"] = (
                    "__unialloc_realloc_layout_with_metadata_local"
                )
                write_json(rewrite_path, realloc_rewrite)
                realloc_audit = copy.deepcopy(audit)
                realloc_audit["runtime_layout_observation_rows"][0].update(
                    {
                        "observed_alloc_size": 128,
                        "observed_alloc_align": 8,
                        "observed_dealloc_size": 64,
                        "observed_dealloc_align": 8,
                    }
                )
                status_realloc_new_alloc_size_ignored = (
                    evaluate.rustc_driver_direct_allocator_mir_probe_companion_status(realloc_audit)
                )
                realloc_audit["runtime_layout_observation_rows"][0]["observed_dealloc_size"] = 63
                status_realloc_old_size_mismatch = (
                    evaluate.rustc_driver_direct_allocator_mir_probe_companion_status(realloc_audit)
                )
                realloc_audit["runtime_layout_observation_rows"][0]["observed_dealloc_size"] = 64
                realloc_audit["runtime_layout_observation_rows"][0]["observed_alloc_align"] = 16
                status_realloc_align_mismatch = (
                    evaluate.rustc_driver_direct_allocator_mir_probe_companion_status(realloc_audit)
                )

        self.assertFalse(status_alloc_mismatch["ready"])
        self.assertIn("raw alloc size/align operands", " | ".join(status_alloc_mismatch["blockers"]))
        self.assertTrue(status_dealloc_alloc_side_ignored["ready"], status_dealloc_alloc_side_ignored["blockers"])
        self.assertFalse(status_dealloc_mismatch["ready"])
        self.assertIn("raw dealloc size/align operands", " | ".join(status_dealloc_mismatch["blockers"]))
        self.assertTrue(status_realloc_new_alloc_size_ignored["ready"], status_realloc_new_alloc_size_ignored["blockers"])
        self.assertFalse(status_realloc_old_size_mismatch["ready"])
        self.assertIn("raw realloc size/align operands", " | ".join(status_realloc_old_size_mismatch["blockers"]))
        self.assertFalse(status_realloc_align_mismatch["ready"])
        self.assertIn("raw realloc size/align operands", " | ".join(status_realloc_align_mismatch["blockers"]))

    def test_direct_allocator_probe_status_accepts_recovery_backed_realloc_split_runtime_evidence(self) -> None:
        with tempfile.TemporaryDirectory() as raw_tmp:
            results = pathlib.Path(raw_tmp) / "results"
            results.mkdir()
            with temporary_eval_results_and_raw(results):
                audit_path = write_ready_direct_allocator_mir_probe_companion(results)
                audit = json.loads(audit_path.read_text(encoding="utf-8"))
                rewrite_path = pathlib.Path(audit["target_rewrite"]["path"])
                rewrite = json.loads(rewrite_path.read_text(encoding="utf-8"))

                old_alloc = copy.deepcopy(rewrite["rewrite_candidates"][0])
                old_alloc.update(
                    {
                        "replacement_symbol": "__unialloc_alloc_layout_with_metadata",
                        "metadata_pairing_contract": "recovery_backed_layout_metadata_abi",
                        "type_id": 0xC002DA7A,
                        "module_id": evaluate.RUSTC_DRIVER_LOWERING_MODULE_ID,
                        "callsite": 0xA110,
                        "size_operand": 64,
                        "align_operand": 8,
                    }
                )
                realloc = copy.deepcopy(old_alloc)
                realloc.update(
                    {
                        "replacement_symbol": "__unialloc_realloc_layout_with_metadata",
                        "callsite": 0xA111,
                        "size_operand": 64,
                        "align_operand": 8,
                    }
                )
                rewrite["rewrite_candidates"] = [old_alloc, realloc]
                write_json(rewrite_path, rewrite)

                audit["runtime_layout_observation_rows"] = [
                    {
                        "type_id": 0xC002DA7A,
                        "module_id": evaluate.RUSTC_DRIVER_LOWERING_MODULE_ID,
                        "callsite": 0xA110,
                        "allocations": 1,
                        "deallocations": 1,
                        "allocated_bytes": 64,
                        "observed_alloc_size": 64,
                        "observed_alloc_align": 8,
                        "observed_dealloc_size": 64,
                        "observed_dealloc_align": 8,
                    },
                    {
                        "type_id": 0xC002DA7A,
                        "module_id": evaluate.RUSTC_DRIVER_LOWERING_MODULE_ID,
                        "callsite": 0xA111,
                        "allocations": 1,
                        "deallocations": 0,
                        "allocated_bytes": 128,
                        "observed_alloc_size": 128,
                        "observed_alloc_align": 8,
                        "observed_dealloc_size": 0,
                        "observed_dealloc_align": 0,
                    },
                ]
                status_split_realloc = evaluate.rustc_driver_direct_allocator_mir_probe_companion_status(
                    audit
                )

                missing_old_dealloc = copy.deepcopy(audit)
                missing_old_dealloc["runtime_layout_observation_rows"][0].update(
                    {
                        "deallocations": 0,
                        "observed_dealloc_size": 0,
                        "observed_dealloc_align": 0,
                    }
                )
                status_missing_old_dealloc = evaluate.rustc_driver_direct_allocator_mir_probe_companion_status(
                    missing_old_dealloc
                )

                old_dealloc_mismatch = copy.deepcopy(audit)
                old_dealloc_mismatch["runtime_layout_observation_rows"][0]["observed_dealloc_size"] = 32
                status_old_dealloc_mismatch = evaluate.rustc_driver_direct_allocator_mir_probe_companion_status(
                    old_dealloc_mismatch
                )

        self.assertTrue(status_split_realloc["ready"], status_split_realloc["blockers"])
        self.assertFalse(status_missing_old_dealloc["ready"])
        self.assertIn("raw realloc", " | ".join(status_missing_old_dealloc["blockers"]))
        self.assertFalse(status_old_dealloc_mismatch["ready"])
        self.assertIn("raw realloc size/align operands", " | ".join(status_old_dealloc_mismatch["blockers"]))

    def test_direct_allocator_probe_status_requires_runtime_probe_false_when_layout_provenance_blocks(self) -> None:
        with tempfile.TemporaryDirectory() as raw_tmp:
            results = pathlib.Path(raw_tmp) / "results"
            results.mkdir()
            with temporary_eval_results_and_raw(results):
                audit_path = write_ready_direct_allocator_mir_probe_companion(results)
                audit = json.loads(audit_path.read_text(encoding="utf-8"))
                audit["summary"]["runtime_probe_validated"] = False
                audit["summary"]["runtime_layout_provenance_validated"] = False
                audit["summary"]["runtime_layout_provenance_blockers"] = [
                    "direct allocator MIR probe has no runtime layout observation rows"
                ]
                audit.pop("runtime_layout_observation_rows")
                status = evaluate.rustc_driver_direct_allocator_mir_probe_companion_status(audit)
        self.assertFalse(status["ready"])
        blockers = " | ".join(status["blockers"])
        self.assertIn("runtime layout observation rows", blockers)
        self.assertIn("not runtime_probe_validated", blockers)

    def test_direct_allocator_probe_status_rejects_self_attesting_summary_without_source_rows(self) -> None:
        with tempfile.TemporaryDirectory() as raw_tmp:
            results = pathlib.Path(raw_tmp) / "results"
            results.mkdir()
            with temporary_eval_results_and_raw(results):
                audit_path = write_ready_direct_allocator_mir_probe_companion(results)
                audit = json.loads(audit_path.read_text(encoding="utf-8"))
                audit["target_rewrite"].pop("path")
                status = evaluate.rustc_driver_direct_allocator_mir_probe_companion_status(audit)
        self.assertFalse(status["ready"])
        blockers = " | ".join(status["blockers"])
        self.assertIn("target_rewrite does not reference a raw rewrite-map artifact", blockers)
        self.assertIn("no raw rewrite-map rows for layout provenance", blockers)

    def test_direct_allocator_probe_status_accepts_recovery_backed_failclosed_size_align(self) -> None:
        with tempfile.TemporaryDirectory() as raw_tmp:
            results = pathlib.Path(raw_tmp) / "results"
            results.mkdir()
            with temporary_eval_results_and_raw(results):
                audit_path = write_ready_direct_allocator_mir_probe_companion(results)
                audit = json.loads(audit_path.read_text(encoding="utf-8"))
                audit["summary"]["direct_local_size_align_alloc_rewrite_applied_count"] = 0
                audit["summary"]["direct_local_size_align_semantic_drop_scope_count"] = 0
                audit["summary"]["direct_local_size_align_pairing_details"] = []
                audit["summary"]["direct_size_align_recovery_backed_required_count"] = 1
                audit["summary"]["direct_conservative_size_align_alloc_rewrite_applied_count"] = 1
                audit["summary"]["direct_replacement_resolution_status"] = (
                    "resolved_unialloc_alloc_with_metadata"
                )
                status = evaluate.rustc_driver_direct_allocator_mir_probe_companion_status(audit)
        self.assertTrue(status["ready"], status["blockers"])

    def test_direct_allocator_probe_status_accepts_current_layout_only_probe_without_exchange_malloc(self) -> None:
        with tempfile.TemporaryDirectory() as raw_tmp:
            results = pathlib.Path(raw_tmp) / "results"
            results.mkdir()
            with temporary_eval_results_and_raw(results):
                audit_path = write_ready_direct_allocator_mir_probe_companion(results)
                audit = json.loads(audit_path.read_text(encoding="utf-8"))
                audit["summary"]["direct_box_shallow_init_candidate_count"] = 0
                audit["summary"]["direct_box_shallow_init_rewrite_applied_count"] = 0
                audit["summary"]["direct_box_shallow_init_type_mapping_record_count"] = 0
                audit["summary"]["direct_local_size_align_alloc_rewrite_applied_count"] = 0
                audit["summary"]["direct_local_size_align_semantic_drop_scope_count"] = 0
                audit["summary"]["direct_local_size_align_pairing_details"] = []
                audit["summary"]["direct_size_align_recovery_backed_required_count"] = 0
                status = evaluate.rustc_driver_direct_allocator_mir_probe_companion_status(audit)
        self.assertTrue(status["ready"], status["blockers"])

    def test_direct_allocator_probe_status_requires_result_option_passthrough_layout_provenance(self) -> None:
        with tempfile.TemporaryDirectory() as raw_tmp:
            results = pathlib.Path(raw_tmp) / "results"
            results.mkdir()
            with temporary_eval_results_and_raw(results):
                audit_path = write_ready_direct_allocator_mir_probe_companion(results)
                audit = json.loads(audit_path.read_text(encoding="utf-8"))
                audit["summary"]["direct_layout_result_option_passthrough_rewrite_applied_count"] = 0
                audit["summary"]["direct_layout_result_option_passthrough_type_mapping_record_count"] = 0
                status = evaluate.rustc_driver_direct_allocator_mir_probe_companion_status(audit)
        self.assertFalse(status["ready"])
        blockers = " | ".join(status["blockers"])
        self.assertIn("Result<Layout>::ok", blockers)
        self.assertIn("Option<Layout>::expect/unwrap", blockers)

    def test_rustc_driver_direct_allocator_mir_probe_has_no_manual_metadata_calls(self) -> None:
        self.assertEqual(
            evaluate.RUSTC_DRIVER_DIRECT_ALLOCATOR_MIR_PROBE_EXAMPLE,
            "rustc_driver_direct_allocator_mir_probe",
        )
        source_path = (
            ROOT
            / "unialloc"
            / "src"
            / "bin"
            / f"{evaluate.RUSTC_DRIVER_DIRECT_ALLOCATOR_MIR_PROBE_EXAMPLE}.rs"
        )
        source = source_path.read_text(encoding="utf-8")
        self.assertIn("#![feature(layout_for_ptr)]", source)
        self.assertIn("Box::new([0u64; 16])", source)
        self.assertNotIn("box [0u64; 16]", source)
        self.assertIn("runtime_len = 6usize + (round % 3)", source)
        self.assertIn("Layout::array::<u32>(runtime_len)", source)
        self.assertIn(".align_to(64)", source)
        self.assertIn(".pad_to_align()", source)
        self.assertIn(".align_to(32)", source)
        self.assertIn(".ok()", source)
        self.assertIn("valid option-wrapped transformed layout", source)
        self.assertIn(".extend(Layout::new::<u64>())", source)
        self.assertIn(".repeat(5)", source)
        self.assertIn("Layout::from_size_align", source)
        self.assertIn("source_layout.size()", source)
        self.assertIn("source_layout.align()", source)
        self.assertIn("size_of::<[u16; 11]>()", source)
        self.assertIn("align_of::<[u16; 11]>()", source)
        self.assertIn(".extend_packed(Layout::new::<u64>())", source)
        self.assertIn(".repeat_packed(4)", source)
        self.assertIn("Layout::for_value_raw", source)
        self.assertIn("raw_slice: *const [u64]", source)
        self.assertIn("manual_metadata_abi_calls", source)
        self.assertIn("semantic_auto_metadata_disable", source)
        self.assertNotIn("__unialloc_alloc_with_metadata", source)
        self.assertNotIn("__unialloc_dealloc_with_metadata", source)
        self.assertNotIn("__unialloc_semantic_scope_enter", source)
        self.assertNotIn("__unialloc_semantic_scope_exit", source)
        self.assertNotIn("semantic_auto_metadata_enable(", source)

    def test_rustc_driver_semantic_scope_probe_has_no_manual_scope_calls(self) -> None:
        self.assertEqual(
            evaluate.RUSTC_DRIVER_MIR_SEMANTIC_SCOPE_PROBE_EXAMPLE,
            "rustc_driver_mir_semantic_scope_probe",
        )
        source_path = (
            ROOT
            / "unialloc"
            / "src"
            / "bin"
            / f"{evaluate.RUSTC_DRIVER_MIR_SEMANTIC_SCOPE_PROBE_EXAMPLE}.rs"
        )
        source = source_path.read_text(encoding="utf-8")
        self.assertIn("Vec::with_capacity", source)
        self.assertIn("BTreeMap::new", source)
        self.assertIn("HashMap::with_capacity", source)
        self.assertIn("HashSet::with_capacity", source)
        self.assertIn("HashMap::from", source)
        self.assertIn("HashSet::from", source)
        self.assertIn("Rc::new", source)
        self.assertIn("Arc::new", source)
        self.assertIn("PathBuf::from", source)
        self.assertIn("OsString::from", source)
        self.assertIn("mutate_pathbuf_receiver", source)
        self.assertIn("mutate_os_string_receiver", source)
        self.assertIn(".push(\"nialloc-mir-semantic-scope-pathbuf", source)
        self.assertIn(".push(\"nialloc-mir-semantic-scope-osstring", source)
        self.assertIn("CString::new", source)
        self.assertIn("call_vec_factory", source)
        self.assertIn("make_indirect_vec", source)
        self.assertIn("make_optional_vec", source)
        self.assertIn("make_result_vec", source)
        self.assertIn("make_wrapped_vec", source)
        self.assertIn("make_pinned_box", source)
        self.assertIn("HeapOwner", source)
        self.assertIn("Pin<Box", source)
        self.assertIn("semantic_auto_metadata_disable", source)
        self.assertIn("compiler_site_id_stream_mode", source)
        self.assertNotIn("__unialloc_semantic_scope_enter", source)
        self.assertNotIn("__unialloc_semantic_scope_exit", source)
        self.assertNotIn("__unialloc_alloc_with_metadata", source)
        self.assertNotIn("semantic_auto_metadata_enable(", source)

    def test_rustc_driver_cross_thread_hint_probe_has_no_manual_metadata_calls(self) -> None:
        self.assertEqual(
            evaluate.RUSTC_DRIVER_MIR_CROSS_THREAD_HINT_PROBE_EXAMPLE,
            "rustc_driver_mir_cross_thread_hint_probe",
        )
        source_path = ROOT / "unialloc" / "src" / "bin" / "rustc_driver_mir_cross_thread_hint_probe.rs"
        source = source_path.read_text(encoding="utf-8")
        self.assertIn("thread::spawn", source)
        self.assertIn("Vec::with_capacity", source)
        self.assertIn("VecDeque::with_capacity", source)
        self.assertIn("Arc::new", source)
        self.assertIn("same_thread_direct_allocator_probe", source)
        self.assertIn("Layout::new::<[u8; 24]>()", source)
        self.assertIn("must not inherit the cross-thread recovery placement", source)
        self.assertIn("semantic_auto_metadata_disable", source)
        self.assertIn("semantic_stats_snapshot", source)
        self.assertIn("semantic_fallback_attribution_snapshot", source)
        self.assertIn("lowered_deallocation_rows", source)
        self.assertIn("cross-thread MIR hint rewrite did not produce typed allocations", source)
        self.assertNotIn("__unialloc_semantic_scope_enter", source)
        self.assertNotIn("__unialloc_semantic_scope_exit", source)
        self.assertNotIn("__unialloc_alloc_with_metadata", source)
        self.assertNotIn("semantic_auto_metadata_enable(", source)

    def test_rustc_driver_mir_rewrite_map_preserves_zero_direct_count(self) -> None:
        with tempfile.TemporaryDirectory() as raw_tmp:
            tmp = pathlib.Path(raw_tmp)
            write_json(
                tmp / "probe.json",
                {
                    "rustc_args": ["--crate-name", "probe"],
                    "summary": {
                        "rewrite_candidate_count": 1,
                        "direct_allocator_call_site_count": 0,
                        "semantic_scope_candidate_count": 1,
                        "semantic_scope_unsolved_candidate_count": 2,
                        "semantic_scope_drop_generic_type_parameter_skipped_count": 3,
                        "semantic_scope_drop_non_heap_skipped_count": 4,
                        "rewrite_applied_count": 0,
                        "semantic_scope_rewrite_applied_count": 0,
                        "cross_thread_recovery_hint_count": 1,
                    },
                    "rewrite_candidates": [
                        {
                            "lowering_kind": "semantic_scope_enter_exit_rewrite",
                            "rewrite_status": "semantic_scope_enter_exit_rewrite_planned",
                            "cross_thread_recovery_hint": True,
                            "placement_hint": 32768,
                            "placement_hint_basis": "auto_cross_thread_escape",
                        }
                    ],
                },
            )

            [entry] = evaluate.collect_rustc_driver_mir_rewrite_dry_run_maps(tmp)

        self.assertEqual(entry["rewrite_candidate_count"], 1)
        self.assertEqual(entry["direct_allocator_call_site_count"], 0)
        self.assertEqual(entry["semantic_scope_candidate_count"], 1)
        self.assertEqual(entry["semantic_scope_unsolved_candidate_count"], 2)
        self.assertEqual(entry["semantic_scope_drop_generic_type_parameter_skipped_count"], 3)
        self.assertEqual(entry["semantic_scope_drop_non_heap_skipped_count"], 4)
        self.assertEqual(entry["cross_thread_recovery_hint_count"], 1)
        self.assertTrue(entry["sample_rewrite_candidates"][0]["cross_thread_recovery_hint"])

    def test_rustc_driver_mir_rewrite_pass_uses_rustc_middle_heap_type_solver(self) -> None:
        source_path = ROOT / "tools" / "unialloc-rustc-pass" / "unialloc-rustc-mir-rewrite-dry-run.rs"
        source = source_path.read_text(encoding="utf-8")

        self.assertIn("ty::Adt", source)
        self.assertIn("heap_object_type_from_ty", source)
        self.assertIn("heap_object_type_from_ty_inner", source)
        self.assertIn(".as_closure()", source)
        self.assertIn(".as_generator()", source)
        self.assertIn(".upvar_tys()", source)
        self.assertIn("HEAP_OBJECT_SUPPORT", source)
        self.assertIn("owned_return_markers", source)
        self.assertIn("supported_heap_adt_def_path", source)
        self.assertIn("std::collections::HashMap<", source)
        self.assertIn("std::collections::hash::map::HashMap", source)
        self.assertIn(") -> std::collections::HashMap", source)
        self.assertIn("std::collections::HashSet<", source)
        self.assertIn(") -> std::collections::HashSet", source)
        self.assertIn("std::rc::Rc<", source)
        self.assertIn("alloc::rc::Rc", source)
        self.assertIn(") -> std::rc::Rc", source)
        self.assertIn("std::sync::Arc<", source)
        self.assertIn("alloc::sync::Arc", source)
        self.assertIn(") -> std::sync::Arc", source)
        self.assertIn("std::path::PathBuf", source)
        self.assertIn(") -> std::path::PathBuf", source)
        self.assertIn("std::ffi::OsString", source)
        self.assertIn("std::ffi::os_str::OsString", source)
        self.assertIn("owned-buffer push::<...>", source)
        self.assertIn('"::push::<"', source)
        self.assertIn("std::ffi::CString", source)
        self.assertIn("alloc::ffi::c_str::CString", source)
        self.assertIn("semantic_scope_candidate_for_mir", source)
        self.assertIn("heap_object_type_from_ty(tcx, destination_ty)", source)
        self.assertIn("substs", source)
        self.assertIn(".types()", source)
        self.assertIn("adt.variants()", source)
        self.assertIn("field.ty(tcx, substs)", source)
        self.assertIn("semantic_scope_unsolved_heap_object_candidate", source)
        self.assertIn("rustc_middle_heap_object_type_not_solved", source)
        self.assertIn("type_contains_generic_param", source)
        self.assertIn("semantic_scope_drop_generic_type_parameter_skipped", source)
        self.assertIn("rustc_middle_drop_generic_type_parameter_not_lowered", source)
        self.assertIn("semantic_scope_drop_non_heap_object_skipped", source)
        self.assertIn("rustc_middle_drop_non_heap_object_type_not_lowered", source)
        self.assertIn("layout_composite_heap_object_solution", source)
        self.assertIn("fallible_layout_passthrough_heap_object_solution", source)
        self.assertIn("layout_reconstructed_heap_object_solution", source)
        self.assertIn("extract_layout_angle_argument", source)
        self.assertIn("extract_mem_angle_argument", source)
        self.assertIn("layout_size_align_operand_heap_object_solution", source)
        self.assertIn("Only literal constants", source)
        self.assertIn("<runtime-len>", source)
        self.assertIn("__unialloc_alloc_layout_with_metadata_local", source)
        self.assertIn("__unialloc_realloc_layout_with_metadata_local", source)
        self.assertIn("__unialloc_dealloc_layout_with_metadata_local", source)
        self.assertIn("recovery_backed_size_align_alloc_unpaired_dealloc", source)
        self.assertIn("place_parent_for_tuple_layout_field_zero", source)
        self.assertIn("rustc_middle_mir_layout_reconstructed_heap_object_type", source)
        self.assertIn("rustc_middle_mir_layout_size_align_constructor_heap_object_type", source)
        self.assertIn("rustc_middle_mir_layout_composite_heap_object_type", source)
        self.assertIn("rustc_middle_mir_layout_result_option_passthrough_heap_object_type", source)
        self.assertIn("std::option::Option", source)
        self.assertIn("Result<Layout>::ok", source)
        self.assertIn("Option<Layout>::expect/unwrap", source)
        self.assertIn("Layout::for_value_raw", source)
        self.assertIn("Layout::extend_packed", source)
        self.assertIn("Layout::repeat_packed", source)
        self.assertIn("semantic_scope_layout_value_call", source)
        self.assertIn("callee_mentions_rust_layout_method", source)
        self.assertIn('"from_size_align_unchecked"', source)
        self.assertIn('"pad_to_align"', source)
        self.assertIn("semantic_scope_layout_value_call(callee)", source)
        self.assertIn("UNIALLOC_LOWERING_AUTO_CROSS_THREAD_HINT", source)
        self.assertIn("UNIALLOC_CONTINUE_COMPILATION", source)
        self.assertIn("PLACEMENT_HINT_CROSS_THREAD_RECOVERY", source)
        self.assertIn("manual_cross_thread_recovery_hint", source)
        self.assertIn("auto_cross_thread_escape", source)
        self.assertIn("body_contains_cross_thread_escape_call", source)
        self.assertIn("body_cross_thread_escape_heap_object_types", source)
        self.assertIn("semantic_object_needs_cross_thread_recovery_hint", source)
        self.assertIn("cross_thread_escape_heap_object_types.contains(semantic_object_type)", source)
        self.assertGreaterEqual(
            source.count("let cross_thread_escape_heap_object_types = if cross_thread_escape"),
            3,
        )
        self.assertIn("std::thread::spawn", source)
        self.assertIn('"test::Bencher"', source)
        self.assertIn('"::insert}"', source)
        self.assertNotIn('"::insert",', source)

    def test_generated_compiler_bench_fallback_source_is_real_probe(self) -> None:
        for target in (
            "std_bench_compiler_proto_generated",
            "std_bench_compiler_exact_dynamic_generated",
        ):
            source = evaluate.compiler_generated_bench_fallback_source(target)
            self.assertNotIn("placeholder", source.lower())
            self.assertNotIn("black_box(())", source)
            self.assertIn("#[test]", source)
            self.assertIn("semantic_type_stats_snapshot", source)
            if target.endswith("exact_dynamic_generated"):
                self.assertIn("__unialloc_alloc_with_metadata", source)
                self.assertIn("__unialloc_dealloc_with_metadata", source)
            else:
                self.assertIn("__unialloc_semantic_scope_enter", source)
                self.assertIn("__unialloc_semantic_scope_exit", source)

    def test_generated_compiler_cleanup_restores_previous_target_source(self) -> None:
        with tempfile.TemporaryDirectory() as raw_tmp:
            tmp = pathlib.Path(raw_tmp)
            target = tmp / "std_bench_compiler_proto_generated.rs"
            module_dir = tmp / "std_bench_compiler_proto_generated_modules"
            module_dir.mkdir()
            (module_dir / "generated.rs").write_text("// generated module\n", encoding="utf-8")
            target.write_text("// temporary generated source\n", encoding="utf-8")
            restore_source = "// previous real probe source\n"
            evaluate._GENERATED_COMPILER_BENCH_RESTORE_SOURCES[str(target)] = restore_source

            evaluate.cleanup_generated_compiler_proto(target, module_dir, keep=False)

            self.assertEqual(target.read_text(encoding="utf-8"), restore_source)
            self.assertFalse(module_dir.exists())
            self.assertNotIn(str(target), evaluate._GENERATED_COMPILER_BENCH_RESTORE_SOURCES)

    def test_registered_generated_compiler_benches_are_real_probes(self) -> None:
        with tempfile.TemporaryDirectory() as raw_tmp:
            tmp = pathlib.Path(raw_tmp)
            args = type(
                "Args",
                (),
                {
                    "run_id": "generated-bench-source-correctness",
                    "output_dir": str(tmp),
                    "no_update_results": True,
                },
            )()
            with contextlib.redirect_stdout(io.StringIO()):
                rc = evaluate.audit_std_bench_source_correctness(args)
            audit = json.loads((tmp / "std_bench_source_correctness_audit.json").read_text())

        self.assertEqual(rc, 0)
        self.assertEqual(audit["summary"]["status"], "pass")
        self.assertGreaterEqual(audit["summary"]["generated_compiler_bench_checked_count"], 2)
        self.assertEqual(audit["summary"]["generated_compiler_bench_placeholder_count"], 0)
        self.assertIn(
            "unialloc/benches/std_bench_compiler_proto_generated.rs",
            audit["generated_compiler_bench_sources"],
        )
        self.assertIn(
            "unialloc/benches/std_bench_compiler_exact_dynamic_generated.rs",
            audit["generated_compiler_bench_sources"],
        )

    def test_std_bench_auto_coverage_uses_and_cleans_run_local_cargo_target(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            results = pathlib.Path(tmpdir) / "results"
            with temporary_eval_results_and_raw(results) as raw:
                run_id = "std-bench-auto-cleanup-unit"

                def fake_run(cmd, cwd, env, text, stdout, stderr, timeout):
                    target_dir = pathlib.Path(env["CARGO_TARGET_DIR"])
                    self.assertEqual(target_dir, raw / run_id / "cargo-target")
                    target_dir.mkdir(parents=True, exist_ok=True)
                    (target_dir / "object.o").write_bytes(b"x" * 17)
                    event = {
                        "source": "std_bench_auto_metadata",
                        "event": "typed_allocations",
                        "typed": True,
                        "count": 1,
                        "bytes": 64,
                        "deallocations": 1,
                    }
                    return types.SimpleNamespace(
                        returncode=0,
                        stdout=json.dumps(event, sort_keys=True) + "\n",
                        stderr="",
                    )

                args = types.SimpleNamespace(
                    run_id=run_id,
                    timeout=60,
                    features="bench_ourself,stats",
                    bench_filter=None,
                    skip=[],
                    compiler_site_replay_type_mapping=None,
                    compiler_site_replay_limit=4096,
                    compiler_site_id_mode="cyclic-replay",
                    no_import_results=True,
                    require_hugepage_success=False,
                    only_with_sentinels=None,
                    keep_cargo_target_dir=False,
                )
                with mock.patch.object(evaluate.shutil, "which", return_value="/usr/bin/cargo"), mock.patch.object(
                    evaluate.subprocess, "run", side_effect=fake_run
                ), mock.patch.object(
                    evaluate, "host_metadata", return_value={"system": "unit-test"}
                ), contextlib.redirect_stdout(io.StringIO()):
                    rc = evaluate.collect_std_bench_auto_coverage(args)
                summary = json.loads(
                    (raw / run_id / "std-bench-auto-summary.json").read_text(encoding="utf-8")
                )

        self.assertEqual(rc, 0)
        self.assertTrue(summary["cargo_target_dir_owned_by_runner"])
        self.assertTrue(summary["cargo_target_cleaned"])
        self.assertEqual(summary["cargo_target_removed_bytes"], 17)
        self.assertFalse(pathlib.Path(summary["cargo_target_dir"]).exists())
        self.assertEqual(summary["cargo_target_cleanup"]["policy"], "owned-run-local-cleanup")

    def test_direct_metadata_probe_runners_cleanup_run_local_cargo_targets(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            results = pathlib.Path(tmpdir) / "results"
            with temporary_eval_results_and_raw(results) as raw:

                def fake_platform_run(out_dir, label, cmd, timeout, *, extra_env=None):
                    target_dir = pathlib.Path((extra_env or {})["CARGO_TARGET_DIR"])
                    self.assertEqual(target_dir, pathlib.Path(out_dir) / "cargo-target")
                    target_dir.mkdir(parents=True, exist_ok=True)
                    (target_dir / f"{label}.o").write_bytes(b"y" * 19)
                    source = (
                        "pac_metadata_probe"
                        if label == "pac-metadata-direct-probe"
                        else "hugepage_metadata_probe"
                    )
                    event = {
                        "source": source,
                        "passed": True,
                        "allocator_validated": True,
                        "typed_cache_inserts": 1,
                        "typed_cache_hits": 1,
                        "side_cache_allocated": True,
                        "reused_same_ptr": True,
                    }
                    stdout_path = pathlib.Path(out_dir) / f"{label}.stdout.txt"
                    stderr_path = pathlib.Path(out_dir) / f"{label}.stderr.txt"
                    stdout_path.write_text(json.dumps(event, sort_keys=True) + "\n", encoding="utf-8")
                    stderr_path.write_text("", encoding="utf-8")
                    return {
                        "command": cmd,
                        "stdout": str(stdout_path),
                        "stderr": str(stderr_path),
                        "exit_code": 0,
                        "passed": True,
                    }

                pac_args = types.SimpleNamespace(
                    run_id="pac-cleanup-unit",
                    output_dir=None,
                    toolchain="nightly-2022-07-01",
                    features="pac,stats",
                    timeout=60,
                    no_update_results=True,
                )
                huge_args = types.SimpleNamespace(
                    run_id="hugepage-cleanup-unit",
                    output_dir=None,
                    toolchain="nightly-2022-07-01",
                    features="hugepage,stats",
                    timeout=60,
                    no_update_results=True,
                )
                with mock.patch.object(evaluate.shutil, "which", return_value="/usr/bin/cargo"), mock.patch.object(
                    evaluate, "run_platform_command", side_effect=fake_platform_run
                ), contextlib.redirect_stdout(io.StringIO()):
                    pac_rc = evaluate.collect_pac_metadata_direct_probe(pac_args)
                    huge_rc = evaluate.collect_hugepage_metadata_direct_probe(huge_args)
                pac_audit = json.loads(
                    (raw / "pac-cleanup-unit" / "pac-metadata-direct-probe-audit.json").read_text(
                        encoding="utf-8"
                    )
                )
                huge_audit = json.loads(
                    (raw / "hugepage-cleanup-unit" / "hugepage-metadata-direct-probe-audit.json").read_text(
                        encoding="utf-8"
                    )
                )

        self.assertEqual(pac_rc, 0)
        self.assertEqual(huge_rc, 0)
        for audit in (pac_audit, huge_audit):
            self.assertTrue(audit["summary"]["cargo_target_cleaned"])
            self.assertEqual(audit["summary"]["cargo_target_removed_bytes"], 19)
            self.assertEqual(audit["cargo_target_cleanup"]["removed_bytes"], 19)
            self.assertFalse(pathlib.Path(audit["cargo_target_dir"]).exists())

    def test_rustc_driver_probe_runners_cleanup_run_local_cargo_targets(self) -> None:
        evaluate_source = (ROOT / "evaluation" / "scripts" / "evaluate.py").read_text(
            encoding="utf-8"
        )
        self.assertGreaterEqual(
            evaluate_source.count(
                "cargo_target_cleanup = remove_directory_if_present(cargo_target_dir)"
            ),
            1,
        )
        self.assertGreaterEqual(
            evaluate_source.count('"cargo_target_cleanup": cargo_target_cleanup'),
            1,
        )
        self.assertEqual(
            evaluate_source.count('no_wrapper_target_dir = out_dir / "no-wrapper-target"'),
            3,
        )
        self.assertEqual(
            evaluate_source.count(
                "no_wrapper_target_cleanup = remove_directory_if_present(no_wrapper_target_dir)"
            ),
            3,
        )
        self.assertGreaterEqual(
            evaluate_source.count('"no_wrapper_target_cleanup": no_wrapper_target_cleanup'),
            3,
        )
        self.assertEqual(evaluate_source.count('"no_wrapper_target_cleaned"'), 3)
        self.assertEqual(evaluate_source.count('"no_wrapper_target_removed_bytes"'), 3)

    def test_std_bench_source_correctness_flags_broader_lazy_iterators(self) -> None:
        cases = {
            "black_box(map.iter());": [("black_box_direct_iter", "iter")],
            "test::black_box(map.range(f(i, j)));": [("black_box_direct_range", "range")],
            "black_box(map.keys());": [("black_box_direct_lazy_iterator", "keys")],
            "black_box(slice.windows(2));": [("black_box_direct_lazy_iterator", "windows")],
            "black_box(slice.chunks_exact(8));": [
                ("black_box_direct_lazy_iterator", "chunks_exact")
            ],
        }
        for source, expected in cases.items():
            with self.subTest(source=source):
                actual = [
                    (finding["pattern"], finding["method"])
                    for finding in evaluate.lazy_iterator_black_box_findings_for_code(source)
                ]
                self.assertEqual(actual, expected)

    def test_std_bench_source_correctness_allows_consumed_lazy_iterators(self) -> None:
        allowed_sources = [
            "black_box(ring.iter().try_fold(0, |a, b| Some(a + b)))",
            "black_box(map.range(f(i, j)).count());",
            "let keys = map.keys(); black_box(total);",
            "some_black_box_like(map.keys());",
        ]
        for source in allowed_sources:
            with self.subTest(source=source):
                self.assertEqual(evaluate.lazy_iterator_black_box_findings_for_code(source), [])

    def test_paper_performance_semantic_validation_keeps_drop_scope_pairs(self) -> None:
        with tempfile.TemporaryDirectory() as raw_tmp:
            tmp = pathlib.Path(raw_tmp)
            rewrites = tmp / "rewrites"
            rewrites.mkdir()
            write_json(
                rewrites / "std_bench.json",
                {
                    "rustc_args": ["--crate-name", "std_bench"],
                    "summary": {
                        "actual_semantic_scope_rewrite": True,
                        "semantic_scope_candidate_count": 2,
                        "semantic_scope_rewrite_applied_count": 1,
                        "semantic_scope_drop_candidate_count": 1,
                        "semantic_scope_drop_rewrite_applied_count": 1,
                        "semantic_scope_replacement_resolution_status": (
                            "resolved_unialloc_semantic_scope_push_pop"
                        ),
                    },
                    "rewrite_candidates": [
                        {
                            "lowering_kind": "semantic_scope_enter_exit_rewrite",
                            "rewrite_status": "actual_semantic_scope_enter_exit_rewrite_applied",
                            "type_id": 101,
                            "module_id": evaluate.RUSTC_DRIVER_LOWERING_MODULE_ID,
                            "callsite": 201,
                            "flags": 1,
                            "semantic_object_type": "std::vec::Vec<u32>",
                            "type_id_basis": "rustc_middle_ty_destination_or_argument_heap_object_type",
                        },
                        {
                            "lowering_kind": "semantic_scope_drop_rewrite",
                            "rewrite_status": "actual_semantic_scope_drop_rewrite_applied",
                            "type_id": 102,
                            "module_id": evaluate.RUSTC_DRIVER_LOWERING_MODULE_ID,
                            "callsite": 202,
                            "flags": 1,
                            "semantic_object_type": "std::string::String",
                            "type_id_basis": "rustc_middle_ty_destination_or_argument_heap_object_type",
                        },
                    ],
                },
            )
            events = [
                {
                    "source": "std_bench_auto_metadata",
                    "event": "typed_allocation_site",
                    "typed": True,
                    "type_id": 101,
                    "module_id": evaluate.RUSTC_DRIVER_LOWERING_MODULE_ID,
                    "callsite": 201,
                    "count": 7,
                    "bytes": 56,
                    "deallocations": 0,
                    "type_id_basis": (
                        "compiler-assigned-allocation-site-object-type-id-"
                        "rustc-driver-mir-semantic-scope"
                    ),
                    "compiler_site_id_stream_mode": "rustc-driver-mir-semantic-scope",
                    "compiler_site_lowered": True,
                    "compiler_site_replay": False,
                },
                {
                    "source": "std_bench_auto_metadata",
                    "event": "typed_allocation_site",
                    "typed": True,
                    "type_id": 102,
                    "module_id": evaluate.RUSTC_DRIVER_LOWERING_MODULE_ID,
                    "callsite": 202,
                    "count": 0,
                    "bytes": 0,
                    "deallocations": 1,
                    "type_id_basis": (
                        "compiler-assigned-allocation-site-object-type-id-"
                        "rustc-driver-mir-semantic-scope"
                    ),
                    "compiler_site_id_stream_mode": "rustc-driver-mir-semantic-scope",
                    "compiler_site_lowered": True,
                    "compiler_site_replay": False,
                },
            ]

            evidence = evaluate.mir_semantic_scope_std_bench_evidence_from_runtime(
                tmp,
                artifact_prefix="paired-validation",
                rewrites_dir=rewrites,
                events=events,
                features="bench_ourself,type_isolation,stats",
            )

        summary = evidence["summary"]
        self.assertTrue(summary["semantic_policy_ready"], summary)
        self.assertEqual(summary["semantic_scope_drop_rewrite_applied_count"], 1)
        self.assertEqual(summary["type_mapping_type_id_callsite_pair_count"], 2)
        self.assertTrue(summary["lowered_module_type_id_callsite_pairs_present_in_type_mapping"])



class PaperPerformanceSampleClaimGradeTests(unittest.TestCase):
    def base_sample(self) -> dict:
        return {
            "dataset": "default_performance",
            "benchmark": "RJS-Compiler",
            "allocator": "unialloc",
            "seconds": 0.01,
            "success": True,
        }

    def test_sample_claim_grade_requires_benchmark_owned_json_marker(self) -> None:
        blockers = evaluate.sample_claim_grade_blockers({**self.base_sample(), "claim_grade": True})
        self.assertIn("sample does not prove benchmark_owned_json=true timing", blockers)

    def test_sample_claim_grade_accepts_top_level_benchmark_owned_json_marker(self) -> None:
        blockers = evaluate.sample_claim_grade_blockers(
            {**self.base_sample(), "claim_grade": True, "benchmark_owned_json": True}
        )
        self.assertNotIn("sample does not prove benchmark_owned_json=true timing", blockers)

    def test_sample_claim_grade_accepts_nested_timing_contract_marker(self) -> None:
        blockers = evaluate.sample_claim_grade_blockers(
            {
                **self.base_sample(),
                "claim_grade": True,
                "metric_metadata": {"timing_contract": {"benchmark_owned_json": True}},
            }
        )
        self.assertNotIn("sample does not prove benchmark_owned_json=true timing", blockers)

    def test_sample_claim_grade_accepts_child_record_marker(self) -> None:
        blockers = evaluate.sample_claim_grade_blockers(
            {
                **self.base_sample(),
                "claim_grade": True,
                "metric_metadata": {"child_record": {"benchmark_owned_json": True}},
            }
        )
        self.assertNotIn("sample does not prove benchmark_owned_json=true timing", blockers)

    def test_sample_claim_grade_rejects_pinned_snapshot_without_paper_exact_source(self) -> None:
        blockers = evaluate.sample_claim_grade_blockers(
            {
                **self.base_sample(),
                "claim_grade": True,
                "benchmark_owned_json": True,
                "source_contract": {
                    "paper_exact_ref_complete": False,
                    "reproducible_snapshot_complete": True,
                    "checkout_pin": {"pinned": True},
                    "blockers": [
                        "adapter checkout is pinned to a reproducible commit but paper exact-ref provenance is still missing"
                    ],
                },
                "source_provenance_class": "reproducible_snapshot",
            }
        )
        joined = " ".join(blockers)
        self.assertIn("pinned/reproducible", joined)
        self.assertIn("paper exact-ref provenance is still missing", joined)

    def test_sample_claim_grade_accepts_paper_exact_source_contract(self) -> None:
        blockers = evaluate.sample_claim_grade_blockers(
            {
                **self.base_sample(),
                "claim_grade": True,
                "benchmark_owned_json": True,
                "source_contract": {
                    "paper_exact_ref_complete": True,
                    "complete": True,
                    "claim_grade_complete": True,
                    "exact_checkout_pin_complete": True,
                    "checkout_pin": {"pinned": True, "exact_pinned": True},
                    "exact_ref_candidates": [{"kind": "exact", "ref": "paper-v1"}],
                },
                "source_provenance_class": "paper_exact_ref",
            }
        )
        self.assertNotIn("sample source", " ".join(blockers))


class PaperPerformancePlanRunnerResourceTests(unittest.TestCase):
    def base_workload(self, script: pathlib.Path, **overrides: object) -> dict:
        workload = {
            "dataset": "default_performance",
            "benchmark": "RJS-Compiler",
            "allocator": "unialloc",
            "command": [sys.executable, str(script)],
            "time_field": "seconds",
            "measurement": "stdout_json",
            "claim_grade": True,
        }
        workload.update(overrides)
        return workload

    def run_workload(self, tmp: pathlib.Path, script: pathlib.Path, **overrides: object) -> dict:
        return evaluate.run_paper_performance_plan_one(
            workload=self.base_workload(script, **overrides),
            workload_index=1,
            run_index=1,
            out_dir=tmp / "out",
            default_timeout=5,
            default_max_output_bytes=512,
            run_id="plan-runner-resource-test",
        )

    def test_plan_runner_keeps_claim_grade_benchmark_owned_json_when_output_is_bounded(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            tmp = pathlib.Path(td)
            script = tmp / "child.py"
            script.write_text(
                "\n".join(
                    [
                        "import json",
                        "print(json.dumps({",
                        "  'seconds': 0.125,",
                        "  'benchmark_owned_json': True,",
                        "  'timing_contract': {'benchmark_owned_json': True},",
                        "  'claim_grade': True,",
                        "}))",
                    ]
                ),
                encoding="utf-8",
            )
            record = self.run_workload(tmp, script)

            self.assertTrue(record["success"], record)
            self.assertEqual(record["measurement_source"], "stdout_json")
            self.assertFalse(record["stdout_truncated"])
            self.assertEqual(record["claim_grade"], True)
            self.assertTrue(record["benchmark_owned_json"])
            self.assertNotIn(
                "sample does not prove benchmark_owned_json=true timing",
                evaluate.sample_claim_grade_blockers(record),
            )

    def test_plan_runner_blocks_claim_grade_for_reproducible_non_paper_exact_source(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            tmp = pathlib.Path(td)
            script = tmp / "child.py"
            script.write_text(
                "\n".join(
                    [
                        "import json",
                        "print(json.dumps({",
                        "  'seconds': 0.125,",
                        "  'benchmark_owned_json': True,",
                        "  'timing_contract': {'benchmark_owned_json': True},",
                        "  'claim_grade': True,",
                        "}))",
                    ]
                ),
                encoding="utf-8",
            )
            source_contract = {
                "paper_exact_ref_complete": False,
                "reproducible_snapshot_complete": True,
                "checkout_pin": {"pinned": True},
                "blockers": [
                    "adapter checkout is pinned to a reproducible commit but paper exact-ref provenance is still missing"
                ],
            }
            record = self.run_workload(
                tmp,
                script,
                source_contract=source_contract,
                source_provenance_class="reproducible_snapshot",
                paper_source={
                    "upstream_url": "https://example.invalid/paper-workload.git",
                    "matchers": ["example.invalid/paper-workload"],
                    "ref_candidates": [{"kind": "head", "ref": "HEAD"}],
                },
            )

            self.assertTrue(record["success"], record)
            self.assertEqual(record["claim_grade"], False)
            self.assertEqual(record["source_contract"], source_contract)
            self.assertIn("source_contract", record["provenance"])
            self.assertIn("pinned/reproducible", " ".join(record["claim_grade_blockers"]))
            self.assertIn("pinned/reproducible", " ".join(evaluate.sample_claim_grade_blockers(record)))

    def test_plan_runner_truncates_stdout_and_blocks_claim_grade(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            tmp = pathlib.Path(td)
            script = tmp / "child.py"
            script.write_text(
                "\n".join(
                    [
                        "import json, sys",
                        "sys.stdout.write('x' * 4096 + '\\n')",
                        "print(json.dumps({",
                        "  'seconds': 0.25,",
                        "  'benchmark_owned_json': True,",
                        "  'timing_contract': {'benchmark_owned_json': True},",
                        "  'claim_grade': True,",
                        "}))",
                    ]
                ),
                encoding="utf-8",
            )
            record = evaluate.run_paper_performance_plan_one(
                workload=self.base_workload(script, max_output_bytes=256),
                workload_index=1,
                run_index=1,
                out_dir=tmp / "out",
                default_timeout=5,
                default_max_output_bytes=512,
                run_id="plan-runner-resource-test",
            )

            self.assertTrue(record["success"], record)
            self.assertTrue(record["stdout_truncated"], record)
            self.assertLessEqual(pathlib.Path(record["stdout"]).stat().st_size, 256)
            self.assertEqual(record["claim_grade"], False)
            self.assertIn(
                "plan stdout exceeded max-output-bytes=256; retained tail only",
                record["claim_grade_blockers"],
            )

    def test_plan_runner_timeout_kills_process_group_and_records_bounded_evidence(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            tmp = pathlib.Path(td)
            script = tmp / "child.py"
            script.write_text(
                "\n".join(
                    [
                        "import sys, time",
                        "sys.stdout.write('started\\n')",
                        "sys.stdout.flush()",
                        "time.sleep(60)",
                    ]
                ),
                encoding="utf-8",
            )
            record = evaluate.run_paper_performance_plan_one(
                workload=self.base_workload(script, timeout=1, max_output_bytes=128),
                workload_index=1,
                run_index=1,
                out_dir=tmp / "out",
                default_timeout=5,
                default_max_output_bytes=512,
                run_id="plan-runner-resource-test",
            )

            self.assertFalse(record["success"], record)
            self.assertEqual(record["exit_code"], 124)
            self.assertIn("timeout after 1s", record["error"])
            self.assertIsNotNone(record["process_group_pid"])
            self.assertTrue(record["process_group_terminated"])
            self.assertLessEqual(pathlib.Path(record["stdout"]).stat().st_size, 128)

if __name__ == "__main__":
    unittest.main()
