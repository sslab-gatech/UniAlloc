#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = ["matplotlib==3.11.0"]
# ///
"""Build target-centric allocator evaluation figures with Matplotlib.

Each figure represents one measured benchmark target. Campaigns stay separated
inside the Oxipng and Rsedis figures. Median markers and measured-run points
retain the observed distributions. The script also writes long-form plotted
data, source hashes, and a three-page PDF.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import os
import statistics
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

os.environ.setdefault("MPLBACKEND", "Agg")
os.environ.setdefault("SOURCE_DATE_EPOCH", "1783987200")

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib.axes import Axes
from matplotlib.backends.backend_pdf import PdfPages
from matplotlib.figure import Figure
from matplotlib.ticker import MaxNLocator, PercentFormatter


REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_FEATURE_DATA = (
    REPOSITORY_ROOT
    / "benchmark-results"
    / "allocator-feature-thp-evaluation-20260714.json"
)
DEFAULT_REALWORLD_DATA = (
    REPOSITORY_ROOT / "benchmark-results" / "realworld-rust-allocator-20260714.json"
)
DEFAULT_PAPER_EVALUATION = (
    REPOSITORY_ROOT / "benchmark-results" / "rust-alloc-paper-evaluation-20260714.json"
)
DEFAULT_OUTPUT_DIR = (
    REPOSITORY_ROOT / "docs" / "figures" / "allocator-feature-thp-20260714"
)

FIGURE_SIZE_INCHES = (16, 9)
PNG_DPI = 150
FIXED_TIMESTAMP = datetime(2026, 7, 14, tzinfo=timezone.utc)

# OpenAI chart palette used on the GPT-5.6 release page.
INK = "#17181A"
MUTED = "#62646B"
AXIS = "#47484F"
GRID = "#DDDEE1"
PALE = "#F0F1F2"
WHITE = "#FFFFFF"
SYSTEM = "#767881"
JEMALLOC = "#BCBEC4"
MIMALLOC = "#FF9365"
MIMALLOC_LIGHT = "#FFBDA1"
TCMALLOC = "#A3D576"
UNIALLOC = "#2E4780"
UNIALLOC_MID = "#5477C4"
UNIALLOC_LIGHT = "#A3BEFA"
TYPE_ISOLATION = "#5477C4"
TYPE_CONTROL = "#A3BEFA"

REALWORLD_ORDER = (
    "system",
    "jemalloc",
    "mimalloc",
    "tcmalloc",
    "unialloc",
    "typed_plain",
    "typeiso_perf",
)
FEATURE_ORDER = (
    "system",
    "mimalloc",
    "mimalloc_no_thp",
    "tcmalloc",
    "unialloc_no_optional",
    "unialloc_rseq",
    "unialloc_pthread_dtor",
    "unialloc_hugepage",
    "unialloc_separate_sc",
    "unialloc_type_isolation",
    "unialloc_metadata_segregation",
    "unialloc_pac",
    "unialloc",
    "typed_plain",
    "typeiso_perf",
)
RSEDIS_ORDER = (
    "system_ptmalloc",
    "jemalloc",
    "mimalloc",
    "tcmalloc",
    "unialloc",
)
RSEDIS_THP_ORDER = (
    "mimalloc_thp_default",
    "mimalloc_thp_off",
    "tcmalloc_default",
)

VARIANT_LABELS = {
    "system": "System",
    "system_ptmalloc": "System (ptmalloc)",
    "jemalloc": "jemalloc",
    "mimalloc": "mimalloc",
    "mimalloc_no_thp": "mimalloc - THP off",
    "mimalloc_thp_default": "mimalloc - THP allowed",
    "mimalloc_thp_off": "mimalloc - THP disabled",
    "tcmalloc": "TCMalloc",
    "tcmalloc_default": "TCMalloc - host default",
    "unialloc": "UniAlloc",
    "unialloc_no_optional": "UniAlloc base",
    "unialloc_rseq": "+ rseq",
    "unialloc_pthread_dtor": "+ pthread dtor",
    "unialloc_hugepage": "+ hugepage",
    "unialloc_separate_sc": "+ separate classes",
    "unialloc_type_isolation": "+ Type Isolation rt",
    "unialloc_metadata_segregation": "+ metadata",
    "unialloc_pac": "+ PAC",
    "typed_plain": "Typed control",
    "typeiso_perf": "Type Isolation",
}

VARIANT_COLORS = {
    "system": SYSTEM,
    "system_ptmalloc": SYSTEM,
    "jemalloc": JEMALLOC,
    "mimalloc": MIMALLOC,
    "mimalloc_no_thp": MIMALLOC_LIGHT,
    "mimalloc_thp_default": MIMALLOC,
    "mimalloc_thp_off": MIMALLOC_LIGHT,
    "tcmalloc": TCMALLOC,
    "tcmalloc_default": TCMALLOC,
    "unialloc": UNIALLOC,
    "unialloc_no_optional": UNIALLOC_LIGHT,
    "unialloc_rseq": UNIALLOC_MID,
    "unialloc_pthread_dtor": UNIALLOC_MID,
    "unialloc_hugepage": UNIALLOC_MID,
    "unialloc_separate_sc": UNIALLOC_MID,
    "unialloc_type_isolation": UNIALLOC_MID,
    "unialloc_metadata_segregation": UNIALLOC_MID,
    "unialloc_pac": UNIALLOC_MID,
    "typed_plain": TYPE_CONTROL,
    "typeiso_perf": TYPE_ISOLATION,
}

CHART_CONTRACTS = (
    {
        "stem": "01-ripgrep-configurations",
        "target": "ripgrep",
        "campaigns": ["ripgrep_realworld_single_thread"],
        "question": "How do allocator and Type Isolation configurations affect ripgrep wall time and peak RSS?",
        "encoding": "horizontal median bars with seven measured-run dots",
    },
    {
        "stem": "02-oxipng-configurations",
        "target": "Oxipng",
        "campaigns": [
            "oxipng_realworld_single_thread",
            "oxipng_feature_matrix_four_threads",
        ],
        "question": "How do allocator, THP, and UniAlloc feature configurations affect Oxipng?",
        "encoding": "two campaign-separated rows of horizontal median bars and measured-run dots",
    },
    {
        "stem": "03-rsedis-configurations",
        "target": "Rsedis",
        "campaigns": [
            "rsedis_allocator_matrix",
            "rsedis_thp_mechanism_matrix",
        ],
        "question": "How do allocator and THP configurations affect Rsedis throughput and peak RSS?",
        "encoding": "two campaign-separated rows of horizontal median bars and measured-run dots",
    },
)


@dataclass(frozen=True)
class Measurement:
    variant: str
    round: int
    performance: float
    peak_rss_mib: float


@dataclass(frozen=True)
class Campaign:
    campaign_id: str
    target: str
    label: str
    performance_metric: str
    performance_unit: str
    higher_is_better: bool
    repetitions: int
    warmups: int
    variants: tuple[str, ...]
    plot_reference: str
    matched_references: Mapping[str, str]
    measurements: tuple[Measurement, ...]
    source_json_path: str


@dataclass(frozen=True)
class PlotSeries:
    variant: str
    label: str
    color: str
    median: float
    raw: tuple[float, ...]


def load_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def median(values: Iterable[float]) -> float:
    values_list = list(values)
    if not values_list:
        raise ValueError("median requires at least one value")
    return float(statistics.median(values_list))


def percent_change(subject: float, reference: float) -> float:
    if reference <= 0:
        raise ValueError("ratio reference must be positive")
    return 100.0 * (subject / reference - 1.0)


def measurements_by_variant(campaign: Campaign) -> dict[str, tuple[Measurement, ...]]:
    grouped: dict[str, list[Measurement]] = {
        variant: [] for variant in campaign.variants
    }
    for measurement in campaign.measurements:
        grouped[measurement.variant].append(measurement)
    return {
        variant: tuple(sorted(rows, key=lambda row: row.round))
        for variant, rows in grouped.items()
    }


def validate_campaign(campaign: Campaign) -> None:
    grouped = measurements_by_variant(campaign)
    expected_rounds = tuple(range(1, campaign.repetitions + 1))
    for variant in campaign.variants:
        rows = grouped.get(variant, ())
        rounds = tuple(row.round for row in rows)
        if rounds != expected_rounds:
            raise ValueError(
                f"{campaign.campaign_id}/{variant} rounds {rounds} != {expected_rounds}"
            )
    if campaign.plot_reference not in campaign.variants:
        raise ValueError(f"missing plot reference in {campaign.campaign_id}")
    for variant in campaign.variants:
        reference = campaign.matched_references.get(variant)
        if reference not in campaign.variants:
            raise ValueError(
                f"missing matched reference for {campaign.campaign_id}/{variant}"
            )


def paired_values(
    campaign: Campaign,
    variant: str,
    reference: str,
    field: str,
) -> tuple[float, ...]:
    grouped = measurements_by_variant(campaign)
    subject_rows = grouped[variant]
    reference_rows = grouped[reference]
    values: list[float] = []
    for subject, baseline in zip(subject_rows, reference_rows, strict=True):
        if subject.round != baseline.round:
            raise ValueError(
                f"unaligned round in {campaign.campaign_id}: {variant} vs {reference}"
            )
        values.append(percent_change(getattr(subject, field), getattr(baseline, field)))
    return tuple(values)


def absolute_values(
    campaign: Campaign,
    variant: str,
    field: str,
) -> tuple[float, ...]:
    return tuple(
        getattr(row, field) for row in measurements_by_variant(campaign)[variant]
    )


def extract_realworld_campaigns(data: dict[str, Any]) -> dict[str, Campaign]:
    campaigns: dict[str, Campaign] = {}
    for item in data["campaigns"]:
        target = item["app"]
        rows = tuple(
            Measurement(
                variant=row["variant"],
                round=int(row["round"]),
                performance=float(row["wall_seconds"]),
                peak_rss_mib=float(row["peak_rss_kib"]) / 1024.0,
            )
            for row in item["measurements"]
            if not row.get("warmup", False)
        )
        matched_references = {
            variant: (
                "typed_plain"
                if variant in {"typed_plain", "typeiso_perf"}
                else "system"
            )
            for variant in REALWORLD_ORDER
        }
        campaign = Campaign(
            campaign_id=f"{target}_realworld_single_thread",
            target="Oxipng" if target == "oxipng" else target,
            label="Real-world matrix, 1 thread",
            performance_metric="wall_time",
            performance_unit="seconds",
            higher_is_better=False,
            repetitions=int(item["configuration"]["repetitions"]),
            warmups=int(item["configuration"]["warmups"]) * len(REALWORLD_ORDER),
            variants=REALWORLD_ORDER,
            plot_reference="system",
            matched_references=matched_references,
            measurements=rows,
            source_json_path=(
                "benchmark-results/realworld-rust-allocator-20260714.json"
                f"#campaigns[app={target}]"
            ),
        )
        validate_campaign(campaign)
        campaigns[target] = campaign
    return campaigns


def extract_oxipng_feature_campaign(data: dict[str, Any]) -> Campaign:
    item = data["campaigns"]["oxipng_feature_matrix"]
    rows = tuple(
        Measurement(
            variant=row["variant"],
            round=int(row["round"]),
            performance=float(row["wall_seconds"]),
            peak_rss_mib=float(row["peak_rss_kib"]) / 1024.0,
        )
        for row in item["measured_rows"]
    )
    matched_references = {
        "system": "system",
        "mimalloc": "system",
        "mimalloc_no_thp": "mimalloc",
        "tcmalloc": "system",
        "unialloc_no_optional": "system",
        "unialloc_rseq": "unialloc_no_optional",
        "unialloc_pthread_dtor": "unialloc_no_optional",
        "unialloc_hugepage": "unialloc_no_optional",
        "unialloc_separate_sc": "unialloc_no_optional",
        "unialloc_type_isolation": "unialloc_no_optional",
        "unialloc_metadata_segregation": "unialloc_separate_sc",
        "unialloc_pac": "unialloc_type_isolation",
        "unialloc": "unialloc_no_optional",
        "typed_plain": "typed_plain",
        "typeiso_perf": "typed_plain",
    }
    campaign = Campaign(
        campaign_id="oxipng_feature_matrix_four_threads",
        target="Oxipng",
        label="Feature matrix, 4 threads",
        performance_metric="wall_time",
        performance_unit="seconds",
        higher_is_better=False,
        repetitions=5,
        warmups=15,
        variants=FEATURE_ORDER,
        plot_reference="system",
        matched_references=matched_references,
        measurements=rows,
        source_json_path=(
            "benchmark-results/allocator-feature-thp-evaluation-20260714.json"
            "#campaigns.oxipng_feature_matrix"
        ),
    )
    validate_campaign(campaign)
    return campaign


def extract_rsedis_allocator_campaign(data: dict[str, Any]) -> Campaign:
    item = data["live_results"]["rsedis_current_head"]
    rows = tuple(
        Measurement(
            variant=row["variant"],
            round=int(row.get("round_index", row.get("repetition"))),
            performance=float(row["combined_effective_rps"]),
            peak_rss_mib=float(row["peak_rss_kib"]) / 1024.0,
        )
        for row in item["measured_rows"]
    )
    campaign = Campaign(
        campaign_id="rsedis_allocator_matrix",
        target="Rsedis",
        label="Allocator matrix",
        performance_metric="throughput",
        performance_unit="requests_per_second",
        higher_is_better=True,
        repetitions=5,
        warmups=5,
        variants=RSEDIS_ORDER,
        plot_reference="system_ptmalloc",
        matched_references={variant: "system_ptmalloc" for variant in RSEDIS_ORDER},
        measurements=rows,
        source_json_path=(
            "benchmark-results/rust-alloc-paper-evaluation-20260714.json"
            "#live_results.rsedis_current_head"
        ),
    )
    validate_campaign(campaign)
    return campaign


def extract_rsedis_thp_campaign(data: dict[str, Any]) -> Campaign:
    item = data["campaigns"]["rsedis_thp_matrix"]
    rows = tuple(
        Measurement(
            variant=row["variant"],
            round=int(row["round"]),
            performance=float(row["combined_effective_requests_per_second"]),
            peak_rss_mib=float(row["peak_rss_kib"]) / 1024.0,
        )
        for row in item["measured_rows"]
    )
    campaign = Campaign(
        campaign_id="rsedis_thp_mechanism_matrix",
        target="Rsedis",
        label="THP mechanism matrix",
        performance_metric="throughput",
        performance_unit="requests_per_second",
        higher_is_better=True,
        repetitions=5,
        warmups=3,
        variants=RSEDIS_THP_ORDER,
        plot_reference="mimalloc_thp_default",
        matched_references={
            variant: "mimalloc_thp_default" for variant in RSEDIS_THP_ORDER
        },
        measurements=rows,
        source_json_path=(
            "benchmark-results/allocator-feature-thp-evaluation-20260714.json"
            "#campaigns.rsedis_thp_matrix"
        ),
    )
    validate_campaign(campaign)
    return campaign


def build_campaigns(
    feature_data: dict[str, Any],
    realworld_data: dict[str, Any],
    paper_data: dict[str, Any],
) -> dict[str, Campaign]:
    if not realworld_data.get("success"):
        raise ValueError("real-world allocator report is not successful")
    realworld = extract_realworld_campaigns(realworld_data)
    campaigns = {campaign.campaign_id: campaign for campaign in realworld.values()}
    feature = extract_oxipng_feature_campaign(feature_data)
    rsedis = extract_rsedis_allocator_campaign(paper_data)
    rsedis_thp = extract_rsedis_thp_campaign(feature_data)
    campaigns[feature.campaign_id] = feature
    campaigns[rsedis.campaign_id] = rsedis
    campaigns[rsedis_thp.campaign_id] = rsedis_thp
    return campaigns


def target_csv_rows(campaigns: Sequence[Campaign]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for campaign in campaigns:
        grouped = measurements_by_variant(campaign)
        for variant in campaign.variants:
            plot_reference = campaign.plot_reference
            matched_reference = campaign.matched_references[variant]
            performance_values = absolute_values(campaign, variant, "performance")
            rss_values = absolute_values(campaign, variant, "peak_rss_mib")
            plot_performance = paired_values(
                campaign, variant, plot_reference, "performance"
            )
            plot_rss = paired_values(campaign, variant, plot_reference, "peak_rss_mib")
            matched_performance = paired_values(
                campaign, variant, matched_reference, "performance"
            )
            matched_rss = paired_values(
                campaign, variant, matched_reference, "peak_rss_mib"
            )
            medians = {
                "performance_value_median": median(performance_values),
                "peak_rss_mib_median": median(rss_values),
                "plot_performance_delta_percent_median": median(plot_performance),
                "plot_peak_rss_delta_percent_median": median(plot_rss),
                "matched_performance_delta_percent_median": median(matched_performance),
                "matched_peak_rss_delta_percent_median": median(matched_rss),
            }
            for index, measurement in enumerate(grouped[variant]):
                rows.append(
                    {
                        "target": campaign.target,
                        "campaign": campaign.campaign_id,
                        "campaign_label": campaign.label,
                        "variant": variant,
                        "label": VARIANT_LABELS[variant],
                        "round": measurement.round,
                        "repetitions": campaign.repetitions,
                        "performance_metric": campaign.performance_metric,
                        "performance_value": performance_values[index],
                        "performance_unit": campaign.performance_unit,
                        "performance_direction": (
                            "higher_is_better"
                            if campaign.higher_is_better
                            else "lower_is_better"
                        ),
                        "plot_reference": plot_reference,
                        "plot_performance_delta_percent": plot_performance[index],
                        "plot_peak_rss_delta_percent": plot_rss[index],
                        "matched_reference": matched_reference,
                        "matched_performance_delta_percent": matched_performance[index],
                        "matched_peak_rss_delta_percent": matched_rss[index],
                        "peak_rss_mib": rss_values[index],
                        **medians,
                        "source_json_path": campaign.source_json_path,
                    }
                )
    return rows


def hugetlb_accounting_rows(feature_data: dict[str, Any]) -> list[dict[str, Any]]:
    sampling = feature_data["runtime_preflights"]["unialloc_feature_activation"][
        "process_memory_sampling"
    ]
    rows: list[dict[str, Any]] = []
    for variant in ("unialloc_no_optional", "unialloc_hugepage"):
        cell = sampling["variants"][variant]
        maxima = cell["maxima"]
        rows.append(
            {
                "target": "Oxipng",
                "variant": variant,
                "label": VARIANT_LABELS[variant],
                "sampling_interval_seconds": sampling["sampling_interval_seconds"],
                "sample_count": cell["sample_count"],
                "vmhwm_mib_max": maxima["VmHWM_kib"] / 1024.0,
                "hugetlb_pages_mib_max": maxima["HugetlbPages_kib"] / 1024.0,
                "boundary": (
                    "VmHWM and HugetlbPages maxima were sampled independently "
                    "and are not stacked."
                ),
                "source_json_path": (
                    "benchmark-results/allocator-feature-thp-evaluation-20260714.json"
                    "#runtime_preflights.unialloc_feature_activation."
                    f"process_memory_sampling.variants.{variant}.maxima"
                ),
            }
        )
    return rows


def write_csv(path: Path, rows: Sequence[dict[str, Any]]) -> None:
    if not rows:
        raise ValueError(f"cannot write empty table: {path}")
    fieldnames: list[str] = []
    for row in rows:
        for key in row:
            if key not in fieldnames:
                fieldnames.append(key)
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames, lineterminator="\n")
        writer.writeheader()
        writer.writerows(rows)


def csv_data_row_count(path: Path) -> int:
    with path.open(encoding="utf-8", newline="") as handle:
        return max(sum(1 for _ in handle) - 1, 0)


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
            "axes.titlesize": 15,
            "axes.titleweight": 700,
            "axes.linewidth": 0.8,
            "xtick.color": AXIS,
            "ytick.color": INK,
            "xtick.labelsize": 10,
            "ytick.labelsize": 11,
            "text.color": INK,
            "svg.hashsalt": "unialloc-allocator-figures-20260714",
            "svg.fonttype": "none",
            "pdf.fonttype": 42,
            "savefig.facecolor": WHITE,
            "savefig.edgecolor": WHITE,
        }
    )


def new_figure(
    title: str,
    subtitle: str,
    takeaway: str,
    *,
    nrows: int,
    ncols: int,
    width_ratios: Sequence[float] | None = None,
    height_ratios: Sequence[float] | None = None,
    left: float = 0.14,
    right: float = 0.96,
    top: float = 0.82,
    bottom: float = 0.14,
    wspace: float = 0.28,
    hspace: float = 0.48,
) -> tuple[Figure, Any]:
    figure = plt.figure(figsize=FIGURE_SIZE_INCHES, dpi=100, layout=None)
    grid = figure.add_gridspec(
        nrows,
        ncols,
        width_ratios=width_ratios,
        height_ratios=height_ratios,
        left=left,
        right=right,
        top=top,
        bottom=bottom,
        wspace=wspace,
        hspace=hspace,
    )
    figure.suptitle(
        title,
        x=0.052,
        y=0.965,
        ha="left",
        va="top",
        fontsize=28,
        fontweight=700,
    )
    figure.text(
        0.052,
        0.918,
        subtitle,
        ha="left",
        va="top",
        fontsize=12.5,
        color=MUTED,
    )
    if takeaway:
        figure.text(
            0.948,
            0.958,
            takeaway,
            ha="right",
            va="top",
            fontsize=12.5,
            fontweight=700,
            color=UNIALLOC,
        )
    return figure, grid


def add_footer(figure: Figure, text: str) -> None:
    figure.text(
        0.052,
        0.035,
        text,
        ha="left",
        va="bottom",
        fontsize=9.5,
        color=MUTED,
    )


def plot_series(
    campaign: Campaign,
    variants: Sequence[str],
    *,
    kind: str,
    reference: str | None = None,
) -> list[PlotSeries]:
    result: list[PlotSeries] = []
    for variant in variants:
        if kind == "performance_delta":
            if reference is None:
                raise ValueError("performance delta plot requires a reference")
            raw = paired_values(campaign, variant, reference, "performance")
        elif kind == "peak_rss_absolute":
            raw = absolute_values(campaign, variant, "peak_rss_mib")
        elif kind == "performance_absolute":
            raw = absolute_values(campaign, variant, "performance")
        else:
            raise ValueError(f"unknown plot kind: {kind}")
        result.append(
            PlotSeries(
                variant=variant,
                label=VARIANT_LABELS[variant],
                color=VARIANT_COLORS[variant],
                median=median(raw),
                raw=raw,
            )
        )
    return result


def _value_domain(
    series: Sequence[PlotSeries],
    *,
    signed: bool,
    forced_limits: tuple[float, float] | None,
) -> tuple[float, float]:
    if forced_limits is not None:
        return forced_limits
    values = [value for item in series for value in (*item.raw, item.median)]
    if signed:
        low = min(0.0, min(values))
        high = max(0.0, max(values))
        negative = abs(low)
        positive = abs(high)
        if (
            negative > 0
            and positive > 0
            and max(negative, positive) <= 4 * min(negative, positive)
        ):
            extent = max(negative, positive) * 1.22
            return -extent, extent
        span = max(high - low, 1.0)
        return low - 0.10 * span, high + 0.18 * span
    high = max(values)
    return 0.0, high * 1.20 if high > 0 else 1.0


def draw_median_bars(
    ax: Axes,
    series: Sequence[PlotSeries],
    *,
    title: str,
    kicker: str,
    signed: bool,
    unit: str,
    show_y_labels: bool = True,
    group_breaks: Sequence[float] = (),
    forced_limits: tuple[float, float] | None = None,
    value_digits: int = 1,
    tick_percent: bool = False,
    label_size: float | None = None,
) -> None:
    y_positions = list(range(len(series)))
    raw_marker_size = 12 if len(series) >= 12 else 17
    value_font_size = 9.0 if len(series) >= 12 else 10.2
    tick_label_size = label_size or (9.0 if len(series) >= 12 else 10.8)

    for y, item in zip(y_positions, series, strict=True):
        ax.barh(
            y,
            item.median,
            height=0.46,
            color=item.color,
            edgecolor="none",
            zorder=2,
        )
        jitter = [
            -0.17 + (0.34 * index / max(1, len(item.raw) - 1))
            for index in range(len(item.raw))
        ]
        ax.scatter(
            item.raw,
            [y + offset for offset in jitter],
            s=raw_marker_size,
            c=INK,
            alpha=0.52,
            edgecolors=WHITE,
            linewidths=0.45,
            zorder=3,
        )
        ax.vlines(
            item.median,
            y - 0.26,
            y + 0.26,
            color=INK,
            linewidth=1.6,
            zorder=5,
        )

    low, high = _value_domain(series, signed=signed, forced_limits=forced_limits)
    ax.set_xlim(low, high)
    span = high - low
    for y, item in zip(y_positions, series, strict=True):
        if signed:
            pad = 0.018 * span
            if item.median < 0 and abs(item.median) < 0.08 * span:
                label_x = pad
                horizontal_alignment = "left"
            else:
                label_x = item.median + (pad if item.median >= 0 else -pad)
                horizontal_alignment = "left" if item.median >= 0 else "right"
            label = f"{item.median:+.{value_digits}f}%"
        else:
            pad = 0.014 * span
            label_x = item.median + pad
            horizontal_alignment = "left"
            label = f"{item.median:.{value_digits}f} {unit}"
        ax.text(
            label_x,
            y,
            label,
            ha=horizontal_alignment,
            va="center",
            fontsize=value_font_size,
            fontweight=700 if item.variant in {"unialloc", "typeiso_perf"} else 400,
            color=INK,
            clip_on=False,
            zorder=6,
        )

    ax.set_yticks(y_positions)
    if show_y_labels:
        ax.set_yticklabels(
            [item.label for item in series],
            fontsize=tick_label_size,
        )
        for tick, item in zip(ax.get_yticklabels(), series, strict=True):
            if item.variant in {"unialloc", "typeiso_perf"}:
                tick.set_fontweight(700)
    else:
        ax.set_yticklabels([])
    ax.invert_yaxis()
    ax.set_ylim(len(series) - 0.5, -0.5)

    ax.set_title(
        f"{kicker}\n{title}",
        loc="left",
        pad=8,
        fontsize=13.5,
        linespacing=1.15,
    )
    ax.set_xlabel(unit if not tick_percent else "Change (%)", labelpad=7)
    ax.xaxis.set_major_locator(MaxNLocator(nbins=5, min_n_ticks=3))
    if tick_percent:
        ax.xaxis.set_major_formatter(PercentFormatter(xmax=100, decimals=0))
    ax.grid(False)
    ax.spines["top"].set_visible(False)
    ax.spines["right"].set_visible(False)
    ax.spines["left"].set_visible(False)
    ax.spines["bottom"].set_color(AXIS)
    ax.spines["bottom"].set_linewidth(0.8)
    ax.tick_params(axis="y", length=0, pad=7)
    ax.tick_params(axis="x", length=3.5, width=0.8, pad=4)
    if signed:
        ax.axvline(0.0, color=AXIS, linewidth=1.0, zorder=1)
    for y in group_breaks:
        ax.axhline(y, color=GRID, linewidth=0.9, zorder=1)


def matched_delta(campaign: Campaign, variant: str, field: str) -> float:
    reference = campaign.matched_references[variant]
    return median(paired_values(campaign, variant, reference, field))


def rss_median(campaign: Campaign, variant: str) -> float:
    return median(absolute_values(campaign, variant, "peak_rss_mib"))


def render_ripgrep(campaign: Campaign) -> Figure:
    unialloc_delta = median(
        paired_values(campaign, "unialloc", "system", "performance")
    )
    figure, grid = new_figure(
        f"ripgrep: UniAlloc uses 6 MiB at {unialloc_delta:+.1f}% wall time",
        "Single-thread deterministic search; n=7 paired measured runs per configuration. Bars are medians; dots are runs.",
        "",
        nrows=1,
        ncols=2,
        width_ratios=(1.0, 1.0),
        left=0.13,
        wspace=0.25,
    )
    wall_ax = figure.add_subplot(grid[0, 0])
    rss_ax = figure.add_subplot(grid[0, 1])
    draw_median_bars(
        wall_ax,
        plot_series(
            campaign,
            REALWORLD_ORDER,
            kind="performance_delta",
            reference="system",
        ),
        title="Wall time vs System (%) | lower is better",
        kicker="Real-world | 1 thread | n=7",
        signed=True,
        unit="%",
        tick_percent=True,
        group_breaks=(4.5,),
        value_digits=1,
    )
    draw_median_bars(
        rss_ax,
        plot_series(campaign, REALWORLD_ORDER, kind="peak_rss_absolute"),
        title="Peak RSS (MiB) | lower is better",
        kicker="Real-world | same runs | n=7",
        signed=False,
        unit="MiB",
        show_y_labels=False,
        group_breaks=(4.5,),
        value_digits=1,
    )
    type_delta = matched_delta(campaign, "typeiso_perf", "performance")
    type_rss = matched_delta(campaign, "typeiso_perf", "peak_rss_mib")
    add_footer(
        figure,
        "Median of same-round ratios; dots are runs. "
        f"Type Isolation / Typed control: {type_delta:+.2f}% wall, {type_rss:+.2f}% RSS. Source: realworld-rust-allocator-20260714.json.",
    )
    return figure


def render_oxipng(realworld: Campaign, feature: Campaign) -> Figure:
    thp_wall = matched_delta(feature, "mimalloc_no_thp", "performance")
    thp_rss = matched_delta(feature, "mimalloc_no_thp", "peak_rss_mib")
    figure, grid = new_figure(
        "Oxipng: UniAlloc stays below System RSS in both campaigns",
        "Campaigns remain separate: a 1-thread real-world matrix (n=7) and a 4-thread feature matrix (n=5). Bars are medians; dots are runs.",
        "",
        nrows=2,
        ncols=2,
        width_ratios=(1.0, 1.0),
        height_ratios=(0.82, 1.58),
        left=0.18,
        right=0.965,
        wspace=0.24,
        hspace=0.56,
        top=0.815,
        bottom=0.135,
    )
    top_wall = figure.add_subplot(grid[0, 0])
    top_rss = figure.add_subplot(grid[0, 1])
    bottom_wall = figure.add_subplot(grid[1, 0])
    bottom_rss = figure.add_subplot(grid[1, 1])
    draw_median_bars(
        top_wall,
        plot_series(
            realworld,
            REALWORLD_ORDER,
            kind="performance_delta",
            reference="system",
        ),
        title="Wall time vs System (%) | lower is better",
        kicker="Real-world | 1 thread | n=7",
        signed=True,
        unit="%",
        tick_percent=True,
        group_breaks=(4.5,),
        value_digits=1,
        label_size=9.4,
    )
    draw_median_bars(
        top_rss,
        plot_series(realworld, REALWORLD_ORDER, kind="peak_rss_absolute"),
        title="Peak RSS (MiB) | lower is better",
        kicker="Real-world | 1 thread | n=7",
        signed=False,
        unit="MiB",
        show_y_labels=False,
        group_breaks=(4.5,),
        value_digits=1,
    )
    draw_median_bars(
        bottom_wall,
        plot_series(
            feature,
            FEATURE_ORDER,
            kind="performance_delta",
            reference="system",
        ),
        title="Wall time vs System (%) | lower is better",
        kicker="Feature matrix | 4 threads | n=5",
        signed=True,
        unit="%",
        tick_percent=True,
        group_breaks=(3.5, 12.5),
        value_digits=1,
        label_size=9.6,
    )
    draw_median_bars(
        bottom_rss,
        plot_series(feature, FEATURE_ORDER, kind="peak_rss_absolute"),
        title="Peak RSS (MiB) | lower is better",
        kicker="Feature matrix | 4 threads | n=5",
        signed=False,
        unit="MiB",
        show_y_labels=False,
        group_breaks=(3.5, 12.5),
        value_digits=1,
    )
    policy_wall = matched_delta(feature, "typeiso_perf", "performance")
    add_footer(
        figure,
        "Independent axes; campaigns are not pooled. "
        f"THP off / allowed: {thp_rss:+.1f}% RSS, {thp_wall:+.1f}% wall; Type Isolation / control: {policy_wall:+.2f}% wall. Sources: realworld + feature/THP JSON.",
    )
    return figure


def render_rsedis(
    main: Campaign, thp: Campaign, feature_data: dict[str, Any]
) -> Figure:
    unialloc_delta = median(
        paired_values(main, "unialloc", "system_ptmalloc", "performance")
    )
    figure, grid = new_figure(
        f"Rsedis: UniAlloc gains {unialloc_delta:+.1f}% throughput at {rss_median(main, 'unialloc'):.1f} MiB RSS",
        "Five fresh-server runs per configuration. Allocator and THP mechanism campaigns use independent axes and baselines.",
        "",
        nrows=2,
        ncols=2,
        width_ratios=(1.0, 1.0),
        height_ratios=(1.0, 0.82),
        left=0.15,
        right=0.965,
        wspace=0.25,
        hspace=0.56,
        top=0.815,
        bottom=0.14,
    )
    main_perf = figure.add_subplot(grid[0, 0])
    main_rss = figure.add_subplot(grid[0, 1])
    thp_perf = figure.add_subplot(grid[1, 0])
    thp_rss = figure.add_subplot(grid[1, 1])
    draw_median_bars(
        main_perf,
        plot_series(
            main,
            RSEDIS_ORDER,
            kind="performance_delta",
            reference="system_ptmalloc",
        ),
        title="Throughput vs System (%) | higher is better",
        kicker="Allocator matrix | n=5",
        signed=True,
        unit="%",
        tick_percent=True,
        value_digits=1,
    )
    draw_median_bars(
        main_rss,
        plot_series(main, RSEDIS_ORDER, kind="peak_rss_absolute"),
        title="Peak RSS (MiB) | lower is better",
        kicker="Allocator matrix | n=5",
        signed=False,
        unit="MiB",
        show_y_labels=False,
        value_digits=1,
    )
    draw_median_bars(
        thp_perf,
        plot_series(
            thp,
            RSEDIS_THP_ORDER,
            kind="performance_delta",
            reference="mimalloc_thp_default",
        ),
        title="Throughput vs THP allowed (%) | higher is better",
        kicker="THP mechanism matrix | n=5",
        signed=True,
        unit="%",
        tick_percent=True,
        value_digits=2,
        label_size=10.0,
    )
    draw_median_bars(
        thp_rss,
        plot_series(thp, RSEDIS_THP_ORDER, kind="peak_rss_absolute"),
        title="Peak RSS (MiB) | lower is better",
        kicker="THP mechanism matrix | n=5",
        signed=False,
        unit="MiB",
        show_y_labels=False,
        value_digits=1,
    )
    summaries = feature_data["campaigns"]["rsedis_thp_matrix"]["summaries"]
    hugepage_medians = {
        row["variant"]: float(row["post_anon_huge_pages_kib_median"]) / 1024.0
        for row in summaries
    }
    add_footer(
        figure,
        "Independent axes; campaigns are not pooled. THP matrix median AnonHugePages: "
        f"{hugepage_medians['mimalloc_thp_default']:.0f} MiB allowed, {hugepage_medians['mimalloc_thp_off']:.0f} MiB disabled. Sources: paper + feature/THP JSON.",
    )
    return figure


def export_figure(figure: Figure, svg_path: Path, png_path: Path) -> None:
    common_metadata = {
        "Creator": "UniAlloc Matplotlib figure exporter",
        "Date": "2026-07-14",
    }
    figure.savefig(
        svg_path,
        format="svg",
        dpi=PNG_DPI,
        metadata=common_metadata,
        bbox_inches=None,
    )
    svg_path.write_text(
        "\n".join(
            line.rstrip() for line in svg_path.read_text(encoding="utf-8").splitlines()
        )
        + "\n",
        encoding="utf-8",
    )
    figure.savefig(
        png_path,
        format="png",
        dpi=PNG_DPI,
        metadata={
            "Software": "UniAlloc Matplotlib figure exporter",
            "Creation Time": "2026-07-14T00:00:00Z",
        },
        bbox_inches=None,
    )


def write_pdf(path: Path, figures: Sequence[Figure]) -> None:
    metadata = {
        "Title": "UniAlloc target-centric allocator evaluation figures",
        "Author": "UniAlloc evaluation",
        "Creator": "Matplotlib 3.11.0",
        "Producer": "Matplotlib PDF backend",
        "CreationDate": FIXED_TIMESTAMP,
        "ModDate": FIXED_TIMESTAMP,
    }
    with PdfPages(path, metadata=metadata) as pdf:
        for figure in figures:
            pdf.savefig(figure, bbox_inches=None)


def write_readme(output_dir: Path, source_hashes: Mapping[str, str]) -> None:
    content = f"""# Target-centric allocator evaluation figures

This pack contains **three** presentation-eligible 16:9 measured-target detail
figures. They show paired performance deltas and absolute peak RSS in MiB using
campaign-local references. Separate Oxipng and Rsedis campaigns remain in
labeled, unpooled sections.

| Figure | Target | Campaigns shown |
|---|---|---|
| `01-ripgrep-configurations` | ripgrep | 7-configuration real-world matrix, n=7 |
| `02-oxipng-configurations` | Oxipng | 1-thread real-world matrix, n=7; 4-thread 15-configuration feature matrix, n=5 |
| `03-rsedis-configurations` | Rsedis | 5-allocator matrix, n=5; separate 3-configuration THP mechanism matrix, n=5 |

The plotted scope contains **3 unique targets, 37 target-configuration cells,
213 measured runs, and 37 warmups**. Type Isolation rows are subsets of these
campaigns. Bars show medians; filled dots show every retained measured run.
Same-round deltas are computed before their medians. Each detail retains its
original diagnostic source and campaign labels.

## Formats

- SVG: three editable vector figures with live text.
- PNG: three 2,400 x 1,350 slide-ready exports.
- PDF: one three-page figure pack.
- CSV: three target tables and one HugeTLB accounting table.
- JSON: source hashes, chart contracts, campaign boundaries, and asset hashes.

## Source artifacts

- `benchmark-results/realworld-rust-allocator-20260714.json`
  SHA-256: `{source_hashes["realworld"]}`
- `benchmark-results/rust-alloc-paper-evaluation-20260714.json`
  SHA-256: `{source_hashes["paper"]}`
- `benchmark-results/allocator-feature-thp-evaluation-20260714.json`
  SHA-256: `{source_hashes["feature"]}`

## Regenerate

```bash
uv run evaluation/scripts/plot_allocator_feature_thp_slides.py
```

The script pins Matplotlib 3.11.0 through PEP 723 metadata. The figures are
current-host descriptive evidence. Raw-point variation and median bars are not
confidence intervals. The 1-thread and 4-thread Oxipng campaigns are not
pooled. The Rsedis allocator and THP campaigns are not pooled. TCMalloc in the
THP mechanism campaign uses the host default; UniAlloc is outside that
mechanism campaign. `VmHWM` and `HugetlbPages` maxima in the supporting table
were sampled independently and are not stacked.
"""
    (output_dir / "README.md").write_text(content, encoding="utf-8")


def write_manifest(
    output_dir: Path,
    source_paths: Mapping[str, Path],
    campaigns: Sequence[Campaign],
    generated_paths: Sequence[Path],
) -> Path:
    data_paths = sorted((output_dir / "data").glob("*.csv"))
    asset_paths = sorted(
        (
            path
            for path in generated_paths
            if path.exists() and path.parent == output_dir and path.suffix != ".md"
        ),
        key=lambda path: path.name,
    )
    manifest = {
        "schema_version": 3,
        "source": "unialloc-target-centric-allocator-figure-pack",
        "evaluation_scope": {
            "unique_targets": ["ripgrep", "Oxipng", "Rsedis"],
            "unique_target_count": 3,
            "timed_configuration_cells": sum(
                len(campaign.variants) for campaign in campaigns
            ),
            "measured_timed_runs": sum(
                len(campaign.measurements) for campaign in campaigns
            ),
            "timed_warmups": sum(campaign.warmups for campaign in campaigns),
            "type_isolation_is_subset": True,
        },
        "campaigns": [
            {
                "id": campaign.campaign_id,
                "target": campaign.target,
                "label": campaign.label,
                "configuration_count": len(campaign.variants),
                "repetitions": campaign.repetitions,
                "measured_runs": len(campaign.measurements),
                "warmups": campaign.warmups,
                "performance_metric": campaign.performance_metric,
                "plot_reference": campaign.plot_reference,
                "source_json_path": campaign.source_json_path,
            }
            for campaign in campaigns
        ],
        "source_artifacts": [
            {
                "name": name,
                "path": str(path.relative_to(REPOSITORY_ROOT)),
                "sha256": sha256_file(path),
            }
            for name, path in source_paths.items()
        ],
        "generator": {
            "path": str(Path(__file__).resolve().relative_to(REPOSITORY_ROOT)),
            "sha256": sha256_file(Path(__file__).resolve()),
            "renderer": f"Matplotlib {matplotlib.__version__}",
            "figure_size_inches": {
                "width": FIGURE_SIZE_INCHES[0],
                "height": FIGURE_SIZE_INCHES[1],
            },
            "svg_viewbox_points": {
                "width": FIGURE_SIZE_INCHES[0] * 72,
                "height": FIGURE_SIZE_INCHES[1] * 72,
            },
            "png_dimensions": {"width": 2400, "height": 1350},
            "pdf_pages": 3,
        },
        "palette": {
            "system": SYSTEM,
            "jemalloc": JEMALLOC,
            "mimalloc": MIMALLOC,
            "tcmalloc": TCMALLOC,
            "unialloc": UNIALLOC,
            "typed_control": TYPE_CONTROL,
            "type_isolation": TYPE_ISOLATION,
        },
        "chart_contracts": list(CHART_CONTRACTS),
        "data_tables": [
            {
                "path": str(path.relative_to(output_dir)),
                "sha256": sha256_file(path),
                "rows": csv_data_row_count(path),
            }
            for path in data_paths
        ],
        "assets": [
            {
                "path": str(path.relative_to(output_dir)),
                "sha256": sha256_file(path),
            }
            for path in asset_paths
        ],
        "boundaries": [
            "Each figure represents one measured target.",
            "Oxipng 1-thread and 4-thread campaigns use independent axes and are not pooled.",
            "Rsedis allocator and THP mechanism campaigns use independent axes and are not pooled.",
            "TCMalloc in the THP mechanism campaign uses the host default.",
            "UniAlloc is outside the Rsedis THP mechanism campaign.",
            "VmHWM and HugetlbPages maxima are independently sampled and are not stacked.",
            "Raw-point ranges are descriptive and are not confidence intervals.",
        ],
    }
    path = output_dir / "slide-data-manifest.json"
    path.write_text(
        json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    return path


def clean_output(output_dir: Path) -> None:
    output_dir.mkdir(parents=True, exist_ok=True)
    data_dir = output_dir / "data"
    data_dir.mkdir(parents=True, exist_ok=True)
    for pattern in ("*.svg", "*.png", "*.pdf"):
        for path in output_dir.glob(pattern):
            path.unlink()
    for path in data_dir.glob("*.csv"):
        path.unlink()


def generate(
    feature_path: Path,
    realworld_path: Path,
    paper_path: Path,
    output_dir: Path,
) -> list[Path]:
    configure_matplotlib()
    feature_path = feature_path.resolve()
    realworld_path = realworld_path.resolve()
    paper_path = paper_path.resolve()
    clean_output(output_dir)
    data_dir = output_dir / "data"

    feature_data = load_json(feature_path)
    realworld_data = load_json(realworld_path)
    paper_data = load_json(paper_path)
    campaign_map = build_campaigns(feature_data, realworld_data, paper_data)
    campaigns = (
        campaign_map["ripgrep_realworld_single_thread"],
        campaign_map["oxipng_realworld_single_thread"],
        campaign_map["oxipng_feature_matrix_four_threads"],
        campaign_map["rsedis_allocator_matrix"],
        campaign_map["rsedis_thp_mechanism_matrix"],
    )

    target_tables = {
        "ripgrep-configurations.csv": target_csv_rows((campaigns[0],)),
        "oxipng-configurations.csv": target_csv_rows((campaigns[1], campaigns[2])),
        "rsedis-configurations.csv": target_csv_rows((campaigns[3], campaigns[4])),
        "oxipng-hugetlb-accounting.csv": hugetlb_accounting_rows(feature_data),
    }
    for name, rows in target_tables.items():
        write_csv(data_dir / name, rows)

    figures = (
        render_ripgrep(campaigns[0]),
        render_oxipng(campaigns[1], campaigns[2]),
        render_rsedis(campaigns[3], campaigns[4], feature_data),
    )
    generated: list[Path] = []
    for contract, figure in zip(CHART_CONTRACTS, figures, strict=True):
        svg_path = output_dir / f"{contract['stem']}.svg"
        png_path = output_dir / f"{contract['stem']}.png"
        export_figure(figure, svg_path, png_path)
        generated.extend((svg_path, png_path))

    pdf_path = output_dir / "allocator-feature-thp-slide-pack.pdf"
    write_pdf(pdf_path, figures)
    generated.append(pdf_path)

    for figure in figures:
        plt.close(figure)

    source_paths = {
        "feature": feature_path,
        "realworld": realworld_path,
        "paper": paper_path,
    }
    write_readme(
        output_dir,
        {name: sha256_file(path) for name, path in source_paths.items()},
    )
    generated.extend(sorted(data_dir.glob("*.csv")))
    generated.append(output_dir / "README.md")
    manifest_path = write_manifest(output_dir, source_paths, campaigns, generated)
    generated.append(manifest_path)
    return generated


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--feature-data", type=Path, default=DEFAULT_FEATURE_DATA)
    parser.add_argument("--realworld-data", type=Path, default=DEFAULT_REALWORLD_DATA)
    parser.add_argument(
        "--paper-evaluation", type=Path, default=DEFAULT_PAPER_EVALUATION
    )
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT_DIR)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    generated = generate(
        args.feature_data,
        args.realworld_data,
        args.paper_evaluation,
        args.output_dir,
    )
    for path in generated:
        print(path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
