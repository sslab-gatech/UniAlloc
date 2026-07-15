#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = ["matplotlib==3.11.0"]
# ///
"""Render the complete current-version Type Isolation primary suite."""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import math
import os
import shutil
import statistics
import sys
import tempfile
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping, Sequence

os.environ.setdefault("MPLBACKEND", "Agg")
os.environ.setdefault("SOURCE_DATE_EPOCH", "1783987200")

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib.axes import Axes
from matplotlib.figure import Figure
from matplotlib.lines import Line2D
from matplotlib.ticker import FuncFormatter


REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_SUITE = (
    REPOSITORY_ROOT / "evaluation" / "config" / "type_isolation_primary_suite.json"
)
DEFAULT_RESULTS = (
    REPOSITORY_ROOT / "benchmark-results" / "type-isolation-primary-v1.json"
)
DEFAULT_OUTPUT_DIR = (
    REPOSITORY_ROOT / "docs" / "figures" / "type-isolation-primary-suite"
)

STEM = "type-isolation-primary-suite"
VARIANTS = ("unialloc", "typed_plain", "typeiso_perf")
PLOTTED_VARIANTS = ("typed_plain", "typeiso_perf")
DATA_ELIGIBILITY_GATES = (
    "correctness",
    "build_success",
    "allocator_activation",
    "actual_mir_provenance",
    "stats_disabled",
    "source_audit_retained",
)
ATTRIBUTION_GATE = "compiler_route_equivalent"
COMPLETE_STATUSES = ("complete", "complete_with_attribution_limits")

FIGURE_SIZE_INCHES = (16, 9)
PNG_DPI = 150
FIXED_TIMESTAMP = datetime(2026, 7, 14, tzinfo=timezone.utc)
DISPLAY_LOG2_CAP = 2.0
FIXED_WORK = "fixed_work"
ADAPTIVE_ITERATIONS = "workload_native_adaptive_iterations"
RSS_WORK_MODELS = {FIXED_WORK, ADAPTIVE_ITERATIONS}
RSS_CORE = "core"
RSS_DIAGNOSTIC = "diagnostic_only"
ADAPTIVE_RSS_LEGEND = "‡ adaptive iterations; RSS is process-volume, not equal-work"

INK = "#17181A"
MUTED = "#62646B"
AXIS = "#47484F"
GRID = "#DDDEE1"
PALE = "#F0F1F2"
WHITE = "#FFFFFF"
UNIALLOC = "#2E4780"
TYPE_CONTROL = "#A3BEFA"
TYPE_ISOLATION = "#5477C4"
COLORS = {
    "typed_plain": TYPE_CONTROL,
    "typeiso_perf": TYPE_ISOLATION,
}
LABELS = {
    "typed_plain": "Typed path, policy off",
    "typeiso_perf": "Type Isolation",
}
MARKERS = {
    "typed_plain": "s",
    "typeiso_perf": "D",
}


class FigureDataError(RuntimeError):
    """Raised when final-figure evidence is incomplete or inconsistent."""


@dataclass(frozen=True)
class Measurement:
    round: int
    variant: str
    performance: float
    peak_rss_mib: float


@dataclass(frozen=True)
class HarnessSummary:
    target_id: str
    target_label: str
    harness_id: str
    harness_label: str
    overview_rank: int
    route_equivalent: bool
    rss_work_model: str
    rss_comparison_eligibility: str
    performance_unit: str
    performance_source: str
    performance_ratios: Mapping[str, tuple[float, ...]]
    rss_ratios: Mapping[str, tuple[float, ...]]


def sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def load_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as error:
        raise FigureDataError(f"required input is missing: {path}") from error
    except json.JSONDecodeError as error:
        raise FigureDataError(f"invalid JSON in {path}: {error}") from error
    if not isinstance(value, dict):
        raise FigureDataError(f"JSON root must be an object: {path}")
    return value


def require_list(value: Any, context: str) -> list[Any]:
    if not isinstance(value, list):
        raise FigureDataError(f"{context} must be a list")
    return value


def require_mapping(value: Any, context: str) -> Mapping[str, Any]:
    if not isinstance(value, dict):
        raise FigureDataError(f"{context} must be an object")
    return value


def positive_number(value: Any, context: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise FigureDataError(f"{context} must be numeric")
    result = float(value)
    if not math.isfinite(result) or result <= 0.0:
        raise FigureDataError(f"{context} must be finite and positive")
    return result


def exact_index(
    rows: Sequence[Any], key: str, expected: Sequence[str], context: str
) -> dict[str, Mapping[str, Any]]:
    indexed: dict[str, Mapping[str, Any]] = {}
    for position, raw in enumerate(rows):
        row = require_mapping(raw, f"{context}[{position}]")
        identifier = row.get(key)
        if not isinstance(identifier, str) or not identifier:
            raise FigureDataError(f"{context}[{position}].{key} must be a string")
        if identifier in indexed:
            raise FigureDataError(f"duplicate {context} entry: {identifier}")
        indexed[identifier] = row
    missing = [identifier for identifier in expected if identifier not in indexed]
    extra = [identifier for identifier in indexed if identifier not in expected]
    if missing or extra:
        pieces: list[str] = []
        if missing:
            pieces.append("missing " + ", ".join(missing))
        if extra:
            pieces.append("unexpected " + ", ".join(extra))
        raise FigureDataError(f"{context} mismatch: {'; '.join(pieces)}")
    return indexed


def parse_measurements(
    target_id: str,
    harness_id: str,
    raw_rows: Any,
) -> tuple[Measurement, ...]:
    rows = require_list(raw_rows, f"{target_id}.{harness_id}.measurements")
    parsed: list[Measurement] = []
    seen: set[tuple[int, str]] = set()
    for position, raw in enumerate(rows):
        row = require_mapping(raw, f"{target_id}.{harness_id}.measurements[{position}]")
        round_number = row.get("round")
        variant = row.get("variant")
        if isinstance(round_number, bool) or not isinstance(round_number, int):
            raise FigureDataError(
                f"{target_id}.{harness_id} measurement round must be an integer"
            )
        if round_number <= 0:
            raise FigureDataError(
                f"{target_id}.{harness_id} measurement round must be positive"
            )
        if variant not in VARIANTS:
            raise FigureDataError(
                f"{target_id}.{harness_id} round {round_number} has invalid variant {variant!r}"
            )
        identity = (round_number, variant)
        if identity in seen:
            raise FigureDataError(
                f"{target_id}.{harness_id} duplicates round {round_number} variant {variant}"
            )
        seen.add(identity)
        parsed.append(
            Measurement(
                round=round_number,
                variant=variant,
                performance=positive_number(
                    row.get("performance"),
                    f"{target_id}.{harness_id} round {round_number} {variant} performance",
                ),
                peak_rss_mib=positive_number(
                    row.get("peak_rss_mib"),
                    f"{target_id}.{harness_id} round {round_number} {variant} peak_rss_mib",
                ),
            )
        )

    rounds = {row.round for row in parsed}
    expected_rounds = set(range(1, 6))
    if rounds != expected_rounds or len(parsed) != len(expected_rounds) * len(VARIANTS):
        raise FigureDataError(
            f"{target_id}.{harness_id} must contain exact measured rounds 1 through 5"
        )
    for round_number in sorted(expected_rounds):
        present = {row.variant for row in parsed if row.round == round_number}
        missing = [variant for variant in VARIANTS if variant not in present]
        if missing:
            raise FigureDataError(
                f"{target_id}.{harness_id} round {round_number} is missing {', '.join(missing)}"
            )
    return tuple(parsed)


def validate_warmup_attestations(
    raw_attestations: Any,
    target_id: str,
    harness_id: str,
) -> None:
    context = f"{target_id}.{harness_id}.warmup_attestations"
    if not isinstance(raw_attestations, dict) or set(raw_attestations) != set(VARIANTS):
        raise FigureDataError(f"{context} must cover every variant")
    for variant in VARIANTS:
        attestation = require_mapping(raw_attestations[variant], f"{context}.{variant}")
        digest = attestation.get("record_sha256")
        if (
            not isinstance(digest, str)
            or len(digest) != 64
            or any(character not in "0123456789abcdef" for character in digest)
        ):
            raise FigureDataError(f"{context}.{variant} digest is invalid")
        if (
            attestation.get("target_id") != target_id
            or attestation.get("harness_id") != harness_id
            or attestation.get("variant") != variant
            or attestation.get("phase") != "warmup"
            or attestation.get("round") != 0
        ):
            raise FigureDataError(f"{context}.{variant} identity mismatch")
        positive_number(
            attestation.get("performance"), f"{context}.{variant}.performance"
        )
        positive_number(
            attestation.get("peak_rss_mib"), f"{context}.{variant}.peak_rss_mib"
        )
        raw_path = attestation.get("raw_record_path")
        if raw_path is not None and (
            not isinstance(raw_path, str) or Path(raw_path).is_absolute()
        ):
            raise FigureDataError(f"{context}.{variant} raw path must be relative")


def paired_ratios(
    measurements: Sequence[Measurement],
    variant: str,
    field: str,
    metric_direction: str,
) -> tuple[float, ...]:
    rows = {(row.round, row.variant): row for row in measurements}
    rounds = sorted({row.round for row in measurements})
    ratios: list[float] = []
    for round_number in rounds:
        reference = rows[(round_number, "unialloc")]
        subject = rows[(round_number, variant)]
        if field == "performance":
            reference_value = reference.performance
            subject_value = subject.performance
            if metric_direction == "lower_is_better":
                ratio = subject_value / reference_value
            elif metric_direction == "higher_is_better":
                ratio = reference_value / subject_value
            else:
                raise FigureDataError(
                    f"unsupported metric direction: {metric_direction}"
                )
        elif field == "peak_rss_mib":
            ratio = subject.peak_rss_mib / reference.peak_rss_mib
        else:
            raise AssertionError(field)
        ratios.append(ratio)
    return tuple(ratios)


def validate_and_summarize(
    suite_path: Path,
    results_path: Path,
) -> tuple[dict[str, Any], dict[str, Any], tuple[HarnessSummary, ...]]:
    suite = load_json(suite_path)
    results = load_json(results_path)
    if suite.get("suite_id") != results.get("suite_id"):
        raise FigureDataError(
            f"suite_id mismatch: {suite.get('suite_id')!r} vs {results.get('suite_id')!r}"
        )
    if results.get("status") not in COMPLETE_STATUSES:
        raise FigureDataError(
            "results status must be complete or complete_with_attribution_limits"
        )
    expected_suite_hash = sha256_file(suite_path)
    if results.get("suite_manifest_sha256") != expected_suite_hash:
        raise FigureDataError("suite manifest hash mismatch")
    if tuple(suite.get("variant_order", ())) != VARIANTS:
        raise FigureDataError("suite variant order does not match the figure contract")
    implementation = require_mapping(
        suite.get("implementation"), "suite.implementation"
    )
    implementation_revision = implementation.get("git_revision")
    implementation_sha256 = implementation.get("canonical_sha256")
    if (
        not isinstance(implementation_revision, str)
        or len(implementation_revision) != 40
    ):
        raise FigureDataError("suite implementation git revision is invalid")
    if not isinstance(implementation_sha256, str) or len(implementation_sha256) != 64:
        raise FigureDataError("suite implementation SHA-256 is invalid")
    if results.get("implementation_revision") != implementation_revision:
        raise FigureDataError(
            "results implementation_revision mismatch: expected "
            f"{implementation_revision}, got {results.get('implementation_revision')}"
        )
    if results.get("implementation_sha256") != implementation_sha256:
        raise FigureDataError(
            "results implementation_sha256 mismatch: expected "
            f"{implementation_sha256}, got {results.get('implementation_sha256')}"
        )
    if results.get("analysis_amendments") != suite.get("analysis_amendments"):
        raise FigureDataError("analysis amendment record mismatch")
    amendments = require_list(suite.get("analysis_amendments"), "analysis_amendments")
    if not amendments:
        raise FigureDataError("analysis amendment record is missing")
    first_amendment = require_mapping(amendments[0], "analysis_amendments[0]")
    if results.get("original_campaign_manifest_sha256") != first_amendment.get(
        "original_campaign_manifest_sha256"
    ):
        raise FigureDataError("original campaign manifest digest mismatch")
    route_gate = require_mapping(
        suite.get("compiler_route_equivalence_gate"),
        "suite.compiler_route_equivalence_gate",
    )
    minimum_route_ratio = positive_number(
        route_gate.get("minimum_ratio"),
        "suite.compiler_route_equivalence_gate.minimum_ratio",
    )
    maximum_route_ratio = positive_number(
        route_gate.get("maximum_ratio"),
        "suite.compiler_route_equivalence_gate.maximum_ratio",
    )
    if minimum_route_ratio > maximum_route_ratio:
        raise FigureDataError("compiler route equivalence interval is invalid")

    suite_targets = require_list(suite.get("targets"), "suite.targets")
    target_ids = [
        require_mapping(target, "suite target").get("id") for target in suite_targets
    ]
    if not all(isinstance(identifier, str) for identifier in target_ids):
        raise FigureDataError("every suite target needs a string id")
    result_targets = exact_index(
        require_list(results.get("targets"), "results.targets"),
        "id",
        target_ids,
        "results.targets",
    )

    summaries: list[HarnessSummary] = []
    route_pass_count = 0
    route_fail_count = 0
    for raw_suite_target in suite_targets:
        suite_target = require_mapping(raw_suite_target, "suite target")
        target_id = str(suite_target["id"])
        target_label = str(suite_target["label"])
        rss_work_model = suite_target.get("rss_work_model")
        if rss_work_model not in RSS_WORK_MODELS:
            raise FigureDataError(f"{target_id}.rss_work_model is invalid")
        rss_eligibility = RSS_CORE if rss_work_model == FIXED_WORK else RSS_DIAGNOSTIC
        result_target = result_targets[target_id]
        if result_target.get("rss_work_model") != rss_work_model:
            raise FigureDataError(f"{target_id} rss_work_model mismatch")
        if result_target.get("rss_comparison_eligibility") != rss_eligibility:
            raise FigureDataError(f"{target_id} rss_comparison_eligibility mismatch")
        expected_commit = require_mapping(
            suite_target.get("source"), f"suite target {target_id}.source"
        ).get("commit")
        if result_target.get("source_commit") != expected_commit:
            raise FigureDataError(
                f"{target_id} source commit mismatch: expected {expected_commit}, got {result_target.get('source_commit')}"
            )
        if result_target.get("implementation_revision") != implementation_revision:
            raise FigureDataError(
                f"{target_id} implementation_revision mismatch: expected "
                f"{implementation_revision}, got "
                f"{result_target.get('implementation_revision')}"
            )
        if result_target.get("implementation_sha256") != implementation_sha256:
            raise FigureDataError(
                f"{target_id} implementation_sha256 mismatch: expected "
                f"{implementation_sha256}, got "
                f"{result_target.get('implementation_sha256')}"
            )

        suite_harnesses = require_list(
            suite_target.get("harnesses"), f"suite target {target_id}.harnesses"
        )
        harness_ids = [
            str(require_mapping(harness, "suite harness")["id"])
            for harness in suite_harnesses
        ]
        result_harnesses = exact_index(
            require_list(
                result_target.get("harnesses"),
                f"results target {target_id}.harnesses",
            ),
            "id",
            harness_ids,
            f"results target {target_id}.harnesses",
        )

        selected_count = 0
        for raw_suite_harness in suite_harnesses:
            suite_harness = require_mapping(raw_suite_harness, "suite harness")
            harness_id = str(suite_harness["id"])
            result_harness = result_harnesses[harness_id]
            if result_harness.get("rss_work_model") != rss_work_model:
                raise FigureDataError(
                    f"{target_id}.{harness_id} rss_work_model mismatch"
                )
            if result_harness.get("rss_comparison_eligibility") != rss_eligibility:
                raise FigureDataError(
                    f"{target_id}.{harness_id} rss_comparison_eligibility mismatch"
                )
            metric_direction = suite_harness.get("metric_direction")
            if result_harness.get("metric_direction") != metric_direction:
                raise FigureDataError(
                    f"{target_id}.{harness_id} metric direction mismatch"
                )
            performance_unit = suite_harness.get("performance_unit")
            performance_source = suite_harness.get("performance_source")
            if result_harness.get("performance_unit") != performance_unit:
                raise FigureDataError(
                    f"{target_id}.{harness_id} performance_unit mismatch"
                )
            if result_harness.get("performance_source") != performance_source:
                raise FigureDataError(
                    f"{target_id}.{harness_id} performance_source mismatch"
                )
            gates = require_mapping(
                result_harness.get("gates"), f"{target_id}.{harness_id}.gates"
            )
            failed_gates = [
                gate for gate in DATA_ELIGIBILITY_GATES if gates.get(gate) is not True
            ]
            if failed_gates:
                raise FigureDataError(
                    f"{target_id}.{harness_id} failed data-eligibility gates: "
                    f"{', '.join(failed_gates)}"
                )
            validate_warmup_attestations(
                result_harness.get("warmup_attestations"), target_id, harness_id
            )
            stored_route_classification = gates.get(ATTRIBUTION_GATE)
            if type(stored_route_classification) is not bool:
                raise FigureDataError(
                    f"{target_id}.{harness_id}.{ATTRIBUTION_GATE} must be a boolean"
                )
            measurements = parse_measurements(
                target_id,
                harness_id,
                result_harness.get("measurements"),
            )
            route_ratio = float(
                statistics.median(
                    paired_ratios(
                        measurements,
                        "typed_plain",
                        "performance",
                        str(metric_direction),
                    )
                )
            )
            route_equivalent = minimum_route_ratio <= route_ratio <= maximum_route_ratio
            if stored_route_classification is not route_equivalent:
                raise FigureDataError(
                    f"{target_id}.{harness_id} stored compiler route classification "
                    f"{stored_route_classification} disagrees with recomputed "
                    f"classification {route_equivalent} at ratio {route_ratio:.6g}"
                )
            stored_attribution = require_mapping(
                result_harness.get("compiler_route_attribution"),
                f"{target_id}.{harness_id}.compiler_route_attribution",
            )
            expected_classification = (
                "within_predeclared_interval"
                if route_equivalent
                else "outside_predeclared_interval"
            )
            if stored_attribution.get("classification") != expected_classification:
                raise FigureDataError(
                    f"{target_id}.{harness_id} compiler route attribution "
                    "classification mismatch"
                )
            if stored_attribution.get("accepted_interval") != [
                minimum_route_ratio,
                maximum_route_ratio,
            ]:
                raise FigureDataError(
                    f"{target_id}.{harness_id} compiler route attribution "
                    "interval mismatch"
                )
            stored_ratio = positive_number(
                stored_attribution.get("median_cost_ratio"),
                f"{target_id}.{harness_id}.compiler_route_attribution."
                "median_cost_ratio",
            )
            if not math.isclose(
                stored_ratio, route_ratio, rel_tol=1e-12, abs_tol=1e-12
            ):
                raise FigureDataError(
                    f"{target_id}.{harness_id} compiler route attribution "
                    "ratio mismatch"
                )
            if route_equivalent:
                route_pass_count += 1
            else:
                route_fail_count += 1
            if "overview_rank" not in suite_harness:
                continue
            selected_count += 1
            performance_ratios = {
                variant: paired_ratios(
                    measurements, variant, "performance", str(metric_direction)
                )
                for variant in PLOTTED_VARIANTS
            }
            rss_ratios = {
                variant: paired_ratios(
                    measurements, variant, "peak_rss_mib", str(metric_direction)
                )
                for variant in PLOTTED_VARIANTS
            }
            summaries.append(
                HarnessSummary(
                    target_id=target_id,
                    target_label=target_label,
                    harness_id=harness_id,
                    harness_label=str(suite_harness["overview_label"]),
                    overview_rank=int(suite_harness["overview_rank"]),
                    route_equivalent=route_equivalent,
                    rss_work_model=str(rss_work_model),
                    rss_comparison_eligibility=rss_eligibility,
                    performance_unit=str(performance_unit),
                    performance_source=str(performance_source),
                    performance_ratios=performance_ratios,
                    rss_ratios=rss_ratios,
                )
            )
        if selected_count != 2:
            raise FigureDataError(
                f"{target_id} must select exactly two overview harnesses"
            )

    summaries.sort(
        key=lambda item: (
            target_ids.index(item.target_id),
            item.overview_rank,
        )
    )
    if len(summaries) != 2 * len(target_ids):
        raise FigureDataError("overview harness selection is incomplete")
    total_routes = route_pass_count + route_fail_count
    expected_classification = (
        "all_routes_equivalent"
        if route_fail_count == 0
        else "attribution_limits_present"
    )
    stored_summary = require_mapping(
        results.get("compiler_route_attribution"),
        "results.compiler_route_attribution",
    )
    expected_summary: dict[str, Any] = {
        "accepted_interval": [minimum_route_ratio, maximum_route_ratio],
        "classification": expected_classification,
        "fail_count": route_fail_count,
        "metric": "paired_execution_cost_ratio",
        "pass_count": route_pass_count,
        "total_count": total_routes,
    }
    for key, expected_value in expected_summary.items():
        if stored_summary.get(key) != expected_value:
            raise FigureDataError(
                f"compiler route attribution {key} mismatch: expected "
                f"{expected_value!r}, got {stored_summary.get(key)!r}"
            )
    expected_status = (
        "complete" if route_fail_count == 0 else "complete_with_attribution_limits"
    )
    if results.get("status") != expected_status:
        raise FigureDataError(
            f"results status mismatch: expected {expected_status}, "
            f"got {results.get('status')}"
        )
    return suite, results, tuple(summaries)


def configure_matplotlib() -> None:
    matplotlib.rcParams.update(
        {
            "font.family": "Liberation Sans",
            "font.size": 10.5,
            "axes.labelcolor": INK,
            "axes.edgecolor": AXIS,
            "axes.linewidth": 0.8,
            "xtick.color": AXIS,
            "ytick.color": INK,
            "text.color": INK,
            "figure.facecolor": WHITE,
            "axes.facecolor": WHITE,
            "savefig.facecolor": WHITE,
            "svg.fonttype": "none",
            "svg.hashsalt": "unialloc-type-isolation-primary-suite-v1",
            "pdf.fonttype": 42,
        }
    )


def ratio_delta_percent(ratio: float) -> float:
    return 100.0 * (ratio - 1.0)


def ratio_log2(ratio: float) -> float:
    return math.log2(ratio)


def multiplicative_tick(value: float, _: int) -> str:
    ratio = 2.0**value
    return f"{ratio:g}x"


def symmetric_log2_bound(summaries: Sequence[HarnessSummary], field: str) -> float:
    values: list[float] = []
    for summary in summaries:
        ratio_map = (
            summary.performance_ratios if field == "performance" else summary.rss_ratios
        )
        for variant in PLOTTED_VARIANTS:
            values.extend(ratio_log2(value) for value in ratio_map[variant])
    maximum = max(abs(value) for value in values)
    return min(DISPLAY_LOG2_CAP, float(max(1, math.ceil(1.15 * maximum))))


def clamp(value: float, bound: float) -> float:
    return min(bound, max(-bound, value))


def ratio_map(summary: HarnessSummary, field: str) -> Mapping[str, tuple[float, ...]]:
    return summary.performance_ratios if field == "performance" else summary.rss_ratios


def panel_clipping_metadata(
    summaries: Sequence[HarnessSummary], field: str
) -> dict[str, Any]:
    bound = symmetric_log2_bound(summaries, field)
    ratios: list[float] = []
    clipped_observation_count = 0
    clipped_summary_count = 0
    for summary in summaries:
        values_by_variant = ratio_map(summary, field)
        for variant in PLOTTED_VARIANTS:
            values = values_by_variant[variant]
            ratios.extend(values)
            clipped_observation_count += sum(
                abs(ratio_log2(value)) > bound for value in values
            )
            median_ratio = float(statistics.median(values))
            clipped_summary_count += abs(ratio_log2(median_ratio)) > bound
    return {
        "clipped_observation_count": clipped_observation_count,
        "clipped_summary_count": clipped_summary_count,
        "display_log2_bound": bound,
        "display_ratio_interval": [2.0 ** (-bound), 2.0**bound],
        "raw_ratio_max": max(ratios),
        "raw_ratio_min": min(ratios),
    }


def clipping_metadata(summaries: Sequence[HarnessSummary]) -> dict[str, Any]:
    panels = {
        field: panel_clipping_metadata(summaries, field)
        for field in ("performance", "peak_rss_mib")
    }
    return {
        "clipped_observation_count": sum(
            panel["clipped_observation_count"] for panel in panels.values()
        ),
        "clipped_summary_count": sum(
            panel["clipped_summary_count"] for panel in panels.values()
        ),
        "display_log2_cap": DISPLAY_LOG2_CAP,
        "panels": panels,
        "rule": (
            "Clamp plotted log2 ratios to each symmetric panel bound; edge "
            "chevrons mark clipped medians, while CSV ratios remain uncapped."
        ),
    }


def draw_panel(
    ax: Axes,
    summaries: Sequence[HarnessSummary],
    *,
    field: str,
    show_y_labels: bool,
) -> None:
    offsets = {"typed_plain": -0.14, "typeiso_perf": 0.14}
    bound = symmetric_log2_bound(summaries, field)
    label_pad = 0.04 * bound

    for row_index, summary in enumerate(summaries):
        values_by_variant = ratio_map(summary, field)
        for variant in PLOTTED_VARIANTS:
            ratios = values_by_variant[variant]
            raw = tuple(ratio_log2(value) for value in ratios)
            displayed_raw = tuple(clamp(value, bound) for value in raw)
            median_ratio = float(statistics.median(ratios))
            median = ratio_log2(median_ratio)
            displayed_median = clamp(median, bound)
            median_clipped = displayed_median != median
            y = row_index + offsets[variant]
            container = ax.barh(
                [y],
                [displayed_median],
                height=0.22,
                left=0.0,
                color=COLORS[variant],
                edgecolor=INK,
                linewidth=0.7,
                alpha=0.92,
                zorder=3,
            )
            container.patches[0].set_gid(
                f"bar-{field}-{summary.target_id}-{summary.harness_id}-{variant}"
            )
            jitter = [
                -0.045 + 0.09 * index / max(1, len(raw) - 1)
                for index in range(len(raw))
            ]
            ax.scatter(
                displayed_raw,
                [y + value for value in jitter],
                s=18,
                marker=MARKERS[variant],
                facecolors=WHITE,
                edgecolors=INK,
                linewidths=0.65,
                alpha=0.82,
                zorder=4,
            )
            for point_index, (raw_value, displayed_value, jitter_value) in enumerate(
                zip(raw, displayed_raw, jitter, strict=True)
            ):
                if raw_value == displayed_value:
                    continue
                point_chevron = ax.scatter(
                    [displayed_value],
                    [y + jitter_value],
                    s=24,
                    marker=">" if raw_value > 0.0 else "<",
                    facecolors=INK,
                    edgecolors=INK,
                    linewidths=0.5,
                    clip_on=False,
                    zorder=5,
                )
                point_chevron.set_gid(
                    f"clip-point-{field}-{summary.target_id}-{summary.harness_id}-"
                    f"{variant}-{point_index + 1}"
                )
            if median_clipped:
                chevron = ax.scatter(
                    [displayed_median],
                    [y],
                    s=42,
                    marker=">" if median > 0.0 else "<",
                    facecolors=WHITE,
                    edgecolors=INK,
                    linewidths=0.9,
                    clip_on=False,
                    zorder=6,
                )
                chevron.set_gid(
                    f"clip-{field}-{summary.target_id}-{summary.harness_id}-{variant}"
                )
            sign = 1.0 if median >= 0.0 else -1.0
            if median_clipped:
                annotation_x = displayed_median - sign * 2.0 * label_pad
                horizontal_alignment = "right" if median >= 0.0 else "left"
                annotation = (
                    f"{median_ratio:.1f}x ({ratio_delta_percent(median_ratio):+.1f}%)"
                )
            else:
                annotation_x = displayed_median + sign * label_pad
                horizontal_alignment = "left" if median >= 0.0 else "right"
                annotation = f"{ratio_delta_percent(median_ratio):+.1f}%"
            ax.text(
                annotation_x,
                y,
                annotation,
                ha=horizontal_alignment,
                va="center",
                fontsize=8.4,
                color=INK,
                clip_on=False,
                zorder=5,
            )

    labels = []
    for summary in summaries:
        markers = ""
        if not summary.route_equivalent:
            markers += "†"
        if summary.rss_work_model == ADAPTIVE_ITERATIONS:
            markers += "‡"
        labels.append(
            summary.harness_label
            if not markers
            else f"{summary.harness_label} {markers}"
        )
    ax.set_yticks(range(len(summaries)))
    ax.set_yticklabels(labels if show_y_labels else [])
    if show_y_labels:
        for pair_start in range(0, len(summaries), 2):
            summary = summaries[pair_start]
            ax.text(
                -0.755,
                pair_start + 0.5,
                summary.target_label,
                transform=ax.get_yaxis_transform(),
                ha="left",
                va="center",
                fontsize=10.2,
                fontweight="bold",
                color=INK,
                clip_on=False,
                gid=f"target-label-{summary.target_id}",
            )
    ax.set_ylim(len(summaries) - 0.55, -0.55)
    ax.set_xlim(-bound, bound)
    ax.axvline(0.0, color=UNIALLOC, linewidth=1.35, zorder=2)
    for boundary in range(2, len(summaries), 2):
        ax.axhline(boundary - 0.5, color=GRID, linewidth=0.9, zorder=1)
    ax.grid(axis="x", color=PALE, linewidth=0.8, zorder=0)
    ax.set_xticks(range(-int(bound), int(bound) + 1))
    ax.xaxis.set_major_formatter(FuncFormatter(multiplicative_tick))
    ax.set_xlabel(
        "Execution-cost ratio to UniAlloc | lower is better"
        if field == "performance"
        else "Peak-RSS ratio to UniAlloc | lower is better",
        labelpad=9,
        fontsize=11.2,
    )
    ax.tick_params(axis="y", length=0, pad=7, labelsize=9.6)
    ax.tick_params(axis="x", length=3.5, width=0.8, pad=5)
    ax.spines["top"].set_visible(False)
    ax.spines["right"].set_visible(False)
    ax.spines["left"].set_visible(False)
    ax.spines["bottom"].set_color(AXIS)


def render_figure(
    summaries: Sequence[HarnessSummary], route_interval: tuple[float, float]
) -> Figure:
    figure = plt.figure(figsize=FIGURE_SIZE_INCHES, dpi=100, layout=None)
    grid = figure.add_gridspec(
        1,
        2,
        left=0.255,
        right=0.975,
        top=0.91,
        bottom=0.12,
        wspace=0.22,
    )
    performance_ax = figure.add_subplot(grid[0, 0])
    rss_ax = figure.add_subplot(grid[0, 1])
    draw_panel(
        performance_ax,
        summaries,
        field="performance",
        show_y_labels=True,
    )
    draw_panel(
        rss_ax,
        summaries,
        field="peak_rss_mib",
        show_y_labels=False,
    )
    handles = [
        Line2D(
            [0],
            [0],
            color=COLORS[variant],
            linewidth=8,
            solid_capstyle="butt",
            marker=MARKERS[variant],
            markerfacecolor=WHITE,
            markeredgecolor=INK,
            markeredgewidth=0.7,
            markersize=5.5,
            label=LABELS[variant],
        )
        for variant in PLOTTED_VARIANTS
    ]
    handles.append(
        Line2D(
            [0],
            [0],
            color=UNIALLOC,
            linewidth=1.35,
            label="UniAlloc baseline (1x)",
        )
    )
    legend_gids = [
        *(f"legend-{variant}" for variant in PLOTTED_VARIANTS),
        "legend-unialloc",
    ]
    if any(summary.rss_work_model == ADAPTIVE_ITERATIONS for summary in summaries):
        handles.append(
            Line2D(
                [0],
                [0],
                color=AXIS,
                linewidth=0,
                marker="o",
                markerfacecolor=WHITE,
                markeredgecolor=AXIS,
                markeredgewidth=1.0,
                markersize=5.5,
                label=ADAPTIVE_RSS_LEGEND,
            )
        )
        legend_gids.append("legend-adaptive-rss")
    if any(not summary.route_equivalent for summary in summaries):
        minimum, maximum = route_interval
        handles.append(
            Line2D(
                [0],
                [0],
                color=AXIS,
                linewidth=0,
                marker="o",
                markerfacecolor=WHITE,
                markeredgecolor=AXIS,
                markeredgewidth=1.0,
                markersize=5.5,
                label=f"† typed route outside {minimum:g}–{maximum:g}x",
            )
        )
        legend_gids.append("legend-route-attribution-limit")
    legend = figure.legend(
        handles=handles,
        loc="upper center",
        bbox_to_anchor=(0.62, 0.975),
        ncol=4 if len(handles) == 4 else 3,
        frameon=False,
        fontsize=10.8,
        handlelength=1.8,
        columnspacing=1.8,
    )
    for gid, handle in zip(legend_gids, legend.legend_handles, strict=True):
        handle.set_gid(gid)
    return figure


def write_csv(path: Path, summaries: Sequence[HarnessSummary]) -> None:
    performance_bound = symmetric_log2_bound(summaries, "performance")
    rss_bound = symmetric_log2_bound(summaries, "peak_rss_mib")
    fieldnames = (
        "target",
        "target_label",
        "harness",
        "harness_label",
        "overview_rank",
        "compiler_route_equivalent",
        "rss_work_model",
        "rss_comparison_eligibility",
        "rss_interpretation",
        "fixed_work_rss_aggregate_eligible",
        "performance_unit",
        "performance_source",
        "variant",
        "round",
        "performance_cost_ratio",
        "performance_cost_log2_ratio",
        "performance_cost_percent_change",
        "performance_display_clipped",
        "peak_rss_ratio",
        "peak_rss_log2_ratio",
        "peak_rss_percent_change",
        "peak_rss_display_clipped",
    )
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames, lineterminator="\n")
        writer.writeheader()
        for summary in summaries:
            for variant in PLOTTED_VARIANTS:
                performance = summary.performance_ratios[variant]
                rss = summary.rss_ratios[variant]
                for index, (performance_ratio, rss_ratio) in enumerate(
                    zip(performance, rss, strict=True), start=1
                ):
                    writer.writerow(
                        {
                            "target": summary.target_id,
                            "target_label": summary.target_label,
                            "harness": summary.harness_id,
                            "harness_label": summary.harness_label,
                            "overview_rank": summary.overview_rank,
                            "compiler_route_equivalent": str(
                                summary.route_equivalent
                            ).lower(),
                            "rss_work_model": summary.rss_work_model,
                            "rss_comparison_eligibility": (
                                summary.rss_comparison_eligibility
                            ),
                            "rss_interpretation": (
                                "equal_work"
                                if summary.rss_work_model == FIXED_WORK
                                else "process_volume"
                            ),
                            "fixed_work_rss_aggregate_eligible": str(
                                summary.rss_work_model == FIXED_WORK
                            ).lower(),
                            "performance_unit": summary.performance_unit,
                            "performance_source": summary.performance_source,
                            "variant": variant,
                            "round": index,
                            "performance_cost_ratio": f"{performance_ratio:.12g}",
                            "performance_cost_log2_ratio": (
                                f"{ratio_log2(performance_ratio):.12g}"
                            ),
                            "performance_cost_percent_change": (
                                f"{ratio_delta_percent(performance_ratio):.12g}"
                            ),
                            "performance_display_clipped": str(
                                abs(ratio_log2(performance_ratio)) > performance_bound
                            ).lower(),
                            "peak_rss_ratio": f"{rss_ratio:.12g}",
                            "peak_rss_log2_ratio": f"{ratio_log2(rss_ratio):.12g}",
                            "peak_rss_percent_change": (
                                f"{ratio_delta_percent(rss_ratio):.12g}"
                            ),
                            "peak_rss_display_clipped": str(
                                abs(ratio_log2(rss_ratio)) > rss_bound
                            ).lower(),
                        }
                    )


def remove_path(path: Path) -> None:
    if path.is_dir() and not path.is_symlink():
        shutil.rmtree(path)
    elif path.exists() or path.is_symlink():
        path.unlink()


def normalize_svg_whitespace(path: Path) -> None:
    """Remove renderer-only trailing spaces without changing SVG geometry."""

    lines = path.read_text(encoding="utf-8").splitlines()
    path.write_text(
        "\n".join(line.rstrip() for line in lines) + "\n",
        encoding="utf-8",
    )


def publish_staged_bundle(stage_dir: Path, output_dir: Path) -> None:
    expected_names = {
        f"{STEM}.svg",
        f"{STEM}.png",
        f"{STEM}-data.csv",
        f"{STEM}-manifest.json",
    }
    actual_names = {path.name for path in stage_dir.iterdir()}
    if actual_names != expected_names:
        raise FigureDataError("staged figure bundle is incomplete")

    backup_dir: Path | None = None
    try:
        if output_dir.exists() or output_dir.is_symlink():
            backup_dir = Path(
                tempfile.mkdtemp(
                    prefix=f".{output_dir.name}.backup-", dir=output_dir.parent
                )
            )
            backup_dir.rmdir()
            os.replace(output_dir, backup_dir)
        if os.environ.get("UNIALLOC_TYPEISO_PLOT_TEST_FAIL_PUBLISH") == "after_backup":
            raise FigureDataError("injected bundle publication failure")
        os.replace(stage_dir, output_dir)
    except Exception as error:
        restoration_error: Exception | None = None
        try:
            if backup_dir is not None and backup_dir.exists():
                remove_path(output_dir)
                os.replace(backup_dir, output_dir)
        except Exception as caught:
            restoration_error = caught
        finally:
            remove_path(stage_dir)
            if backup_dir is not None:
                remove_path(backup_dir)
        if restoration_error is not None:
            raise FigureDataError(
                "figure bundle publication and rollback both failed"
            ) from restoration_error
        if isinstance(error, FigureDataError):
            raise
        raise FigureDataError(f"figure bundle publication failed: {error}") from error
    if backup_dir is not None:
        remove_path(backup_dir)


def export(
    suite_path: Path,
    results_path: Path,
    output_dir: Path,
) -> tuple[Path, ...]:
    suite_path = suite_path.resolve()
    results_path = results_path.resolve()
    output_dir = output_dir.resolve()
    suite, results, summaries = validate_and_summarize(suite_path, results_path)
    route_interval = tuple(results["compiler_route_attribution"]["accepted_interval"])
    if len(route_interval) != 2:
        raise FigureDataError("compiler route attribution interval is invalid")

    output_dir.parent.mkdir(parents=True, exist_ok=True)
    stage_dir = Path(
        tempfile.mkdtemp(prefix=f".{output_dir.name}.stage-", dir=output_dir.parent)
    )
    svg_path = stage_dir / f"{STEM}.svg"
    png_path = stage_dir / f"{STEM}.png"
    csv_path = stage_dir / f"{STEM}-data.csv"
    manifest_path = stage_dir / f"{STEM}-manifest.json"
    configure_matplotlib()
    figure: Figure | None = None
    try:
        figure = render_figure(
            summaries, (float(route_interval[0]), float(route_interval[1]))
        )
        metadata = {
            "Date": FIXED_TIMESTAMP.isoformat(),
            "Creator": "UniAlloc Matplotlib figure exporter",
        }
        figure.savefig(svg_path, format="svg", metadata=metadata, bbox_inches=None)
        normalize_svg_whitespace(svg_path)
        figure.savefig(
            png_path,
            format="png",
            dpi=PNG_DPI,
            metadata={"Software": "UniAlloc Matplotlib figure exporter"},
            bbox_inches=None,
        )
        write_csv(csv_path, summaries)

        target_order = [str(target["id"]) for target in suite["targets"]]
        target_work_models = {
            str(target["id"]): str(target["rss_work_model"])
            for target in suite["targets"]
        }
        clip_contract = clipping_metadata(summaries)
        manifest = {
            "schema_version": 1,
            "source": "type-isolation-primary-suite",
            "presentation_eligible": True,
            "attribution_limited": (
                results["status"] == "complete_with_attribution_limits"
            ),
            "compiler_route_attribution": results["compiler_route_attribution"],
            "analysis_amendments": results["analysis_amendments"],
            "original_campaign_manifest_sha256": results[
                "original_campaign_manifest_sha256"
            ],
            "implementation": {
                "canonical_sha256": results["implementation_sha256"],
                "git_revision": results["implementation_revision"],
            },
            "encoding": "grouped horizontal median bars on centered log2 ratio axes",
            "axis_encoding": {
                "annotation": "paired-median percent change",
                "center_ratio": 1.0,
                "tick_unit": "multiplicative ratio",
                "transform": "log2",
            },
            "clipping": clip_contract,
            "rss_interpretation": {
                "adaptive_iterations_target_count": sum(
                    model == ADAPTIVE_ITERATIONS
                    for model in target_work_models.values()
                ),
                "adaptive_selected_harness_count": sum(
                    summary.rss_work_model == ADAPTIVE_ITERATIONS
                    for summary in summaries
                ),
                "fixed_work_target_count": sum(
                    model == FIXED_WORK for model in target_work_models.values()
                ),
                "fixed_work_selected_harness_count": sum(
                    summary.rss_work_model == FIXED_WORK for summary in summaries
                ),
                "adaptive_marker": "‡",
                "adaptive_legend": ADAPTIVE_RSS_LEGEND,
                "target_work_models": target_work_models,
                "target_comparison_eligibility": {
                    target_id: (RSS_CORE if model == FIXED_WORK else RSS_DIAGNOSTIC)
                    for target_id, model in target_work_models.items()
                },
                "uncapped_observations_preserved": True,
            },
            "panel_log2_bounds": {
                field: clip_contract["panels"][field]["display_log2_bound"]
                for field in ("performance", "peak_rss_mib")
            },
            "canvas_text": {
                "title": False,
                "subtitle": False,
                "panel_title": False,
                "footer": False,
            },
            "target_order": target_order,
            "results_status": results["status"],
            "selected_harness_count": len(summaries),
            "selected_harnesses": [
                {
                    "target": summary.target_id,
                    "harness": summary.harness_id,
                    "rank": summary.overview_rank,
                    "compiler_route_equivalent": summary.route_equivalent,
                    "rss_work_model": summary.rss_work_model,
                    "rss_comparison_eligibility": (summary.rss_comparison_eligibility),
                    "performance_unit": summary.performance_unit,
                    "performance_source": summary.performance_source,
                }
                for summary in summaries
            ],
            "variant_order": list(PLOTTED_VARIANTS),
            "source_artifacts": {
                "suite": {"sha256": sha256_file(suite_path)},
                "results": {"sha256": sha256_file(results_path)},
            },
            "assets": [
                {"path": svg_path.name, "sha256": sha256_file(svg_path)},
                {"path": png_path.name, "sha256": sha256_file(png_path)},
                {"path": csv_path.name, "sha256": sha256_file(csv_path)},
            ],
        }
        manifest_path.write_text(
            json.dumps(manifest, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        publish_staged_bundle(stage_dir, output_dir)
    except Exception:
        remove_path(stage_dir)
        raise
    finally:
        if figure is not None:
            plt.close(figure)

    return tuple(
        output_dir / name
        for name in (
            f"{STEM}.svg",
            f"{STEM}.png",
            f"{STEM}-data.csv",
            f"{STEM}-manifest.json",
        )
    )


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--suite", type=Path, default=DEFAULT_SUITE)
    parser.add_argument("--results", type=Path, default=DEFAULT_RESULTS)
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT_DIR)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    try:
        generated = export(args.suite, args.results, args.output_dir)
    except FigureDataError as error:
        print(f"error: {error}", file=sys.stderr)
        return 2
    for path in generated:
        print(path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
