#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = ["matplotlib==3.11.0"]
# ///
"""Tests for the current allocator evaluation figure exporter."""

from __future__ import annotations

import hashlib
import importlib.util
import json
import math
import re
import struct
import sys
import tempfile
import unittest
from pathlib import Path

SCRIPT = Path(__file__).with_name("plot_current_allocator_evaluation.py")
SPEC = importlib.util.spec_from_file_location(
    "plot_current_allocator_evaluation", SCRIPT
)
assert SPEC is not None and SPEC.loader is not None
MODULE = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = MODULE
SPEC.loader.exec_module(MODULE)


def write_json(path: Path, value: object) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    return path


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


class Fixture:
    revision = "1" * 40
    implementation_sha256 = "2" * 64
    source_commits = {"fixed": "3" * 40, "adaptive": "4" * 40}
    variants = ("unialloc", "typed_plain", "typeiso_perf")
    baseline_variants = (
        "jemalloc",
        "mimalloc",
        "mimalloc_no_thp",
        "google_tcmalloc",
    )
    benchmarks = ("alpha::one", "alpha::two", "beta::one", "beta::two")

    def __init__(self, root: Path) -> None:
        self.root = root
        self.suite = self._suite()
        self.rss_view = self._rss_view()
        self.micro_feature = self._micro_feature()
        self.micro_baseline = self._micro_baseline()
        self.macro_feature = self._macro_feature()
        self.macro_baseline = self._macro_baseline()
        self.output = root / "published"

    def _rss_view(self) -> Path:
        value = {
            "schema_version": 1,
            "view_id": "fixture-macro-rss-eligibility-v1",
            "suite": {
                "id": "fixture-current-suite",
                "sha256": sha256(self.suite),
            },
            "implementation": {
                "git_revision": self.revision,
                "canonical_sha256": self.implementation_sha256,
            },
            "harnesses": [
                {
                    "target_id": "fixed",
                    "harness_id": "fixed_harness",
                    "classification": "fixed_work",
                    "reason": "The process executes one predeclared fixed operation set.",
                    "work_attestation": {
                        "input": "The fixture input is fixed before measurement.",
                        "work": "The fixture operation count is fixed before measurement.",
                        "measurement_boundary": "Peak RSS covers the complete measured process.",
                    },
                },
                {
                    "target_id": "adaptive",
                    "harness_id": "adaptive_harness",
                    "classification": "diagnostic",
                    "reason": "The harness controls its own adaptive iteration count.",
                    "work_attestation": {
                        "input": "The fixture input is fixed before measurement.",
                        "work": "The measured process may execute an adaptive amount of work.",
                        "measurement_boundary": "Peak RSS includes harness startup and calibration.",
                    },
                },
            ],
        }
        value["payload_sha256"] = MODULE.canonical_sha256(value)
        return write_json(self.root / "rss-view.json", value)

    def artifact(self, stem: str, value: object | None = None) -> tuple[str, str]:
        path = write_json(
            self.root / "artifacts" / f"{stem}.json", value or {"artifact": stem}
        )
        return str(path.resolve()), sha256(path)

    def artifact_reference(self, path: Path) -> dict[str, object]:
        return {
            "path": str(path.resolve()),
            "sha256": sha256(path),
            "bytes": path.stat().st_size,
        }

    def measurement_artifact(
        self,
        stem: str,
        *,
        identity: dict[str, object],
        performance: float,
        performance_unit: str,
        peak_rss_mib: float,
        record_path: Path | None = None,
        binary_path: Path | None = None,
        baseline_contract: bool = False,
    ) -> tuple[str, str]:
        artifact_root = self.root / "measurement-artifacts" / stem
        binary = binary_path or artifact_root / "binary"
        if binary_path is None:
            binary.parent.mkdir(parents=True, exist_ok=True)
            binary.write_bytes(f"binary:{stem}\n".encode())
        runtime_values = {
            "LANG": "C",
            "LC_ALL": "C",
            "TMPDIR": str(artifact_root.resolve()),
        }
        runtime_record = {
            "policy": "allowlisted-production-runtime-v1",
            "inherited_allowlist": [],
            "rseq_policy": "libc_default",
            "glibc_tunables_present": False,
            "environment": runtime_values,
            "environment_sha256": MODULE.canonical_sha256(runtime_values),
        }
        runtime = write_json(artifact_root / "runtime.json", runtime_record)
        stdout = artifact_root / "stdout.txt"
        stdout.write_text(f"ok:{stem}\n", encoding="utf-8")
        command_contract = {"target": identity["target_id"], "stem": stem}
        full_identity = {**identity, "binary_sha256": sha256(binary)}
        if baseline_contract:
            full_identity["command_contract_sha256"] = MODULE.canonical_sha256(
                command_contract
            )
        metrics = {
            "performance": performance,
            "performance_unit": performance_unit,
            "peak_rss_mib": peak_rss_mib,
        }
        artifacts = {
            "binary": self.artifact_reference(binary),
            "stdout": self.artifact_reference(stdout),
        }
        if baseline_contract:
            artifacts.update(
                {
                    "runtime_environment": self.artifact_reference(runtime),
                    "suite_manifest": self.artifact_reference(self.suite),
                }
            )
        value = {
            "evidence_schema_version": 1,
            "schema_version": 1,
            "identity": full_identity,
            "metrics": metrics,
            "artifacts": artifacts,
            **full_identity,
            **metrics,
            "correctness": (
                {"passed": True}
                if baseline_contract
                else {"oracle": "fixture workload completed"}
            ),
            "runtime_environment": runtime_record,
        }
        if baseline_contract:
            value["success"] = True
            value["command_contract"] = command_contract
        else:
            value["suite_manifest"] = self.artifact_reference(self.suite)
        path = write_json(
            record_path or self.root / "artifacts" / f"{stem}.json", value
        )
        return str(path.resolve()), sha256(path)

    def _suite(self) -> Path:
        value = {
            "schema_version": 2,
            "suite_id": "fixture-current-suite",
            "implementation": {
                "git_revision": self.revision,
                "canonical_sha256": self.implementation_sha256,
                "canonical_file_count": 1,
                "canonical_size_bytes": 1,
            },
            "variant_order": list(self.variants),
            "measurement_contract": {"warmup_rounds": 1, "measured_rounds": 3},
            "publication": {
                "namespace": "fixture-current-suite",
                "result_namespace": "fixture-current-suite",
            },
            "comparison_families": [
                {
                    "id": "compiler_route",
                    "reference": "unialloc",
                    "subject": "typed_plain",
                },
                {
                    "id": "policy_increment",
                    "reference": "typed_plain",
                    "subject": "typeiso_perf",
                },
                {
                    "id": "end_to_end",
                    "reference": "unialloc",
                    "subject": "typeiso_perf",
                },
            ],
            "targets": [
                {
                    "id": "fixed",
                    "label": "Fixed Work",
                    "rss_work_model": "fixed_work",
                    "source": {"commit": self.source_commits["fixed"]},
                    "harnesses": [
                        {
                            "id": "fixed_harness",
                            "metric_direction": "lower_is_better",
                            "performance_unit": "seconds",
                            "performance_source": "process-wall-seconds",
                        }
                    ],
                },
                {
                    "id": "adaptive",
                    "label": "Adaptive Work",
                    "rss_work_model": "workload_native_adaptive_iterations",
                    "source": {"commit": self.source_commits["adaptive"]},
                    "harnesses": [
                        {
                            "id": "adaptive_harness",
                            "metric_direction": "higher_is_better",
                            "performance_unit": "operations_per_second",
                            "performance_source": "fixture-counter",
                        }
                    ],
                },
            ],
        }
        return write_json(self.root / "suite.json", value)

    def _micro_feature(self) -> Path:
        root = self.root / "micro-feature"
        protocol = {
            "schema_version": 1,
            "provenance": {"runner_sha256": "5" * 64},
            "compatibility": {
                "protocol_revision": "full-std-bench-feature-process-v6",
                "raw_record_schema_version": 4,
                "variant_ids": list(self.variants),
                "source_head": self.revision,
                "source_git_objects": {"unialloc": "6" * 40},
                "measured_rounds": 3,
                "canonical_inventory_count": len(self.benchmarks),
                "timeout_seconds": 30,
                "timeout_censoring_contract": (
                    "a process timeout is a terminal right-censored cell; an "
                    "incomplete GNU time footer is retained and attested without "
                    "metric imputation"
                ),
                "common_complete_selection_contract": (
                    "aggregate only canonical leaves with one valid warmup and "
                    "every measured round for every selected variant"
                ),
                "peer_timeout_contract": (
                    "after any variant timeout, remaining processes for that "
                    "canonical leaf are not launched and are terminally accounted "
                    "as peer blocked"
                ),
            },
        }
        protocol["protocol_sha256"] = hashlib.sha256(
            (
                json.dumps(
                    {
                        "schema_version": protocol["schema_version"],
                        "compatibility": protocol["compatibility"],
                    },
                    sort_keys=True,
                    separators=(",", ":"),
                )
                + "\n"
            ).encode()
        ).hexdigest()
        write_json(root / "campaign-protocol.json", protocol)
        cohort = "fixture-cohort"
        session_id = "fixture-measurement-session"
        write_json(
            root / "sessions" / cohort / "measurement-session.json",
            {
                "schema_version": 2,
                "cohort_id": cohort,
                "measurement_session_id": session_id,
                "variant_ids": list(self.variants),
                "protocol_sha256": protocol["protocol_sha256"],
            },
        )
        write_json(
            root / "views" / cohort / "selection.json",
            {
                "schema_version": 1,
                "cohort_id": cohort,
                "variant_ids": list(self.variants),
                "raw_data_mutated": False,
            },
        )
        performance_ratios = {
            "typed_plain": (1.1, 1.2, 0.8, 0.9),
            "typeiso_perf": (1.05, 1.1, 0.9, 0.95),
        }
        rss_ratios = {
            "typed_plain": (1.02, 1.04, 0.98, 1.0),
            "typeiso_perf": (1.01, 1.02, 0.97, 0.99),
        }
        reference_ns = (120.0, 220.0, 420.0, 820.0)
        lines = []
        for benchmark_index, benchmark in enumerate(self.benchmarks):
            for phase, rounds in (("warmup", (0,)), ("measured", (1, 2, 3))):
                for round_number in rounds:
                    for variant in self.variants:
                        ns_ratio = (
                            1.0
                            if variant == "unialloc"
                            else performance_ratios[variant][benchmark_index]
                        )
                        rss_ratio = (
                            1.0
                            if variant == "unialloc"
                            else rss_ratios[variant][benchmark_index]
                        )
                        raw_record = {
                            "schema_version": 4,
                            "protocol_sha256": protocol["protocol_sha256"],
                            "cohort_id": cohort,
                            "measurement_session_id": session_id,
                            "measurement_anchor": variant == "unialloc",
                            "variant_id": variant,
                            "allocator": variant,
                            "benchmark": benchmark,
                            "phase": phase,
                            "round": round_number,
                            "status": "valid",
                            "valid": True,
                            "timed_out": False,
                            "terminal_reason": None,
                            "time_parse_status": "complete",
                            "timeout_seconds": 30,
                            "fixed_work_contract": False,
                            "performance_claim_eligible": True,
                            "peak_rss_claim_eligible": False,
                            "rss_work_model": "workload_native_adaptive_iterations",
                            "ns_per_iter": reference_ns[benchmark_index] * ns_ratio,
                            "peak_rss_kib": 1000.0 * rss_ratio,
                        }
                        record = write_json(
                            root
                            / "raw"
                            / variant
                            / benchmark.replace("::", "-")
                            / phase
                            / f"{round_number}.json",
                            raw_record,
                        )
                        lines.append(
                            {
                                "benchmark": benchmark,
                                "cohort_id": cohort,
                                "fixed_work_contract": False,
                                "measurement_anchor": variant == "unialloc",
                                "measurement_session_id": session_id,
                                "ns_per_iter": raw_record["ns_per_iter"],
                                "peak_rss_claim_eligible": False,
                                "peak_rss_kib": raw_record["peak_rss_kib"],
                                "performance_claim_eligible": True,
                                "phase": phase,
                                "record_path": str(record.relative_to(root)),
                                "record_sha256": sha256(record),
                                "round": round_number,
                                "rss_work_model": "workload_native_adaptive_iterations",
                                "status": "valid",
                                "timed_out": False,
                                "terminal_reason": None,
                                "time_parse_status": "complete",
                                "variant_id": variant,
                            }
                        )
        index = root / "derived" / cohort / "absolute-process-index.jsonl"
        index.parent.mkdir(parents=True, exist_ok=True)
        index.write_text(
            "".join(json.dumps(row, sort_keys=True) + "\n" for row in lines),
            encoding="utf-8",
        )
        grouped = {
            (benchmark, variant): [
                row
                for row in lines
                if row["benchmark"] == benchmark and row["variant_id"] == variant
            ]
            for benchmark in self.benchmarks
            for variant in self.variants
        }
        terminal_rows = []
        for benchmark in self.benchmarks:
            cells = []
            for variant in self.variants:
                history = sorted(
                    grouped[(benchmark, variant)],
                    key=lambda row: (str(row["phase"]), int(row["round"])),
                )
                cells.append(
                    {
                        "variant_id": variant,
                        "status": "complete",
                        "valid_measured_rounds": [1, 2, 3],
                        "observed_process_slots": [
                            {
                                "phase": row["phase"],
                                "round": row["round"],
                                "status": row["status"],
                                "record_path": row["record_path"],
                                "record_sha256": row["record_sha256"],
                            }
                            for row in history
                        ],
                        "timeout_phase": None,
                        "timeout_round": None,
                        "timeout_record_path": None,
                        "timeout_record_sha256": None,
                        "blocked_by_timeout_variants": [],
                    }
                )
            terminal_rows.append(
                {
                    "benchmark": benchmark,
                    "family": benchmark.split("::", 1)[0],
                    "status": "common_complete",
                    "timeout_variants": [],
                    "cells": cells,
                }
            )
        write_json(
            root / "derived" / cohort / "terminal-accounting.json",
            {
                "schema_version": 1,
                "cohort_id": cohort,
                "measurement_session_id": session_id,
                "protocol_sha256": protocol["protocol_sha256"],
                "selected_variant_ids": list(self.variants),
                "canonical_inventory_count": len(self.benchmarks),
                "measured_rounds": 3,
                "terminal_benchmark_count": len(self.benchmarks),
                "terminal_accounting_complete": True,
                "common_complete_rule": (
                    "every selected variant has one valid warmup and every measured "
                    "round; a timeout excludes the leaf symmetrically from all "
                    "comparisons"
                ),
                "common_complete_benchmark_count": len(self.benchmarks),
                "excluded_benchmark_count": 0,
                "common_complete_benchmarks": list(self.benchmarks),
                "excluded_benchmarks": [],
                "cell_status_counts": {
                    "complete": len(self.benchmarks) * len(self.variants),
                    "timeout_censored": 0,
                    "peer_timeout_blocked": 0,
                },
                "raw_process_record_count": len(lines),
                "benchmarks": terminal_rows,
            },
        )
        return root

    def censor_micro_feature(
        self, benchmark: str = "alpha::one", variant: str = "unialloc"
    ) -> None:
        root = self.micro_feature
        cohort = "fixture-cohort"
        index_path = root / "derived" / cohort / "absolute-process-index.jsonl"
        rows = [json.loads(line) for line in index_path.read_text().splitlines()]
        retained = []
        timeout_row = None
        for row in rows:
            if row["benchmark"] != benchmark:
                retained.append(row)
                continue
            if row["variant_id"] == variant and row["phase"] == "warmup":
                record_path = root / row["record_path"]
                raw_record = json.loads(record_path.read_text())
                raw_record.update(
                    {
                        "status": "timeout_censored",
                        "valid": False,
                        "timed_out": True,
                        "terminal_reason": "process_timeout",
                        "time_parse_status": "incomplete_after_timeout",
                    }
                )
                raw_record.pop("ns_per_iter", None)
                raw_record.pop("peak_rss_kib", None)
                write_json(record_path, raw_record)
                row.update(
                    {
                        "status": "timeout_censored",
                        "timed_out": True,
                        "terminal_reason": "process_timeout",
                        "time_parse_status": "incomplete_after_timeout",
                        "ns_per_iter": None,
                        "peak_rss_kib": None,
                        "record_sha256": sha256(record_path),
                    }
                )
                timeout_row = row
                retained.append(row)
        assert timeout_row is not None
        index_path.write_text(
            "".join(json.dumps(row, sort_keys=True) + "\n" for row in retained),
            encoding="utf-8",
        )
        terminal_path = root / "derived" / cohort / "terminal-accounting.json"
        terminal = json.loads(terminal_path.read_text())
        benchmark_row = next(
            row for row in terminal["benchmarks"] if row["benchmark"] == benchmark
        )
        benchmark_row.update(
            {
                "status": "excluded_timeout_censored",
                "timeout_variants": [variant],
            }
        )
        for cell in benchmark_row["cells"]:
            if cell["variant_id"] == variant:
                cell.update(
                    {
                        "status": "timeout_censored",
                        "valid_measured_rounds": [],
                        "observed_process_slots": [
                            {
                                "phase": timeout_row["phase"],
                                "round": timeout_row["round"],
                                "status": timeout_row["status"],
                                "record_path": timeout_row["record_path"],
                                "record_sha256": timeout_row["record_sha256"],
                            }
                        ],
                        "timeout_phase": "warmup",
                        "timeout_round": 0,
                        "timeout_record_path": timeout_row["record_path"],
                        "timeout_record_sha256": timeout_row["record_sha256"],
                        "blocked_by_timeout_variants": [],
                    }
                )
            else:
                cell.update(
                    {
                        "status": "peer_timeout_blocked",
                        "valid_measured_rounds": [],
                        "observed_process_slots": [],
                        "timeout_phase": None,
                        "timeout_round": None,
                        "timeout_record_path": None,
                        "timeout_record_sha256": None,
                        "blocked_by_timeout_variants": [variant],
                    }
                )
        terminal["common_complete_benchmarks"].remove(benchmark)
        terminal["excluded_benchmarks"] = [benchmark]
        terminal["common_complete_benchmark_count"] -= 1
        terminal["excluded_benchmark_count"] = 1
        terminal["cell_status_counts"]["complete"] -= len(self.variants)
        terminal["cell_status_counts"]["timeout_censored"] = 1
        terminal["cell_status_counts"]["peer_timeout_blocked"] = len(self.variants) - 1
        terminal["raw_process_record_count"] = len(retained)
        write_json(terminal_path, terminal)

    def _micro_baseline(self) -> Path:
        root = self.root / "micro-baseline"
        variants = ("unialloc", *self.baseline_variants)
        variant_rows = [
            {
                "allocator": variant,
                "feature": f"bench_{variant}",
                "label": variant,
            }
            for variant in variants
        ]
        selection_payload = {
            "variant_ids": list(variants),
            "variants": variant_rows,
        }
        selection = {
            "schema_version": 1,
            **selection_payload,
            "selection_sha256": MODULE.canonical_sha256(selection_payload),
        }
        write_json(root / "campaign-selection.json", selection)
        write_json(
            root / "campaign-config.json",
            {
                "schema_version": 2,
                "repo_head": self.revision,
                "measured_rounds": 3,
                "warmups": 1,
                "benchmark_inventory": list(self.benchmarks),
                "benchmark_inventory_sha256": "b" * 64,
                "variant_ids": list(variants),
                "variants": variant_rows,
                "selection_sha256": selection["selection_sha256"],
                "ratio_floor_ns_per_iter": 100.0,
                "timeout_seconds": 30,
            },
        )
        write_json(
            root / "provenance.json",
            {
                "schema_version": 2,
                "repo_head": self.revision,
                "canonical_benchmark_count": len(self.benchmarks),
                "canonical_benchmark_list_sha256": "c" * 64,
                "variant_selection": selection,
                "inventories": {
                    variant: {
                        "binary_sha256": "7" * 64,
                        "selected_count": len(self.benchmarks),
                    }
                    for variant in variants
                },
            },
        )
        records = []
        cells = []
        ratios = {
            "jemalloc": 0.95,
            "mimalloc": 0.9,
            "mimalloc_no_thp": 0.92,
            "google_tcmalloc": 1.1,
        }
        for benchmark_index, benchmark in enumerate(self.benchmarks):
            for variant in variants:
                cells.append(
                    {"allocator": variant, "benchmark": benchmark, "status": "complete"}
                )
                ratio = 1.0 if variant == "unialloc" else ratios[variant]
                for phase, rounds in (("warmup", (0,)), ("measured", (1, 2, 3))):
                    for round_number in rounds:
                        records.append(
                            {
                                "schema_version": 2,
                                "allocator": variant,
                                "benchmark": benchmark,
                                "phase": phase,
                                "round": round_number,
                                "status": "valid",
                                "valid": True,
                                "timed_out": False,
                                "ns_per_iter": (150.0 + benchmark_index * 100.0)
                                * ratio,
                                "peak_rss_kib": 2000.0
                                * (1.0 if variant == "unialloc" else ratio + 0.05),
                            }
                        )
        records_path = root / "records.jsonl"
        records_path.parent.mkdir(parents=True, exist_ok=True)
        records_path.write_text(
            "".join(json.dumps(row, sort_keys=True) + "\n" for row in records),
            encoding="utf-8",
        )
        summary_path = write_json(
            root / "summary.json",
            {
                "schema_version": 2,
                "methodology": {
                    "allocators": list(variants),
                    "timeout_seconds_per_process": 30,
                    "timeout_censoring": (
                        "a warmup timeout terminates the cell; a measured timeout "
                        "retains prior raw observations and suppresses the cell median"
                    ),
                },
                "record_counts": {
                    "all": len(records),
                    "valid": len(records),
                    "timeout_censored": 0,
                    "all_warmups_attempted": True,
                },
                "coverage": {
                    "canonical_inventory_count": len(self.benchmarks),
                    "allocator_cells": len(self.benchmarks) * len(variants),
                    "complete_cells": len(self.benchmarks) * len(variants),
                    "censored_cells": 0,
                    "pending_cells": 0,
                    "all_allocator_complete_benchmarks": len(self.benchmarks),
                },
                "comparison_selection": {
                    "complete_all_allocator_benchmarks": list(self.benchmarks),
                    "comparable_benchmarks": list(self.benchmarks),
                    "comparable_count": len(self.benchmarks),
                    "excluded_count": 0,
                },
                "robustness_selection": {
                    "threshold_ns_per_iter": 100.0,
                    "selected_benchmarks": list(self.benchmarks),
                },
                "cells": cells,
                "censored_cells": [],
            },
        )
        write_json(
            root / "campaign-state.json",
            {
                "status": "complete",
                "all_warmups_attempted": True,
                "records_sha256": sha256(records_path),
                "summary_sha256": sha256(summary_path),
                "maximum_processes": len(self.benchmarks) * len(variants) * 4,
                "completed_terminal_processes": len(self.benchmarks)
                * len(variants)
                * 4,
                "censored_cells": 0,
            },
        )
        return root

    def censor_micro_baseline(
        self, benchmark: str = "alpha::one", variant: str = "jemalloc"
    ) -> None:
        root = self.micro_baseline
        records_path = root / "records.jsonl"
        records = [json.loads(line) for line in records_path.read_text().splitlines()]
        retained = []
        for record in records:
            if record["benchmark"] != benchmark or record["allocator"] != variant:
                retained.append(record)
            elif record["phase"] == "warmup":
                record.update(
                    {
                        "status": "timeout_censored",
                        "valid": False,
                        "timed_out": True,
                        "timeout_seconds": 30,
                    }
                )
                record.pop("ns_per_iter", None)
                record.pop("peak_rss_kib", None)
                retained.append(record)
        records_path.write_text(
            "".join(json.dumps(row, sort_keys=True) + "\n" for row in retained),
            encoding="utf-8",
        )
        summary_path = root / "summary.json"
        summary = json.loads(summary_path.read_text())
        censoring = {
            "benchmark": benchmark,
            "allocator": variant,
            "phase": "warmup",
            "round": 0,
            "timeout_seconds": 30,
            "valid_measured_rounds_before_timeout": [],
        }
        cell = next(
            row
            for row in summary["cells"]
            if row["benchmark"] == benchmark and row["allocator"] == variant
        )
        cell.update(
            {
                "status": "censored",
                "valid_measured_rounds": [],
                "censoring": censoring,
            }
        )
        summary["censored_cells"] = [censoring]
        summary["coverage"]["complete_cells"] -= 1
        summary["coverage"]["censored_cells"] = 1
        summary["coverage"]["all_allocator_complete_benchmarks"] -= 1
        summary["record_counts"].update(
            {
                "all": len(retained),
                "valid": len(retained) - 1,
                "timeout_censored": 1,
            }
        )
        completed = [name for name in self.benchmarks if name != benchmark]
        summary["comparison_selection"].update(
            {
                "complete_all_allocator_benchmarks": completed,
                "comparable_benchmarks": completed,
                "comparable_count": len(completed),
                "excluded_count": 1,
            }
        )
        summary["robustness_selection"]["selected_benchmarks"] = completed
        write_json(summary_path, summary)
        state_path = root / "campaign-state.json"
        state = json.loads(state_path.read_text())
        state.update(
            {
                "status": "complete_with_timeout_censoring",
                "completed_terminal_processes": len(retained),
                "censored_cells": 1,
                "records_sha256": sha256(records_path),
                "summary_sha256": sha256(summary_path),
            }
        )
        write_json(state_path, state)

    def _macro_feature(self) -> Path:
        suite_digest = sha256(self.suite)
        targets = []
        performance_ratios = {
            "fixed": {"typed_plain": 1.10, "typeiso_perf": 1.05},
            "adaptive": {"typed_plain": 1.20, "typeiso_perf": 0.95},
        }
        rss_ratios = {
            "fixed": {"typed_plain": 1.08, "typeiso_perf": 1.02},
            "adaptive": {"typed_plain": 0.80, "typeiso_perf": 0.70},
        }
        family_contracts = (
            ("compiler_route", "unialloc", "typed_plain"),
            ("policy_increment", "typed_plain", "typeiso_perf"),
            ("end_to_end", "unialloc", "typeiso_perf"),
        )
        for target_id, harness_id, direction, rss_model in (
            ("fixed", "fixed_harness", "lower_is_better", "fixed_work"),
            (
                "adaptive",
                "adaptive_harness",
                "higher_is_better",
                "workload_native_adaptive_iterations",
            ),
        ):
            performance_unit = (
                "seconds" if target_id == "fixed" else "operations_per_second"
            )
            audit_path, audit_digest = self.artifact(f"{target_id}-source-audit")
            builds = {}
            for variant in self.variants:
                build_path, build_digest = self.artifact(
                    f"{target_id}-{variant}-build",
                    {
                        "source_commit": self.source_commits[target_id],
                        "implementation_sha256": self.implementation_sha256,
                    },
                )
                builds[variant] = {
                    "success": True,
                    "source_commit": self.source_commits[target_id],
                    "implementation_sha256": self.implementation_sha256,
                    "allocator_activation": True,
                    "actual_mir_provenance": True,
                    "stats_disabled": True,
                    "raw_record_path": build_path,
                    "record_sha256": build_digest,
                }
            warmups = {}
            measurements = []
            for variant in self.variants:
                if variant == "unialloc":
                    warmup_performance = 100.0
                    warmup_rss = 100.0
                else:
                    ratio = performance_ratios[target_id][variant]
                    warmup_performance = 100.0 * (
                        ratio if direction == "lower_is_better" else 1.0 / ratio
                    )
                    warmup_rss = 100.0 * rss_ratios[target_id][variant]
                warmup_path, warmup_digest = self.measurement_artifact(
                    f"{target_id}-{variant}-warmup",
                    identity={
                        "suite_id": "fixture-current-suite",
                        "suite_manifest_sha256": suite_digest,
                        "target_id": target_id,
                        "harness_id": harness_id,
                        "source_commit": self.source_commits[target_id],
                        "implementation_revision": self.revision,
                        "implementation_sha256": self.implementation_sha256,
                        "variant": variant,
                        "phase": "warmup",
                        "round": 0,
                    },
                    performance=warmup_performance,
                    performance_unit=performance_unit,
                    peak_rss_mib=warmup_rss,
                )
                warmups[variant] = {
                    "target_id": target_id,
                    "harness_id": harness_id,
                    "phase": "warmup",
                    "round": 0,
                    "variant": variant,
                    "performance": warmup_performance,
                    "peak_rss_mib": warmup_rss,
                    "correctness": True,
                    "raw_record_path": warmup_path,
                    "record_sha256": warmup_digest,
                }
                for round_number in (1, 2, 3):
                    if variant == "unialloc":
                        performance = 100.0
                        rss = 100.0
                    else:
                        ratio = performance_ratios[target_id][variant]
                        performance = 100.0 * (
                            ratio if direction == "lower_is_better" else 1.0 / ratio
                        )
                        rss = 100.0 * rss_ratios[target_id][variant]
                    record_path, record_digest = self.measurement_artifact(
                        f"{target_id}-{variant}-measurement-{round_number}",
                        identity={
                            "suite_id": "fixture-current-suite",
                            "suite_manifest_sha256": suite_digest,
                            "target_id": target_id,
                            "harness_id": harness_id,
                            "source_commit": self.source_commits[target_id],
                            "implementation_revision": self.revision,
                            "implementation_sha256": self.implementation_sha256,
                            "variant": variant,
                            "phase": "measurement",
                            "round": round_number,
                        },
                        performance=performance,
                        performance_unit=performance_unit,
                        peak_rss_mib=rss,
                    )
                    measurements.append(
                        {
                            "round": round_number,
                            "variant": variant,
                            "performance": performance,
                            "peak_rss_mib": rss,
                            "raw_record_path": record_path,
                            "record_sha256": record_digest,
                        }
                    )
            performance_costs = {
                "unialloc": 1.0,
                **performance_ratios[target_id],
            }
            rss_costs = {"unialloc": 1.0, **rss_ratios[target_id]}
            harness_comparisons = {
                family_id: {
                    "reference": reference,
                    "subject": subject,
                    "execution_cost_ratio_median": (
                        performance_costs[subject] / performance_costs[reference]
                    ),
                    "peak_rss_ratio_median": (
                        rss_costs[subject] / rss_costs[reference]
                    ),
                }
                for family_id, reference, subject in family_contracts
            }
            target_comparisons = {
                family_id: {
                    "reference": value["reference"],
                    "subject": value["subject"],
                    "execution_cost_ratio_geometric_mean": value[
                        "execution_cost_ratio_median"
                    ],
                    "peak_rss_process_observed_ratio_geometric_mean": value[
                        "peak_rss_ratio_median"
                    ],
                    "fixed_work_peak_rss_ratio_geometric_mean": (
                        value["peak_rss_ratio_median"]
                        if rss_model == "fixed_work"
                        else None
                    ),
                }
                for family_id, value in harness_comparisons.items()
            }
            targets.append(
                {
                    "id": target_id,
                    "source_commit": self.source_commits[target_id],
                    "implementation_revision": self.revision,
                    "implementation_sha256": self.implementation_sha256,
                    "comparison_families": target_comparisons,
                    "compact_provenance": {
                        "source_audit": {
                            "raw_record_path": audit_path,
                            "sha256": audit_digest,
                        },
                        "builds": builds,
                    },
                    "harnesses": [
                        {
                            "id": harness_id,
                            "metric_direction": direction,
                            "rss_work_model": rss_model,
                            "gates": {
                                "correctness": True,
                                "build_success": True,
                                "allocator_activation": True,
                                "actual_mir_provenance": True,
                                "stats_disabled": True,
                                "source_audit_retained": True,
                            },
                            "warmup_attestations": warmups,
                            "comparison_families": harness_comparisons,
                            "measurements": measurements,
                        }
                    ],
                }
            )
        population_comparisons = {}
        for family_id, reference, subject in family_contracts:
            target_values = [
                target["comparison_families"][family_id] for target in targets
            ]
            population_comparisons[family_id] = {
                "reference": reference,
                "subject": subject,
                "execution_cost_ratio_geometric_mean": math.sqrt(
                    target_values[0]["execution_cost_ratio_geometric_mean"]
                    * target_values[1]["execution_cost_ratio_geometric_mean"]
                ),
                "fixed_work_peak_rss_ratio_geometric_mean": target_values[0][
                    "fixed_work_peak_rss_ratio_geometric_mean"
                ],
            }
        return write_json(
            self.root / "macro-feature.json",
            {
                "schema_version": 1,
                "suite_id": "fixture-current-suite",
                "suite_manifest_sha256": suite_digest,
                "implementation_revision": self.revision,
                "implementation_sha256": self.implementation_sha256,
                "status": "complete",
                "comparison_families": population_comparisons,
                "targets": targets,
            },
        )

    def _macro_baseline(self) -> Path:
        root = self.root / "macro-baseline"
        root.mkdir(parents=True, exist_ok=True)
        (root / "suite-manifest.json").write_bytes(self.suite.read_bytes())
        targets = ["fixed", "adaptive"]
        requested = list(self.baseline_variants)
        execution = ["unialloc", *requested]
        target_harnesses = {
            "fixed": {
                "id": "fixed_harness",
                "selector": "fixture-fixed",
                "metric_direction": "lower_is_better",
                "performance_unit": "seconds",
                "performance_source": "process-wall-seconds",
            },
            "adaptive": {
                "id": "adaptive_harness",
                "selector": "fixture-adaptive",
                "metric_direction": "higher_is_better",
                "performance_unit": "operations_per_second",
                "performance_source": "fixture-counter",
            },
        }
        protocol_payload = {
            "schema_version": 1,
            "protocol_id": "primary-macro-allocator-baselines-v1",
            "build_adapter_version": 2,
            "runner": {
                "path": str(
                    MODULE.MACRO_RUNNER_PATH.relative_to(MODULE.REPOSITORY_ROOT)
                ),
                "sha256": sha256(MODULE.MACRO_RUNNER_PATH),
            },
            "suite": {
                "id": "fixture-current-suite",
                "manifest_sha256": sha256(self.suite),
            },
            "implementation": {
                "git_revision": self.revision,
                "canonical_sha256": self.implementation_sha256,
            },
            "measurement": {"warmup_rounds": 1, "measured_rounds": 3},
            "targets": [
                {
                    "id": target_id,
                    "source_ref": f"fixture-{target_id}-ref",
                    "source_commit": self.source_commits[target_id],
                    "rss_work_model": (
                        "fixed_work"
                        if target_id == "fixed"
                        else "workload_native_adaptive_iterations"
                    ),
                    "harnesses": [target_harnesses[target_id]],
                }
                for target_id in targets
            ],
        }
        protocol_fingerprint = MODULE.canonical_sha256(protocol_payload)
        build_rows = []
        build_binaries: dict[tuple[str, str, str], Path] = {}
        build_records: dict[tuple[str, str], dict[str, object]] = {}
        for target_id in targets:
            harness_id = str(target_harnesses[target_id]["id"])
            for variant in ("unialloc", "jemalloc", "mimalloc", "system"):
                build_root = root / "builds" / target_id / variant
                binary = build_root / "binary"
                binary.parent.mkdir(parents=True, exist_ok=True)
                binary.write_bytes(f"{target_id}:{variant}\n".encode())
                stdout = build_root / "build.stdout"
                stderr = build_root / "build.stderr"
                stdout.write_text(f"built:{target_id}:{variant}\n", encoding="utf-8")
                stderr.write_text("", encoding="utf-8")
                build_path = build_root / "build.json"
                binary_reference = {
                    "path": str(binary.resolve()),
                    "sha256": sha256(binary),
                    "size_bytes": binary.stat().st_size,
                }
                record: dict[str, object] = {
                    "schema_version": 1,
                    "protocol_id": "primary-macro-allocator-baselines-v1",
                    "protocol_fingerprint": protocol_fingerprint,
                    "target_fingerprint": MODULE.macro_target_fingerprint(
                        protocol_payload, target_id
                    ),
                    "build_fingerprint": MODULE.macro_build_contract_fingerprint(
                        protocol_payload, target_id, variant
                    ),
                    "target_id": target_id,
                    "variant": variant,
                    "source_ref": f"fixture-{target_id}-ref",
                    "source_commit": self.source_commits[target_id],
                    "source_record": {
                        "source_ref": f"fixture-{target_id}-ref",
                        "source_commit": self.source_commits[target_id],
                    },
                    "toolchain": "fixture-toolchain",
                    "implementation_revision": self.revision,
                    "implementation_sha256": self.implementation_sha256,
                    "implementation_snapshot": str(self.root.resolve()),
                    "stats_enabled": False,
                    "allocator_route": (
                        "rust-system-default"
                        if variant == "system"
                        else "injected-rust-global-allocator"
                    ),
                    "commands": [
                        {
                            "command": ["fixture-build", target_id, variant],
                            "exit_code": 0,
                            "timed_out": False,
                            "wall_seconds": 0.01,
                            "stdout": str(stdout.resolve()),
                            "stdout_sha256": sha256(stdout),
                            "stderr": str(stderr.resolve()),
                            "stderr_sha256": sha256(stderr),
                        }
                    ],
                    "harness_binaries": {harness_id: binary_reference},
                    "build_path": str(build_path.resolve()),
                }
                record["build_id"] = MODULE.canonical_sha256(
                    {
                        "build_fingerprint": record["build_fingerprint"],
                        "target_id": target_id,
                        "variant": variant,
                        "source_commit": self.source_commits[target_id],
                        "harness_binaries": record["harness_binaries"],
                    }
                )
                record["success"] = True
                write_json(build_path, record)
                build_records[(target_id, variant)] = record
            for variant, base_variant in (
                ("mimalloc_no_thp", "mimalloc"),
                ("google_tcmalloc", "system"),
            ):
                base = build_records[(target_id, base_variant)]
                build_root = root / "builds" / target_id / variant
                build_path = build_root / "build.json"
                build_path.parent.mkdir(parents=True, exist_ok=True)
                record = {
                    **base,
                    "variant": variant,
                    "base_build_variant": base_variant,
                    "base_build_path": base["build_path"],
                    "base_build_id": base["build_id"],
                    "build_path": str(build_path.resolve()),
                }
                if variant == "google_tcmalloc":
                    library = root / "runtime" / "libtcmalloc.so"
                    library.parent.mkdir(parents=True, exist_ok=True)
                    if not library.exists():
                        library.write_bytes(b"fixture-tcmalloc\n")
                    record["tcmalloc_runtime"] = {
                        "library": str(library.resolve()),
                        "library_sha256": sha256(library),
                        "label": "fixture-google-tcmalloc",
                        "runtime_requirements": {
                            "revision": "fixture-google-tcmalloc-revision",
                            "hpaa_active": 1,
                            "malloc_provider_is_self": 1,
                        },
                    }
                    record["tcmalloc_identity"] = {
                        "revision": "fixture-google-tcmalloc-revision",
                        "library_sha256": sha256(library),
                        "hpaa_active": 1,
                        "malloc_provider_is_self": 1,
                        "label": "fixture-google-tcmalloc",
                    }
                record["build_fingerprint"] = MODULE.macro_variant_contract_fingerprint(
                    protocol_payload,
                    target_id,
                    variant,
                    record.get("tcmalloc_identity"),
                )
                record["build_id"] = MODULE.canonical_sha256(
                    {
                        "build_fingerprint": record["build_fingerprint"],
                        "target_id": target_id,
                        "variant": variant,
                        "source_commit": self.source_commits[target_id],
                        "harness_binaries": record["harness_binaries"],
                    }
                )
                record["success"] = True
                write_json(build_path, record)
                build_records[(target_id, variant)] = record
            for variant in execution:
                record = build_records[(target_id, variant)]
                binary = Path(str(record["harness_binaries"][harness_id]["path"]))
                build_binaries[(target_id, variant, harness_id)] = binary
                build_rows.append(
                    {
                        "target_id": target_id,
                        "variant": variant,
                        "build_id": record["build_id"],
                        "build_path": record["build_path"],
                        "harness_binaries": record["harness_binaries"],
                    }
                )
        write_json(
            root / "build-index.json",
            {"schema_version": 1, "builds": build_rows},
        )
        selection = MODULE.canonical_sha256(
            {
                "protocol_fingerprint": protocol_fingerprint,
                "targets": targets,
                "requested_variants": requested,
                "execution_variants": execution,
            }
        )
        cells = []
        cohorts = []
        for target_id, harness_id in (
            ("fixed", "fixed_harness"),
            ("adaptive", "adaptive_harness"),
        ):
            target_fingerprint = MODULE.macro_target_fingerprint(
                protocol_payload, target_id
            )
            for subject in requested:
                cohort = f"{target_id}-{subject}"
                cohorts.append(
                    {
                        "target_id": target_id,
                        "subject_variant": subject,
                        "variants": ["unialloc", subject],
                        "cohort_id": cohort,
                        "target_fingerprint": target_fingerprint,
                        "variant_fingerprints": {
                            "unialloc": "a" * 64,
                            subject: "a" * 64,
                        },
                    }
                )
                for variant in ("unialloc", subject):
                    for phase, rounds in (("warmup", (0,)), ("measurement", (1, 2, 3))):
                        for round_number in rounds:
                            relative = f"cells/{target_id}/{harness_id}/{cohort}/{variant}/{phase}-{round_number}.json"
                            fingerprint = hashlib.sha256(relative.encode()).hexdigest()
                            cell = {
                                "target_id": target_id,
                                "harness_id": harness_id,
                                "cohort_id": cohort,
                                "variant": variant,
                                "phase": phase,
                                "round": round_number,
                                "relative_path": relative,
                                "target_fingerprint": target_fingerprint,
                                "variant_fingerprint": "a" * 64,
                                "cell_fingerprint": fingerprint,
                            }
                            cells.append(cell)
                            performance_ratio = (
                                1.0
                                if variant == "unialloc"
                                else (0.9 if variant == "mimalloc" else 1.1)
                            )
                            rss_ratio = (
                                1.0
                                if variant == "unialloc"
                                else (0.93 if variant == "mimalloc" else 1.13)
                            )
                            direction = (
                                "lower_is_better"
                                if target_id == "fixed"
                                else "higher_is_better"
                            )
                            performance = 100.0 * (
                                performance_ratio
                                if direction == "lower_is_better"
                                else 1.0 / performance_ratio
                            )
                            build = next(
                                row
                                for row in build_rows
                                if row["target_id"] == target_id
                                and row["variant"] == variant
                            )
                            self.measurement_artifact(
                                f"macro-baseline-{target_id}-{subject}-{variant}-{phase}-{round_number}",
                                identity={
                                    "suite_id": "fixture-current-suite",
                                    "suite_manifest_sha256": sha256(self.suite),
                                    "protocol_fingerprint": protocol_fingerprint,
                                    "target_fingerprint": cell["target_fingerprint"],
                                    "variant_fingerprint": cell["variant_fingerprint"],
                                    "cell_fingerprint": fingerprint,
                                    "target_id": target_id,
                                    "harness_id": harness_id,
                                    "cohort_id": cohort,
                                    "variant": variant,
                                    "phase": phase,
                                    "round": round_number,
                                    "source_commit": self.source_commits[target_id],
                                    "implementation_revision": self.revision,
                                    "implementation_sha256": self.implementation_sha256,
                                    "build_id": build["build_id"],
                                    "build_fingerprint": build_records[
                                        (target_id, variant)
                                    ]["build_fingerprint"],
                                },
                                performance=performance,
                                performance_unit=(
                                    "seconds"
                                    if target_id == "fixed"
                                    else "operations_per_second"
                                ),
                                peak_rss_mib=100.0 * rss_ratio,
                                record_path=root / relative,
                                binary_path=build_binaries[
                                    (target_id, variant, harness_id)
                                ],
                                baseline_contract=True,
                            )
        plan_path = write_json(
            root / "selections" / selection / "plan.json",
            {
                "schema_version": 1,
                "protocol_id": "primary-macro-allocator-baselines-v1",
                "protocol_fingerprint": protocol_fingerprint,
                "selection_fingerprint": selection,
                "requested_targets": targets,
                "requested_variants": requested,
                "execution_variants": execution,
                "warmup_rounds": 1,
                "measured_rounds": 3,
                "cohorts": cohorts,
                "cell_count": len(cells),
                "cells": cells,
            },
        )
        cell_index = root / "cells.jsonl"
        cell_index.write_text(
            "".join(
                json.dumps(
                    {
                        "path": cell["relative_path"],
                        "sha256": sha256(root / cell["relative_path"]),
                        **{
                            field: cell[field]
                            for field in (
                                "target_id",
                                "harness_id",
                                "cohort_id",
                                "variant",
                                "phase",
                                "round",
                            )
                        },
                        "protocol_fingerprint": protocol_fingerprint,
                    },
                    sort_keys=True,
                )
                + "\n"
                for cell in cells
            ),
            encoding="utf-8",
        )
        workloads = {}
        target_aggregates = {target_id: {} for target_id in targets}
        for target_id, harness_id in (
            ("fixed", "fixed_harness"),
            ("adaptive", "adaptive_harness"),
        ):
            workloads[f"{target_id}/{harness_id}"] = {}
            for variant in requested:
                performance_ratio = 0.9 if variant == "mimalloc" else 1.1
                rss_ratio = 0.93 if variant == "mimalloc" else 1.13
                observations = [
                    {
                        "round": round_number,
                        "performance_cost_ratio": performance_ratio,
                        "peak_rss_ratio": rss_ratio,
                    }
                    for round_number in (1, 2, 3)
                ]
                fixed = target_id == "fixed"
                workloads[f"{target_id}/{harness_id}"][variant] = {
                    "cohort_id": f"{target_id}-{variant}",
                    "performance_cost_ratio_median": performance_ratio,
                    "peak_rss_ratio_median": rss_ratio,
                    "peak_rss_headline_eligible": fixed,
                    "same_round_observations": observations,
                }
                target_aggregates[target_id][variant] = {
                    "equal_weight_harness_geomean_performance_cost_ratio": performance_ratio,
                    "equal_weight_harness_geomean_peak_rss_ratio": (
                        rss_ratio if fixed else None
                    ),
                    "peak_rss_headline_eligible": fixed,
                    "eligible_harness_count": 1,
                }
        summary_path = write_json(
            root / "selections" / selection / "summary.json",
            {
                "schema_version": 1,
                "protocol_id": "primary-macro-allocator-baselines-v1",
                "protocol_fingerprint": protocol_fingerprint,
                "selection_fingerprint": selection,
                "requested_targets": targets,
                "requested_variants": requested,
                "execution_variants": execution,
                "measured_rounds": 3,
                "workloads": workloads,
                "targets": target_aggregates,
            },
        )
        write_json(
            root / "latest-selection.json",
            {
                "selection_fingerprint": selection,
                "plan": str(plan_path.resolve()),
                "summary": str(summary_path.resolve()),
            },
        )
        write_json(
            root / "campaigns" / protocol_fingerprint / "campaign.json",
            {
                "schema_version": 1,
                "protocol_id": "primary-macro-allocator-baselines-v1",
                "protocol_fingerprint": protocol_fingerprint,
                "protocol": protocol_payload,
                "suite_manifest": {
                    "path": str((root / "suite-manifest.json").resolve()),
                    "sha256": sha256(self.suite),
                    "bytes": self.suite.stat().st_size,
                },
            },
        )
        return root

    def swc_jemalloc_direct_load_record(self) -> dict[str, object]:
        root = self.root / "swc-jemalloc-route"
        worktree = root / "worktree"
        worktree.mkdir(parents=True)
        upstream_lock = worktree / "Cargo.lock"
        upstream_lock.write_text(
            'version = 4\n\n[[package]]\nname = "tikv-jemallocator"\n'
            'version = "0.5.4"\n',
            encoding="utf-8",
        )
        direct = root / "direct"
        source_root = direct / "source"
        source_root.mkdir(parents=True)
        manifest = source_root / "Cargo.toml"
        source = source_root / "src/lib.rs"
        source.parent.mkdir(parents=True)
        lock = source_root / "Cargo.lock"
        rlib = direct / "libunialloc_direct_jemallocator.rlib"
        wrapper = direct / "wrapper"
        manifest.write_text(
            MODULE.macro_runner.SWC_JEMALLOC_DIRECT_LOAD_MANIFEST,
            encoding="utf-8",
        )
        source.write_text("pub use jemallocator::Jemalloc;\n", encoding="utf-8")
        lock.write_text(
            'version = 4\n\n[[package]]\nname = "tikv-jemallocator"\n'
            'version = "0.7.0"\n\n[[package]]\n'
            'name = "tikv-jemalloc-sys"\n'
            f'version = "{MODULE.SWC_JEMALLOC_SYS_VERSION}"\n',
            encoding="utf-8",
        )
        rlib.write_bytes(b"fixture-jemalloc-rlib\n")
        wrapper.write_text(
            MODULE.macro_runner.SWC_JEMALLOC_DIRECT_LOAD_WRAPPER,
            encoding="utf-8",
        )
        commands = []
        for index in range(2):
            stdout = direct / f"command-{index}.stdout"
            stderr = direct / f"command-{index}.stderr"
            stdout.write_text(f"command-{index}\n", encoding="utf-8")
            stderr.write_text("", encoding="utf-8")
            commands.append(
                {
                    "command": ["fixture", str(index)],
                    "exit_code": 0,
                    "timed_out": False,
                    "wall_seconds": 0.01,
                    "stdout": str(stdout.resolve()),
                    "stdout_sha256": sha256(stdout),
                    "stderr": str(stderr.resolve()),
                    "stderr_sha256": sha256(stderr),
                }
            )
        direct_record = {
            "schema_version": 1,
            "success": True,
            "route": MODULE.SWC_JEMALLOC_DIRECT_LOAD_ROUTE,
            "wrapper_package": "tikv-jemallocator",
            "wrapper_version": MODULE.SWC_JEMALLOC_VERSION,
            "sys_package": "tikv-jemalloc-sys",
            "sys_version": MODULE.SWC_JEMALLOC_SYS_VERSION,
            "locked_wrapper_package": {
                "name": "tikv-jemallocator",
                "version": MODULE.SWC_JEMALLOC_VERSION,
            },
            "locked_sys_package": {
                "name": "tikv-jemalloc-sys",
                "version": MODULE.SWC_JEMALLOC_SYS_VERSION,
            },
            "adapter_source_sha256": sha256(MODULE.MACRO_RUNNER_PATH),
            "toolchain": "fixture-toolchain",
            "commands": commands,
            "artifacts": {
                "manifest": self.artifact_reference(manifest),
                "source": self.artifact_reference(source),
                "lockfile": self.artifact_reference(lock),
                "rlib": self.artifact_reference(rlib),
                "wrapper": self.artifact_reference(wrapper),
            },
        }
        record_path = write_json(direct / "build.json", direct_record)
        upstream_package = {
            "name": "tikv-jemallocator",
            "version": MODULE.SWC_UPSTREAM_JEMALLOC_VERSION,
        }
        return {
            "target_id": "swc",
            "variant": "jemalloc",
            "toolchain": "fixture-toolchain",
            "worktree": str(worktree.resolve()),
            "derived_cargo_lock_sha256": sha256(upstream_lock),
            "jemalloc_direct_load_route": {
                "id": MODULE.SWC_JEMALLOC_DIRECT_LOAD_ROUTE,
                "adapter_source_sha256": sha256(MODULE.MACRO_RUNNER_PATH),
                "target_crate": "typescript",
                "wrapper_package": "tikv-jemallocator",
                "wrapper_version": MODULE.SWC_JEMALLOC_VERSION,
                "sys_package": "tikv-jemalloc-sys",
                "sys_version": MODULE.SWC_JEMALLOC_SYS_VERSION,
                "upstream_inactive_lock_package": upstream_package,
                "cargo_lock_before_sha256": sha256(upstream_lock),
                "cargo_lock_after_sha256": sha256(upstream_lock),
                "record": str(record_path.resolve()),
                "record_sha256": sha256(record_path),
                "rlib": direct_record["artifacts"]["rlib"],
                "wrapper": direct_record["artifacts"]["wrapper"],
            },
        }

    def export(self) -> Path:
        return MODULE.export(
            suite_path=self.suite,
            rss_view_path=self.rss_view,
            micro_feature_path=self.micro_feature,
            micro_baseline_path=self.micro_baseline,
            macro_feature_path=self.macro_feature,
            macro_baseline_path=self.macro_baseline,
            output=self.output,
        )


class CurrentAllocatorEvaluationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.original_suite_path = MODULE.CURRENT_SUITE_PATH
        self.original_suite_sha256 = MODULE.CURRENT_SUITE_SHA256
        self.original_rss_view_path = MODULE.CURRENT_RSS_VIEW_PATH
        self.original_rss_view_sha256 = MODULE.CURRENT_RSS_VIEW_SHA256
        self.original_std_bench_count = MODULE.CANONICAL_STD_BENCH_COUNT
        self.temporary = tempfile.TemporaryDirectory()
        self.fixture = Fixture(Path(self.temporary.name))
        MODULE.CURRENT_SUITE_PATH = self.fixture.suite.resolve()
        MODULE.CURRENT_SUITE_SHA256 = sha256(self.fixture.suite)
        MODULE.CURRENT_RSS_VIEW_PATH = self.fixture.rss_view.resolve()
        MODULE.CURRENT_RSS_VIEW_SHA256 = sha256(self.fixture.rss_view)
        MODULE.CANONICAL_STD_BENCH_COUNT = len(self.fixture.benchmarks)

    def tearDown(self) -> None:
        MODULE.CURRENT_SUITE_PATH = self.original_suite_path
        MODULE.CURRENT_SUITE_SHA256 = self.original_suite_sha256
        MODULE.CURRENT_RSS_VIEW_PATH = self.original_rss_view_path
        MODULE.CURRENT_RSS_VIEW_SHA256 = self.original_rss_view_sha256
        MODULE.CANONICAL_STD_BENCH_COUNT = self.original_std_bench_count
        self.temporary.cleanup()

    def test_exports_four_title_free_three_format_figures(self) -> None:
        output = self.fixture.export()
        expected_stems = (
            "allocator-baselines-performance",
            "allocator-baselines-peak-rss",
            "unialloc-features-performance",
            "unialloc-features-peak-rss",
        )
        expected = {
            "artifact-manifest.json",
            "presentation-data.json",
            "normalized-observations.csv",
            *(
                f"{stem}.{extension}"
                for stem in expected_stems
                for extension in ("svg", "pdf", "png")
            ),
        }
        self.assertEqual(expected, {path.name for path in output.iterdir()})
        manifest = json.loads((output / "artifact-manifest.json").read_text())
        self.assertEqual(300, manifest["png_dpi"])
        self.assertEqual(list(expected_stems), manifest["figure_groups"])
        self.assertEqual(14, len(manifest["artifacts"]))
        presentation = json.loads((output / "presentation-data.json").read_text())
        self.assertEqual(presentation["inputs"], manifest["input_evidence"])
        self.assertEqual(
            {
                "path": str(self.fixture.rss_view.resolve()),
                "sha256": sha256(self.fixture.rss_view),
                "view_id": "fixture-macro-rss-eligibility-v1",
                "payload_sha256": json.loads(self.fixture.rss_view.read_text())[
                    "payload_sha256"
                ],
                "fixed_work_harness_count": 1,
                "diagnostic_harness_count": 1,
            },
            presentation["inputs"]["macro_rss_eligibility_view"],
        )
        macro_input = presentation["inputs"]["macro_baseline"]
        self.assertEqual(
            sha256(self.fixture.macro_baseline / "build-index.json"),
            macro_input["build_index_sha256"],
        )
        self.assertEqual(
            sha256(self.fixture.macro_baseline / "cells.jsonl"),
            macro_input["cell_index_sha256"],
        )
        self.assertEqual(10, len(macro_input["selected_build_records"]))
        self.assertTrue(macro_input["referenced_artifacts"])
        for stem in expected_stems:
            svg = (output / f"{stem}.svg").read_text(encoding="utf-8")
            self.assertNotIn("<title>", svg)
            self.assertNotIn("Allocator Baselines", svg)
            self.assertTrue(
                all(line == line.rstrip() for line in svg.splitlines()),
                f"{stem}.svg contains trailing whitespace",
            )
            png = (output / f"{stem}.png").read_bytes()
            self.assertEqual(b"\x89PNG\r\n\x1a\n", png[:8])
            width, height = struct.unpack(">II", png[16:24])
            self.assertEqual((4800, 2610), (width, height))
            self.assertGreater((output / f"{stem}.pdf").stat().st_size, 1000)
        for stem in (
            "allocator-baselines-peak-rss",
            "unialloc-features-peak-rss",
        ):
            svg = (output / f"{stem}.svg").read_text(encoding="utf-8")
            self.assertIn("diagnostic; process startup included", svg)
            label_match = re.search(
                r"<!-- \(diagnostic; process startup included\) -->\s*"
                r'<g transform="translate\([^ ]+ ([0-9.]+)\)',
                svg,
            )
            legend_match = re.search(
                r"<!-- Family median / target geometric mean -->\s*"
                r'<g transform="translate\([^ ]+ ([0-9.]+)\)',
                svg,
            )
            self.assertIsNotNone(label_match)
            self.assertIsNotNone(legend_match)
            assert label_match is not None and legend_match is not None
            self.assertGreaterEqual(
                float(legend_match.group(1)) - float(label_match.group(1)), 20.0
            )

    def test_formal_micro_baseline_contract_has_9360_processes(self) -> None:
        self.assertEqual(
            9360,
            self.original_std_bench_count * len(MODULE.ALLOCATOR_BASELINE_VARIANTS) * 4,
        )

    def test_hierarchical_micro_aggregation_and_fixed_work_rss(self) -> None:
        output = self.fixture.export()
        data = json.loads((output / "presentation-data.json").read_text())
        rows = data["normalized_rows"]
        alpha = next(
            row
            for row in rows
            if row["comparison_group"] == "feature"
            and row["population"] == "micro"
            and row["metric"] == "performance"
            and row["variant_id"] == "typed_plain"
            and row["aggregation_level"] == "family_median"
            and row["family"] == "alpha"
        )
        self.assertAlmostEqual(math.sqrt(1.1 * 1.2), alpha["ratio"], places=12)
        adaptive = next(
            row
            for row in rows
            if row["comparison_group"] == "feature"
            and row["population"] == "macro"
            and row["metric"] == "rss"
            and row["variant_id"] == "typeiso_perf"
            and row["aggregation_level"] == "target_geomean"
            and row["target_id"] == "adaptive"
        )
        aggregate = next(
            row
            for row in rows
            if row["comparison_group"] == "feature"
            and row["population"] == "macro"
            and row["metric"] == "rss"
            and row["variant_id"] == "typeiso_perf"
            and row["aggregation_level"] == "population_geomean"
        )
        self.assertTrue(adaptive["diagnostic_only"])
        self.assertFalse(adaptive["eligible_for_headline"])
        self.assertAlmostEqual(1.02, aggregate["ratio"], places=12)

    def test_mixed_target_rss_uses_only_fixed_work_harnesses(self) -> None:
        suite = MODULE.SuiteContract(
            path=self.fixture.suite,
            digest=sha256(self.fixture.suite),
            suite_id="mixed-suite",
            implementation_revision=self.fixture.revision,
            implementation_sha256=self.fixture.implementation_sha256,
            measured_rounds=3,
            warmup_rounds=1,
            comparison_families=(),
            harnesses=(
                MODULE.SuiteHarness(
                    "mixed",
                    "Mixed",
                    "cli",
                    "lower_is_better",
                    "fixed_work",
                    "3" * 40,
                    "seconds",
                    "process-wall-seconds",
                ),
                MODULE.SuiteHarness(
                    "mixed",
                    "Mixed",
                    "libtest",
                    "lower_is_better",
                    "fixed_work",
                    "3" * 40,
                    "ns_per_iter",
                    "rust-libtest-benchmark-estimate",
                ),
            ),
        )
        view = MODULE.RssEligibilityView(
            path=self.fixture.rss_view,
            digest=sha256(self.fixture.rss_view),
            payload_digest="a" * 64,
            view_id="mixed-view",
            harnesses={
                ("mixed", "cli"): MODULE.RssHarnessEligibility(
                    "fixed_work", "fixed CLI work", "input", "work", "boundary"
                ),
                ("mixed", "libtest"): MODULE.RssHarnessEligibility(
                    "diagnostic",
                    "adaptive libtest work",
                    "input",
                    "work",
                    "boundary",
                ),
            },
        )
        rows = MODULE.macro_aggregate(
            group="feature",
            comparison_family="end_to_end",
            metric="rss",
            variant="typeiso_perf",
            reference="unialloc",
            observations={
                ("mixed", "cli"): [(1, 2.0), (2, 2.0), (3, 2.0)],
                ("mixed", "libtest"): [(1, 8.0), (2, 8.0), (3, 8.0)],
            },
            suite=suite,
            rss_view=view,
            source=self.fixture.rss_view,
        )
        target = next(
            row for row in rows if row["aggregation_level"] == "target_geomean"
        )
        population = next(
            row for row in rows if row["aggregation_level"] == "population_geomean"
        )
        libtest = next(
            row
            for row in rows
            if row["aggregation_level"] == "workload_median"
            and row["unit_id"] == "libtest"
        )
        self.assertAlmostEqual(2.0, target["ratio"], places=12)
        self.assertAlmostEqual(2.0, population["ratio"], places=12)
        self.assertTrue(target["eligible_for_headline"])
        self.assertTrue(libtest["diagnostic_only"])

    def _rewrite_rss_view(
        self, transform: object, *, update_official_digest: bool = True
    ) -> None:
        value = json.loads(self.fixture.rss_view.read_text(encoding="utf-8"))
        value.pop("payload_sha256")
        transform(value)
        value["payload_sha256"] = MODULE.canonical_sha256(value)
        write_json(self.fixture.rss_view, value)
        if update_official_digest:
            MODULE.CURRENT_RSS_VIEW_SHA256 = sha256(self.fixture.rss_view)

    def test_rss_view_missing_harness_fails_closed(self) -> None:
        self._rewrite_rss_view(lambda value: value["harnesses"].pop())
        with self.assertRaisesRegex(MODULE.EvidenceError, "coverage differs"):
            self.fixture.export()

    def test_rss_view_extra_harness_fails_closed(self) -> None:
        def add_extra(value: dict[str, object]) -> None:
            extra = dict(value["harnesses"][0])
            extra["target_id"] = "outside-suite"
            value["harnesses"].append(extra)

        self._rewrite_rss_view(add_extra)
        with self.assertRaisesRegex(MODULE.EvidenceError, "coverage differs"):
            self.fixture.export()

    def test_rss_view_duplicate_harness_fails_closed(self) -> None:
        def add_duplicate(value: dict[str, object]) -> None:
            value["harnesses"].append(dict(value["harnesses"][0]))

        self._rewrite_rss_view(add_duplicate)
        with self.assertRaisesRegex(MODULE.EvidenceError, "duplicate harness"):
            self.fixture.export()

    def test_rss_view_digest_mismatch_fails_closed(self) -> None:
        value = json.loads(self.fixture.rss_view.read_text(encoding="utf-8"))
        value["harnesses"][0]["reason"] = "tampered"
        write_json(self.fixture.rss_view, value)
        with self.assertRaisesRegex(MODULE.EvidenceError, "view digest mismatch"):
            self.fixture.export()

    def _assert_fixed_work_source_requires_equal_work_override(
        self, performance_source: str
    ) -> None:
        suite = MODULE.read_suite(self.fixture.suite)
        original = suite.harnesses[0]
        adaptive = MODULE.SuiteHarness(
            original.target_id,
            original.target_label,
            original.harness_id,
            original.direction,
            original.rss_work_model,
            original.source_commit,
            original.performance_unit,
            performance_source,
        )
        changed_suite = MODULE.SuiteContract(
            path=suite.path,
            digest=suite.digest,
            suite_id=suite.suite_id,
            implementation_revision=suite.implementation_revision,
            implementation_sha256=suite.implementation_sha256,
            measured_rounds=suite.measured_rounds,
            warmup_rounds=suite.warmup_rounds,
            comparison_families=suite.comparison_families,
            harnesses=(adaptive, *suite.harnesses[1:]),
        )
        with self.assertRaisesRegex(MODULE.EvidenceError, "equal-work override"):
            MODULE.read_rss_eligibility_view(self.fixture.rss_view, changed_suite)

    def test_fixed_work_libtest_requires_equal_work_override(self) -> None:
        self._assert_fixed_work_source_requires_equal_work_override(
            "rust-libtest-benchmark-estimate"
        )

    def test_fixed_work_criterion_requires_equal_work_override(self) -> None:
        self._assert_fixed_work_source_requires_equal_work_override(
            "criterion-median-seconds-per-iteration"
        )

    def test_formal_ce8_rss_view_has_exact_fixed_work_membership(self) -> None:
        fixture_suite_path = MODULE.CURRENT_SUITE_PATH
        fixture_suite_sha256 = MODULE.CURRENT_SUITE_SHA256
        fixture_view_path = MODULE.CURRENT_RSS_VIEW_PATH
        fixture_view_sha256 = MODULE.CURRENT_RSS_VIEW_SHA256
        try:
            MODULE.CURRENT_SUITE_PATH = self.original_suite_path
            MODULE.CURRENT_SUITE_SHA256 = self.original_suite_sha256
            MODULE.CURRENT_RSS_VIEW_PATH = self.original_rss_view_path
            MODULE.CURRENT_RSS_VIEW_SHA256 = self.original_rss_view_sha256
            suite = MODULE.read_suite(self.original_suite_path)
            view = MODULE.read_rss_eligibility_view(self.original_rss_view_path, suite)
        finally:
            MODULE.CURRENT_SUITE_PATH = fixture_suite_path
            MODULE.CURRENT_SUITE_SHA256 = fixture_suite_sha256
            MODULE.CURRENT_RSS_VIEW_PATH = fixture_view_path
            MODULE.CURRENT_RSS_VIEW_SHA256 = fixture_view_sha256
        self.assertEqual(34, len(view.harnesses))
        self.assertEqual(("oxipng", "redb", "polars"), view.fixed_work_targets)
        self.assertEqual(
            {
                ("oxipng", "cli_issue_141_t1"),
                ("oxipng", "cli_issue_141_t4"),
                ("redb", "bulk_small"),
                ("redb", "transaction_churn"),
                ("redb", "delete_reinsert"),
                ("redb", "large_values"),
                ("polars", "groupby_low_cardinality"),
                ("polars", "groupby_high_cardinality"),
                ("polars", "groupby_multikey"),
                ("polars", "filter_retain"),
                ("polars", "csv_scan"),
            },
            view.fixed_work_harnesses,
        )

    def test_declared_feature_comparisons_use_their_exact_references(self) -> None:
        output = self.fixture.export()
        data = json.loads((output / "presentation-data.json").read_text())
        rows = data["normalized_rows"]
        policy_micro = next(
            row
            for row in rows
            if row["comparison_group"] == "feature"
            and row["comparison_family"] == "policy_increment"
            and row["population"] == "micro"
            and row["metric"] == "performance"
            and row["aggregation_level"] == "benchmark_case"
            and row["unit_id"] == "alpha::one"
        )
        self.assertEqual("typed_plain", policy_micro["reference_variant_id"])
        self.assertEqual("typeiso_perf", policy_micro["variant_id"])
        self.assertAlmostEqual(1.05 / 1.1, policy_micro["ratio"], places=12)
        policy_macro = next(
            row
            for row in rows
            if row["comparison_group"] == "feature"
            and row["comparison_family"] == "policy_increment"
            and row["population"] == "macro"
            and row["metric"] == "performance"
            and row["aggregation_level"] == "target_geomean"
            and row["target_id"] == "fixed"
        )
        self.assertAlmostEqual(1.05 / 1.1, policy_macro["ratio"], places=12)

    def test_common_micro_robust_set_applies_to_every_feature_comparison(self) -> None:
        index = (
            self.fixture.micro_feature
            / "derived"
            / "fixture-cohort"
            / "absolute-process-index.jsonl"
        )
        rows = [json.loads(line) for line in index.read_text().splitlines()]
        changed_digests = {}
        for row in rows:
            if (
                row["benchmark"] == "alpha::one"
                and row["variant_id"] == "typeiso_perf"
                and row["phase"] == "measured"
            ):
                row["ns_per_iter"] = 50.0
                record_path = self.fixture.micro_feature / row["record_path"]
                record = json.loads(record_path.read_text())
                record["ns_per_iter"] = 50.0
                write_json(record_path, record)
                row["record_sha256"] = sha256(record_path)
                changed_digests[(row["phase"], row["round"])] = row["record_sha256"]
        index.write_text(
            "".join(json.dumps(row, sort_keys=True) + "\n" for row in rows),
            encoding="utf-8",
        )
        terminal_path = (
            self.fixture.micro_feature
            / "derived"
            / "fixture-cohort"
            / "terminal-accounting.json"
        )
        terminal = json.loads(terminal_path.read_text())
        terminal_cell = next(
            cell
            for benchmark in terminal["benchmarks"]
            if benchmark["benchmark"] == "alpha::one"
            for cell in benchmark["cells"]
            if cell["variant_id"] == "typeiso_perf"
        )
        for slot in terminal_cell["observed_process_slots"]:
            key = (slot["phase"], slot["round"])
            if key in changed_digests:
                slot["record_sha256"] = changed_digests[key]
        write_json(terminal_path, terminal)
        output = self.fixture.export()
        data = json.loads((output / "presentation-data.json").read_text())
        compiler_alpha = next(
            row
            for row in data["normalized_rows"]
            if row["comparison_family"] == "compiler_route"
            and row["population"] == "micro"
            and row["metric"] == "performance"
            and row["aggregation_level"] == "family_median"
            and row["family"] == "alpha"
        )
        self.assertAlmostEqual(1.2, compiler_alpha["ratio"], places=12)
        self.assertEqual(
            3, data["inputs"]["micro_feature"]["common_robust_benchmark_count"]
        )

    def test_micro_rss_population_aggregate_remains_diagnostic(self) -> None:
        suite = MODULE.read_suite(self.fixture.suite)
        rows, _identity = MODULE.read_micro_feature(self.fixture.micro_feature, suite)
        summaries = MODULE.select_summaries(rows, "feature", "micro", "rss")
        self.assertTrue(all(row["aggregate_diagnostic"] for row in summaries))
        self.assertTrue(all(not row["aggregate_eligible"] for row in summaries))

    def test_file_digest_cache_tracks_stable_file_identity(self) -> None:
        artifact = self.fixture.root / "digest-cache.bin"
        artifact.write_bytes(b"first")
        MODULE._FILE_SHA256_CACHE.clear()
        first = MODULE.sha256_file(artifact)
        self.assertEqual(1, len(MODULE._FILE_SHA256_CACHE))
        self.assertEqual(first, MODULE.sha256_file(artifact))
        self.assertEqual(1, len(MODULE._FILE_SHA256_CACHE))
        artifact.write_bytes(b"later!")
        second = MODULE.sha256_file(artifact)
        self.assertNotEqual(first, second)
        self.assertEqual(2, len(MODULE._FILE_SHA256_CACHE))

    def test_off_scale_ratio_uses_an_explicit_overflow_position(self) -> None:
        displayed, direction = MODULE._display_log_ratio(32.0, 3.0)
        self.assertEqual(1, direction)
        self.assertLess(displayed, 3.0)
        displayed, direction = MODULE._display_log_ratio(1.0 / 32.0, 3.0)
        self.assertEqual(-1, direction)
        self.assertGreater(displayed, -3.0)

    def test_tampered_raw_artifact_fails_before_replacing_output(self) -> None:
        self.fixture.output.mkdir()
        sentinel = self.fixture.output / "keep.txt"
        sentinel.write_text("keep\n", encoding="utf-8")
        target = next((self.fixture.micro_feature / "raw").rglob("*.json"))
        target.write_text("{}\n", encoding="utf-8")
        with self.assertRaisesRegex(MODULE.EvidenceError, "digest mismatch"):
            self.fixture.export()
        self.assertEqual("keep\n", sentinel.read_text(encoding="utf-8"))

    def test_incomplete_micro_feature_matrix_fails_closed(self) -> None:
        index = (
            self.fixture.micro_feature
            / "derived"
            / "fixture-cohort"
            / "absolute-process-index.jsonl"
        )
        lines = index.read_text(encoding="utf-8").splitlines()
        index.write_text("\n".join(lines[:-1]) + "\n", encoding="utf-8")
        with self.assertRaisesRegex(MODULE.EvidenceError, "terminal"):
            self.fixture.export()

    def test_censored_micro_feature_uses_only_common_completed_benchmarks(
        self,
    ) -> None:
        self.fixture.censor_micro_feature()
        output = self.fixture.export()
        data = json.loads((output / "presentation-data.json").read_text())
        identity = data["inputs"]["micro_feature"]
        self.assertEqual(3, identity["common_completed_benchmark_count"])
        self.assertEqual(1, identity["common_completed_excluded_benchmark_count"])
        self.assertEqual(
            ["alpha::one"], identity["common_completed_excluded_benchmarks"]
        )
        self.assertEqual(1, identity["timeout_censored_cell_count"])
        self.assertEqual(2, identity["peer_timeout_blocked_cell_count"])
        feature_units = {
            row["unit_id"]
            for row in data["normalized_rows"]
            if row["comparison_group"] == "feature"
            and row["population"] == "micro"
            and row["aggregation_level"] == "benchmark_case"
        }
        self.assertNotIn("alpha::one", feature_units)

    def test_censored_micro_feature_peer_blocking_tamper_fails_closed(self) -> None:
        self.fixture.censor_micro_feature()
        terminal_path = (
            self.fixture.micro_feature
            / "derived"
            / "fixture-cohort"
            / "terminal-accounting.json"
        )
        terminal = json.loads(terminal_path.read_text())
        excluded = next(
            row for row in terminal["benchmarks"] if row["benchmark"] == "alpha::one"
        )
        excluded["cells"][1]["blocked_by_timeout_variants"] = []
        write_json(terminal_path, terminal)
        with self.assertRaisesRegex(MODULE.EvidenceError, "cell accounting"):
            self.fixture.export()

    def test_censored_micro_baseline_uses_only_common_completed_benchmarks(
        self,
    ) -> None:
        self.fixture.censor_micro_baseline()
        output = self.fixture.export()
        data = json.loads((output / "presentation-data.json").read_text())
        identity = data["inputs"]["micro_baseline"]
        self.assertEqual(3, identity["common_completed_benchmark_count"])
        self.assertEqual(1, identity["common_completed_excluded_benchmark_count"])
        self.assertEqual(
            ["alpha::one"], identity["common_completed_excluded_benchmarks"]
        )
        self.assertEqual(1, identity["censored_cell_count"])
        baseline_units = {
            row["unit_id"]
            for row in data["normalized_rows"]
            if row["comparison_group"] == "baseline"
            and row["aggregation_level"] == "benchmark_case"
        }
        self.assertNotIn("alpha::one", baseline_units)

    def test_censored_micro_baseline_asymmetric_selection_fails_closed(self) -> None:
        self.fixture.censor_micro_baseline()
        summary_path = self.fixture.micro_baseline / "summary.json"
        summary = json.loads(summary_path.read_text())
        summary["comparison_selection"]["comparable_benchmarks"].append("alpha::one")
        write_json(summary_path, summary)
        state_path = self.fixture.micro_baseline / "campaign-state.json"
        state = json.loads(state_path.read_text())
        state["summary_sha256"] = sha256(summary_path)
        write_json(state_path, state)
        with self.assertRaisesRegex(MODULE.EvidenceError, "comparison selection"):
            self.fixture.export()

    def test_macro_plan_requires_every_warmup_cell(self) -> None:
        selector = json.loads(
            (self.fixture.macro_baseline / "latest-selection.json").read_text()
        )
        plan_path = Path(selector["plan"])
        plan = json.loads(plan_path.read_text())
        removed = next(cell for cell in plan["cells"] if cell["phase"] == "warmup")
        plan["cells"].remove(removed)
        plan["cell_count"] -= 1
        write_json(plan_path, plan)
        with self.assertRaisesRegex(MODULE.EvidenceError, "warmup and measurement"):
            self.fixture.export()

    def test_macro_attempt_artifacts_are_not_committed_cell_records(self) -> None:
        attempt_artifact = (
            self.fixture.macro_baseline
            / "cells/fixed/fixed_harness/fixed-mimalloc/unialloc"
            / "attempts/measurement-1/fixture/process-timing.json"
        )
        write_json(attempt_artifact, {"wall_seconds": 0.1})
        suite = MODULE.read_suite(self.fixture.suite)
        view = MODULE.read_rss_eligibility_view(self.fixture.rss_view, suite)
        rows, identity = MODULE.read_macro_baseline(
            self.fixture.macro_baseline, suite, view
        )
        self.assertTrue(rows)
        self.assertEqual(64, len(identity["cell_index_sha256"]))

    def test_macro_cell_artifact_tamper_fails_closed(self) -> None:
        binary = self.fixture.macro_baseline / "builds/fixed/unialloc/binary"
        binary.write_bytes(b"tampered\n")
        with self.assertRaisesRegex(MODULE.EvidenceError, "content differs"):
            self.fixture.export()

    def test_macro_build_source_commit_tamper_fails_closed(self) -> None:
        build = self.fixture.macro_baseline / "builds/fixed/unialloc/build.json"
        value = json.loads(build.read_text(encoding="utf-8"))
        value["source_commit"] = "6" * 40
        write_json(build, value)
        with self.assertRaisesRegex(MODULE.EvidenceError, "build identity mismatch"):
            self.fixture.export()

    def test_swc_jemalloc_direct_load_route_is_fully_bound(self) -> None:
        build_record = self.fixture.swc_jemalloc_direct_load_record()
        retained = MODULE.validate_swc_jemalloc_direct_load(
            build_record, "fixture SWC jemalloc"
        )
        self.assertGreaterEqual(len(retained), 11)
        roles = {role for _identity, role in retained}
        self.assertIn("swc_upstream_lock", roles)
        self.assertIn("swc_jemalloc_direct_load_artifact", roles)

    def test_swc_jemalloc_direct_load_route_tamper_fails_closed(self) -> None:
        build_record = self.fixture.swc_jemalloc_direct_load_record()
        route = build_record["jemalloc_direct_load_route"]
        route["sys_version"] = "0.7.1+tampered"
        with self.assertRaisesRegex(MODULE.EvidenceError, "route identity mismatch"):
            MODULE.validate_swc_jemalloc_direct_load(
                build_record, "fixture SWC jemalloc"
            )

    def test_arbitrary_suite_path_is_rejected(self) -> None:
        copied = self.fixture.root / "copied-suite.json"
        copied.write_bytes(self.fixture.suite.read_bytes())
        with self.assertRaisesRegex(MODULE.EvidenceError, "official current suite"):
            MODULE.read_suite(copied)

    def test_macro_summary_must_match_bound_measurement_cells(self) -> None:
        cell = next((self.fixture.macro_baseline / "cells").rglob("measurement-1.json"))
        value = json.loads(cell.read_text(encoding="utf-8"))
        value["performance"] *= 2.0
        write_json(cell, value)
        with self.assertRaisesRegex(MODULE.EvidenceError, "metric mismatch"):
            self.fixture.export()


if __name__ == "__main__":
    unittest.main()
