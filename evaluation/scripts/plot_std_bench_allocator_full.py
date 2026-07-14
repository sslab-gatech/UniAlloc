#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = ["matplotlib==3.11.0"]
# ///
"""Validate, export, and plot the complete Rust std_bench allocator campaign.

The process-level JSONL is the source of truth.  This exporter reconstructs
every allocator/benchmark cell, treats a timeout as an explicit censored
terminal state, and derives comparisons only from leaves with three valid
measured processes for all seven allocators.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import math
import os
import statistics
from collections import Counter, defaultdict
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence


os.environ.setdefault("MPLBACKEND", "Agg")
os.environ.setdefault("SOURCE_DATE_EPOCH", "1783987200")

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib.colors import TwoSlopeNorm
from matplotlib.figure import Figure
from matplotlib.lines import Line2D
from matplotlib.ticker import FuncFormatter


REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
CAMPAIGN_ID = "std-bench-allocator-full-20260714"
DEFAULT_RAW_DIR = REPOSITORY_ROOT / "evaluation" / "raw" / CAMPAIGN_ID
DEFAULT_SUMMARY = DEFAULT_RAW_DIR / "summary.json"
DEFAULT_RECORDS = DEFAULT_RAW_DIR / "records.jsonl"
DEFAULT_RESULT_JSON = REPOSITORY_ROOT / "benchmark-results" / f"{CAMPAIGN_ID}.json"
DEFAULT_FIGURE_DIR = REPOSITORY_ROOT / "docs" / "figures" / CAMPAIGN_ID
DEFAULT_LONG_CSV = DEFAULT_FIGURE_DIR / "std-bench-allocator-full-all-cells.csv"
DEFAULT_DISTRIBUTION_SVG = DEFAULT_FIGURE_DIR / "allocator-ratio-distribution.svg"
DEFAULT_DISTRIBUTION_PNG = DEFAULT_FIGURE_DIR / "allocator-ratio-distribution.png"
DEFAULT_HEATMAP_SVG = DEFAULT_FIGURE_DIR / "allocator-ratio-heatmap.svg"
DEFAULT_HEATMAP_PNG = DEFAULT_FIGURE_DIR / "allocator-ratio-heatmap.png"
DEFAULT_MANIFEST = DEFAULT_FIGURE_DIR / "artifact-manifest.json"

REFERENCE_ALLOCATOR = "unialloc"
EXPECTED_ALLOCATORS = (
    "unialloc",
    "ptmalloc",
    "jemalloc",
    "mimalloc",
    "tcmalloc",
    "snmalloc",
    "scudo",
)
EXPECTED_FEATURES = {
    "unialloc": "bench_ourself",
    "ptmalloc": "bench_ptmalloc",
    "jemalloc": "bench_jemalloc",
    "mimalloc": "bench_mimalloc",
    "tcmalloc": "bench_tcmalloc",
    "snmalloc": "bench_snmalloc",
    "scudo": "bench_scudo",
}
EXPECTED_FAMILY_COUNTS = {
    "binary_heap": 6,
    "btree": 100,
    "linked_list": 9,
    "slice": 74,
    "str": 129,
    "string": 17,
    "vec": 118,
    "vec_deque": 15,
}
EXPECTED_BENCHMARKS = sum(EXPECTED_FAMILY_COUNTS.values())
EXPECTED_MEASURED_SAMPLES = 3
ROBUST_THRESHOLD_NS = 100.0
PNG_DPI = 150
DIST_FIGURE_SIZE = (16, 9)
HEATMAP_FIGURE_SIZE = (14, 32)

INK = "#17181A"
MUTED = "#62646B"
AXIS = "#47484F"
GRID = "#DDDEE1"
PALE = "#D7DCE5"
WHITE = "#FFFFFF"
BLUE = "#2E4780"
ORANGE = "#E46F3E"
EXCLUDED = "#D9DADD"

ALLOCATOR_LABELS = {
    "unialloc": "UniAlloc",
    "ptmalloc": "ptmalloc",
    "jemalloc": "jemalloc",
    "mimalloc": "mimalloc",
    "tcmalloc": "TCMalloc",
    "snmalloc": "snmalloc",
    "scudo": "Scudo",
}


@dataclass(frozen=True)
class ProcessRecord:
    allocator: str
    feature: str
    binary_sha256: str
    benchmark: str
    phase: str
    round: int
    valid: bool
    timed_out: bool
    ns_per_iter: float | None
    stdout_path: str | None
    stderr_path: str | None
    timeout_seconds: float | None
    scudo_runtime_library: str | None


@dataclass(frozen=True)
class Cell:
    allocator: str
    feature: str
    benchmark: str
    family: str
    status: str
    records: tuple[ProcessRecord, ...]
    measured_samples: tuple[ProcessRecord, ...]
    median_ns_per_iter: float | None
    mad_ns_per_iter: float | None
    min_ns_per_iter: float | None
    max_ns_per_iter: float | None
    timeout_phase: str | None
    timeout_round: int | None
    timeout_seconds: float | None
    timeout_reported_ns_per_iter: float | None
    timeout_stdout_path: str | None
    timeout_stderr_path: str | None


def load_json(file: Path) -> dict[str, Any]:
    value = json.loads(file.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"expected a JSON object: {file}")
    return value


def load_jsonl(file: Path) -> list[dict[str, Any]]:
    values: list[dict[str, Any]] = []
    with file.open(encoding="utf-8") as handle:
        for line_number, line in enumerate(handle, start=1):
            if not line.strip():
                continue
            try:
                value = json.loads(line)
            except json.JSONDecodeError as error:
                raise ValueError(f"invalid JSON at {file}:{line_number}: {error}") from error
            if not isinstance(value, dict):
                raise ValueError(f"expected a JSON object at {file}:{line_number}")
            values.append(value)
    return values


def finite_positive(value: Any, *, field: str) -> float:
    try:
        result = float(value)
    except (TypeError, ValueError) as error:
        raise ValueError(f"{field} must be numeric, got {value!r}") from error
    if not math.isfinite(result) or result <= 0:
        raise ValueError(f"{field} must be finite and positive, got {value!r}")
    return result


def finite_nonnegative(value: Any, *, field: str) -> float:
    try:
        result = float(value)
    except (TypeError, ValueError) as error:
        raise ValueError(f"{field} must be numeric, got {value!r}") from error
    if not math.isfinite(result) or result < 0:
        raise ValueError(f"{field} must be finite and non-negative, got {value!r}")
    return result


def optional_positive(value: Any, *, field: str) -> float | None:
    if value is None:
        return None
    return finite_positive(value, field=field)


def string_list(value: Any, *, field: str, allow_empty: bool = False) -> tuple[str, ...]:
    if not isinstance(value, list) or (not value and not allow_empty):
        qualifier = "a list" if allow_empty else "a non-empty list"
        raise ValueError(f"{field} must be {qualifier}")
    result = tuple(value)
    if any(not isinstance(item, str) or not item for item in result):
        raise ValueError(f"{field} must contain non-empty strings")
    if len(result) != len(set(result)):
        raise ValueError(f"{field} contains duplicates")
    return result


def family_of(benchmark: str) -> str:
    return benchmark.split("::", 1)[0]


def validate_inventory(benchmarks: Sequence[str]) -> dict[str, int]:
    if len(benchmarks) != EXPECTED_BENCHMARKS:
        raise ValueError(
            f"canonical inventory must contain {EXPECTED_BENCHMARKS} benchmarks; "
            f"found {len(benchmarks)}"
        )
    if len(set(benchmarks)) != len(benchmarks):
        raise ValueError("canonical inventory contains duplicate benchmarks")
    if any("::" not in benchmark for benchmark in benchmarks):
        raise ValueError("canonical inventory entries must include a family prefix")
    counts = dict(sorted(Counter(family_of(item) for item in benchmarks).items()))
    if counts != EXPECTED_FAMILY_COUNTS:
        raise ValueError(
            "canonical inventory family counts differ from the authenticated "
            f"468-leaf surface: {counts!r}"
        )
    return counts


def campaign_axes(summary: Mapping[str, Any]) -> tuple[tuple[str, ...], tuple[str, ...]]:
    methodology = summary.get("methodology")
    if not isinstance(methodology, Mapping):
        raise ValueError("summary.methodology must be an object")
    allocators = string_list(methodology.get("allocators"), field="methodology.allocators")
    if allocators != EXPECTED_ALLOCATORS:
        raise ValueError(
            "methodology.allocators must be the seven authenticated variants in "
            f"canonical order: {EXPECTED_ALLOCATORS!r}"
        )
    warmups = methodology.get("warmup_fresh_processes_per_cell")
    if warmups != 1:
        raise ValueError("methodology.warmup_fresh_processes_per_cell must equal 1")
    measured = methodology.get("measured_fresh_processes_per_complete_cell")
    if measured is None:
        measured = methodology.get("measured_fresh_processes_per_cell")
    if measured != EXPECTED_MEASURED_SAMPLES:
        raise ValueError(
            "methodology measured fresh processes per complete cell must equal 3"
        )
    benchmarks = string_list(
        methodology.get("benchmarks"), field="methodology.benchmarks"
    )
    validate_inventory(benchmarks)
    return allocators, benchmarks


def parse_record(
    raw: Mapping[str, Any],
    *,
    index: int,
    allocators: set[str],
    benchmarks: set[str],
) -> ProcessRecord:
    allocator = raw.get("allocator")
    benchmark = raw.get("benchmark")
    if allocator not in allocators:
        raise ValueError(f"record {index} has unknown allocator {allocator!r}")
    if benchmark not in benchmarks:
        raise ValueError(f"record {index} has unknown benchmark {benchmark!r}")
    feature = raw.get("feature")
    if not isinstance(feature, str) or not feature:
        raise ValueError(f"record {index} has no Cargo feature")
    expected_feature = EXPECTED_FEATURES[str(allocator)]
    if feature != expected_feature:
        raise ValueError(
            f"record {index} feature mismatch for {allocator}: {feature!r} != "
            f"{expected_feature!r}"
        )
    binary_sha256 = raw.get("binary_sha256")
    if not isinstance(binary_sha256, str) or not binary_sha256:
        raise ValueError(f"record {index} has no binary identity")
    if raw.get("glibc_tunables_present") is not False:
        raise ValueError(f"record {index} violated the clean glibc environment")
    expected_markers = 1 if allocator == "scudo" else 0
    if raw.get("scudo_identity_marker_count") != expected_markers:
        raise ValueError(f"record {index} has the wrong Scudo marker count")
    scudo_runtime = raw.get("scudo_runtime_library")
    if allocator == "scudo":
        if not isinstance(scudo_runtime, str) or not scudo_runtime:
            raise ValueError(f"record {index} has no authenticated Scudo runtime")
    elif scudo_runtime is not None:
        raise ValueError(f"record {index} unexpectedly names a Scudo runtime")
    phase = raw.get("phase")
    if phase not in {"warmup", "measured"}:
        raise ValueError(f"record {index} has invalid phase {phase!r}")
    round_value = raw.get("round")
    if isinstance(round_value, bool) or not isinstance(round_value, int):
        raise ValueError(f"record {index} has invalid round {round_value!r}")
    if (phase == "warmup" and round_value != 0) or (
        phase == "measured" and round_value not in {1, 2, 3}
    ):
        raise ValueError(f"record {index} has invalid phase/round pair")
    valid = raw.get("valid")
    timed_out = raw.get("timed_out")
    if not isinstance(valid, bool) or not isinstance(timed_out, bool):
        raise ValueError(f"record {index} must declare boolean valid/timed_out")
    status = raw.get("status")
    if valid:
        if timed_out or status not in {None, "valid"}:
            raise ValueError(f"record {index} has inconsistent valid state")
        if raw.get("exit_code") != 0 or raw.get("time_exit_status") != 0:
            raise ValueError(f"record {index} has a nonzero valid exit status")
        if raw.get("reported_benchmark") != benchmark:
            raise ValueError(f"record {index} did not report its exact benchmark")
        if raw.get("time_parse_error") is not None:
            raise ValueError(f"record {index} has a GNU time parse error")
        ns_per_iter = finite_nonnegative(
            raw.get("ns_per_iter"), field=f"record {index} ns_per_iter"
        )
    else:
        if not timed_out or status not in {
            "timeout_censored",
            "censored_timeout",
            None,
        }:
            raise ValueError(
                f"record {index} is invalid without an authenticated timeout state"
            )
        if raw.get("reported_benchmark") not in (None, benchmark):
            raise ValueError(f"timeout record {index} reported a different benchmark")
        if int(raw.get("benchmark_line_count", 0)) > 1:
            raise ValueError(f"timeout record {index} reported multiple benchmarks")
        ns_per_iter = (
            finite_nonnegative(
                raw.get("ns_per_iter"),
                field=f"timeout record {index} diagnostic ns_per_iter",
            )
            if raw.get("ns_per_iter") is not None
            else None
        )
    stdout_path = raw.get("stdout_path")
    stderr_path = raw.get("stderr_path")
    if stdout_path is not None and not isinstance(stdout_path, str):
        raise ValueError(f"record {index} has invalid stdout_path")
    if stderr_path is not None and not isinstance(stderr_path, str):
        raise ValueError(f"record {index} has invalid stderr_path")
    return ProcessRecord(
        allocator=str(allocator),
        feature=feature,
        binary_sha256=binary_sha256,
        benchmark=str(benchmark),
        phase=str(phase),
        round=round_value,
        valid=valid,
        timed_out=timed_out,
        ns_per_iter=ns_per_iter,
        stdout_path=stdout_path,
        stderr_path=stderr_path,
        timeout_seconds=optional_positive(
            raw.get("timeout_seconds"), field=f"record {index} timeout_seconds"
        ),
        scudo_runtime_library=(
            str(scudo_runtime) if scudo_runtime is not None else None
        ),
    )


def median_absolute_deviation(values: Iterable[float]) -> float:
    sequence = tuple(values)
    median = float(statistics.median(sequence))
    return float(statistics.median(abs(value - median) for value in sequence))


def classify_cell(records: Sequence[ProcessRecord], *, key: tuple[str, str]) -> Cell:
    by_step: dict[tuple[str, int], ProcessRecord] = {}
    for record in records:
        step = (record.phase, record.round)
        if step in by_step:
            raise ValueError(f"cell {key[0]}/{key[1]} duplicates {step}")
        by_step[step] = record
    warmup = by_step.get(("warmup", 0))
    if warmup is None:
        raise ValueError(f"cell {key[0]}/{key[1]} has no warmup attempt")
    measured = [by_step[step] for step in sorted(by_step) if step[0] == "measured"]
    expected_round = 1
    timeout: ProcessRecord | None = None
    valid_measured: list[ProcessRecord] = []
    if warmup.timed_out:
        timeout = warmup
        if measured:
            raise ValueError(
                f"cell {key[0]}/{key[1]} has records after terminal timeout"
            )
    else:
        for record in measured:
            if timeout is not None:
                raise ValueError(
                    f"cell {key[0]}/{key[1]} has records after terminal timeout"
                )
            if record.round != expected_round:
                raise ValueError(
                    f"cell {key[0]}/{key[1]} measured rounds are not a valid prefix"
                )
            expected_round += 1
            if record.timed_out:
                timeout = record
            else:
                valid_measured.append(record)
    if timeout is None and len(valid_measured) != EXPECTED_MEASURED_SAMPLES:
        raise ValueError(
            f"cell {key[0]}/{key[1]} is incomplete without a terminal timeout"
        )
    if timeout is not None:
        status = "censored"
        median = mad = None
        partial_values = tuple(float(record.ns_per_iter) for record in valid_measured)
        minimum = min(partial_values) if partial_values else None
        maximum = max(partial_values) if partial_values else None
    else:
        status = "complete"
        values = tuple(float(record.ns_per_iter) for record in valid_measured)
        median = float(statistics.median(values))
        mad = median_absolute_deviation(values)
        minimum = min(values)
        maximum = max(values)
    first = records[0]
    return Cell(
        allocator=key[0],
        feature=first.feature,
        benchmark=key[1],
        family=family_of(key[1]),
        status=status,
        records=tuple(sorted(records, key=lambda item: (item.phase == "measured", item.round))),
        measured_samples=tuple(valid_measured),
        median_ns_per_iter=median,
        mad_ns_per_iter=mad,
        min_ns_per_iter=minimum,
        max_ns_per_iter=maximum,
        timeout_phase=timeout.phase if timeout else None,
        timeout_round=timeout.round if timeout else None,
        timeout_seconds=timeout.timeout_seconds if timeout else None,
        timeout_reported_ns_per_iter=timeout.ns_per_iter if timeout else None,
        timeout_stdout_path=timeout.stdout_path if timeout else None,
        timeout_stderr_path=timeout.stderr_path if timeout else None,
    )


def reconstruct_cells(
    raw_records: Sequence[Mapping[str, Any]],
    allocators: Sequence[str],
    benchmarks: Sequence[str],
) -> tuple[Cell, ...]:
    grouped: dict[tuple[str, str], list[ProcessRecord]] = defaultdict(list)
    evidence_paths: set[str] = set()
    binary_identities: dict[str, set[str]] = defaultdict(set)
    scudo_runtimes: set[str] = set()
    for index, raw in enumerate(raw_records, start=1):
        record = parse_record(
            raw,
            index=index,
            allocators=set(allocators),
            benchmarks=set(benchmarks),
        )
        binary_identities[record.allocator].add(record.binary_sha256)
        if record.scudo_runtime_library is not None:
            scudo_runtimes.add(record.scudo_runtime_library)
        for evidence in (record.stdout_path, record.stderr_path):
            if evidence is None:
                continue
            if evidence in evidence_paths:
                raise ValueError(f"process evidence path is reused: {evidence}")
            evidence_paths.add(evidence)
        grouped[(record.allocator, record.benchmark)].append(record)
    for allocator in allocators:
        identities = binary_identities[allocator]
        if len(identities) != 1:
            raise ValueError(
                f"allocator {allocator} records use multiple binary identities"
            )
    if len(scudo_runtimes) != 1:
        raise ValueError("Scudo records use multiple runtime identities")
    expected = {(allocator, benchmark) for allocator in allocators for benchmark in benchmarks}
    if set(grouped) != expected:
        missing = sorted(expected - set(grouped))
        extra = sorted(set(grouped) - expected)
        raise ValueError(f"records do not cover the full matrix; missing={missing[:3]!r} extra={extra[:3]!r}")
    return tuple(
        classify_cell(grouped[(allocator, benchmark)], key=(allocator, benchmark))
        for benchmark in benchmarks
        for allocator in allocators
    )


def geomean(values: Iterable[float]) -> float:
    sequence = tuple(values)
    if not sequence or any(value <= 0 or not math.isfinite(value) for value in sequence):
        raise ValueError("geomean requires finite positive values")
    return math.exp(math.fsum(math.log(value) for value in sequence) / len(sequence))


def quantile(values: Sequence[float], probability: float) -> float:
    ordered = sorted(values)
    if not ordered:
        raise ValueError("quantile requires values")
    position = (len(ordered) - 1) * probability
    lower = math.floor(position)
    upper = math.ceil(position)
    if lower == upper:
        return float(ordered[lower])
    fraction = position - lower
    return float(ordered[lower] + (ordered[upper] - ordered[lower]) * fraction)


def quantiles(values: Sequence[float]) -> dict[str, float]:
    return {
        "p05": quantile(values, 0.05),
        "p25": quantile(values, 0.25),
        "p50": quantile(values, 0.50),
        "p75": quantile(values, 0.75),
        "p95": quantile(values, 0.95),
    }


def comparable_and_robust(
    cells: Sequence[Cell], benchmarks: Sequence[str], allocators: Sequence[str]
) -> tuple[tuple[str, ...], tuple[str, ...]]:
    by_key = {(cell.allocator, cell.benchmark): cell for cell in cells}
    complete = tuple(
        benchmark
        for benchmark in benchmarks
        if all(by_key[(allocator, benchmark)].status == "complete" for allocator in allocators)
    )
    comparable = tuple(
        benchmark
        for benchmark in complete
        if all(
            float(by_key[(allocator, benchmark)].median_ns_per_iter) > 0
            for allocator in allocators
        )
    )
    robust = tuple(
        benchmark
        for benchmark in comparable
        if all(
            float(by_key[(allocator, benchmark)].median_ns_per_iter) >= ROBUST_THRESHOLD_NS
            for allocator in allocators
        )
    )
    return comparable, robust


def extrema_document(
    benchmark: str,
    ratio: float,
    allocator_cell: Cell,
    reference_cell: Cell,
) -> dict[str, Any]:
    return {
        "benchmark": benchmark,
        "family": family_of(benchmark),
        "ratio_vs_unialloc": ratio,
        "allocator_median_ns_per_iter": allocator_cell.median_ns_per_iter,
        "unialloc_median_ns_per_iter": reference_cell.median_ns_per_iter,
        "allocator_samples_ns_per_iter": [
            sample.ns_per_iter for sample in allocator_cell.measured_samples
        ],
        "unialloc_samples_ns_per_iter": [
            sample.ns_per_iter for sample in reference_cell.measured_samples
        ],
    }


def distribution(
    allocator: str,
    selected: Sequence[str],
    cell_map: Mapping[tuple[str, str], Cell],
) -> dict[str, Any]:
    rows: list[tuple[str, float, Cell, Cell]] = []
    for benchmark in selected:
        allocator_cell = cell_map[(allocator, benchmark)]
        reference_cell = cell_map[(REFERENCE_ALLOCATOR, benchmark)]
        ratio = float(allocator_cell.median_ns_per_iter) / float(
            reference_cell.median_ns_per_iter
        )
        rows.append((benchmark, ratio, allocator_cell, reference_cell))
    ratios = [row[1] for row in rows]
    minimum = min(rows, key=lambda row: (row[1], row[0]))
    maximum = min(rows, key=lambda row: (-row[1], row[0]))
    by_family: dict[str, list[float]] = defaultdict(list)
    for benchmark, ratio, _, _ in rows:
        by_family[family_of(benchmark)].append(ratio)
    family_geomeans = {
        family: geomean(by_family[family]) for family in sorted(by_family)
    }
    leaf_geomean = geomean(ratios)
    return {
        "benchmark_count": len(rows),
        "leaf_geomean": leaf_geomean,
        "geomean": leaf_geomean,
        "family_balanced_geomean": geomean(family_geomeans.values()),
        "family_count": len(family_geomeans),
        "family_geomeans": family_geomeans,
        "quantiles": quantiles(ratios),
        "minimum": extrema_document(*minimum),
        "maximum": extrema_document(*maximum),
    }


def distributions(
    cells: Sequence[Cell],
    allocators: Sequence[str],
    comparable: Sequence[str],
    robust: Sequence[str],
) -> list[dict[str, Any]]:
    cell_map = {(cell.allocator, cell.benchmark): cell for cell in cells}
    return [
        {
            "allocator": allocator,
            "feature": EXPECTED_FEATURES[allocator],
            "label": ALLOCATOR_LABELS[allocator],
            "raw": distribution(allocator, comparable, cell_map),
            "robust": distribution(allocator, robust, cell_map),
        }
        for allocator in allocators
    ]


def numeric_close(left: Any, right: float) -> bool:
    try:
        number = float(left)
    except (TypeError, ValueError):
        return False
    return math.isclose(number, right, rel_tol=1e-11, abs_tol=1e-12)


def validate_summary(
    summary: Mapping[str, Any],
    cells: Sequence[Cell],
    comparable: Sequence[str],
    robust: Sequence[str],
    derived_distributions: Sequence[Mapping[str, Any]],
) -> None:
    cell_map = {(cell.allocator, cell.benchmark): cell for cell in cells}
    complete_all_benchmarks = [
        benchmark
        for benchmark in dict.fromkeys(cell.benchmark for cell in cells)
        if all(
            cell_map[(allocator, benchmark)].status == "complete"
            for allocator in EXPECTED_ALLOCATORS
        )
    ]
    source_cells = summary.get("cells")
    if source_cells is not None:
        if not isinstance(source_cells, list):
            raise ValueError("summary.cells must be a list")
        source_map: dict[tuple[str, str], Mapping[str, Any]] = {}
        for source in source_cells:
            if not isinstance(source, Mapping):
                raise ValueError("summary.cells entries must be objects")
            key = (source.get("allocator"), source.get("benchmark"))
            if key in source_map:
                raise ValueError(f"summary.cells duplicates {key!r}")
            source_map[key] = source
        complete_keys = {key for key, cell in cell_map.items() if cell.status == "complete"}
        if set(source_map) not in (set(cell_map), complete_keys):
            raise ValueError("summary.cells does not match the reconstructed campaign matrix")
        for key, source in source_map.items():
            if key not in cell_map:
                raise ValueError(f"summary.cells contains unknown cell {key!r}")
            cell = cell_map[key]
            if source.get("status", cell.status) != cell.status:
                raise ValueError(f"summary cell state mismatch for {key!r}")
            measured_rounds = source.get("valid_measured_rounds")
            if measured_rounds is not None and list(measured_rounds) != [
                record.round for record in cell.measured_samples
            ]:
                raise ValueError(f"summary measured rounds mismatch for {key!r}")
            samples = source.get("ns_per_iter_samples")
            if samples is not None and list(samples) != [
                record.ns_per_iter for record in cell.measured_samples
            ]:
                raise ValueError(f"summary samples mismatch for {key!r}")
            for field, derived in (
                ("median_ns_per_iter", cell.median_ns_per_iter),
                ("mad_ns_per_iter", cell.mad_ns_per_iter),
                ("min_ns_per_iter", cell.min_ns_per_iter),
                ("max_ns_per_iter", cell.max_ns_per_iter),
            ):
                if field not in source:
                    continue
                if derived is None:
                    matches = source[field] is None
                else:
                    matches = numeric_close(source[field], float(derived))
                if not matches:
                    raise ValueError(f"summary {field} mismatch for {key!r}")
            if "ratio_vs_unialloc" in source:
                reference = cell_map[(REFERENCE_ALLOCATOR, cell.benchmark)]
                expected_ratio = (
                    float(cell.median_ns_per_iter)
                    / float(reference.median_ns_per_iter)
                    if cell.benchmark in comparable
                    else None
                )
                if expected_ratio is None:
                    matches = source["ratio_vs_unialloc"] is None
                else:
                    matches = numeric_close(source["ratio_vs_unialloc"], expected_ratio)
                if not matches:
                    raise ValueError(f"summary ratio mismatch for {key!r}")

    source_censored = summary.get("censored_cells")
    if source_censored is not None:
        if not isinstance(source_censored, list):
            raise ValueError("summary.censored_cells must be a list")
        source_map = {}
        for source in source_censored:
            if not isinstance(source, Mapping):
                raise ValueError("summary.censored_cells entries must be objects")
            key = (source.get("allocator"), source.get("benchmark"))
            if key in source_map:
                raise ValueError(f"summary.censored_cells duplicates {key!r}")
            source_map[key] = source
        derived_keys = {key for key, cell in cell_map.items() if cell.status == "censored"}
        if set(source_map) != derived_keys:
            raise ValueError("summary.censored_cells does not match timeout records")
        for key, source in source_map.items():
            cell = cell_map[key]
            source_phase = source.get("phase", source.get("timeout_phase"))
            source_round = source.get("round", source.get("timeout_round"))
            if source_phase != cell.timeout_phase or source_round != cell.timeout_round:
                raise ValueError(f"summary censoring mismatch for {key!r}")

    comparison = summary.get("comparison_selection")
    if comparison is not None:
        if not isinstance(comparison, Mapping):
            raise ValueError("summary.comparison_selection must be an object")
        declared = comparison.get("comparable_benchmarks")
        if declared is not None and list(declared) != list(comparable):
            raise ValueError("summary.comparison_selection comparable_benchmarks drift")
        complete_all = comparison.get("complete_all_allocator_benchmarks")
        if complete_all is not None and list(complete_all) != complete_all_benchmarks:
            raise ValueError("summary complete-all selection differs from reconstructed cells")
        non_positive = comparison.get("non_positive_median_benchmarks")
        expected_non_positive = [
            benchmark
            for benchmark in complete_all_benchmarks
            if benchmark not in comparable
        ]
        if non_positive is not None and list(non_positive) != expected_non_positive:
            raise ValueError("summary non-positive-median selection drift")

    robustness = summary.get("robustness_selection")
    if robustness is not None:
        if not isinstance(robustness, Mapping):
            raise ValueError("summary.robustness_selection must be an object")
        threshold = robustness.get("threshold_ns_per_iter")
        if threshold is not None and not numeric_close(threshold, ROBUST_THRESHOLD_NS):
            raise ValueError("summary.robustness_selection threshold must equal 100 ns/iter")
        declared = robustness.get("selected_benchmarks")
        if declared is not None and list(declared) != list(robust):
            raise ValueError("summary.robustness_selection selected_benchmarks drift")
        if "selected_count" in robustness and robustness["selected_count"] != len(robust):
            raise ValueError("summary.robustness_selection selected_count drift")

    source_aggregates = summary.get("aggregates")
    if source_aggregates is not None:
        if not isinstance(source_aggregates, list):
            raise ValueError("summary.aggregates must be a list")
        derived_map = {row["allocator"]: row for row in derived_distributions}
        for source in source_aggregates:
            if not isinstance(source, Mapping) or source.get("allocator") not in derived_map:
                raise ValueError("summary.aggregates contains an unknown allocator")
            derived = derived_map[str(source["allocator"])]
            for subset in ("raw", "robust"):
                source_subset = source.get(subset)
                if source_subset is None:
                    continue
                if not isinstance(source_subset, Mapping):
                    raise ValueError(f"summary aggregate {subset} must be an object")
                derived_subset = derived[subset]
                aliases = {
                    "benchmark_count": "benchmark_count",
                    "geomean": "geomean",
                    "family_balanced_geomean": "family_balanced_geomean",
                    "family_count": "family_count",
                }
                for source_field, derived_field in aliases.items():
                    if source_field not in source_subset:
                        continue
                    expected = derived_subset[derived_field]
                    if isinstance(expected, int):
                        matches = source_subset[source_field] == expected
                    else:
                        matches = numeric_close(source_subset[source_field], float(expected))
                    if not matches:
                        raise ValueError(
                            f"summary aggregate {source['allocator']}/{subset}/{source_field} drift"
                        )
                source_quantiles = source_subset.get("quantiles")
                if source_quantiles is not None:
                    if not isinstance(source_quantiles, Mapping):
                        raise ValueError("summary aggregate quantiles must be an object")
                    for name, expected in derived_subset["quantiles"].items():
                        if name in source_quantiles and not numeric_close(
                            source_quantiles[name], float(expected)
                        ):
                            raise ValueError(
                                f"summary aggregate {source['allocator']}/{subset}/{name} drift"
                            )
                for extreme_name in ("minimum", "maximum"):
                    source_extreme = source_subset.get(extreme_name)
                    if source_extreme is None:
                        continue
                    if not isinstance(source_extreme, Mapping):
                        raise ValueError("summary aggregate extrema must be objects")
                    derived_extreme = derived_subset[extreme_name]
                    for name in (
                        "benchmark",
                        "family",
                    ):
                        if name in source_extreme and source_extreme[name] != derived_extreme[name]:
                            raise ValueError(
                                f"summary aggregate {source['allocator']}/{subset}/{extreme_name}/{name} drift"
                            )
                    for name in (
                        "ratio_vs_unialloc",
                        "allocator_median_ns_per_iter",
                        "unialloc_median_ns_per_iter",
                    ):
                        if name in source_extreme and not numeric_close(
                            source_extreme[name], float(derived_extreme[name])
                        ):
                            raise ValueError(
                                f"summary aggregate {source['allocator']}/{subset}/{extreme_name}/{name} drift"
                            )


def sha256_file(file: Path) -> str:
    digest = hashlib.sha256()
    with file.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def display_path(file: Path) -> str:
    resolved = file.resolve()
    try:
        return str(resolved.relative_to(REPOSITORY_ROOT))
    except ValueError:
        return str(resolved)


def artifact_descriptor(file: Path) -> dict[str, Any]:
    return {
        "path": display_path(file),
        "sha256": sha256_file(file),
        "bytes": file.stat().st_size,
    }


def cell_document(
    cell: Cell,
    *,
    comparable_set: set[str],
    robust_set: set[str],
    cell_map: Mapping[tuple[str, str], Cell],
) -> dict[str, Any]:
    document: dict[str, Any] = {
        "allocator": cell.allocator,
        "feature": cell.feature,
        "benchmark": cell.benchmark,
        "family": cell.family,
        "status": cell.status,
        "valid_measured_rounds": [record.round for record in cell.measured_samples],
        "sample_ns_per_iter": [record.ns_per_iter for record in cell.measured_samples],
        "comparable": cell.benchmark in comparable_set,
        "robust_selected": cell.benchmark in robust_set,
    }
    if cell.status == "complete":
        reference = cell_map[(REFERENCE_ALLOCATOR, cell.benchmark)]
        document.update(
            {
                "median_ns_per_iter": cell.median_ns_per_iter,
                "mad_ns_per_iter": cell.mad_ns_per_iter,
                "min_ns_per_iter": cell.min_ns_per_iter,
                "max_ns_per_iter": cell.max_ns_per_iter,
                "ratio_vs_unialloc": (
                    float(cell.median_ns_per_iter) / float(reference.median_ns_per_iter)
                    if cell.benchmark in comparable_set
                    else None
                ),
            }
        )
    else:
        document["censoring"] = {
            "phase": cell.timeout_phase,
            "round": cell.timeout_round,
            "timeout_seconds": cell.timeout_seconds,
            "diagnostic_reported_ns_per_iter": cell.timeout_reported_ns_per_iter,
            "stdout_path": cell.timeout_stdout_path,
            "stderr_path": cell.timeout_stderr_path,
        }
    return document


def censored_document(cell: Cell) -> dict[str, Any]:
    return {
        "allocator": cell.allocator,
        "feature": cell.feature,
        "benchmark": cell.benchmark,
        "family": cell.family,
        "timeout_phase": cell.timeout_phase,
        "timeout_round": cell.timeout_round,
        "timeout_seconds": cell.timeout_seconds,
        "diagnostic_reported_ns_per_iter": cell.timeout_reported_ns_per_iter,
        "valid_measured_rounds_before_timeout": [
            record.round for record in cell.measured_samples
        ],
        "valid_measured_samples_ns_per_iter": [
            record.ns_per_iter for record in cell.measured_samples
        ],
        "stdout_path": cell.timeout_stdout_path,
        "stderr_path": cell.timeout_stderr_path,
    }


def result_document(
    summary: Mapping[str, Any],
    summary_path: Path,
    records_path: Path,
    benchmarks: Sequence[str],
    family_counts: Mapping[str, int],
    cells: Sequence[Cell],
    comparable: Sequence[str],
    robust: Sequence[str],
    derived_distributions: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    cell_map = {(cell.allocator, cell.benchmark): cell for cell in cells}
    comparable_set = set(comparable)
    robust_set = set(robust)
    complete_count = sum(cell.status == "complete" for cell in cells)
    censored = [cell for cell in cells if cell.status == "censored"]
    return {
        "schema_version": 2,
        "source": "rust-std-bench-full-allocator-feature-variant-distribution",
        "success": True,
        "generated_utc": summary.get("generated_utc"),
        "diagnostic_label": summary.get("diagnostic_label"),
        "claim_grade": bool(summary.get("claim_grade", False)),
        "input_artifacts": {
            "summary": artifact_descriptor(summary_path),
            "records": artifact_descriptor(records_path),
        },
        "methodology": {
            "reference_allocator": REFERENCE_ALLOCATOR,
            "warmup_fresh_processes_per_cell": 1,
            "measured_fresh_processes_per_complete_cell": EXPECTED_MEASURED_SAMPLES,
            "cell_statistic": "median ns/iter of exactly 3 measured fresh processes",
            "comparison_rule": "all seven allocator cells are complete",
            "robustness_rule": "every allocator cell median is at least 100 ns/iter",
            "quantile_rule": "linear interpolation at index (n - 1) * p",
            "extrema_tie_break": "benchmark lexical ascending",
            "direction": "lower ratio is faster",
        },
        "inventory": {
            "benchmark_count": len(benchmarks),
            "family_counts": dict(family_counts),
            "benchmarks": list(benchmarks),
        },
        "matrix": {
            "allocator_feature_variants": len(EXPECTED_ALLOCATORS),
            "cells": len(cells),
            "complete_cells": complete_count,
            "censored_cells": len(censored),
        },
        "selection": {
            "comparable_count": len(comparable),
            "comparable_benchmarks": list(comparable),
            "robust_threshold_ns_per_iter": ROBUST_THRESHOLD_NS,
            "robust_count": len(robust),
            "robust_benchmarks": list(robust),
        },
        "variants": [
            {
                "allocator": allocator,
                "feature": EXPECTED_FEATURES[allocator],
                "label": ALLOCATOR_LABELS[allocator],
            }
            for allocator in EXPECTED_ALLOCATORS
        ],
        "benchmarks": [
            {
                "id": benchmark,
                "family": family_of(benchmark),
                "comparable": benchmark in comparable_set,
                "robust_selected": benchmark in robust_set,
            }
            for benchmark in benchmarks
        ],
        "cells": [
            cell_document(
                cell,
                comparable_set=comparable_set,
                robust_set=robust_set,
                cell_map=cell_map,
            )
            for cell in cells
        ],
        "distributions": list(derived_distributions),
        "censored_cells": [censored_document(cell) for cell in censored],
        "boundaries": [
            "This is a full std_bench feature-variant diagnostic; many timed closures do not allocate.",
            "Timeouts are retained as censored terminal cells and excluded from cross-allocator ratios.",
            "The raw distribution retains timer-floor leaves; the robust distribution requires every cell median to be at least 100 ns/iter.",
            "Min/max and quantile ranges are descriptive across benchmark leaves, not confidence intervals.",
        ],
    }


def write_json(file: Path, value: Mapping[str, Any]) -> None:
    file.parent.mkdir(parents=True, exist_ok=True)
    file.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def format_float(value: float | None) -> str:
    return "" if value is None else format(value, ".17g")


CSV_FIELDS = (
    "allocator",
    "feature",
    "allocator_label",
    "benchmark",
    "family",
    "cell_status",
    "comparable",
    "robust_selected",
    "timeout_phase",
    "timeout_round",
    "timeout_seconds",
    "sample_index",
    "measured_round",
    "sample_ns_per_iter",
    "sample_stdout_path",
    "cell_median_ns_per_iter",
    "cell_mad_ns_per_iter",
    "cell_min_ns_per_iter",
    "cell_max_ns_per_iter",
    "cell_ratio_vs_unialloc",
    "unialloc_median_ns_per_iter",
    "raw_leaf_geomean",
    "raw_family_balanced_geomean",
    "robust_leaf_geomean",
    "robust_family_balanced_geomean",
)


def write_long_csv(
    file: Path,
    cells: Sequence[Cell],
    comparable: Sequence[str],
    robust: Sequence[str],
    derived_distributions: Sequence[Mapping[str, Any]],
) -> None:
    cell_map = {(cell.allocator, cell.benchmark): cell for cell in cells}
    distribution_map = {row["allocator"]: row for row in derived_distributions}
    comparable_set = set(comparable)
    robust_set = set(robust)
    file.parent.mkdir(parents=True, exist_ok=True)
    with file.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=CSV_FIELDS, lineterminator="\n")
        writer.writeheader()
        for cell in cells:
            reference = cell_map[(REFERENCE_ALLOCATOR, cell.benchmark)]
            ratio = (
                float(cell.median_ns_per_iter) / float(reference.median_ns_per_iter)
                if cell.benchmark in comparable_set
                else None
            )
            samples: Sequence[ProcessRecord | None] = cell.measured_samples or (None,)
            aggregate = distribution_map[cell.allocator]
            for sample_index, sample in enumerate(samples, start=1):
                writer.writerow(
                    {
                        "allocator": cell.allocator,
                        "feature": cell.feature,
                        "allocator_label": ALLOCATOR_LABELS[cell.allocator],
                        "benchmark": cell.benchmark,
                        "family": cell.family,
                        "cell_status": cell.status,
                        "comparable": str(cell.benchmark in comparable_set).lower(),
                        "robust_selected": str(cell.benchmark in robust_set).lower(),
                        "timeout_phase": cell.timeout_phase or "",
                        "timeout_round": "" if cell.timeout_round is None else cell.timeout_round,
                        "timeout_seconds": format_float(cell.timeout_seconds),
                        "sample_index": "" if sample is None else sample_index,
                        "measured_round": "" if sample is None else sample.round,
                        "sample_ns_per_iter": "" if sample is None else format_float(sample.ns_per_iter),
                        "sample_stdout_path": "" if sample is None else sample.stdout_path or "",
                        "cell_median_ns_per_iter": format_float(cell.median_ns_per_iter),
                        "cell_mad_ns_per_iter": format_float(cell.mad_ns_per_iter),
                        "cell_min_ns_per_iter": format_float(cell.min_ns_per_iter),
                        "cell_max_ns_per_iter": format_float(cell.max_ns_per_iter),
                        "cell_ratio_vs_unialloc": format_float(ratio),
                        "unialloc_median_ns_per_iter": format_float(reference.median_ns_per_iter),
                        "raw_leaf_geomean": format_float(aggregate["raw"]["leaf_geomean"]),
                        "raw_family_balanced_geomean": format_float(
                            aggregate["raw"]["family_balanced_geomean"]
                        ),
                        "robust_leaf_geomean": format_float(aggregate["robust"]["leaf_geomean"]),
                        "robust_family_balanced_geomean": format_float(
                            aggregate["robust"]["family_balanced_geomean"]
                        ),
                    }
                )


def configure_matplotlib() -> None:
    plt.rcParams.update(
        {
            "font.family": "Liberation Sans",
            "font.size": 11,
            "figure.facecolor": WHITE,
            "axes.facecolor": WHITE,
            "axes.edgecolor": AXIS,
            "axes.labelcolor": INK,
            "axes.titlecolor": INK,
            "xtick.color": AXIS,
            "ytick.color": INK,
            "text.color": INK,
            "svg.hashsalt": "unialloc-std-bench-allocator-full-20260714",
            "svg.fonttype": "none",
            "savefig.facecolor": WHITE,
            "savefig.edgecolor": WHITE,
        }
    )


def log2_tick(value: float, _: int) -> str:
    ratio = 2.0**value
    if ratio >= 10:
        return f"{ratio:.0f}×"
    if ratio >= 1:
        return f"{ratio:.1f}×"
    return f"{ratio:.2f}×"


def render_distribution(
    derived_distributions: Sequence[Mapping[str, Any]],
    robust_count: int,
    comparable_count: int,
    diagnostic_label: Any,
) -> Figure:
    configure_matplotlib()
    figure, axis = plt.subplots(figsize=DIST_FIGURE_SIZE)
    figure.subplots_adjust(left=0.17, right=0.94, top=0.73, bottom=0.18)
    rows = [row for row in derived_distributions if row["allocator"] != REFERENCE_ALLOCATOR]
    y_positions = list(range(len(rows)))
    plotted: list[float] = [0.0]
    for y, row in zip(y_positions, rows, strict=True):
        stats = row["robust"]
        values = {key: math.log2(float(value)) for key, value in stats["quantiles"].items()}
        geomean_log = math.log2(float(stats["leaf_geomean"]))
        plotted.extend(values.values())
        plotted.append(geomean_log)
        axis.plot([values["p05"], values["p95"]], [y, y], color=PALE, linewidth=4, zorder=1)
        axis.plot([values["p25"], values["p75"]], [y, y], color=BLUE, linewidth=9, solid_capstyle="round", zorder=2)
        axis.scatter(values["p50"], y, s=75, color=WHITE, edgecolor=BLUE, linewidth=2, zorder=3)
        axis.scatter(geomean_log, y, s=75, marker="D", color=ORANGE, edgecolor=WHITE, linewidth=0.8, zorder=4)
    axis.axvline(0.0, color=INK, linestyle="--", linewidth=1.2, zorder=0)
    axis.set_yticks(y_positions, [row["label"] for row in rows])
    axis.invert_yaxis()
    pad = max((max(plotted) - min(plotted)) * 0.12, 0.18)
    axis.set_xlim(min(plotted) - pad, max(plotted) + pad)
    axis.xaxis.set_major_formatter(FuncFormatter(log2_tick))
    axis.set_xlabel("Median-time ratio versus UniAlloc on a log₂ axis · lower is faster")
    axis.grid(axis="x", color=GRID, linewidth=0.8, zorder=0)
    axis.tick_params(axis="y", length=0)
    axis.spines[["top", "right", "left"]].set_visible(False)
    figure.text(0.08, 0.92, "Full Rust std_bench distribution", fontsize=25, fontweight=700)
    figure.text(
        0.08,
        0.855,
        f"Robust leaves: all seven cell medians ≥100 ns/iter (n={robust_count}) · "
        f"complete seven-way leaves n={comparable_count}",
        fontsize=12.5,
        color=MUTED,
    )
    label = diagnostic_label if isinstance(diagnostic_label, str) else "current-toolchain diagnostic"
    figure.text(0.08, 0.81, str(label), fontsize=10.5, color=MUTED)
    handles = [
        Line2D([0], [0], color=PALE, linewidth=4, label="p05–p95"),
        Line2D([0], [0], color=BLUE, linewidth=9, label="p25–p75"),
        Line2D([0], [0], marker="o", markerfacecolor=WHITE, markeredgecolor=BLUE, linewidth=0, label="p50"),
        Line2D([0], [0], marker="D", markerfacecolor=ORANGE, markeredgecolor=WHITE, linewidth=0, label="leaf geomean"),
    ]
    axis.legend(handles=handles, loc="upper center", bbox_to_anchor=(0.5, -0.19), ncol=4, frameon=False)
    figure.text(
        0.08,
        0.035,
        "Raw and robust min/max leaf names, both medians, all three samples, quantiles, and family-balanced geomeans are retained in JSON/CSV.",
        fontsize=9.4,
        color=MUTED,
    )
    return figure


def render_heatmap(
    cells: Sequence[Cell],
    benchmarks: Sequence[str],
    robust: Sequence[str],
    derived_distributions: Sequence[Mapping[str, Any]],
) -> Figure:
    configure_matplotlib()
    figure, axis = plt.subplots(figsize=HEATMAP_FIGURE_SIZE)
    figure.subplots_adjust(left=0.22, right=0.91, top=0.94, bottom=0.055)
    comparison_allocators = EXPECTED_ALLOCATORS[1:]
    cell_map = {(cell.allocator, cell.benchmark): cell for cell in cells}
    robust_set = set(robust)
    ordered_benchmarks = sorted(benchmarks, key=lambda item: (family_of(item), item))
    matrix: list[list[float]] = []
    finite_values: list[float] = []
    for benchmark in ordered_benchmarks:
        row: list[float] = []
        reference = cell_map[(REFERENCE_ALLOCATOR, benchmark)]
        for allocator in comparison_allocators:
            cell = cell_map[(allocator, benchmark)]
            if benchmark in robust_set:
                value = math.log2(
                    float(cell.median_ns_per_iter) / float(reference.median_ns_per_iter)
                )
                finite_values.append(value)
            else:
                value = math.nan
            row.append(value)
        matrix.append(row)
    quantile_bounds: list[float] = []
    for row in derived_distributions:
        if row["allocator"] == REFERENCE_ALLOCATOR:
            continue
        quantile_bounds.extend(
            abs(math.log2(float(row["robust"]["quantiles"][key])))
            for key in ("p05", "p95")
        )
    extent = max(quantile_bounds + [0.1])
    cmap = matplotlib.colormaps["RdYlGn_r"].copy()
    cmap.set_bad(EXCLUDED)
    image = axis.imshow(
        matrix,
        aspect="auto",
        interpolation="nearest",
        cmap=cmap,
        norm=TwoSlopeNorm(vmin=-extent, vcenter=0.0, vmax=extent),
    )
    axis.set_xticks(
        range(len(comparison_allocators)),
        [ALLOCATOR_LABELS[item] for item in comparison_allocators],
        rotation=25,
        ha="right",
    )
    family_ranges: list[tuple[str, int, int]] = []
    start = 0
    while start < len(ordered_benchmarks):
        family = family_of(ordered_benchmarks[start])
        end = start + 1
        while end < len(ordered_benchmarks) and family_of(ordered_benchmarks[end]) == family:
            end += 1
        family_ranges.append((family, start, end))
        start = end
    axis.set_yticks(
        [(start_index + end_index - 1) / 2 for _, start_index, end_index in family_ranges],
        [f"{family} ({end_index - start_index})" for family, start_index, end_index in family_ranges],
    )
    for _, _, end_index in family_ranges[:-1]:
        axis.axhline(end_index - 0.5, color=WHITE, linewidth=2.2)
    axis.tick_params(axis="both", length=0)
    axis.set_title(
        "468-leaf robust ratio map",
        loc="left",
        fontsize=22,
        fontweight=700,
        pad=20,
    )
    axis.text(
        0,
        1.007,
        "log₂(cell median / UniAlloc median) · gray = censored or any seven-way median <100 ns/iter",
        transform=axis.transAxes,
        fontsize=10.5,
        color=MUTED,
        va="bottom",
    )
    colorbar = figure.colorbar(image, ax=axis, fraction=0.03, pad=0.025)
    colorbar.ax.yaxis.set_major_formatter(FuncFormatter(log2_tick))
    colorbar.set_label("Median-time ratio versus UniAlloc")
    colorbar.outline.set_linewidth(0.6)
    return figure


def export_figure(figure: Figure, svg_file: Path, png_file: Path, *, description: str) -> None:
    svg_file.parent.mkdir(parents=True, exist_ok=True)
    png_file.parent.mkdir(parents=True, exist_ok=True)
    figure.savefig(
        svg_file,
        format="svg",
        dpi=PNG_DPI,
        metadata={
            "Creator": "UniAlloc full std_bench exporter",
            "Date": "2026-07-14",
            "Description": description,
        },
        bbox_inches=None,
    )
    svg_text = svg_file.read_text(encoding="utf-8")
    svg_file.write_text(
        "\n".join(line.rstrip() for line in svg_text.splitlines()) + "\n",
        encoding="utf-8",
    )
    figure.savefig(
        png_file,
        format="png",
        dpi=PNG_DPI,
        metadata={
            "Software": "UniAlloc full std_bench exporter",
            "Creation Time": "2026-07-14T00:00:00Z",
        },
        bbox_inches=None,
    )


def write_manifest(
    file: Path,
    *,
    summary_path: Path,
    records_path: Path,
    generated_files: Sequence[Path],
    complete_count: int,
    censored_count: int,
    comparable_count: int,
    robust_count: int,
) -> None:
    manifest = {
        "schema_version": 2,
        "source": "rust-std-bench-full-allocator-artifacts",
        "source_artifacts": [
            artifact_descriptor(summary_path),
            artifact_descriptor(records_path),
        ],
        "generated_artifacts": [artifact_descriptor(path) for path in generated_files],
        "generator": {
            "path": display_path(Path(__file__)),
            "sha256": sha256_file(Path(__file__)),
            "renderer": f"Matplotlib {matplotlib.__version__}",
            "distribution_png_dimensions": {"width": 2400, "height": 1350},
            "heatmap_png_dimensions": {"width": 2100, "height": 4800},
        },
        "matrix": {
            "allocator_feature_variants": len(EXPECTED_ALLOCATORS),
            "benchmarks": EXPECTED_BENCHMARKS,
            "complete_cells": complete_count,
            "censored_cells": censored_count,
            "comparable_benchmarks": comparable_count,
            "robust_benchmarks": robust_count,
        },
    }
    write_json(file, manifest)


def generate(
    summary_path: Path,
    records_path: Path,
    result_json_path: Path,
    long_csv_path: Path,
    distribution_svg_path: Path,
    distribution_png_path: Path,
    heatmap_svg_path: Path,
    heatmap_png_path: Path,
    manifest_path: Path,
) -> tuple[Path, ...]:
    summary_path = summary_path.resolve()
    records_path = records_path.resolve()
    summary = load_json(summary_path)
    raw_records = load_jsonl(records_path)
    allocators, benchmarks = campaign_axes(summary)
    family_counts = validate_inventory(benchmarks)
    cells = reconstruct_cells(raw_records, allocators, benchmarks)
    comparable, robust = comparable_and_robust(cells, benchmarks, allocators)
    if not comparable:
        raise ValueError("campaign contains no complete seven-way comparable benchmark")
    if not robust:
        raise ValueError("campaign contains no robust benchmark at the 100 ns threshold")
    derived_distributions = distributions(cells, allocators, comparable, robust)
    validate_summary(summary, cells, comparable, robust, derived_distributions)
    result = result_document(
        summary,
        summary_path,
        records_path,
        benchmarks,
        family_counts,
        cells,
        comparable,
        robust,
        derived_distributions,
    )
    write_json(result_json_path, result)
    write_long_csv(long_csv_path, cells, comparable, robust, derived_distributions)
    distribution_figure = render_distribution(
        derived_distributions,
        len(robust),
        len(comparable),
        summary.get("diagnostic_label"),
    )
    export_figure(
        distribution_figure,
        distribution_svg_path,
        distribution_png_path,
        description="Full std_bench allocator ratio distributions",
    )
    plt.close(distribution_figure)
    heatmap_figure = render_heatmap(
        cells, benchmarks, robust, derived_distributions
    )
    export_figure(
        heatmap_figure,
        heatmap_svg_path,
        heatmap_png_path,
        description="Full std_bench 468-leaf allocator ratio heatmap",
    )
    plt.close(heatmap_figure)
    generated_files = (
        result_json_path,
        long_csv_path,
        distribution_svg_path,
        distribution_png_path,
        heatmap_svg_path,
        heatmap_png_path,
    )
    complete_count = sum(cell.status == "complete" for cell in cells)
    write_manifest(
        manifest_path,
        summary_path=summary_path,
        records_path=records_path,
        generated_files=generated_files,
        complete_count=complete_count,
        censored_count=len(cells) - complete_count,
        comparable_count=len(comparable),
        robust_count=len(robust),
    )
    return (*generated_files, manifest_path)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--summary", type=Path, default=DEFAULT_SUMMARY)
    parser.add_argument("--records", type=Path, default=DEFAULT_RECORDS)
    parser.add_argument("--result-json", type=Path, default=DEFAULT_RESULT_JSON)
    parser.add_argument("--long-csv", type=Path, default=DEFAULT_LONG_CSV)
    parser.add_argument("--distribution-svg", type=Path, default=DEFAULT_DISTRIBUTION_SVG)
    parser.add_argument("--distribution-png", type=Path, default=DEFAULT_DISTRIBUTION_PNG)
    parser.add_argument("--heatmap-svg", type=Path, default=DEFAULT_HEATMAP_SVG)
    parser.add_argument("--heatmap-png", type=Path, default=DEFAULT_HEATMAP_PNG)
    parser.add_argument("--manifest", type=Path, default=DEFAULT_MANIFEST)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    generated = generate(
        args.summary,
        args.records,
        args.result_json,
        args.long_csv,
        args.distribution_svg,
        args.distribution_png,
        args.heatmap_svg,
        args.heatmap_png,
        args.manifest,
    )
    for file in generated:
        print(file)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
