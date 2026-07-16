#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = ["matplotlib==3.11.0"]
# ///
"""Build slide-ready Micro and Macro allocator overview figures."""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import math
import os
import shutil
import statistics
import tempfile
from collections import defaultdict
from pathlib import Path
from typing import Any, Mapping, Sequence

os.environ.setdefault("MPLBACKEND", "Agg")
os.environ.setdefault("SOURCE_DATE_EPOCH", "1784246400")

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib.lines import Line2D
from matplotlib.ticker import FuncFormatter

matplotlib.rcParams["svg.hashsalt"] = "unialloc-allocator-overview-v1"

ROOT = Path(__file__).resolve().parents[2]
MAIN_MICRO_ROOT = ROOT / "evaluation/raw/std-bench-allocator-baselines-ce8af7b-v1"
EXTRA_MICRO_ROOT = ROOT / "evaluation/raw/std-bench-extra-allocators-ce8af7b-v1"
PRESENTATION_DATA = ROOT / "docs/figures/allocator-evaluation-20260716/presentation-data.json"
OUTPUT = ROOT / "docs/figures/allocator-evaluation-20260716/allocator-overview"
IMPLEMENTATION_REVISION = "ce8af7b89a5cba9a9b3f57d9b02bb0c8cb5c3503"
FIXED_DATE = "2026-07-16"
FIXED_DATETIME = dt.datetime(2026, 7, 16, tzinfo=dt.timezone.utc)
PNG_DPI = 300
FIGURE_SIZE = (23.0, 10.2)
ROBUST_FLOOR_NS = 100.0

MAIN_VARIANTS = (
    "unialloc",
    "jemalloc",
    "mimalloc",
    "mimalloc_no_thp",
    "google_tcmalloc",
)
EXTRA_VARIANTS = ("unialloc", "ptmalloc", "snmalloc", "scudo")
EXTERNAL_ORDER = (
    "ptmalloc",
    "jemalloc",
    "mimalloc",
    "mimalloc_no_thp",
    "google_tcmalloc",
    "snmalloc",
    "scudo",
)
MICRO_ORDER = ("unialloc", "type_isolation", *EXTERNAL_ORDER)
MACRO_ORDER = MICRO_ORDER
TARGETS = (
    "Collections",
    "Oxipng",
    "redb",
    "Polars",
    "SWC",
    "RustPython",
    "Actix Web",
)

LABELS = {
    "unialloc": "UniAlloc (reference)",
    "type_isolation": "UniAlloc + Type Isolation",
    "ptmalloc": "ptmalloc",
    "jemalloc": "jemalloc",
    "mimalloc": "mimalloc",
    "mimalloc_no_thp": "mimalloc (THP off)",
    "google_tcmalloc": "Google TCMalloc (modern)",
    "snmalloc": "snmalloc",
    "scudo": "Scudo",
}
COLORS = {
    "unialloc": "#111111",
    "type_isolation": "#0072B2",
    "ptmalloc": "#6E6E6E",
    "jemalloc": "#009E73",
    "mimalloc": "#E69F00",
    "mimalloc_no_thp": "#D55E00",
    "google_tcmalloc": "#CC79A7",
    "snmalloc": "#4263C7",
    "scudo": "#6A3D9A",
}

# Illustrative planning data. These values are intentionally separate from the
# frozen measured Macro campaign and must retain the simulated evidence label.
SIMULATED_MACRO: Mapping[str, Mapping[str, Sequence[float]]] = {
    "unialloc": {
        "performance": (1.0,) * len(TARGETS),
        "rss": (1.0,) * len(TARGETS),
    },
    "type_isolation": {
        "performance": (1.20, 1.24, 1.26, 1.28, 1.35, 1.30, 1.27),
        "rss": (1.05, 1.08, 1.10, 1.12, 1.15, 1.11, 1.09),
    },
    "ptmalloc": {
        "performance": (1.01, 1.04, 1.02, 1.00, 1.08, 1.03, 1.03),
        "rss": (1.08, 1.12, 1.10, 1.09, 1.18, 1.13, 1.12),
    },
    "jemalloc": {
        "performance": (0.98, 0.95, 0.94, 0.92, 0.97, 0.96, 0.94),
        "rss": (1.02, 1.06, 1.04, 1.03, 1.10, 1.07, 1.05),
    },
    "mimalloc": {
        "performance": (0.88, 0.83, 0.78, 0.76, 0.85, 0.80, 0.79),
        "rss": (1.22, 1.35, 1.42, 1.48, 1.31, 1.40, 1.39),
    },
    "mimalloc_no_thp": {
        "performance": (0.94, 0.91, 0.87, 0.85, 0.93, 0.89, 0.90),
        "rss": (1.04, 1.10, 1.08, 1.12, 1.09, 1.11, 1.10),
    },
    "google_tcmalloc": {
        "performance": (1.02, 0.99, 0.96, 0.95, 1.01, 0.98, 1.00),
        "rss": (1.10, 1.16, 1.13, 1.14, 1.21, 1.17, 1.15),
    },
    "snmalloc": {
        "performance": (0.97, 0.95, 0.92, 0.91, 0.98, 0.93, 0.94),
        "rss": (1.04, 1.08, 1.06, 1.07, 1.13, 1.09, 1.08),
    },
    "scudo": {
        "performance": (1.12, 1.18, 1.15, 1.16, 1.25, 1.20, 1.21),
        "rss": (1.18, 1.24, 1.21, 1.23, 1.32, 1.27, 1.26),
    },
}


class EvidenceError(RuntimeError):
    """Raised when an overview input violates its registered contract."""


def require(condition: bool, message: str) -> None:
    if not condition:
        raise EvidenceError(message)


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def load_json(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    require(isinstance(value, dict), f"JSON object required: {path}")
    return value


def geometric_mean(values: Sequence[float]) -> float:
    require(bool(values) and all(value > 0 for value in values), "positive ratios required")
    return math.exp(sum(math.log(value) for value in values) / len(values))


def load_micro_campaign(root: Path, expected_variants: Sequence[str]) -> dict[str, Any]:
    paths = {
        name: root / name
        for name in (
            "campaign-config.json",
            "campaign-selection.json",
            "campaign-state.json",
            "provenance.json",
            "records.jsonl",
            "summary.json",
        )
    }
    require(all(path.is_file() for path in paths.values()), f"campaign incomplete: {root}")
    config = load_json(paths["campaign-config.json"])
    state = load_json(paths["campaign-state.json"])
    provenance = load_json(paths["provenance.json"])
    summary = load_json(paths["summary.json"])
    variants = tuple(config.get("variant_ids", ()))
    require(variants == tuple(expected_variants), f"variant contract mismatch: {root}")
    require("tcmalloc" not in variants, "legacy TCMalloc is excluded")
    require(
        config.get("repo_head") == IMPLEMENTATION_REVISION
        and provenance.get("repo_head") == IMPLEMENTATION_REVISION,
        f"implementation revision mismatch: {root}",
    )
    require(state.get("records_sha256") == sha256(paths["records.jsonl"]), "records digest mismatch")
    require(state.get("summary_sha256") == sha256(paths["summary.json"]), "summary digest mismatch")
    selected = summary.get("robustness_selection", {}).get("selected_benchmarks")
    require(isinstance(selected, list) and selected, "robust selection missing")

    measurements: dict[tuple[str, str, str], list[float]] = defaultdict(list)
    with paths["records.jsonl"].open(encoding="utf-8") as handle:
        for line in handle:
            row = json.loads(line)
            if row.get("phase") != "measured" or row.get("status") != "valid":
                continue
            benchmark = row.get("benchmark")
            variant = row.get("allocator")
            if benchmark not in selected or variant not in variants:
                continue
            measurements[(benchmark, variant, "performance")].append(float(row["ns_per_iter"]))
            measurements[(benchmark, variant, "rss")].append(float(row["peak_rss_kib"]))

    medians: dict[tuple[str, str, str], float] = {}
    for benchmark in selected:
        for variant in variants:
            for metric in ("performance", "rss"):
                values = measurements[(benchmark, variant, metric)]
                require(len(values) == 3, f"three measured rounds required: {benchmark}/{variant}/{metric}")
                medians[(benchmark, variant, metric)] = statistics.median(values)
    return {
        "root": root,
        "paths": paths,
        "variants": variants,
        "selected": tuple(selected),
        "medians": medians,
        "identity": {
            "root": str(root),
            "variant_ids": list(variants),
            "robust_case_count": len(selected),
            "artifacts": {
                name: {"path": str(path), "sha256": sha256(path), "bytes": path.stat().st_size}
                for name, path in paths.items()
            },
        },
    }


def aggregate_micro(
    campaign: Mapping[str, Any], variant: str, common: Sequence[str], metric: str
) -> dict[str, Any]:
    medians = campaign["medians"]
    families: dict[str, list[float]] = defaultdict(list)
    for benchmark in common:
        ratio = medians[(benchmark, variant, metric)] / medians[(benchmark, "unialloc", metric)]
        families[benchmark.split("::", 1)[0]].append(ratio)
    family_ratios = {
        family: math.exp(statistics.median([math.log(value) for value in values]))
        for family, values in sorted(families.items())
    }
    return {
        "aggregate_ratio": geometric_mean(list(family_ratios.values())),
        "detail_kind": "Rust benchmark family",
        "details": [
            {"id": family, "ratio": ratio} for family, ratio in family_ratios.items()
        ],
    }


def load_type_isolation_micro() -> tuple[dict[str, Any], dict[str, Any]]:
    presentation = load_json(PRESENTATION_DATA)
    require(
        presentation.get("suite", {}).get("implementation_revision") == IMPLEMENTATION_REVISION,
        "Type Isolation presentation data revision mismatch",
    )
    normalized = presentation.get("normalized_rows")
    require(isinstance(normalized, list), "normalized Type Isolation rows missing")
    metrics: dict[str, Any] = {}
    for metric in ("performance", "rss"):
        selected = [
            row
            for row in normalized
            if row.get("population") == "micro"
            and row.get("comparison_family") == "end_to_end"
            and row.get("variant_id") == "typeiso_perf"
            and row.get("reference_variant_id") == "unialloc"
            and row.get("metric") == metric
        ]
        aggregate_rows = [
            row for row in selected if row.get("aggregation_level") == "population_geomean"
        ]
        family_rows = [
            row for row in selected if row.get("aggregation_level") == "family_median"
        ]
        require(len(aggregate_rows) == 1, f"one Type Isolation aggregate required: {metric}")
        require(len(family_rows) == 8, f"eight Type Isolation families required: {metric}")
        require(
            len({str(row["family"]) for row in family_rows}) == len(family_rows),
            f"unique Type Isolation families required: {metric}",
        )
        metrics[metric] = {
            "aggregate_ratio": float(aggregate_rows[0]["ratio"]),
            "detail_kind": "Rust benchmark family",
            "details": [
                {"id": str(row["family"]), "ratio": float(row["ratio"])}
                for row in sorted(family_rows, key=lambda row: str(row["family"]))
            ],
        }
    source_paths = {
        str(row.get("source_artifact"))
        for row in normalized
        if row.get("population") == "micro"
        and row.get("comparison_family") == "end_to_end"
        and row.get("variant_id") == "typeiso_perf"
    }
    require(len(source_paths) == 1, "one Type Isolation Micro source artifact required")
    source_path = Path(source_paths.pop())
    require(source_path.is_file(), "Type Isolation Micro source artifact missing")
    identity = {
        "presentation_data": {
            "path": str(PRESENTATION_DATA),
            "sha256": sha256(PRESENTATION_DATA),
            "bytes": PRESENTATION_DATA.stat().st_size,
        },
        "source_artifact": {
            "path": str(source_path),
            "sha256": sha256(source_path),
            "bytes": source_path.stat().st_size,
        },
        "comparison_family": "end_to_end",
        "reference_variant_id": "unialloc",
        "subject_variant_id": "typeiso_perf",
    }
    return {
        "variant_id": "type_isolation",
        "label": LABELS["type_isolation"],
        "metrics": metrics,
    }, identity


def build_micro_data() -> tuple[dict[str, Any], dict[str, Any]]:
    main = load_micro_campaign(MAIN_MICRO_ROOT, MAIN_VARIANTS)
    extra = load_micro_campaign(EXTRA_MICRO_ROOT, EXTRA_VARIANTS)
    common = sorted(set(main["selected"]) & set(extra["selected"]))
    require(len(common) == 236, f"expected 236 common robust cases, found {len(common)}")
    campaign_for = {
        "ptmalloc": extra,
        "jemalloc": main,
        "mimalloc": main,
        "mimalloc_no_thp": main,
        "google_tcmalloc": main,
        "snmalloc": extra,
        "scudo": extra,
    }
    external_rows = []
    for variant in EXTERNAL_ORDER:
        row = {"variant_id": variant, "label": LABELS[variant], "metrics": {}}
        for metric in ("performance", "rss"):
            row["metrics"][metric] = aggregate_micro(campaign_for[variant], variant, common, metric)
        external_rows.append(row)

    type_isolation, type_isolation_identity = load_type_isolation_micro()
    families = [detail["id"] for detail in external_rows[0]["metrics"]["performance"]["details"]]
    require(len(families) == 8, "eight Micro benchmark families required")
    reference = {
        "variant_id": "unialloc",
        "label": LABELS["unialloc"],
        "metrics": {
            metric: {
                "aggregate_ratio": 1.0,
                "detail_kind": "Rust benchmark family",
                "details": [{"id": family, "ratio": 1.0} for family in families],
            }
            for metric in ("performance", "rss")
        },
    }
    rows = [reference, type_isolation, *external_rows]

    anchor_families: dict[str, list[float]] = defaultdict(list)
    for benchmark in common:
        ratio = (
            extra["medians"][(benchmark, "unialloc", "performance")]
            / main["medians"][(benchmark, "unialloc", "performance")]
        )
        anchor_families[benchmark.split("::", 1)[0]].append(ratio)
    anchor_family_ratios = [
        math.exp(statistics.median([math.log(value) for value in values]))
        for values in anchor_families.values()
    ]
    identity = {
        "common_robust_case_count": len(common),
        "common_selection_sha256": hashlib.sha256(("\n".join(common) + "\n").encode()).hexdigest(),
        "main_campaign": main["identity"],
        "supplemental_campaign": extra["identity"],
        "type_isolation_campaign": type_isolation_identity,
        "unialloc_cross_session_performance_anchor_ratio": geometric_mean(anchor_family_ratios),
        "unialloc_cross_session_rss_anchor_ratio": 1.0,
    }
    return {
        "evidence_class": "derived_existing_measurements",
        "claim_grade": False,
        "measurement_boundary": {
            "performance": "existing measured campaigns: Type Isolation end-to-end plus external allocators on 236 common robust cases",
            "rss": "diagnostic process peak RSS; startup and adaptive libtest work included",
        },
        "rows": rows,
    }, identity


def build_macro_data() -> dict[str, Any]:
    rows = []
    for variant in MACRO_ORDER:
        metrics = {}
        for metric in ("performance", "rss"):
            values = list(SIMULATED_MACRO[variant][metric])
            require(len(values) == len(TARGETS), f"target count mismatch: {variant}/{metric}")
            metrics[metric] = {
                "aggregate_ratio": geometric_mean(values),
                "detail_kind": "simulated real-world target",
                "details": [
                    {"id": target, "ratio": ratio}
                    for target, ratio in zip(TARGETS, values, strict=True)
                ],
            }
        rows.append({"variant_id": variant, "label": LABELS[variant], "metrics": metrics})
    return {
        "evidence_class": "illustrative_static_model",
        "claim_grade": False,
        "eligible_for_measured_comparison": False,
        "required_badge": "ILLUSTRATIVE MODEL - NOT MEASURED",
        "targets": list(TARGETS),
        "type_isolation_projection": {
            "type_coverage": {"range": [0.80, 0.90], "central_estimate": 0.87},
            "effective_cache_coverage": {"range": [0.72, 0.80], "central_estimate": 0.76},
            "execution_cost_overhead": {"range": [0.20, 0.35], "presentation_range": [0.25, 0.30]},
            "peak_rss_growth": {"range": [0.05, 0.15], "central_estimate": 0.10},
            "fixed_tls_kib_per_thread": 1.28,
            "extreme_retained_cache_kib_per_thread": 2048,
        },
        "rows": rows,
    }


def ratio_formatter(value: float, _position: int) -> str:
    ratio = 2.0**value
    return f"{ratio:.2f}x" if ratio < 10 else f"{ratio:.1f}x"


def draw_panel(
    ax: Any,
    data: Mapping[str, Any],
    metric: str,
    *,
    show_y_labels: bool,
) -> None:
    rows = data["rows"]
    log_values = [math.log2(row["metrics"][metric]["aggregate_ratio"]) for row in rows]
    log_values.extend(
        math.log2(detail["ratio"])
        for row in rows
        for detail in row["metrics"][metric]["details"]
    )
    bound = max(0.075, max(abs(value) for value in log_values) * 1.22)
    ax.axvline(0, color="#202124", linewidth=2.0, zorder=1)
    ax.grid(axis="x", color="#D9DDE3", linewidth=1.0, zorder=0)
    simulated = data["evidence_class"] == "illustrative_static_model"
    for position, row in enumerate(rows):
        variant = row["variant_id"]
        color = COLORS[variant]
        metric_row = row["metrics"][metric]
        aggregate = math.log2(metric_row["aggregate_ratio"])
        hollow = simulated
        ax.barh(
            position,
            aggregate,
            left=0,
            height=0.48,
            color="white" if hollow else color,
            alpha=0.82,
            edgecolor=color,
            linewidth=1.8 if hollow else 0.8,
            hatch="//" if simulated else None,
            zorder=2,
        )
        details = metric_row["details"]
        for index, detail in enumerate(details):
            jitter = (index - (len(details) - 1) / 2) * 0.052
            ax.scatter(
                math.log2(detail["ratio"]),
                position + jitter,
                s=62,
                marker="o",
                facecolor="white" if hollow else color,
                edgecolor=color,
                linewidth=1.6,
                zorder=3,
            )
        ax.scatter(
            aggregate,
            position,
            s=155,
            marker="D",
            facecolor="white" if hollow else "#111111",
            edgecolor=color if hollow else "white",
            linewidth=1.9,
            zorder=4,
        )
        label_offset = 0.018 if bound < 0.5 else 0.035
        ax.text(
            aggregate + (label_offset if aggregate >= 0 else -label_offset),
            position - 0.31,
            f"{metric_row['aggregate_ratio']:.3f}x",
            ha="left" if aggregate >= 0 else "right",
            va="center",
            fontsize=16,
            color="#202124",
            clip_on=False,
        )
    ax.set_yticks(
        range(len(rows)),
        [row["label"] for row in rows] if show_y_labels else [""] * len(rows),
        fontsize=19,
    )
    ax.invert_yaxis()
    ax.set_xlim(-bound, bound)
    ax.xaxis.set_major_formatter(FuncFormatter(ratio_formatter))
    ax.tick_params(axis="x", labelsize=16)
    ax.tick_params(axis="y", length=0, pad=12)
    for spine in ("top", "right", "left"):
        ax.spines[spine].set_visible(False)
    ax.spines["bottom"].set_color("#6B7078")

    if metric == "performance":
        metric_label = "Performance: execution cost / UniAlloc"
        boundary = (
            "illustrative real-world model; not measured"
            if simulated
            else "original measured Micro data"
        )
    else:
        metric_label = "Memory: peak RSS / UniAlloc"
        boundary = (
            "illustrative real-world model; not measured"
            if simulated
            else "diagnostic measured Micro data"
        )
    ax.set_xlabel(f"{metric_label}\n({boundary})", fontsize=20, labelpad=10)


def draw_overview(data: Mapping[str, Any], stem: str, output: Path) -> list[Path]:
    simulated = data["evidence_class"] == "illustrative_static_model"
    figure, axes = plt.subplots(1, 2, figsize=FIGURE_SIZE)
    draw_panel(axes[0], data, "performance", show_y_labels=True)
    draw_panel(axes[1], data, "rss", show_y_labels=False)

    legend = [
        Line2D(
            [0],
            [0],
            marker="o",
            color="none",
            markerfacecolor="white" if simulated else "#4C78A8",
            markeredgecolor="#4C78A8",
            markersize=9,
            label="Illustrative target" if simulated else "Measured benchmark family",
        ),
        Line2D(
            [0],
            [0],
            marker="D",
            color="none",
            markerfacecolor="white" if simulated else "#111111",
            markeredgecolor="#4C78A8" if simulated else "#111111",
            markersize=9,
            label="Illustrative geometric mean" if simulated else "Measured geometric mean",
        ),
    ]
    figure.legend(
        handles=legend,
        loc="lower center",
        ncol=2,
        frameon=False,
        fontsize=18,
        bbox_to_anchor=(0.5, 0.005),
    )
    if simulated:
        figure.text(
            0.5,
            0.112,
            data["required_badge"],
            ha="center",
            va="center",
            fontsize=18,
            fontweight="bold",
            color="#8B1E1E",
        )
    figure.subplots_adjust(left=0.175, right=0.985, top=0.975, bottom=0.235, wspace=0.12)

    outputs = []
    for extension in ("svg", "pdf", "png"):
        path = output / f"{stem}.{extension}"
        if extension == "png":
            figure.savefig(path, dpi=PNG_DPI, facecolor="white", metadata={"Software": "UniAlloc allocator overview"})
        elif extension == "pdf":
            figure.savefig(
                path,
                facecolor="white",
                metadata={
                    "Creator": "UniAlloc allocator overview",
                    "Producer": "UniAlloc allocator overview",
                    "CreationDate": FIXED_DATETIME,
                    "ModDate": FIXED_DATETIME,
                },
            )
        else:
            figure.savefig(path, facecolor="white", metadata={"Creator": "UniAlloc allocator overview", "Date": FIXED_DATE})
            path.write_text(
                "\n".join(line.rstrip() for line in path.read_text(encoding="utf-8").splitlines()) + "\n",
                encoding="utf-8",
            )
        outputs.append(path)
    plt.close(figure)
    return outputs


def write_json(path: Path, value: Any) -> None:
    path.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def main() -> int:
    micro, identity = build_micro_data()
    macro = build_macro_data()
    payload = {
        "schema_version": 1,
        "implementation_revision": IMPLEMENTATION_REVISION,
        "legacy_tcmalloc_included": False,
        "micro": micro,
        "micro_input_identity": identity,
        "simulated_macro": macro,
    }
    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    stage = Path(tempfile.mkdtemp(prefix=f".{OUTPUT.name}.stage-", dir=OUTPUT.parent))
    try:
        data_path = stage / "allocator-overview-data.json"
        write_json(data_path, payload)
        figure_paths = []
        for data, stem in (
            (micro, "micro-performance-rss-measured"),
            (macro, "macro-performance-rss-simulated"),
        ):
            figure_paths.extend(draw_overview(data, stem, stage))
        manifest_path = stage / "artifact-manifest.json"
        write_json(
            manifest_path,
            {
                "schema_version": 1,
                "legacy_tcmalloc_included": False,
                "generator": {
                    "path": str(Path(__file__).resolve().relative_to(ROOT)),
                    "sha256": sha256(Path(__file__).resolve()),
                    "bytes": Path(__file__).resolve().stat().st_size,
                },
                "figure_groups": [
                    "micro-performance-rss-measured",
                    "macro-performance-rss-simulated",
                ],
                "input_evidence": identity,
                "artifacts": [
                    {"path": path.name, "sha256": sha256(path), "bytes": path.stat().st_size}
                    for path in [data_path, *figure_paths]
                ],
            },
        )
        backup = OUTPUT.with_name(f".{OUTPUT.name}.backup")
        if backup.exists():
            shutil.rmtree(backup)
        if OUTPUT.exists():
            OUTPUT.rename(backup)
        stage.rename(OUTPUT)
        if backup.exists():
            shutil.rmtree(backup)
    except BaseException:
        if stage.exists():
            shutil.rmtree(stage)
        raise
    print(OUTPUT)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
