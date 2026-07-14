#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = ["matplotlib==3.11.0"]
# ///
"""Summarize and plot the Rust std_bench allocator-feature campaign.

The raw campaign is the source of truth.  Each plotted cell is recomputed from
exactly three valid measured fresh-process records.  The exporter writes a
machine-readable result, a long-form table retaining all three observations,
one two-panel figure, and a SHA256 artifact manifest.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import math
import os
import statistics
from collections import defaultdict
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
from matplotlib.patches import Patch


REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
CAMPAIGN_ID = "std-bench-allocator-variants-20260714"
DEFAULT_RAW_DIR = REPOSITORY_ROOT / "evaluation" / "raw" / CAMPAIGN_ID
DEFAULT_SUMMARY = DEFAULT_RAW_DIR / "summary.json"
DEFAULT_RECORDS = DEFAULT_RAW_DIR / "records.jsonl"
DEFAULT_RESULT_JSON = REPOSITORY_ROOT / "benchmark-results" / f"{CAMPAIGN_ID}.json"
DEFAULT_FIGURE_DIR = REPOSITORY_ROOT / "docs" / "figures" / CAMPAIGN_ID
DEFAULT_LONG_CSV = DEFAULT_FIGURE_DIR / "std-bench-allocator-variants.csv"
DEFAULT_FIGURE_SVG = DEFAULT_FIGURE_DIR / "allocator-variant-microbench.svg"
DEFAULT_FIGURE_PNG = DEFAULT_FIGURE_DIR / "allocator-variant-microbench.png"
DEFAULT_MANIFEST = DEFAULT_FIGURE_DIR / "artifact-manifest.json"

REFERENCE_ALLOCATOR = "unialloc"
EXPECTED_MEASURED_SAMPLES = 3
FIGURE_SIZE_INCHES = (18, 10)
PNG_DPI = 150

INK = "#17181A"
MUTED = "#62646B"
AXIS = "#47484F"
GRID = "#DDDEE1"
PALE = "#F0F1F2"
WHITE = "#FFFFFF"
UNIALLOC = "#2E4780"
OTHER = "#A8B2C5"
ROBUST = "#E46F3E"

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
class Sample:
    allocator: str
    feature: str
    benchmark: str
    round: int
    ns_per_iter: float
    stdout_path: str | None


@dataclass(frozen=True)
class Cell:
    allocator: str
    feature: str
    benchmark: str
    samples: tuple[Sample, ...]
    median_ns_per_iter: float
    mad_ns_per_iter: float
    min_ns_per_iter: float
    max_ns_per_iter: float
    median_ratio_vs_unialloc: float


def load_json(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"expected a JSON object: {path}")
    return value


def load_jsonl(path: Path) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    with path.open(encoding="utf-8") as handle:
        for line_number, line in enumerate(handle, start=1):
            if not line.strip():
                continue
            try:
                value = json.loads(line)
            except json.JSONDecodeError as error:
                raise ValueError(
                    f"invalid JSON at {path}:{line_number}: {error}"
                ) from error
            if not isinstance(value, dict):
                raise ValueError(f"expected a JSON object at {path}:{line_number}")
            records.append(value)
    return records


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def display_path(path: Path) -> str:
    resolved = path.resolve()
    try:
        return str(resolved.relative_to(REPOSITORY_ROOT))
    except ValueError:
        return str(resolved)


def finite_positive_float(value: Any, *, field: str) -> float:
    try:
        result = float(value)
    except (TypeError, ValueError) as error:
        raise ValueError(f"{field} must be a number, got {value!r}") from error
    if not math.isfinite(result) or result <= 0:
        raise ValueError(f"{field} must be finite and positive, got {value!r}")
    return result


def string_list(value: Any, *, field: str) -> tuple[str, ...]:
    if not isinstance(value, list) or not value:
        raise ValueError(f"{field} must be a non-empty list")
    result = tuple(value)
    if any(not isinstance(item, str) or not item for item in result):
        raise ValueError(f"{field} must contain non-empty strings")
    if len(result) != len(set(result)):
        raise ValueError(f"{field} contains duplicates")
    return result


def campaign_axes(
    summary: Mapping[str, Any],
) -> tuple[tuple[str, ...], tuple[str, ...]]:
    methodology = summary.get("methodology")
    if not isinstance(methodology, Mapping):
        raise ValueError("summary.methodology must be an object")
    measured = methodology.get("measured_fresh_processes_per_cell")
    if measured != EXPECTED_MEASURED_SAMPLES:
        raise ValueError(
            "summary.methodology.measured_fresh_processes_per_cell must equal "
            f"{EXPECTED_MEASURED_SAMPLES}, got {measured!r}"
        )
    allocators = string_list(
        methodology.get("allocators"), field="methodology.allocators"
    )
    benchmarks = string_list(
        methodology.get("benchmarks"), field="methodology.benchmarks"
    )
    if REFERENCE_ALLOCATOR not in allocators:
        raise ValueError(f"reference allocator {REFERENCE_ALLOCATOR!r} is absent")
    return allocators, benchmarks


def measured_samples(
    records: Sequence[Mapping[str, Any]],
    allocators: Sequence[str],
    benchmarks: Sequence[str],
) -> tuple[dict[tuple[str, str], tuple[Sample, ...]], dict[str, str]]:
    allocator_set = set(allocators)
    benchmark_set = set(benchmarks)
    grouped: dict[tuple[str, str], list[Sample]] = defaultdict(list)
    features: dict[str, str] = {}

    for index, record in enumerate(records, start=1):
        if record.get("phase") != "measured":
            continue
        allocator = record.get("allocator")
        benchmark = record.get("benchmark")
        if allocator not in allocator_set:
            raise ValueError(
                f"measured record {index} has unknown allocator {allocator!r}"
            )
        if benchmark not in benchmark_set:
            raise ValueError(
                f"measured record {index} has unknown benchmark {benchmark!r}"
            )
        if record.get("valid") is not True:
            raise ValueError(
                f"measured record {index} is invalid for {allocator}/{benchmark}"
            )
        feature = record.get("feature")
        if not isinstance(feature, str) or not feature:
            raise ValueError(f"measured record {index} has no Cargo feature")
        previous_feature = features.setdefault(allocator, feature)
        if previous_feature != feature:
            raise ValueError(
                f"allocator {allocator!r} uses multiple features: "
                f"{previous_feature!r} and {feature!r}"
            )
        round_value = record.get("round")
        if isinstance(round_value, bool) or not isinstance(round_value, int):
            raise ValueError(
                f"measured record {index} has non-integer round {round_value!r}"
            )
        value = finite_positive_float(
            record.get("ns_per_iter"), field=f"record {index} ns_per_iter"
        )
        stdout_path = record.get("stdout_path")
        if stdout_path is not None and not isinstance(stdout_path, str):
            raise ValueError(f"measured record {index} has invalid stdout_path")
        grouped[(allocator, benchmark)].append(
            Sample(
                allocator=allocator,
                feature=feature,
                benchmark=benchmark,
                round=round_value,
                ns_per_iter=value,
                stdout_path=stdout_path,
            )
        )

    expected_keys = {
        (allocator, benchmark) for allocator in allocators for benchmark in benchmarks
    }
    extras = set(grouped) - expected_keys
    if extras:
        raise ValueError(f"unexpected measured cells: {sorted(extras)!r}")
    for key in sorted(expected_keys):
        samples = grouped.get(key, [])
        if len(samples) != EXPECTED_MEASURED_SAMPLES:
            raise ValueError(
                f"cell {key[0]}/{key[1]} must contain exactly "
                f"{EXPECTED_MEASURED_SAMPLES} measured fresh-process records; "
                f"found {len(samples)}"
            )
        rounds = [sample.round for sample in samples]
        if len(rounds) != len(set(rounds)):
            raise ValueError(f"cell {key[0]}/{key[1]} has duplicate measured rounds")
        paths = [sample.stdout_path for sample in samples]
        if all(paths) and len(paths) != len(set(paths)):
            raise ValueError(
                f"cell {key[0]}/{key[1]} reuses stdout evidence across processes"
            )
        grouped[key] = sorted(samples, key=lambda sample: sample.round)

    missing_features = [
        allocator for allocator in allocators if allocator not in features
    ]
    if missing_features:
        raise ValueError(f"missing Cargo features for allocators: {missing_features!r}")
    if len(set(features.values())) != len(features):
        raise ValueError("each allocator variant must use a distinct Cargo feature")
    return {key: tuple(value) for key, value in grouped.items()}, features


def median_absolute_deviation(values: Iterable[float]) -> float:
    sequence = tuple(values)
    median = float(statistics.median(sequence))
    return float(statistics.median(abs(value - median) for value in sequence))


def geomean(values: Iterable[float]) -> float:
    sequence = tuple(values)
    if not sequence or any(
        value <= 0 or not math.isfinite(value) for value in sequence
    ):
        raise ValueError("geomean requires finite positive values")
    return math.exp(math.fsum(math.log(value) for value in sequence) / len(sequence))


def derive_cells(
    grouped: Mapping[tuple[str, str], tuple[Sample, ...]],
    allocators: Sequence[str],
    benchmarks: Sequence[str],
) -> tuple[Cell, ...]:
    raw_stats: dict[tuple[str, str], tuple[float, float, float, float]] = {}
    for key, samples in grouped.items():
        values = tuple(sample.ns_per_iter for sample in samples)
        raw_stats[key] = (
            float(statistics.median(values)),
            median_absolute_deviation(values),
            min(values),
            max(values),
        )

    cells: list[Cell] = []
    for benchmark in benchmarks:
        reference_median = raw_stats[(REFERENCE_ALLOCATOR, benchmark)][0]
        for allocator in allocators:
            key = (allocator, benchmark)
            median, mad, minimum, maximum = raw_stats[key]
            samples = grouped[key]
            cells.append(
                Cell(
                    allocator=allocator,
                    feature=samples[0].feature,
                    benchmark=benchmark,
                    samples=samples,
                    median_ns_per_iter=median,
                    mad_ns_per_iter=mad,
                    min_ns_per_iter=minimum,
                    max_ns_per_iter=maximum,
                    median_ratio_vs_unialloc=median / reference_median,
                )
            )
    return tuple(cells)


def validate_summary_cells(summary: Mapping[str, Any], cells: Sequence[Cell]) -> None:
    source_cells = summary.get("cells")
    if source_cells is None:
        return
    if not isinstance(source_cells, list):
        raise ValueError("summary.cells must be a list")
    source_map: dict[tuple[str, str], Mapping[str, Any]] = {}
    for source_cell in source_cells:
        if not isinstance(source_cell, Mapping):
            raise ValueError("summary.cells entries must be objects")
        key = (source_cell.get("allocator"), source_cell.get("benchmark"))
        if key in source_map:
            raise ValueError(f"summary.cells duplicates {key!r}")
        source_map[key] = source_cell
    derived_keys = {(cell.allocator, cell.benchmark) for cell in cells}
    if set(source_map) != derived_keys:
        raise ValueError("summary.cells does not cover the measured campaign matrix")
    for cell in cells:
        source = source_map[(cell.allocator, cell.benchmark)]
        if source.get("samples") != EXPECTED_MEASURED_SAMPLES:
            raise ValueError(
                f"summary cell {cell.allocator}/{cell.benchmark} does not declare 3 samples"
            )
        source_median = finite_positive_float(
            source.get("median_ns_per_iter"),
            field=f"summary cell {cell.allocator}/{cell.benchmark} median_ns_per_iter",
        )
        if not math.isclose(source_median, cell.median_ns_per_iter, rel_tol=1e-12):
            raise ValueError(
                f"summary median mismatch for {cell.allocator}/{cell.benchmark}: "
                f"{source_median} != {cell.median_ns_per_iter}"
            )


def selected_benchmarks(
    summary: Mapping[str, Any], benchmarks: Sequence[str], cells: Sequence[Cell]
) -> tuple[tuple[str, ...], str, float | None]:
    selection = summary.get("robustness_selection")
    if selection is None:
        return tuple(benchmarks), "all benchmark cells", None
    if not isinstance(selection, Mapping):
        raise ValueError("summary.robustness_selection must be an object")
    selected_value = selection.get("selected_benchmarks")
    selected = string_list(
        selected_value, field="robustness_selection.selected_benchmarks"
    )
    unknown = set(selected) - set(benchmarks)
    if unknown:
        raise ValueError(
            f"robustness selection contains unknown benchmarks: {sorted(unknown)!r}"
        )
    rule = selection.get("rule", "campaign-defined robustness subset")
    if not isinstance(rule, str) or not rule:
        raise ValueError("robustness_selection.rule must be a non-empty string")
    threshold_value = selection.get("threshold_ns_per_iter")
    threshold: float | None = None
    if threshold_value is not None:
        threshold = finite_positive_float(
            threshold_value, field="robustness_selection.threshold_ns_per_iter"
        )
        expected = tuple(
            benchmark
            for benchmark in benchmarks
            if min(
                cell.median_ns_per_iter for cell in cells if cell.benchmark == benchmark
            )
            >= threshold
        )
        if selected != expected:
            raise ValueError(
                "robustness_selection.selected_benchmarks disagrees with its threshold rule"
            )
    return selected, rule, threshold


def aggregate_rows(
    cells: Sequence[Cell],
    allocators: Sequence[str],
    benchmarks: Sequence[str],
    robust_benchmarks: Sequence[str],
    features: Mapping[str, str],
) -> list[dict[str, Any]]:
    cell_map = {(cell.allocator, cell.benchmark): cell for cell in cells}
    rows: list[dict[str, Any]] = []
    for allocator in allocators:
        all_ratios = [
            cell_map[(allocator, benchmark)].median_ratio_vs_unialloc
            for benchmark in benchmarks
        ]
        robust_ratios = [
            cell_map[(allocator, benchmark)].median_ratio_vs_unialloc
            for benchmark in robust_benchmarks
        ]
        rows.append(
            {
                "variant": features[allocator],
                "allocator": allocator,
                "feature": features[allocator],
                "label": ALLOCATOR_LABELS.get(allocator, allocator),
                "all_benchmark_count": len(all_ratios),
                "geomean_median_ratio_vs_unialloc": geomean(all_ratios),
                "robust_benchmark_count": len(robust_ratios),
                "robust_geomean_median_ratio_vs_unialloc": geomean(robust_ratios),
            }
        )
    return rows


def source_descriptor(path: Path) -> dict[str, Any]:
    return {
        "path": display_path(path),
        "sha256": sha256_file(path),
        "bytes": path.stat().st_size,
    }


def result_document(
    summary: Mapping[str, Any],
    summary_path: Path,
    records_path: Path,
    allocators: Sequence[str],
    benchmarks: Sequence[str],
    features: Mapping[str, str],
    cells: Sequence[Cell],
    aggregates: Sequence[Mapping[str, Any]],
    robust_benchmarks: Sequence[str],
    robust_rule: str,
    robust_threshold: float | None,
) -> dict[str, Any]:
    return {
        "schema_version": 1,
        "source": "rust-std-bench-allocator-feature-variant-medians",
        "success": True,
        "generated_utc": summary.get("generated_utc"),
        "diagnostic_label": summary.get("diagnostic_label"),
        "claim_grade": bool(summary.get("claim_grade", False)),
        "input_artifacts": {
            "summary": source_descriptor(summary_path),
            "records": source_descriptor(records_path),
        },
        "methodology": {
            "reference_allocator": REFERENCE_ALLOCATOR,
            "reference_feature": features[REFERENCE_ALLOCATOR],
            "warmup_fresh_processes_per_cell": summary.get("methodology", {}).get(
                "warmup_fresh_processes_per_cell"
            ),
            "measured_fresh_processes_per_cell": EXPECTED_MEASURED_SAMPLES,
            "cell_statistic": "median ns/iter of exactly 3 measured fresh-process samples",
            "dispersion": (
                "median absolute deviation (MAD), minimum, and maximum of the "
                "same 3 samples"
            ),
            "aggregate_statistic": (
                "geometric mean of per-benchmark cell-median ratios versus UniAlloc"
            ),
            "direction": "lower is faster",
        },
        "variants": [
            {
                "id": features[allocator],
                "allocator": allocator,
                "feature": features[allocator],
                "label": ALLOCATOR_LABELS.get(allocator, allocator),
            }
            for allocator in allocators
        ],
        "benchmarks": [
            {
                "id": benchmark,
                "robustness_selected": benchmark in set(robust_benchmarks),
            }
            for benchmark in benchmarks
        ],
        "robustness_selection": {
            "rule": robust_rule,
            "threshold_ns_per_iter": robust_threshold,
            "selected_benchmarks": list(robust_benchmarks),
            "selected_count": len(robust_benchmarks),
        },
        "cells": [
            {
                "variant": cell.feature,
                "allocator": cell.allocator,
                "feature": cell.feature,
                "benchmark": cell.benchmark,
                "samples": EXPECTED_MEASURED_SAMPLES,
                "measured_rounds": [sample.round for sample in cell.samples],
                "sample_ns_per_iter": [sample.ns_per_iter for sample in cell.samples],
                "median_ns_per_iter": cell.median_ns_per_iter,
                "mad_ns_per_iter": cell.mad_ns_per_iter,
                "min_ns_per_iter": cell.min_ns_per_iter,
                "max_ns_per_iter": cell.max_ns_per_iter,
                "median_ratio_vs_unialloc": cell.median_ratio_vs_unialloc,
                "robustness_selected": cell.benchmark in set(robust_benchmarks),
            }
            for cell in cells
        ],
        "aggregates": list(aggregates),
        "boundaries": [
            "The result is a current-toolchain diagnostic over the selected std_bench leaves.",
            (
                "Each cell has exactly three measured fresh-process observations; "
                "min/max ranges are descriptive, not confidence intervals."
            ),
            (
                "Ratios are computed after taking each cell median, so raw samples "
                "are never pooled across benchmarks."
            ),
            (
                "The robustness subset follows the raw campaign summary and remains "
                "separate from the complete heatmap."
            ),
        ],
    }


def write_json(path: Path, value: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )


def format_float(value: float) -> str:
    return format(value, ".17g")


CSV_FIELDS = (
    "variant",
    "allocator",
    "feature",
    "allocator_label",
    "benchmark",
    "robustness_selected",
    "sample_index",
    "measured_round",
    "sample_ns_per_iter",
    "cell_median_ns_per_iter",
    "cell_mad_ns_per_iter",
    "cell_min_ns_per_iter",
    "cell_max_ns_per_iter",
    "cell_median_ratio_vs_unialloc",
    "aggregate_geomean_median_ratio_vs_unialloc",
    "robust_geomean_median_ratio_vs_unialloc",
)


def write_long_csv(
    path: Path,
    cells: Sequence[Cell],
    aggregates: Sequence[Mapping[str, Any]],
    robust_benchmarks: Sequence[str],
) -> None:
    aggregate_map = {str(row["allocator"]): row for row in aggregates}
    robust_set = set(robust_benchmarks)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=CSV_FIELDS, lineterminator="\n")
        writer.writeheader()
        for cell in cells:
            aggregate = aggregate_map[cell.allocator]
            for sample_index, sample in enumerate(cell.samples, start=1):
                writer.writerow(
                    {
                        "variant": cell.feature,
                        "allocator": cell.allocator,
                        "feature": cell.feature,
                        "allocator_label": ALLOCATOR_LABELS.get(
                            cell.allocator, cell.allocator
                        ),
                        "benchmark": cell.benchmark,
                        "robustness_selected": str(
                            cell.benchmark in robust_set
                        ).lower(),
                        "sample_index": sample_index,
                        "measured_round": sample.round,
                        "sample_ns_per_iter": format_float(sample.ns_per_iter),
                        "cell_median_ns_per_iter": format_float(
                            cell.median_ns_per_iter
                        ),
                        "cell_mad_ns_per_iter": format_float(cell.mad_ns_per_iter),
                        "cell_min_ns_per_iter": format_float(cell.min_ns_per_iter),
                        "cell_max_ns_per_iter": format_float(cell.max_ns_per_iter),
                        "cell_median_ratio_vs_unialloc": format_float(
                            cell.median_ratio_vs_unialloc
                        ),
                        "aggregate_geomean_median_ratio_vs_unialloc": format_float(
                            float(aggregate["geomean_median_ratio_vs_unialloc"])
                        ),
                        "robust_geomean_median_ratio_vs_unialloc": format_float(
                            float(aggregate["robust_geomean_median_ratio_vs_unialloc"])
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
            "axes.titlesize": 14,
            "axes.titleweight": 700,
            "axes.linewidth": 0.8,
            "xtick.color": AXIS,
            "ytick.color": INK,
            "text.color": INK,
            "svg.hashsalt": "unialloc-std-bench-allocator-variants-20260714",
            "svg.fonttype": "none",
            "savefig.facecolor": WHITE,
            "savefig.edgecolor": WHITE,
        }
    )


def short_benchmark_label(benchmark: str) -> str:
    parts = benchmark.split("::")
    if len(parts) == 1:
        return benchmark
    leaf = " / ".join(parts[1:])
    if leaf.startswith("bench_"):
        leaf = leaf.removeprefix("bench_")
    return f"{parts[0]}\n{leaf}"


def render_figure(
    allocators: Sequence[str],
    benchmarks: Sequence[str],
    cells: Sequence[Cell],
    aggregates: Sequence[Mapping[str, Any]],
    robust_benchmarks: Sequence[str],
    diagnostic_label: Any,
) -> Figure:
    configure_matplotlib()
    figure = plt.figure(figsize=FIGURE_SIZE_INCHES)
    grid = figure.add_gridspec(
        1,
        2,
        width_ratios=(0.85, 1.75),
        left=0.07,
        right=0.965,
        top=0.79,
        bottom=0.23,
        wspace=0.27,
    )
    aggregate_axis = figure.add_subplot(grid[0, 0])
    heatmap_axis = figure.add_subplot(grid[0, 1])

    label = (
        diagnostic_label
        if isinstance(diagnostic_label, str)
        else "current-toolchain diagnostic"
    )
    figure.text(
        0.07,
        0.925,
        "Rust std_bench allocator features",
        fontsize=23,
        fontweight=700,
        color=INK,
    )
    figure.text(
        0.07,
        0.875,
        "Each cell: median of 3 measured fresh processes after warm-up · "
        "normalized to UniAlloc · lower is faster",
        fontsize=12.5,
        color=MUTED,
    )
    figure.text(
        0.07,
        0.837,
        f"{label} · Cargo bench_* feature toggles define the seven variants",
        fontsize=10.5,
        color=MUTED,
    )

    aggregate_map = {str(row["allocator"]): row for row in aggregates}
    y_positions = list(range(len(allocators)))
    all_ratios = [
        float(aggregate_map[allocator]["geomean_median_ratio_vs_unialloc"])
        for allocator in allocators
    ]
    robust_ratios = [
        float(aggregate_map[allocator]["robust_geomean_median_ratio_vs_unialloc"])
        for allocator in allocators
    ]
    colors = [
        UNIALLOC if allocator == REFERENCE_ALLOCATOR else OTHER
        for allocator in allocators
    ]
    aggregate_axis.barh(
        y_positions,
        all_ratios,
        height=0.58,
        color=colors,
        edgecolor=WHITE,
        linewidth=0.5,
        zorder=2,
    )
    if tuple(robust_benchmarks) != tuple(benchmarks):
        aggregate_axis.scatter(
            robust_ratios,
            y_positions,
            marker="D",
            s=48,
            color=ROBUST,
            edgecolor=WHITE,
            linewidth=0.7,
            zorder=4,
        )
    aggregate_axis.axvline(1.0, color=INK, linewidth=1.0, linestyle="--", zorder=1)
    aggregate_axis.set_yticks(
        y_positions,
        [ALLOCATOR_LABELS.get(allocator, allocator) for allocator in allocators],
    )
    aggregate_axis.invert_yaxis()
    aggregate_axis.set_xlabel("Geomean of median-time ratios (× UniAlloc)")
    aggregate_axis.set_title(
        f"Aggregate across {len(benchmarks)} leaves",
        loc="left",
        pad=15,
    )
    maximum_ratio = max(all_ratios + robust_ratios + [1.0])
    aggregate_axis.set_xlim(0, maximum_ratio * 1.18)
    aggregate_axis.grid(axis="x", color=GRID, linewidth=0.8, zorder=0)
    aggregate_axis.spines[["top", "right", "left"]].set_visible(False)
    aggregate_axis.tick_params(axis="y", length=0)
    for y, value in zip(y_positions, all_ratios, strict=True):
        aggregate_axis.text(
            value + maximum_ratio * 0.018,
            y,
            f"{value:.2f}×",
            va="center",
            ha="left",
            fontsize=10,
            color=INK,
        )
    legend_handles: list[Any] = [
        Patch(
            facecolor=OTHER, edgecolor="none", label=f"All leaves (n={len(benchmarks)})"
        )
    ]
    if tuple(robust_benchmarks) != tuple(benchmarks):
        legend_handles.append(
            Line2D(
                [0],
                [0],
                marker="D",
                linestyle="none",
                markerfacecolor=ROBUST,
                markeredgecolor=WHITE,
                markersize=7,
                label=f"≥100 ns robustness subset (n={len(robust_benchmarks)})",
            )
        )
    aggregate_axis.legend(
        handles=legend_handles,
        loc="upper left",
        bbox_to_anchor=(0.0, -0.20),
        borderaxespad=0,
        frameon=False,
        fontsize=9.5,
    )

    cell_map = {(cell.allocator, cell.benchmark): cell for cell in cells}
    matrix = [
        [
            cell_map[(allocator, benchmark)].median_ratio_vs_unialloc
            for allocator in allocators
        ]
        for benchmark in benchmarks
    ]
    flat = [value for row in matrix for value in row]
    extent = max(max(abs(value - 1.0) for value in flat), 0.05)
    norm = TwoSlopeNorm(vmin=max(0.0, 1.0 - extent), vcenter=1.0, vmax=1.0 + extent)
    image = heatmap_axis.imshow(
        matrix,
        aspect="auto",
        interpolation="nearest",
        cmap="RdYlGn_r",
        norm=norm,
    )
    heatmap_axis.set_xticks(
        range(len(allocators)),
        [
            f"{ALLOCATOR_LABELS.get(allocator, allocator)}\n"
            f"{aggregate_map[allocator]['feature']}"
            for allocator in allocators
        ],
        rotation=32,
        ha="right",
    )
    heatmap_axis.set_yticks(
        range(len(benchmarks)),
        [short_benchmark_label(benchmark) for benchmark in benchmarks],
    )
    heatmap_axis.set_title("Per-leaf cell medians", loc="left", pad=15)
    heatmap_axis.tick_params(axis="both", length=0)
    heatmap_axis.tick_params(axis="y", labelsize=9.5, pad=6)
    heatmap_axis.set_xticks(
        [value - 0.5 for value in range(1, len(allocators))], minor=True
    )
    heatmap_axis.set_yticks(
        [value - 0.5 for value in range(1, len(benchmarks))], minor=True
    )
    heatmap_axis.grid(which="minor", color=WHITE, linewidth=1.5)
    for row_index, row in enumerate(matrix):
        for column_index, value in enumerate(row):
            rgba = image.cmap(image.norm(value))
            luminance = 0.2126 * rgba[0] + 0.7152 * rgba[1] + 0.0722 * rgba[2]
            heatmap_axis.text(
                column_index,
                row_index,
                f"{value:.2f}×",
                ha="center",
                va="center",
                fontsize=8.4,
                color=INK if luminance > 0.58 else WHITE,
            )
    colorbar = figure.colorbar(image, ax=heatmap_axis, fraction=0.033, pad=0.025)
    colorbar.set_label("Median ratio (× UniAlloc)")
    colorbar.outline.set_linewidth(0.6)

    selection_note = (
        "Diamonds show the campaign-defined robustness subset. "
        if tuple(robust_benchmarks) != tuple(benchmarks)
        else "All selected leaves contribute to the aggregate. "
    )
    figure.text(
        0.07,
        0.035,
        "Bars and heatmap use per-cell medians. "
        + selection_note
        + "MAD/min/max and all three observations are in the JSON and long-form CSV.",
        fontsize=9.5,
        color=MUTED,
    )
    return figure


def export_figure(figure: Figure, svg_path: Path, png_path: Path) -> None:
    svg_path.parent.mkdir(parents=True, exist_ok=True)
    png_path.parent.mkdir(parents=True, exist_ok=True)
    figure.savefig(
        svg_path,
        format="svg",
        dpi=PNG_DPI,
        metadata={
            "Creator": "UniAlloc std_bench allocator-variant exporter",
            "Date": "2026-07-14",
            "Description": "Three-run median allocator-feature benchmark results",
        },
        bbox_inches=None,
    )
    # Matplotlib emits spaces before newlines inside path data.  Normalizing
    # them keeps the tracked vector artifact friendly to `git diff --check`
    # without changing the rendered geometry.
    svg_text = svg_path.read_text(encoding="utf-8")
    svg_path.write_text(
        "\n".join(line.rstrip() for line in svg_text.splitlines()) + "\n",
        encoding="utf-8",
    )
    figure.savefig(
        png_path,
        format="png",
        dpi=PNG_DPI,
        metadata={
            "Software": "UniAlloc std_bench allocator-variant exporter",
            "Creation Time": "2026-07-14T00:00:00Z",
        },
        bbox_inches=None,
    )


def artifact_descriptor(path: Path) -> dict[str, Any]:
    return {
        "path": display_path(path),
        "sha256": sha256_file(path),
        "bytes": path.stat().st_size,
    }


def write_manifest(
    path: Path,
    summary_path: Path,
    records_path: Path,
    result_json_path: Path,
    csv_path: Path,
    svg_path: Path,
    png_path: Path,
    allocators: Sequence[str],
    benchmarks: Sequence[str],
) -> None:
    manifest = {
        "schema_version": 1,
        "source": "rust-std-bench-allocator-feature-variant-artifacts",
        "source_artifacts": [
            artifact_descriptor(summary_path),
            artifact_descriptor(records_path),
        ],
        "generated_artifacts": [
            artifact_descriptor(result_json_path),
            artifact_descriptor(csv_path),
            artifact_descriptor(svg_path),
            artifact_descriptor(png_path),
        ],
        "generator": {
            "path": display_path(Path(__file__)),
            "sha256": sha256_file(Path(__file__)),
            "renderer": f"Matplotlib {matplotlib.__version__}",
            "figure_size_inches": {
                "width": FIGURE_SIZE_INCHES[0],
                "height": FIGURE_SIZE_INCHES[1],
            },
            "png_dimensions": {
                "width": round(FIGURE_SIZE_INCHES[0] * PNG_DPI),
                "height": round(FIGURE_SIZE_INCHES[1] * PNG_DPI),
            },
        },
        "matrix": {
            "allocator_feature_variants": len(allocators),
            "benchmarks": len(benchmarks),
            "measured_fresh_processes_per_cell": EXPECTED_MEASURED_SAMPLES,
            "measured_processes": len(allocators)
            * len(benchmarks)
            * EXPECTED_MEASURED_SAMPLES,
        },
    }
    write_json(path, manifest)


def generate(
    summary_path: Path,
    records_path: Path,
    result_json_path: Path,
    long_csv_path: Path,
    figure_svg_path: Path,
    figure_png_path: Path,
    manifest_path: Path,
) -> tuple[Path, ...]:
    summary_path = summary_path.resolve()
    records_path = records_path.resolve()
    summary = load_json(summary_path)
    records = load_jsonl(records_path)
    allocators, benchmarks = campaign_axes(summary)
    grouped, features = measured_samples(records, allocators, benchmarks)
    cells = derive_cells(grouped, allocators, benchmarks)
    validate_summary_cells(summary, cells)
    robust_benchmarks, robust_rule, robust_threshold = selected_benchmarks(
        summary, benchmarks, cells
    )
    aggregates = aggregate_rows(
        cells, allocators, benchmarks, robust_benchmarks, features
    )
    result = result_document(
        summary,
        summary_path,
        records_path,
        allocators,
        benchmarks,
        features,
        cells,
        aggregates,
        robust_benchmarks,
        robust_rule,
        robust_threshold,
    )

    write_json(result_json_path, result)
    write_long_csv(long_csv_path, cells, aggregates, robust_benchmarks)
    figure = render_figure(
        allocators,
        benchmarks,
        cells,
        aggregates,
        robust_benchmarks,
        summary.get("diagnostic_label"),
    )
    export_figure(figure, figure_svg_path, figure_png_path)
    plt.close(figure)
    write_manifest(
        manifest_path,
        summary_path,
        records_path,
        result_json_path,
        long_csv_path,
        figure_svg_path,
        figure_png_path,
        allocators,
        benchmarks,
    )
    return (
        result_json_path,
        long_csv_path,
        figure_svg_path,
        figure_png_path,
        manifest_path,
    )


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--summary", type=Path, default=DEFAULT_SUMMARY)
    parser.add_argument("--records", type=Path, default=DEFAULT_RECORDS)
    parser.add_argument("--result-json", type=Path, default=DEFAULT_RESULT_JSON)
    parser.add_argument("--long-csv", type=Path, default=DEFAULT_LONG_CSV)
    parser.add_argument("--figure-svg", type=Path, default=DEFAULT_FIGURE_SVG)
    parser.add_argument("--figure-png", type=Path, default=DEFAULT_FIGURE_PNG)
    parser.add_argument("--manifest", type=Path, default=DEFAULT_MANIFEST)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    generated = generate(
        args.summary,
        args.records,
        args.result_json,
        args.long_csv,
        args.figure_svg,
        args.figure_png,
        args.manifest,
    )
    for path in generated:
        print(path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
