#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = ["matplotlib==3.11.0"]
# ///
"""Build the canonical micro/macro allocator presentation bundle.

The exporter keeps the Rust ``std_bench`` campaign and the real-world Type
Isolation campaign as separate statistical populations.  It validates the
process evidence before deriving ratios of run medians or medians of paired-run
ratios, publishes uncapped machine-readable ratios, and applies clipping only
while drawing.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import math
import os
import shutil
import statistics
import tempfile
from collections import Counter, defaultdict
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence


os.environ.setdefault("MPLBACKEND", "Agg")
os.environ.setdefault("SOURCE_DATE_EPOCH", "1784077200")

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib.axes import Axes
from matplotlib.lines import Line2D
from matplotlib.ticker import FuncFormatter


REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_MICRO_RESULTS = (
    REPOSITORY_ROOT / "benchmark-results" / "std-bench-allocator-full-20260714.json"
)
DEFAULT_MICRO_RECORDS = (
    REPOSITORY_ROOT
    / "evaluation"
    / "raw"
    / "std-bench-allocator-full-optimized-20260714"
    / "records.jsonl"
)
DEFAULT_TYPE_ISOLATION_RESULTS = (
    REPOSITORY_ROOT / "benchmark-results" / "type-isolation-primary-v1.json"
)
DEFAULT_OUTPUT_DIR = (
    REPOSITORY_ROOT / "docs" / "figures" / "allocator-evaluation-20260714"
)

MICRO_SOURCE = "rust-std-bench-full-allocator-feature-variant-distribution"
MICRO_SCHEMA_VERSION = 2
MACRO_SUITE_ID = "type-isolation-primary-v1"
MACRO_SCHEMA_VERSION = 1
REFERENCE_ALLOCATOR = "unialloc"
ROBUST_THRESHOLD_NS = 100.0
MEASURED_ROUNDS = (1, 2, 3)
TYPE_ISOLATION_ROUNDS = (1, 2, 3, 4, 5)

ALLOCATOR_ORDER = (
    "unialloc",
    "ptmalloc",
    "jemalloc",
    "mimalloc",
    "tcmalloc",
    "snmalloc",
    "scudo",
)
ALLOCATOR_FEATURES = {
    "unialloc": "bench_ourself",
    "ptmalloc": "bench_ptmalloc",
    "jemalloc": "bench_jemalloc",
    "mimalloc": "bench_mimalloc",
    "tcmalloc": "bench_tcmalloc",
    "snmalloc": "bench_snmalloc",
    "scudo": "bench_scudo",
}
ALLOCATOR_LABELS = {
    "unialloc": "UniAlloc",
    "ptmalloc": "ptmalloc",
    "jemalloc": "jemalloc",
    "mimalloc": "mimalloc",
    "tcmalloc": "TCMalloc",
    "snmalloc": "snmalloc",
    "scudo": "Scudo",
}
PRODUCTION_FAMILY_COUNTS = {
    "binary_heap": 6,
    "btree": 100,
    "linked_list": 9,
    "slice": 74,
    "str": 129,
    "string": 17,
    "vec": 118,
    "vec_deque": 15,
}
PRODUCTION_COMPARABLE_COUNT = 466
PRODUCTION_ROBUST_COUNT = 236

TYPE_VARIANTS = ("unialloc", "typed_plain", "typeiso_perf")
PLOTTED_TYPE_VARIANTS = ("typed_plain", "typeiso_perf")
TYPE_LABELS = {
    "typed_plain": "Typed control",
    "typeiso_perf": "Type Isolation",
}
COMPARISON_FAMILIES = {
    "compiler_route": {
        "label": "Compiler route",
        "reference": "unialloc",
        "subject": "typed_plain",
    },
    "end_to_end": {
        "label": "End to end",
        "reference": "unialloc",
        "subject": "typeiso_perf",
    },
    "policy_increment": {
        "label": "Policy increment",
        "reference": "typed_plain",
        "subject": "typeiso_perf",
    },
}
VARIANT_COMPARISON = {
    "typed_plain": "compiler_route",
    "typeiso_perf": "end_to_end",
}
TARGET_ORDER = (
    "collections",
    "oxipng",
    "redb",
    "polars",
    "swc",
    "rustpython",
    "actix_web",
)
MACRO_TARGET_ORDER = TARGET_ORDER[1:]
TARGET_LABELS = {
    "collections": "Collections",
    "oxipng": "Oxipng",
    "redb": "redb",
    "polars": "Polars",
    "swc": "SWC",
    "rustpython": "RustPython",
    "actix_web": "Actix Web",
}
PRODUCTION_TARGET_HARNESS_COUNTS = {
    "collections": 5,
    "oxipng": 5,
    "redb": 4,
    "polars": 5,
    "swc": 5,
    "rustpython": 5,
    "actix_web": 5,
}
FIXED_WORK_TARGETS = ("oxipng", "redb", "polars")
ADAPTIVE_TARGETS = ("collections", "swc", "rustpython", "actix_web")

MICRO_CSV_FIELDS = (
    "cohort",
    "metric",
    "comparison",
    "comparison_label",
    "aggregation_level",
    "unit_id",
    "family",
    "eligible_for_headline",
    "diagnostic_only",
    "reference_median",
    "subject_median",
    "ratio",
    "observation_count",
    "note",
)
MACRO_CSV_FIELDS = (
    "metric",
    "comparison",
    "comparison_label",
    "aggregation_level",
    "target_id",
    "target_label",
    "workload_id",
    "rss_work_model",
    "eligible_for_suite_headline",
    "diagnostic_only",
    "ratio",
    "observation_count",
    "note",
)

FIGURE_SIZE = (15.2, 8.0)
PNG_DPI = 180
DISPLAY_LOG2_CAP = 3.0
MACRO_RATIO_BOUNDS = (0.8, 1.25)
FIXED_TIMESTAMP = "2026-07-15T01:00:00Z"

INK = "#15171A"
AXIS = "#45484E"
MUTED = "#696D75"
GRID = "#DADDE2"
PALE = "#D7DBE2"
BLUE = "#315B9D"
ORANGE = "#D86D3B"
WHITE = "#FFFFFF"


class EvidenceError(RuntimeError):
    """Raised when an input cannot support the canonical presentation."""


@dataclass(frozen=True)
class RawProcess:
    allocator: str
    feature: str
    benchmark: str
    phase: str
    round: int
    valid: bool
    timed_out: bool
    ns_per_iter: float | None
    peak_rss_kib: float | None


@dataclass(frozen=True)
class MicroLeaf:
    allocator: str
    label: str
    benchmark: str
    family: str
    performance_ratio: float
    rss_ratio: float
    robust: bool
    reference_performance_median: float
    subject_performance_median: float
    reference_rss_median: float
    subject_rss_median: float


@dataclass(frozen=True)
class HarnessRatios:
    target_id: str
    harness_id: str
    rss_work_model: str
    rss_eligibility: str
    performance: Mapping[str, float]
    rss: Mapping[str, float]
    comparisons: Mapping[str, Mapping[str, float]]
    compiler_route_equivalent: bool


@dataclass(frozen=True)
class LoadedEvidence:
    micro_result: Mapping[str, Any]
    micro_leaves: tuple[MicroLeaf, ...]
    macro_result: Mapping[str, Any]
    harnesses: tuple[HarnessRatios, ...]
    source_hashes: Mapping[str, str]
    production_contract: bool


def sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def load_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as error:
        raise EvidenceError(f"required input is missing: {path}") from error
    except json.JSONDecodeError as error:
        raise EvidenceError(f"invalid JSON in {path}: {error}") from error
    if not isinstance(value, dict):
        raise EvidenceError(f"JSON root must be an object: {path}")
    return value


def require_mapping(value: Any, context: str) -> Mapping[str, Any]:
    if not isinstance(value, dict):
        raise EvidenceError(f"{context} must be an object")
    return value


def require_list(value: Any, context: str) -> list[Any]:
    if not isinstance(value, list):
        raise EvidenceError(f"{context} must be a list")
    return value


def positive_number(value: Any, context: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise EvidenceError(f"{context} must be numeric")
    result = float(value)
    if not math.isfinite(result) or result <= 0.0:
        raise EvidenceError(f"{context} must be finite and positive")
    return result


def optional_positive(value: Any, context: str) -> float | None:
    if value is None:
        return None
    return positive_number(value, context)


def require_lower_hex(value: Any, length: int, context: str) -> str:
    if (
        not isinstance(value, str)
        or len(value) != length
        or any(character not in "0123456789abcdef" for character in value)
    ):
        raise EvidenceError(
            f"{context} must be {length} lowercase hexadecimal characters"
        )
    return value


def require_exact_strings(value: Any, expected: Sequence[str], context: str) -> None:
    if not isinstance(value, list) or tuple(value) != tuple(expected):
        raise EvidenceError(f"{context} must equal {tuple(expected)!r}")


def family_of(benchmark: str) -> str:
    if "::" not in benchmark:
        raise EvidenceError(f"benchmark lacks a family prefix: {benchmark!r}")
    return benchmark.split("::", 1)[0]


def geometric_mean(values: Iterable[float]) -> float:
    items = tuple(values)
    if not items:
        raise EvidenceError("geometric mean requires at least one value")
    return math.exp(math.fsum(math.log(value) for value in items) / len(items))


def hierarchical_summary(
    values_by_family: Mapping[str, Sequence[float]],
) -> dict[str, Any]:
    if not values_by_family:
        raise EvidenceError("hierarchical summary has no families")
    family_medians = {
        family: math.exp(statistics.median([math.log(value) for value in values]))
        for family, values in sorted(values_by_family.items())
    }
    family_geomeans = {
        family: geometric_mean(values)
        for family, values in sorted(values_by_family.items())
    }
    headline = math.exp(
        math.fsum(math.log(value) for value in family_medians.values())
        / len(family_medians)
    )
    sensitivity = geometric_mean(family_geomeans.values())
    return {
        "family_count": len(family_medians),
        "observation_count": sum(len(values) for values in values_by_family.values()),
        "family_median_ratios": family_medians,
        "headline_ratio": headline,
        "sensitivity_geometric_mean_of_family_geometric_means_ratio": sensitivity,
        "sensitivity_family_geomean_ratios": family_geomeans,
    }


def parse_raw_records(
    path: Path,
    allocators: Sequence[str],
    benchmarks: Sequence[str],
) -> dict[tuple[str, str], tuple[RawProcess, ...]]:
    allocator_set = set(allocators)
    benchmark_set = set(benchmarks)
    grouped: dict[tuple[str, str], list[RawProcess]] = defaultdict(list)
    identities: set[tuple[str, str, str, int]] = set()
    try:
        handle = path.open(encoding="utf-8")
    except FileNotFoundError as error:
        raise EvidenceError(f"required input is missing: {path}") from error
    with handle:
        for line_number, line in enumerate(handle, start=1):
            if not line.strip():
                continue
            try:
                value = json.loads(line)
            except json.JSONDecodeError as error:
                raise EvidenceError(
                    f"invalid JSON at {path}:{line_number}: {error}"
                ) from error
            row = require_mapping(value, f"records[{line_number}]")
            if row.get("schema_version") != MICRO_SCHEMA_VERSION:
                raise EvidenceError(f"records[{line_number}].schema_version mismatch")
            allocator = row.get("allocator")
            feature = row.get("feature")
            benchmark = row.get("benchmark")
            phase = row.get("phase")
            round_number = row.get("round")
            if allocator not in allocator_set:
                raise EvidenceError(f"records[{line_number}] has an unknown allocator")
            if feature != ALLOCATOR_FEATURES[allocator]:
                raise EvidenceError(f"records[{line_number}] feature mismatch")
            if benchmark not in benchmark_set:
                raise EvidenceError(f"records[{line_number}] has an unknown benchmark")
            if phase not in ("warmup", "measured"):
                raise EvidenceError(f"records[{line_number}] phase is invalid")
            if isinstance(round_number, bool) or not isinstance(round_number, int):
                raise EvidenceError(f"records[{line_number}] round must be an integer")
            expected_rounds = (0,) if phase == "warmup" else MEASURED_ROUNDS
            if round_number not in expected_rounds:
                raise EvidenceError(
                    f"records[{line_number}] round is invalid for {phase}"
                )
            identity = (str(allocator), str(benchmark), str(phase), round_number)
            if identity in identities:
                raise EvidenceError(f"duplicate process record: {identity!r}")
            identities.add(identity)
            valid = row.get("valid")
            timed_out = row.get("timed_out")
            if type(valid) is not bool or type(timed_out) is not bool:
                raise EvidenceError(
                    f"records[{line_number}] validity flags must be boolean"
                )
            ns_per_iter = optional_positive(
                row.get("ns_per_iter"), f"records[{line_number}].ns_per_iter"
            )
            peak_rss_kib = optional_positive(
                row.get("peak_rss_kib"), f"records[{line_number}].peak_rss_kib"
            )
            if valid and (timed_out or ns_per_iter is None or peak_rss_kib is None):
                raise EvidenceError(
                    f"records[{line_number}] valid measurement is incomplete"
                )
            if timed_out and (valid or row.get("status") != "timeout_censored"):
                raise EvidenceError(
                    f"records[{line_number}] timeout state is inconsistent"
                )
            if not valid and not timed_out:
                raise EvidenceError(
                    f"records[{line_number}] has an unsupported invalid state"
                )
            grouped[(str(allocator), str(benchmark))].append(
                RawProcess(
                    allocator=str(allocator),
                    feature=str(feature),
                    benchmark=str(benchmark),
                    phase=str(phase),
                    round=round_number,
                    valid=valid,
                    timed_out=timed_out,
                    ns_per_iter=ns_per_iter,
                    peak_rss_kib=peak_rss_kib,
                )
            )
    if not grouped:
        raise EvidenceError("process record input is empty")
    return {key: tuple(value) for key, value in grouped.items()}


def validate_micro_evidence(
    result_path: Path,
    records_path: Path,
    *,
    production_contract: bool,
) -> tuple[dict[str, Any], tuple[MicroLeaf, ...]]:
    result = load_json(result_path)
    if result.get("schema_version") != MICRO_SCHEMA_VERSION:
        raise EvidenceError("micro result schema_version mismatch")
    if result.get("source") != MICRO_SOURCE or result.get("success") is not True:
        raise EvidenceError("micro result source or success state mismatch")
    if result.get("claim_grade") is not False:
        raise EvidenceError("micro result must retain its diagnostic claim boundary")

    variants = require_list(result.get("variants"), "micro.variants")
    if len(variants) != len(ALLOCATOR_ORDER):
        raise EvidenceError("micro variant inventory is incomplete")
    for index, allocator in enumerate(ALLOCATOR_ORDER):
        variant = require_mapping(variants[index], f"micro.variants[{index}]")
        expected = {
            "allocator": allocator,
            "feature": ALLOCATOR_FEATURES[allocator],
            "label": ALLOCATOR_LABELS[allocator],
        }
        if any(variant.get(key) != value for key, value in expected.items()):
            raise EvidenceError(f"micro variant contract mismatch for {allocator}")

    inventory = require_mapping(result.get("inventory"), "micro.inventory")
    benchmarks_raw = require_list(
        inventory.get("benchmarks"), "micro.inventory.benchmarks"
    )
    if any(not isinstance(value, str) or not value for value in benchmarks_raw):
        raise EvidenceError("micro benchmark inventory contains an invalid identifier")
    benchmarks = tuple(str(value) for value in benchmarks_raw)
    if (
        len(benchmarks) != len(set(benchmarks))
        or tuple(sorted(benchmarks)) != benchmarks
    ):
        raise EvidenceError("micro benchmark inventory must be unique and sorted")
    family_counts = dict(
        sorted(Counter(family_of(value) for value in benchmarks).items())
    )
    stored_family_counts = require_mapping(
        inventory.get("family_counts"), "micro.inventory.family_counts"
    )
    if dict(stored_family_counts) != family_counts:
        raise EvidenceError(
            "micro family inventory disagrees with benchmark identifiers"
        )
    if inventory.get("benchmark_count") != len(benchmarks):
        raise EvidenceError("micro benchmark_count disagrees with inventory")
    if production_contract and family_counts != PRODUCTION_FAMILY_COUNTS:
        raise EvidenceError(
            "micro production inventory must contain all 468 std_bench leaves"
        )
    if not production_contract and set(family_counts) != set(PRODUCTION_FAMILY_COUNTS):
        raise EvidenceError("fixture inventory must exercise every std_bench family")

    methodology = require_mapping(result.get("methodology"), "micro.methodology")
    expected_methodology = {
        "reference_allocator": REFERENCE_ALLOCATOR,
        "measured_fresh_processes_per_complete_cell": len(MEASURED_ROUNDS),
        "warmup_fresh_processes_per_cell": 1,
        "direction": "lower ratio is faster",
    }
    if any(
        methodology.get(key) != value for key, value in expected_methodology.items()
    ):
        raise EvidenceError("micro methodology contract mismatch")

    artifacts = require_mapping(result.get("input_artifacts"), "micro.input_artifacts")
    records_artifact = require_mapping(
        artifacts.get("records"), "micro.input_artifacts.records"
    )
    if records_artifact.get("sha256") != sha256_file(records_path):
        raise EvidenceError("micro process-record digest mismatch")
    if records_artifact.get("bytes") != records_path.stat().st_size:
        raise EvidenceError("micro process-record byte count mismatch")

    raw = parse_raw_records(records_path, ALLOCATOR_ORDER, benchmarks)
    cells = require_list(result.get("cells"), "micro.cells")
    if len(cells) != len(benchmarks) * len(ALLOCATOR_ORDER):
        raise EvidenceError("micro cell matrix is incomplete")
    indexed_cells: dict[tuple[str, str], Mapping[str, Any]] = {}
    for index, value in enumerate(cells):
        cell = require_mapping(value, f"micro.cells[{index}]")
        allocator = cell.get("allocator")
        benchmark = cell.get("benchmark")
        if allocator not in ALLOCATOR_ORDER or benchmark not in benchmarks:
            raise EvidenceError(
                f"micro.cells[{index}] identity is outside the inventory"
            )
        key = (str(allocator), str(benchmark))
        if key in indexed_cells:
            raise EvidenceError(f"duplicate micro cell: {key!r}")
        if cell.get("feature") != ALLOCATOR_FEATURES[str(allocator)]:
            raise EvidenceError(f"micro cell feature mismatch: {key!r}")
        if cell.get("family") != family_of(str(benchmark)):
            raise EvidenceError(f"micro cell family mismatch: {key!r}")
        indexed_cells[key] = cell
    expected_keys = {
        (allocator, benchmark)
        for allocator in ALLOCATOR_ORDER
        for benchmark in benchmarks
    }
    if set(indexed_cells) != expected_keys:
        raise EvidenceError("micro cell identities do not cover the complete matrix")

    comparable: list[str] = []
    robust: list[str] = []
    rss_medians: dict[tuple[str, str], float] = {}
    for benchmark in benchmarks:
        complete = True
        benchmark_medians: list[float] = []
        for allocator in ALLOCATOR_ORDER:
            key = (allocator, benchmark)
            cell = indexed_cells[key]
            records = raw.get(key, ())
            status = cell.get("status")
            if status == "complete":
                identities = {(record.phase, record.round) for record in records}
                expected_identities = {("warmup", 0)} | {
                    ("measured", round_number) for round_number in MEASURED_ROUNDS
                }
                if identities != expected_identities or any(
                    not record.valid for record in records
                ):
                    raise EvidenceError(
                        f"complete micro cell has incomplete process evidence: {key!r}"
                    )
                measured = sorted(
                    (record for record in records if record.phase == "measured"),
                    key=lambda record: record.round,
                )
                ns_samples = tuple(float(record.ns_per_iter) for record in measured)
                rss_samples = tuple(float(record.peak_rss_kib) for record in measured)
                stored_samples = cell.get("sample_ns_per_iter")
                if not isinstance(stored_samples, list) or len(stored_samples) != 3:
                    raise EvidenceError(f"micro cell sample vector is invalid: {key!r}")
                for stored, actual in zip(stored_samples, ns_samples, strict=True):
                    if not math.isclose(
                        positive_number(stored, f"{key!r}.sample"),
                        actual,
                        rel_tol=1e-12,
                        abs_tol=1e-12,
                    ):
                        raise EvidenceError(
                            f"micro cell samples disagree with process evidence: {key!r}"
                        )
                median_ns = statistics.median(ns_samples)
                stored_median = positive_number(
                    cell.get("median_ns_per_iter"), f"{key!r}.median"
                )
                if not math.isclose(
                    stored_median, median_ns, rel_tol=1e-12, abs_tol=1e-12
                ):
                    raise EvidenceError(
                        f"micro cell median disagrees with process evidence: {key!r}"
                    )
                benchmark_medians.append(median_ns)
                rss_medians[key] = float(statistics.median(rss_samples))
            elif status == "censored":
                complete = False
                if not records or not any(record.timed_out for record in records):
                    raise EvidenceError(
                        f"censored micro cell lacks timeout evidence: {key!r}"
                    )
                if (
                    cell.get("comparable") is not False
                    or cell.get("robust_selected") is not False
                ):
                    raise EvidenceError(
                        f"censored micro cell cannot be selected: {key!r}"
                    )
            else:
                raise EvidenceError(f"unsupported micro cell status: {status!r}")
        if complete:
            comparable.append(benchmark)
            if min(benchmark_medians) >= ROBUST_THRESHOLD_NS:
                robust.append(benchmark)

    selection = require_mapping(result.get("selection"), "micro.selection")
    stored_comparable = require_list(
        selection.get("comparable_benchmarks"), "micro.selection.comparable_benchmarks"
    )
    stored_robust = require_list(
        selection.get("robust_benchmarks"), "micro.selection.robust_benchmarks"
    )
    if stored_comparable != comparable or stored_robust != robust:
        raise EvidenceError("micro benchmark selection disagrees with cell evidence")
    if selection.get("comparable_count") != len(comparable):
        raise EvidenceError("micro comparable_count mismatch")
    if selection.get("robust_count") != len(robust):
        raise EvidenceError("micro robust_count mismatch")
    if (
        positive_number(
            selection.get("robust_threshold_ns_per_iter"),
            "micro.selection.robust_threshold_ns_per_iter",
        )
        != ROBUST_THRESHOLD_NS
    ):
        raise EvidenceError("micro robustness threshold mismatch")
    if production_contract and (
        len(comparable) != PRODUCTION_COMPARABLE_COUNT
        or len(robust) != PRODUCTION_ROBUST_COUNT
    ):
        raise EvidenceError("micro production selection count mismatch")

    comparable_set = set(comparable)
    robust_set = set(robust)
    leaves: list[MicroLeaf] = []
    for allocator in ALLOCATOR_ORDER[1:]:
        for benchmark in comparable:
            subject = indexed_cells[(allocator, benchmark)]
            reference = indexed_cells[(REFERENCE_ALLOCATOR, benchmark)]
            subject_ns = positive_number(
                subject.get("median_ns_per_iter"), f"{allocator}.{benchmark}.median"
            )
            reference_ns = positive_number(
                reference.get("median_ns_per_iter"), f"unialloc.{benchmark}.median"
            )
            subject_rss = rss_medians[(allocator, benchmark)]
            reference_rss = rss_medians[(REFERENCE_ALLOCATOR, benchmark)]
            stored_ratio = positive_number(
                subject.get("ratio_vs_unialloc"), f"{allocator}.{benchmark}.ratio"
            )
            calculated_ratio = subject_ns / reference_ns
            if not math.isclose(
                stored_ratio, calculated_ratio, rel_tol=1e-12, abs_tol=1e-12
            ):
                raise EvidenceError(
                    f"micro stored ratio mismatch: {allocator}.{benchmark}"
                )
            leaves.append(
                MicroLeaf(
                    allocator=allocator,
                    label=ALLOCATOR_LABELS[allocator],
                    benchmark=benchmark,
                    family=family_of(benchmark),
                    performance_ratio=calculated_ratio,
                    rss_ratio=subject_rss / reference_rss,
                    robust=benchmark in robust_set,
                    reference_performance_median=reference_ns,
                    subject_performance_median=subject_ns,
                    reference_rss_median=reference_rss,
                    subject_rss_median=subject_rss,
                )
            )
    if any(leaf.benchmark not in comparable_set for leaf in leaves):
        raise AssertionError("internal comparable selection error")
    return result, tuple(leaves)


def measurement_ratios(
    rows: Sequence[Mapping[str, Any]],
    *,
    target_id: str,
    harness_id: str,
    metric_direction: str,
) -> Mapping[str, Mapping[str, float]]:
    indexed: dict[tuple[int, str], Mapping[str, Any]] = {}
    for index, row in enumerate(rows):
        round_number = row.get("round")
        variant = row.get("variant")
        if round_number not in TYPE_ISOLATION_ROUNDS or variant not in TYPE_VARIANTS:
            raise EvidenceError(
                f"{target_id}.{harness_id}.measurements[{index}] identity is invalid"
            )
        key = (int(round_number), str(variant))
        if key in indexed:
            raise EvidenceError(f"duplicate Type Isolation measurement: {key!r}")
        positive_number(row.get("performance"), f"{target_id}.{harness_id}.performance")
        positive_number(
            row.get("peak_rss_mib"), f"{target_id}.{harness_id}.peak_rss_mib"
        )
        indexed[key] = row
    expected = {
        (round_number, variant)
        for round_number in TYPE_ISOLATION_ROUNDS
        for variant in TYPE_VARIANTS
    }
    if set(indexed) != expected:
        raise EvidenceError(
            f"{target_id}.{harness_id} measurement matrix is incomplete"
        )
    if metric_direction not in ("lower_is_better", "higher_is_better"):
        raise EvidenceError(f"{target_id}.{harness_id} metric direction is invalid")
    comparisons: dict[str, Mapping[str, float]] = {}
    for family, contract in COMPARISON_FAMILIES.items():
        performance_round_ratios: list[float] = []
        rss_round_ratios: list[float] = []
        for round_number in TYPE_ISOLATION_ROUNDS:
            reference = indexed[(round_number, contract["reference"])]
            subject = indexed[(round_number, contract["subject"])]
            reference_performance = float(reference["performance"])
            subject_performance = float(subject["performance"])
            if metric_direction == "lower_is_better":
                performance_round_ratios.append(
                    subject_performance / reference_performance
                )
            else:
                performance_round_ratios.append(
                    reference_performance / subject_performance
                )
            rss_round_ratios.append(
                float(subject["peak_rss_mib"]) / float(reference["peak_rss_mib"])
            )
        comparisons[family] = {
            "performance": float(statistics.median(performance_round_ratios)),
            "peak_rss": float(statistics.median(rss_round_ratios)),
        }
    return comparisons


def validate_stored_comparisons(
    harness: Mapping[str, Any],
    calculated: Mapping[str, Mapping[str, float]],
    *,
    context: str,
) -> None:
    stored = require_mapping(
        harness.get("comparison_families"), f"{context}.comparison_families"
    )
    if set(stored) != set(COMPARISON_FAMILIES):
        raise EvidenceError(f"{context} comparison-family inventory mismatch")
    for family, contract in COMPARISON_FAMILIES.items():
        value = require_mapping(stored[family], f"{context}.{family}")
        if (
            value.get("reference") != contract["reference"]
            or value.get("subject") != contract["subject"]
        ):
            raise EvidenceError(f"{context}.{family} comparison identity mismatch")
        for stored_name, calculated_name in (
            ("execution_cost_ratio_median", "performance"),
            ("peak_rss_ratio_median", "peak_rss"),
        ):
            observed = positive_number(
                value.get(stored_name), f"{context}.{family}.{stored_name}"
            )
            expected = calculated[family][calculated_name]
            if not math.isclose(observed, expected, rel_tol=1e-12, abs_tol=1e-12):
                raise EvidenceError(
                    f"{context}.{family}.{stored_name} disagrees with paired rounds"
                )


def validate_macro_evidence(
    result_path: Path,
    *,
    production_contract: bool,
) -> tuple[dict[str, Any], tuple[HarnessRatios, ...]]:
    result = load_json(result_path)
    if result.get("schema_version") != MACRO_SCHEMA_VERSION:
        raise EvidenceError("Type Isolation result schema_version mismatch")
    if result.get("suite_id") != MACRO_SUITE_ID:
        raise EvidenceError("Type Isolation suite_id mismatch")
    if result.get("status") not in ("complete", "complete_with_attribution_limits"):
        raise EvidenceError("Type Isolation result is incomplete")
    revision = require_lower_hex(
        result.get("implementation_revision"),
        40,
        "Type Isolation implementation revision",
    )
    implementation_hash = require_lower_hex(
        result.get("implementation_sha256"), 64, "Type Isolation implementation digest"
    )
    require_lower_hex(
        result.get("suite_manifest_sha256"), 64, "Type Isolation suite manifest digest"
    )
    require_lower_hex(
        result.get("original_campaign_manifest_sha256"),
        64,
        "Type Isolation original campaign manifest digest",
    )

    targets_raw = require_list(result.get("targets"), "type_isolation.targets")
    target_index: dict[str, Mapping[str, Any]] = {}
    for index, value in enumerate(targets_raw):
        target = require_mapping(value, f"type_isolation.targets[{index}]")
        target_id = target.get("id")
        if target_id not in TARGET_ORDER or target_id in target_index:
            raise EvidenceError("Type Isolation target inventory mismatch")
        target_index[str(target_id)] = target
    if tuple(target_index) != TARGET_ORDER:
        raise EvidenceError("Type Isolation targets must be in canonical order")

    summaries: list[HarnessRatios] = []
    for target_id in TARGET_ORDER:
        target = target_index[target_id]
        expected_model = (
            "fixed_work"
            if target_id in FIXED_WORK_TARGETS
            else "workload_native_adaptive_iterations"
        )
        expected_eligibility = (
            "core" if target_id in FIXED_WORK_TARGETS else "diagnostic_only"
        )
        if target.get("rss_work_model") != expected_model:
            raise EvidenceError(f"{target_id} RSS work model mismatch")
        if target.get("rss_comparison_eligibility") != expected_eligibility:
            raise EvidenceError(f"{target_id} RSS eligibility mismatch")
        if target.get("implementation_revision") != revision:
            raise EvidenceError(f"{target_id} implementation revision mismatch")
        if target.get("implementation_sha256") != implementation_hash:
            raise EvidenceError(f"{target_id} implementation digest mismatch")
        harnesses = require_list(target.get("harnesses"), f"{target_id}.harnesses")
        expected_count = PRODUCTION_TARGET_HARNESS_COUNTS[target_id]
        if production_contract and len(harnesses) != expected_count:
            raise EvidenceError(f"{target_id} production harness inventory mismatch")
        if not production_contract and not harnesses:
            raise EvidenceError(f"{target_id} fixture needs at least one harness")
        seen: set[str] = set()
        for index, value in enumerate(harnesses):
            harness = require_mapping(value, f"{target_id}.harnesses[{index}]")
            harness_id = harness.get("id")
            if not isinstance(harness_id, str) or not harness_id or harness_id in seen:
                raise EvidenceError(f"{target_id} harness inventory is invalid")
            seen.add(harness_id)
            if harness.get("rss_work_model") != expected_model:
                raise EvidenceError(f"{target_id}.{harness_id} RSS work model mismatch")
            if harness.get("rss_comparison_eligibility") != expected_eligibility:
                raise EvidenceError(
                    f"{target_id}.{harness_id} RSS eligibility mismatch"
                )
            gates = require_mapping(
                harness.get("gates"), f"{target_id}.{harness_id}.gates"
            )
            for gate in (
                "correctness",
                "build_success",
                "allocator_activation",
                "actual_mir_provenance",
                "stats_disabled",
                "source_audit_retained",
            ):
                if gates.get(gate) is not True:
                    raise EvidenceError(f"{target_id}.{harness_id} failed gate {gate}")
            if type(gates.get("compiler_route_equivalent")) is not bool:
                raise EvidenceError(
                    f"{target_id}.{harness_id} compiler route classification is invalid"
                )
            rows = [
                require_mapping(row, f"{target_id}.{harness_id}.measurement")
                for row in require_list(
                    harness.get("measurements"),
                    f"{target_id}.{harness_id}.measurements",
                )
            ]
            comparisons = measurement_ratios(
                rows,
                target_id=target_id,
                harness_id=harness_id,
                metric_direction=str(harness.get("metric_direction")),
            )
            context = f"{target_id}.{harness_id}"
            validate_stored_comparisons(harness, comparisons, context=context)
            attribution = require_mapping(
                harness.get("compiler_route_attribution"),
                f"{context}.compiler_route_attribution",
            )
            if attribution.get("accepted_interval") != [0.85, 1.15]:
                raise EvidenceError(f"{context} compiler-route interval mismatch")
            attribution_ratio = positive_number(
                attribution.get("median_cost_ratio"),
                f"{context}.compiler_route_attribution.median_cost_ratio",
            )
            compiler_route_ratio = comparisons["compiler_route"]["performance"]
            if not math.isclose(
                attribution_ratio, compiler_route_ratio, rel_tol=1e-12, abs_tol=1e-12
            ):
                raise EvidenceError(
                    f"{context} compiler-route attribution ratio mismatch"
                )
            equivalent = 0.85 <= compiler_route_ratio <= 1.15
            if gates["compiler_route_equivalent"] is not equivalent:
                raise EvidenceError(
                    f"{context} compiler-route gate disagrees with paired rounds"
                )
            expected_classification = (
                "within_predeclared_interval"
                if equivalent
                else "outside_predeclared_interval"
            )
            if attribution.get("classification") != expected_classification:
                raise EvidenceError(f"{context} compiler-route classification mismatch")
            performance = {
                variant: comparisons[family]["performance"]
                for variant, family in VARIANT_COMPARISON.items()
            }
            rss = {
                variant: comparisons[family]["peak_rss"]
                for variant, family in VARIANT_COMPARISON.items()
            }
            summaries.append(
                HarnessRatios(
                    target_id=target_id,
                    harness_id=harness_id,
                    rss_work_model=expected_model,
                    rss_eligibility=expected_eligibility,
                    performance=performance,
                    rss=rss,
                    comparisons=comparisons,
                    compiler_route_equivalent=equivalent,
                )
            )

    if production_contract:
        collections_count = sum(row.target_id == "collections" for row in summaries)
        macro_count = sum(row.target_id in MACRO_TARGET_ORDER for row in summaries)
        if collections_count != 5 or macro_count != 29:
            raise EvidenceError("Type Isolation production cohort counts mismatch")
    return result, tuple(summaries)


def load_evidence(
    micro_result_path: Path,
    micro_records_path: Path,
    type_isolation_result_path: Path,
    *,
    production_contract: bool,
) -> LoadedEvidence:
    micro_result, micro_leaves = validate_micro_evidence(
        micro_result_path,
        micro_records_path,
        production_contract=production_contract,
    )
    macro_result, harnesses = validate_macro_evidence(
        type_isolation_result_path,
        production_contract=production_contract,
    )
    return LoadedEvidence(
        micro_result=micro_result,
        micro_leaves=micro_leaves,
        macro_result=macro_result,
        harnesses=harnesses,
        source_hashes={
            "micro_results": sha256_file(micro_result_path),
            "micro_records": sha256_file(micro_records_path),
            "type_isolation_results": sha256_file(type_isolation_result_path),
        },
        production_contract=production_contract,
    )


def summarize_micro(
    evidence: LoadedEvidence,
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    rows: list[dict[str, Any]] = []
    std_summary: dict[str, Any] = {}
    for allocator in ALLOCATOR_ORDER[1:]:
        allocator_leaves = [
            leaf for leaf in evidence.micro_leaves if leaf.allocator == allocator
        ]
        metric_summaries: dict[str, Any] = {}
        for metric in ("performance", "peak_rss"):
            selected = (
                [leaf for leaf in allocator_leaves if leaf.robust]
                if metric == "performance"
                else allocator_leaves
            )
            values_by_family: dict[str, list[float]] = defaultdict(list)
            for leaf in selected:
                ratio = (
                    leaf.performance_ratio
                    if metric == "performance"
                    else leaf.rss_ratio
                )
                values_by_family[leaf.family].append(ratio)
            summary = hierarchical_summary(values_by_family)
            summary["diagnostic_only"] = metric == "peak_rss"
            summary["eligibility"] = (
                "timer-floor-safe"
                if metric == "performance"
                else "process-observed adaptive-libtest"
            )
            metric_summaries[metric] = summary

        std_summary[allocator] = {
            "label": ALLOCATOR_LABELS[allocator],
            "performance": metric_summaries["performance"],
            "peak_rss": metric_summaries["peak_rss"],
        }

        for leaf in allocator_leaves:
            for metric in ("performance", "peak_rss"):
                ratio = (
                    leaf.performance_ratio
                    if metric == "performance"
                    else leaf.rss_ratio
                )
                reference_median = (
                    leaf.reference_performance_median
                    if metric == "performance"
                    else leaf.reference_rss_median
                )
                subject_median = (
                    leaf.subject_performance_median
                    if metric == "performance"
                    else leaf.subject_rss_median
                )
                rows.append(
                    {
                        "cohort": "rust_std_bench",
                        "metric": metric,
                        "comparison": allocator,
                        "comparison_label": leaf.label,
                        "aggregation_level": "benchmark_case",
                        "unit_id": leaf.benchmark,
                        "family": leaf.family,
                        "eligible_for_headline": metric == "peak_rss" or leaf.robust,
                        "diagnostic_only": metric == "peak_rss"
                        or (metric == "performance" and not leaf.robust),
                        "reference_median": reference_median,
                        "subject_median": subject_median,
                        "ratio": ratio,
                        "observation_count": 3,
                        "note": (
                            "timer-floor benchmark case excluded from the performance aggregate"
                            if metric == "performance" and not leaf.robust
                            else "process-observed RSS; iteration counts were not captured"
                            if metric == "peak_rss"
                            else ""
                        ),
                    }
                )
        for metric, summary in metric_summaries.items():
            for family, ratio in summary["family_median_ratios"].items():
                rows.append(
                    {
                        "cohort": "rust_std_bench",
                        "metric": metric,
                        "comparison": allocator,
                        "comparison_label": ALLOCATOR_LABELS[allocator],
                        "aggregation_level": "family_median",
                        "unit_id": family,
                        "family": family,
                        "eligible_for_headline": True,
                        "diagnostic_only": metric == "peak_rss",
                        "reference_median": "",
                        "subject_median": "",
                        "ratio": ratio,
                        "observation_count": len(
                            [
                                leaf
                                for leaf in allocator_leaves
                                if leaf.family == family
                                and (metric == "peak_rss" or leaf.robust)
                            ]
                        ),
                        "note": "back-transformed median log-ratio within benchmark family",
                    }
                )
            rows.append(
                {
                    "cohort": "rust_std_bench",
                    "metric": metric,
                    "comparison": allocator,
                    "comparison_label": ALLOCATOR_LABELS[allocator],
                    "aggregation_level": "geometric_mean_across_family_medians",
                    "unit_id": "all_families",
                    "family": "",
                    "eligible_for_headline": True,
                    "diagnostic_only": metric == "peak_rss",
                    "reference_median": "",
                    "subject_median": "",
                    "ratio": summary["headline_ratio"],
                    "observation_count": summary["observation_count"],
                    "note": "unweighted geometric mean of back-transformed family median log-ratios",
                }
            )
            rows.append(
                {
                    "cohort": "rust_std_bench",
                    "metric": metric,
                    "comparison": allocator,
                    "comparison_label": ALLOCATOR_LABELS[allocator],
                    "aggregation_level": "geometric_mean_across_family_geometric_means",
                    "unit_id": "all_families",
                    "family": "",
                    "eligible_for_headline": False,
                    "diagnostic_only": True,
                    "reference_median": "",
                    "subject_median": "",
                    "ratio": summary[
                        "sensitivity_geometric_mean_of_family_geometric_means_ratio"
                    ],
                    "observation_count": summary["observation_count"],
                    "note": "unweighted geometric mean of within-family geometric means; sensitivity analysis",
                }
            )

    collections = [row for row in evidence.harnesses if row.target_id == "collections"]
    collections_summary: dict[str, Any] = {}
    for variant in PLOTTED_TYPE_VARIANTS:
        metrics: dict[str, Any] = {}
        for metric in ("performance", "peak_rss"):
            ratios = [
                row.performance[variant]
                if metric == "performance"
                else row.rss[variant]
                for row in collections
            ]
            summary = hierarchical_summary(
                {
                    row.harness_id: [ratio]
                    for row, ratio in zip(collections, ratios, strict=True)
                }
            )
            summary["diagnostic_only"] = metric == "peak_rss"
            metrics[metric] = summary
            for harness, ratio in zip(collections, ratios, strict=True):
                rows.append(
                    {
                        "cohort": "collections_type_isolation_subset",
                        "metric": metric,
                        "comparison": variant,
                        "comparison_label": TYPE_LABELS[variant],
                        "aggregation_level": "benchmark_case",
                        "unit_id": harness.harness_id,
                        "family": harness.harness_id,
                        "eligible_for_headline": True,
                        "diagnostic_only": metric == "peak_rss",
                        "reference_median": "",
                        "subject_median": "",
                        "ratio": ratio,
                        "observation_count": 5,
                        "note": "five-case Collections diagnostic cohort",
                    }
                )
            rows.append(
                {
                    "cohort": "collections_type_isolation_subset",
                    "metric": metric,
                    "comparison": variant,
                    "comparison_label": TYPE_LABELS[variant],
                    "aggregation_level": "geometric_mean_across_benchmark_cases",
                    "unit_id": "collections",
                    "family": "",
                    "eligible_for_headline": True,
                    "diagnostic_only": metric == "peak_rss",
                    "reference_median": "",
                    "subject_median": "",
                    "ratio": summary["headline_ratio"],
                    "observation_count": len(collections),
                    "note": "geometric mean across five benchmark-case ratios; retained outside the primary figure",
                }
            )
        collections_summary[variant] = {"label": TYPE_LABELS[variant], **metrics}

    selection = require_mapping(evidence.micro_result["selection"], "micro.selection")
    presentation = {
        "rust_std_bench": {
            "benchmark_count": evidence.micro_result["inventory"]["benchmark_count"],
            "comparable_leaf_count": selection["comparable_count"],
            "family_count": len(evidence.micro_result["inventory"]["family_counts"]),
            "performance_headline_leaf_count": selection["robust_count"],
            "performance_threshold_ns_per_iter": ROBUST_THRESHOLD_NS,
            "peak_rss_claim": "process-observed adaptive-libtest diagnostic only",
            "reference": "unialloc",
            "variants": std_summary,
        },
        "collections_type_isolation_subset": {
            "cohort_pooling": "separate",
            "benchmark_case_count": len(collections),
            "reference": "unialloc",
            "rss_claim": "process-observed adaptive-iteration diagnostic only",
            "variants": collections_summary,
        },
    }
    return presentation, rows


def summarize_macro(
    evidence: LoadedEvidence,
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    rows: list[dict[str, Any]] = []
    target_summaries: dict[str, Any] = {}
    for target_id in MACRO_TARGET_ORDER:
        target_harnesses = [
            row for row in evidence.harnesses if row.target_id == target_id
        ]
        target_entry: dict[str, Any] = {
            "label": TARGET_LABELS[target_id],
            "workload_count": len(target_harnesses),
            "rss_work_model": target_harnesses[0].rss_work_model,
            "rss_eligibility": target_harnesses[0].rss_eligibility,
            "comparison_families": {},
        }
        for family, contract in COMPARISON_FAMILIES.items():
            family_entry: dict[str, Any] = {
                "label": contract["label"],
                "reference": contract["reference"],
                "subject": contract["subject"],
            }
            for metric in ("performance", "peak_rss"):
                ratios = [row.comparisons[family][metric] for row in target_harnesses]
                target_ratio = geometric_mean(ratios)
                diagnostic = metric == "peak_rss" and target_id in ADAPTIVE_TARGETS
                metric_entry: dict[str, Any] = {
                    "workload_paired_run_median_ratios": {
                        row.harness_id: ratio
                        for row, ratio in zip(target_harnesses, ratios, strict=True)
                    },
                    "within_target_geometric_mean_ratio": target_ratio,
                    "diagnostic_only": diagnostic,
                }
                if metric == "performance":
                    route_rows = [
                        row for row in target_harnesses if row.compiler_route_equivalent
                    ]
                    metric_entry[
                        "route_equivalent_workload_paired_run_median_ratios"
                    ] = {
                        row.harness_id: row.comparisons[family][metric]
                        for row in route_rows
                    }
                    metric_entry[
                        "route_equivalent_within_target_geometric_mean_ratio"
                    ] = (
                        geometric_mean(
                            row.comparisons[family][metric] for row in route_rows
                        )
                        if route_rows
                        else None
                    )
                family_entry[metric] = metric_entry
                for harness, ratio in zip(target_harnesses, ratios, strict=True):
                    rows.append(
                        {
                            "metric": metric,
                            "comparison": family,
                            "comparison_label": contract["label"],
                            "aggregation_level": "workload_paired_run_median",
                            "target_id": target_id,
                            "target_label": TARGET_LABELS[target_id],
                            "workload_id": harness.harness_id,
                            "rss_work_model": harness.rss_work_model,
                            "eligible_for_suite_headline": metric == "performance"
                            or not diagnostic,
                            "diagnostic_only": diagnostic,
                            "ratio": ratio,
                            "observation_count": 5,
                            "note": (
                                "median of five paired-run ratios; compiler-route-equivalent"
                                if harness.compiler_route_equivalent
                                else "median of five paired-run ratios"
                            ),
                        }
                    )
                rows.append(
                    {
                        "metric": metric,
                        "comparison": family,
                        "comparison_label": contract["label"],
                        "aggregation_level": "within_target_geometric_mean",
                        "target_id": target_id,
                        "target_label": TARGET_LABELS[target_id],
                        "workload_id": "",
                        "rss_work_model": target_harnesses[0].rss_work_model,
                        "eligible_for_suite_headline": metric == "performance"
                        or not diagnostic,
                        "diagnostic_only": diagnostic,
                        "ratio": target_ratio,
                        "observation_count": len(target_harnesses),
                        "note": "geometric mean across workload paired-run medians within target",
                    }
                )
                route_ratio = metric_entry.get(
                    "route_equivalent_within_target_geometric_mean_ratio"
                )
                if route_ratio is not None:
                    rows.append(
                        {
                            "metric": metric,
                            "comparison": family,
                            "comparison_label": contract["label"],
                            "aggregation_level": "route_equivalent_within_target_geometric_mean",
                            "target_id": target_id,
                            "target_label": TARGET_LABELS[target_id],
                            "workload_id": "",
                            "rss_work_model": target_harnesses[0].rss_work_model,
                            "eligible_for_suite_headline": True,
                            "diagnostic_only": False,
                            "ratio": route_ratio,
                            "observation_count": len(
                                metric_entry[
                                    "route_equivalent_workload_paired_run_median_ratios"
                                ]
                            ),
                            "note": "geometric mean across route-equivalent workloads within target",
                        }
                    )
            target_entry["comparison_families"][family] = family_entry
        target_entry["variants"] = {
            variant: {
                "label": TYPE_LABELS[variant],
                "comparison_family": family,
                "performance": target_entry["comparison_families"][family][
                    "performance"
                ],
                "peak_rss": target_entry["comparison_families"][family]["peak_rss"],
            }
            for variant, family in VARIANT_COMPARISON.items()
        }
        target_summaries[target_id] = target_entry

    comparison_suites: dict[str, Any] = {}
    for family, contract in COMPARISON_FAMILIES.items():
        performance_target_ratios = [
            target_summaries[target_id]["comparison_families"][family]["performance"][
                "within_target_geometric_mean_ratio"
            ]
            for target_id in MACRO_TARGET_ORDER
        ]
        rss_target_ratios = [
            target_summaries[target_id]["comparison_families"][family]["peak_rss"][
                "within_target_geometric_mean_ratio"
            ]
            for target_id in FIXED_WORK_TARGETS
        ]
        route_target_ratios = [
            (
                target_id,
                target_summaries[target_id]["comparison_families"][family][
                    "performance"
                ]["route_equivalent_within_target_geometric_mean_ratio"],
            )
            for target_id in MACRO_TARGET_ORDER
            if target_summaries[target_id]["comparison_families"][family][
                "performance"
            ]["route_equivalent_within_target_geometric_mean_ratio"]
            is not None
        ]
        comparison_suites[family] = {
            "label": contract["label"],
            "reference": contract["reference"],
            "subject": contract["subject"],
            "performance": {
                "across_target_geometric_mean_ratio": geometric_mean(
                    performance_target_ratios
                ),
                "target_count": len(MACRO_TARGET_ORDER),
                "workload_count": sum(
                    target_summaries[target_id]["workload_count"]
                    for target_id in MACRO_TARGET_ORDER
                ),
                "route_equivalent_across_target_geometric_mean_ratio": geometric_mean(
                    ratio for _, ratio in route_target_ratios
                ),
                "route_equivalent_target_count": len(route_target_ratios),
                "route_equivalent_workload_count": sum(
                    len(
                        target_summaries[target_id]["comparison_families"][family][
                            "performance"
                        ]["route_equivalent_workload_paired_run_median_ratios"]
                    )
                    for target_id, _ in route_target_ratios
                ),
                "route_equivalent_targets": [
                    target_id for target_id, _ in route_target_ratios
                ],
            },
            "peak_rss": {
                "across_target_geometric_mean_ratio": geometric_mean(rss_target_ratios),
                "target_count": len(FIXED_WORK_TARGETS),
                "workload_count": sum(
                    target_summaries[target_id]["workload_count"]
                    for target_id in FIXED_WORK_TARGETS
                ),
                "included_targets": list(FIXED_WORK_TARGETS),
                "excluded_diagnostic_targets": [
                    target
                    for target in MACRO_TARGET_ORDER
                    if target in ADAPTIVE_TARGETS
                ],
            },
        }
        for metric, target_ids in (
            ("performance", MACRO_TARGET_ORDER),
            ("peak_rss", FIXED_WORK_TARGETS),
        ):
            rows.append(
                {
                    "metric": metric,
                    "comparison": family,
                    "comparison_label": contract["label"],
                    "aggregation_level": "across_target_geometric_mean",
                    "target_id": "all_real_world_targets"
                    if metric == "performance"
                    else "fixed_work_targets",
                    "target_label": "Across-target geometric mean",
                    "workload_id": "",
                    "rss_work_model": "mixed"
                    if metric == "performance"
                    else "fixed_work",
                    "eligible_for_suite_headline": True,
                    "diagnostic_only": False,
                    "ratio": comparison_suites[family][metric][
                        "across_target_geometric_mean_ratio"
                    ],
                    "observation_count": len(target_ids),
                    "note": "unweighted geometric mean of within-target geometric means",
                }
            )
        rows.append(
            {
                "metric": "performance",
                "comparison": family,
                "comparison_label": contract["label"],
                "aggregation_level": "route_equivalent_across_target_geometric_mean",
                "target_id": "route_equivalent_real_world_targets",
                "target_label": "Route-equivalent across-target geometric mean",
                "workload_id": "",
                "rss_work_model": "mixed",
                "eligible_for_suite_headline": True,
                "diagnostic_only": False,
                "ratio": comparison_suites[family]["performance"][
                    "route_equivalent_across_target_geometric_mean_ratio"
                ],
                "observation_count": comparison_suites[family]["performance"][
                    "route_equivalent_target_count"
                ],
                "note": "unweighted geometric mean of route-equivalent within-target geometric means",
            }
        )

    suite_summaries = {
        variant: {
            "label": TYPE_LABELS[variant],
            "comparison_family": family,
            "performance": comparison_suites[family]["performance"],
            "peak_rss": comparison_suites[family]["peak_rss"],
        }
        for variant, family in VARIANT_COMPARISON.items()
    }

    return {
        "reference": "unialloc",
        "target_count": len(MACRO_TARGET_ORDER),
        "workload_count": sum(
            row.target_id in MACRO_TARGET_ORDER for row in evidence.harnesses
        ),
        "target_order": list(MACRO_TARGET_ORDER),
        "targets": target_summaries,
        "suite": suite_summaries,
        "comparison_families": comparison_suites,
    }, rows


def configure_matplotlib() -> None:
    matplotlib.rcParams.update(
        {
            "font.family": "Liberation Sans",
            "font.size": 10.5,
            "axes.edgecolor": AXIS,
            "axes.labelcolor": INK,
            "axes.linewidth": 0.8,
            "xtick.color": AXIS,
            "ytick.color": INK,
            "text.color": INK,
            "figure.facecolor": WHITE,
            "axes.facecolor": WHITE,
            "savefig.facecolor": WHITE,
            "svg.fonttype": "none",
            "svg.hashsalt": "unialloc-two-tier-allocator-evaluation-v1",
        }
    )


def log2_tick(value: float, _: int) -> str:
    return f"{2.0**value:g}x"


def clamp_log2(
    ratio: float,
    ratio_bounds: tuple[float, float] = (2.0**-DISPLAY_LOG2_CAP, 2.0**DISPLAY_LOG2_CAP),
) -> tuple[float, bool]:
    raw = math.log2(ratio)
    lower, upper = (math.log2(value) for value in ratio_bounds)
    displayed = min(upper, max(lower, raw))
    return displayed, displayed != raw


def style_ratio_axis(ax: Axes, label: str) -> None:
    ax.axvline(0.0, color=INK, linewidth=1.0, zorder=1)
    for value in range(-3, 4):
        ax.axvline(value, color=GRID, linewidth=0.6, zorder=0)
    ax.set_xlim(-DISPLAY_LOG2_CAP - 0.1, DISPLAY_LOG2_CAP + 0.1)
    ax.set_xticks(range(-3, 4))
    ax.xaxis.set_major_formatter(FuncFormatter(log2_tick))
    ax.set_xlabel(label, labelpad=10)
    ax.tick_params(axis="y", length=0)
    ax.spines["top"].set_visible(False)
    ax.spines["right"].set_visible(False)
    ax.spines["left"].set_visible(False)


def style_macro_ratio_axis(ax: Axes, label: str) -> None:
    lower, upper = (math.log2(value) for value in MACRO_RATIO_BOUNDS)
    ax.axvline(0.0, color=INK, linewidth=1.0, zorder=1)
    ticks = (0.8, 0.9, 1.0, 1.1, 1.25)
    for ratio in ticks:
        ax.axvline(math.log2(ratio), color=GRID, linewidth=0.6, zorder=0)
    ax.set_xlim(lower - 0.012, upper + 0.012)
    ax.set_xticks([math.log2(value) for value in ticks])
    ax.set_xticklabels([f"{value:g}x" for value in ticks])
    ax.set_xlabel(label, labelpad=10)
    ax.tick_params(axis="y", length=0)
    ax.spines["top"].set_visible(False)
    ax.spines["right"].set_visible(False)
    ax.spines["left"].set_visible(False)


def scatter_ratios(
    ax: Axes,
    ratios: Sequence[float],
    y: float,
    *,
    color: str,
    hollow: bool,
    size: float,
    alpha: float,
    marker: str = "o",
    gid: str | None = None,
    ratio_bounds: tuple[float, float] | None = None,
) -> None:
    bounds = ratio_bounds or (2.0**-DISPLAY_LOG2_CAP, 2.0**DISPLAY_LOG2_CAP)
    clamped = [clamp_log2(value, bounds) for value in ratios]
    displayed = [value for value, _ in clamped]
    offsets = [
        -0.055 + 0.11 * index / max(1, len(displayed) - 1)
        for index in range(len(displayed))
    ]
    marker_groups: dict[str, list[int]] = defaultdict(list)
    for index, ((value, clipped), ratio) in enumerate(
        zip(clamped, ratios, strict=True)
    ):
        displayed_marker = ("<" if value < 0 else ">") if clipped else marker
        marker_groups[displayed_marker].append(index)
    for displayed_marker, indices in marker_groups.items():
        collection = ax.scatter(
            [displayed[index] for index in indices],
            [y + offsets[index] for index in indices],
            s=size,
            marker=displayed_marker,
            facecolors="none" if hollow else color,
            edgecolors=color,
            linewidths=0.8,
            alpha=alpha,
            zorder=3,
        )
        if gid:
            collection.set_gid(
                gid
                if displayed_marker == marker
                else f"{gid}-clipped-{displayed_marker}"
            )


def headline_marker(
    ax: Axes,
    ratio: float,
    y: float,
    *,
    color: str,
    hollow: bool,
    marker: str,
    gid: str,
    ratio_bounds: tuple[float, float] | None = None,
    annotate_clipped: bool = False,
) -> None:
    displayed, clipped = clamp_log2(
        ratio,
        ratio_bounds or (2.0**-DISPLAY_LOG2_CAP, 2.0**DISPLAY_LOG2_CAP),
    )
    actual_marker = ("<" if displayed < 0 else ">") if clipped else marker
    collection = ax.scatter(
        [displayed],
        [y],
        s=84,
        marker=actual_marker,
        facecolors="none" if hollow else color,
        edgecolors=color if hollow else INK,
        linewidths=1.1,
        zorder=5,
    )
    collection.set_gid(gid)
    if clipped and annotate_clipped:
        annotation = ax.annotate(
            f"{ratio:.3g}x",
            (displayed, y),
            xytext=(-7 if displayed > 0.0 else 7, 0),
            textcoords="offset points",
            ha="right" if displayed > 0.0 else "left",
            va="center",
            fontsize=8.0,
            color=INK,
            clip_on=False,
            zorder=6,
        )
        annotation.set_gid(f"{gid}-clipped-value")


def render_micro_figure(
    path_svg: Path,
    path_png: Path,
    micro: Mapping[str, Any],
    micro_rows: Sequence[Mapping[str, Any]],
) -> None:
    configure_matplotlib()
    figure, axes = plt.subplots(1, 2, figsize=FIGURE_SIZE, sharey=True)
    figure.subplots_adjust(left=0.19, right=0.985, top=0.90, bottom=0.22, wspace=0.12)
    row_keys = list(ALLOCATOR_ORDER[1:])
    y_positions = {key: index for index, key in enumerate(row_keys)}
    colors = {key: ORANGE for key in ALLOCATOR_ORDER[1:]}

    for ax, metric, label in (
        (axes[0], "performance", "Execution cost / UniAlloc  |  lower is better"),
        (axes[1], "peak_rss", "Peak RSS / UniAlloc  |  lower is better"),
    ):
        style_ratio_axis(ax, label)
        for key in row_keys:
            y = y_positions[key]
            variant = micro["rust_std_bench"]["variants"][key][metric]
            benchmark_rows = [
                row
                for row in micro_rows
                if row["cohort"] == "rust_std_bench"
                and row["metric"] == metric
                and row["comparison"] == key
                and row["aggregation_level"] == "benchmark_case"
            ]
            if metric == "performance":
                diagnostic = [
                    float(row["ratio"])
                    for row in benchmark_rows
                    if not row["eligible_for_headline"]
                ]
                if diagnostic:
                    scatter_ratios(
                        ax,
                        diagnostic,
                        y,
                        color=PALE,
                        hollow=True,
                        size=9,
                        alpha=0.45,
                        gid=f"timer-floor-{key}",
                    )
                robust = [
                    float(row["ratio"])
                    for row in benchmark_rows
                    if row["eligible_for_headline"]
                ]
                scatter_ratios(
                    ax,
                    robust,
                    y,
                    color=colors[key],
                    hollow=False,
                    size=10,
                    alpha=0.22,
                )
            else:
                scatter_ratios(
                    ax,
                    [float(row["ratio"]) for row in benchmark_rows],
                    y,
                    color=colors[key],
                    hollow=True,
                    size=10,
                    alpha=0.22,
                    gid=f"diagnostic-rss-{key}",
                )
            family_ratios = list(variant["family_median_ratios"].values())
            scatter_ratios(
                ax,
                family_ratios,
                y,
                color=colors[key],
                hollow=metric == "peak_rss",
                size=32,
                alpha=0.95,
                marker="s",
                gid=f"family-medians-{metric}-{key}",
            )
            headline_marker(
                ax,
                float(variant["headline_ratio"]),
                y,
                color=colors[key],
                hollow=metric == "peak_rss",
                marker="D",
                gid=f"headline-micro-{metric}-{key}",
            )
        ax.set_ylim(y_positions[row_keys[-1]] + 0.65, -0.65)

    axes[0].set_yticks([y_positions[key] for key in row_keys])
    axes[0].set_yticklabels([ALLOCATOR_LABELS[key] for key in row_keys])
    axes[1].tick_params(labelleft=False)
    figure.text(
        0.018,
        0.872,
        (
            "Rust std_bench\n"
            f"{micro['rust_std_bench']['performance_headline_leaf_count']} "
            "benchmark cases\n"
            f"median >= {ROBUST_THRESHOLD_NS:g} ns; "
            f"{micro['rust_std_bench']['family_count']} families"
        ),
        ha="left",
        va="top",
        fontsize=9.5,
        color=MUTED,
    )
    legend = [
        Line2D(
            [0],
            [0],
            marker="o",
            linestyle="none",
            color=ORANGE,
            markerfacecolor=ORANGE,
            alpha=0.45,
            label="Per-benchmark ratio",
        ),
        Line2D(
            [0],
            [0],
            marker="s",
            linestyle="none",
            color=ORANGE,
            markerfacecolor=ORANGE,
            label="Family median (log-ratio scale)",
        ),
        Line2D(
            [0],
            [0],
            marker="D",
            linestyle="none",
            color=INK,
            markerfacecolor=ORANGE,
            label="Geometric mean across family medians",
        ),
        Line2D(
            [0],
            [0],
            marker="o",
            linestyle="none",
            color=PALE,
            markerfacecolor=WHITE,
            label="Timer-floor diagnostic",
        ),
        Line2D(
            [0],
            [0],
            marker="o",
            linestyle="none",
            color=ORANGE,
            markerfacecolor=WHITE,
            label="Process-observed RSS diagnostic",
        ),
    ]
    figure.legend(
        handles=legend,
        loc="lower center",
        bbox_to_anchor=(0.57, 0.025),
        ncol=3,
        frameon=False,
        columnspacing=2.0,
        handletextpad=0.6,
    )
    figure.savefig(path_svg, format="svg", metadata={"Date": "2026-07-15"})
    path_svg.write_text(
        "\n".join(
            line.rstrip() for line in path_svg.read_text(encoding="utf-8").splitlines()
        )
        + "\n",
        encoding="utf-8",
    )
    figure.savefig(
        path_png,
        format="png",
        dpi=PNG_DPI,
        metadata={"Software": f"Matplotlib {matplotlib.__version__}"},
    )
    plt.close(figure)


def render_macro_figure(
    path_svg: Path,
    path_png: Path,
    macro: Mapping[str, Any],
) -> None:
    configure_matplotlib()
    figure, axes = plt.subplots(1, 2, figsize=FIGURE_SIZE, sharey=True)
    figure.subplots_adjust(left=0.16, right=0.985, top=0.92, bottom=0.20, wspace=0.12)
    row_keys = ["suite", *MACRO_TARGET_ORDER]
    y_positions = {
        key: index + (0.35 if index else 0.0) for index, key in enumerate(row_keys)
    }
    end_to_end = macro["comparison_families"]["end_to_end"]

    for ax, metric, label in (
        (
            axes[0],
            "performance",
            "Type Isolation / default UniAlloc execution cost  |  lower is better",
        ),
        (
            axes[1],
            "peak_rss",
            "Type Isolation / default UniAlloc peak RSS  |  lower is better",
        ),
    ):
        style_macro_ratio_axis(ax, label)
        suite_ratio = float(end_to_end[metric]["across_target_geometric_mean_ratio"])
        headline_marker(
            ax,
            suite_ratio,
            y_positions["suite"],
            color=BLUE,
            hollow=False,
            marker="D",
            gid=f"headline-macro-end-to-end-{metric}",
            ratio_bounds=MACRO_RATIO_BOUNDS,
            annotate_clipped=True,
        )
        for target_id in MACRO_TARGET_ORDER:
            target = macro["targets"][target_id]
            diagnostic = (
                metric == "peak_rss" and target["rss_eligibility"] == "diagnostic_only"
            )
            metric_data = target["comparison_families"]["end_to_end"][metric]
            y = y_positions[target_id]
            scatter_ratios(
                ax,
                list(metric_data["workload_paired_run_median_ratios"].values()),
                y,
                color=BLUE,
                hollow=diagnostic,
                size=25,
                alpha=0.42,
                gid=f"macro-end-to-end-harnesses-{metric}-{target_id}",
                ratio_bounds=MACRO_RATIO_BOUNDS,
            )
            headline_marker(
                ax,
                float(metric_data["within_target_geometric_mean_ratio"]),
                y,
                color=BLUE,
                hollow=diagnostic,
                marker="s",
                gid=f"target-end-to-end-summary-{metric}-{target_id}",
                ratio_bounds=MACRO_RATIO_BOUNDS,
                annotate_clipped=True,
            )
        separator = ax.axhline(0.8, color=AXIS, linewidth=0.8, zorder=1)
        separator.set_gid("suite-separator")
        ax.set_ylim(y_positions[MACRO_TARGET_ORDER[-1]] + 0.65, -0.6)

    axes[0].set_yticks([y_positions[key] for key in row_keys])
    axes[0].set_yticklabels(
        [
            "Across-target geometric mean",
            *[TARGET_LABELS[key] for key in MACRO_TARGET_ORDER],
        ]
    )
    axes[1].tick_params(labelleft=False)
    legend = [
        Line2D(
            [0],
            [0],
            marker="o",
            linestyle="none",
            color=BLUE,
            markerfacecolor=BLUE,
            alpha=0.42,
            label="Median paired-run ratio (workload, n=5)",
        ),
        Line2D(
            [0],
            [0],
            marker="s",
            linestyle="none",
            color=BLUE,
            markerfacecolor=BLUE,
            label="Within-target geometric mean",
        ),
        Line2D(
            [0],
            [0],
            marker="D",
            linestyle="none",
            color=BLUE,
            markerfacecolor=BLUE,
            label="Across-target geometric mean",
        ),
        Line2D(
            [0],
            [0],
            marker="o",
            linestyle="none",
            color=BLUE,
            markerfacecolor=WHITE,
            label="Adaptive-work RSS diagnostic; excluded from RSS aggregate",
        ),
    ]
    figure.legend(
        handles=legend,
        loc="lower center",
        bbox_to_anchor=(0.57, 0.025),
        ncol=2,
        frameon=False,
        columnspacing=1.8,
        handletextpad=0.55,
    )
    figure.savefig(path_svg, format="svg", metadata={"Date": "2026-07-15"})
    path_svg.write_text(
        "\n".join(
            line.rstrip() for line in path_svg.read_text(encoding="utf-8").splitlines()
        )
        + "\n",
        encoding="utf-8",
    )
    figure.savefig(
        path_png,
        format="png",
        dpi=PNG_DPI,
        metadata={"Software": f"Matplotlib {matplotlib.__version__}"},
    )
    plt.close(figure)


def write_csv(
    path: Path, fields: Sequence[str], rows: Sequence[Mapping[str, Any]]
) -> None:
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields, lineterminator="\n")
        writer.writeheader()
        for row in rows:
            writer.writerow({field: row.get(field, "") for field in fields})


def write_json(path: Path, value: Mapping[str, Any]) -> None:
    path.write_text(
        json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )


def artifact_entry(path: Path) -> dict[str, Any]:
    return {"bytes": path.stat().st_size, "sha256": sha256_file(path)}


def presentation_data(
    evidence: LoadedEvidence,
    micro: Mapping[str, Any],
    macro: Mapping[str, Any],
) -> dict[str, Any]:
    return {
        "schema_version": 2,
        "generated_utc": FIXED_TIMESTAMP,
        "production_contract": evidence.production_contract,
        "lower_ratio_is_better": True,
        "reference": "unialloc",
        "aggregation_contract": {
            "micro_performance": "subject/reference ratio of three-process medians per benchmark case; back-transformed median log-ratio within each family; unweighted geometric mean across family medians; cases below 100 ns excluded from the aggregate",
            "micro_peak_rss": "subject/reference ratio of three-process peak-RSS medians per benchmark case; the same family hierarchy; diagnostic because iteration counts were not captured",
            "macro_performance": "median of five paired-run ratios per workload; geometric mean across workloads within each target; unweighted geometric mean across target summaries",
            "macro_peak_rss": "median of five paired-run ratios per workload; fixed-work targets only in the across-target aggregate; adaptive-work targets diagnostic only",
            "cohort_rule": "the primary micro figure contains the full Rust std_bench allocator cohort; the Collections Type Isolation diagnostic remains in CSV and JSON; real-world targets form the macro population",
            "statistical_units": "benchmark case for microbenchmarks; workload for macrobenchmarks; harness reserved for the execution driver or adapter",
        },
        "display": {
            "log2_ratio_cap": DISPLAY_LOG2_CAP,
            "raw_csv_ratios_capped": False,
        },
        "microbenchmarks": micro,
        "macrobenchmarks": macro,
        "sources": dict(evidence.source_hashes),
    }


def atomic_publish(stage: Path, output_dir: Path) -> None:
    backup: Path | None = None
    if output_dir.exists():
        backup = Path(
            tempfile.mkdtemp(
                prefix=f".{output_dir.name}.backup-", dir=output_dir.parent
            )
        )
        backup.rmdir()
        os.replace(output_dir, backup)
    try:
        os.replace(stage, output_dir)
    except BaseException:
        if backup is not None and backup.exists() and not output_dir.exists():
            os.replace(backup, output_dir)
        raise
    if backup is not None:
        shutil.rmtree(backup)


def build_bundle(evidence: LoadedEvidence, output_dir: Path) -> tuple[Path, ...]:
    micro, micro_rows = summarize_micro(evidence)
    macro, macro_rows = summarize_macro(evidence)
    output_dir.parent.mkdir(parents=True, exist_ok=True)
    stage = Path(
        tempfile.mkdtemp(prefix=f".{output_dir.name}.stage-", dir=output_dir.parent)
    )
    try:
        micro_svg = stage / "microbenchmarks.svg"
        micro_png = stage / "microbenchmarks.png"
        micro_csv = stage / "microbenchmarks.csv"
        macro_svg = stage / "macrobenchmarks.svg"
        macro_png = stage / "macrobenchmarks.png"
        macro_csv = stage / "macrobenchmarks.csv"
        data_json = stage / "presentation-data.json"
        manifest_json = stage / "artifact-manifest.json"

        write_csv(micro_csv, MICRO_CSV_FIELDS, micro_rows)
        write_csv(macro_csv, MACRO_CSV_FIELDS, macro_rows)
        write_json(data_json, presentation_data(evidence, micro, macro))
        render_micro_figure(micro_svg, micro_png, micro, micro_rows)
        render_macro_figure(macro_svg, macro_png, macro)

        artifacts = {
            path.name: artifact_entry(path)
            for path in (
                micro_svg,
                micro_png,
                micro_csv,
                macro_svg,
                macro_png,
                macro_csv,
                data_json,
            )
        }
        write_json(
            manifest_json,
            {
                "schema_version": 1,
                "generated_utc": FIXED_TIMESTAMP,
                "renderer": f"Matplotlib {matplotlib.__version__}",
                "production_contract": evidence.production_contract,
                "sources": dict(evidence.source_hashes),
                "artifacts": artifacts,
            },
        )
        expected_names = {
            "microbenchmarks.svg",
            "microbenchmarks.png",
            "microbenchmarks.csv",
            "macrobenchmarks.svg",
            "macrobenchmarks.png",
            "macrobenchmarks.csv",
            "presentation-data.json",
            "artifact-manifest.json",
        }
        if {path.name for path in stage.iterdir()} != expected_names:
            raise EvidenceError("staged artifact inventory mismatch")
        atomic_publish(stage, output_dir)
    except BaseException:
        if stage.exists():
            shutil.rmtree(stage)
        raise
    return tuple(output_dir / name for name in sorted(expected_names))


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--micro-results", type=Path, default=DEFAULT_MICRO_RESULTS)
    parser.add_argument("--micro-records", type=Path, default=DEFAULT_MICRO_RECORDS)
    parser.add_argument(
        "--type-isolation-results", type=Path, default=DEFAULT_TYPE_ISOLATION_RESULTS
    )
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT_DIR)
    parser.add_argument(
        "--fixture-contract",
        action="store_true",
        help=argparse.SUPPRESS,
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    evidence = load_evidence(
        args.micro_results,
        args.micro_records,
        args.type_isolation_results,
        production_contract=not args.fixture_contract,
    )
    for path in build_bundle(evidence, args.output_dir):
        print(path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
