#!/usr/bin/env python3
"""Render a slide-ready compiler/runtime heap-lifetime evidence overview.

The input is the JSON summary emitted by
``compiler_heap_lifetime_ground_truth.py``.  The canonical output is an
editable 16:9 SVG accompanied by a long-form CSV.  A 2x PNG companion is
created when ImageMagick is available.

Only Python's standard library is used.  The figure deliberately presents MIR
features as observational associations and keeps compiler abstentions outside
any error-rate interpretation.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import html
import json
import math
import pathlib
import shutil
import subprocess
import sys
import tempfile
from collections.abc import Mapping, Sequence
from typing import Any


WIDTH = 1600
HEIGHT = 900
BACKGROUND = "#FFFFFF"
TEXT = "#17202A"
MUTED = "#566573"
GRID = "#D5DBDB"
PANEL = "#FAFBFC"

SHORT_COLOR = "#0072B2"
LONG_COLOR = "#D55E00"
INDETERMINATE_COLOR = "#999999"
FEATURE_COLOR = "#CC79A7"
ACTIONABLE_COLOR = "#009E73"
ABSTAIN_COLOR = "#B8C2C8"

FEATURE_LIMIT = 5
TINY_DENOMINATOR_FRACTION = 0.01
ACTIONABLE_HINT_SEMANTICS = frozenset(
    {"bounded_process_long_oracle", "other_advisory"}
)
ABSTAIN_HINT_SEMANTICS = frozenset(
    {"unknown", "proven_scoped_no_duration_claim"}
)
KNOWN_HINT_SEMANTICS = ACTIONABLE_HINT_SEMANTICS | ABSTAIN_HINT_SEMANTICS

OUTCOME_COUNT_FIELDS = {
    "Short": "decisive_short_outcomes",
    "Long": "decisive_long_outcomes",
    "Indeterminate": "completed_indeterminate_outcomes",
}
OUTCOME_BYTE_FIELDS = {
    "Short": "decisive_short_requested_bytes",
    "Long": "decisive_long_requested_bytes",
    "Indeterminate": "completed_indeterminate_requested_bytes",
}
CSV_FIELDS = (
    "panel",
    "group",
    "category",
    "count",
    "denominator_count",
    "requested_bytes",
    "denominator_requested_bytes",
    "share",
    "runtime_sites",
    "threshold_minimum_decisive_outcomes",
    "threshold_minimum_long_requested_byte_share",
    "caveat",
)


class ChartError(RuntimeError):
    """The source summary cannot support the requested evidence figure."""


def _mapping(value: Any, label: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise ChartError(f"{label} must be an object")
    return value


def _array(value: Any, label: str, *, nonempty: bool = False) -> list[Any]:
    if not isinstance(value, list) or (nonempty and not value):
        suffix = " a non-empty" if nonempty else " an"
        raise ChartError(f"{label} must be{suffix} array")
    return value


def _string(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ChartError(f"{label} must be a non-empty string")
    if any(ord(character) < 32 for character in value):
        raise ChartError(f"{label} contains a control character")
    return value.strip()


def _integer(value: Any, label: str, *, positive: bool = False) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ChartError(f"{label} must be an integer")
    if value < (1 if positive else 0):
        qualifier = "positive" if positive else "non-negative"
        raise ChartError(f"{label} must be {qualifier}")
    return value


def _number(value: Any, label: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ChartError(f"{label} must be a number")
    result = float(value)
    if not math.isfinite(result):
        raise ChartError(f"{label} must be finite")
    return result


def _fraction(value: Any, label: str) -> float:
    result = _number(value, label)
    if not 0.0 <= result <= 1.0:
        raise ChartError(f"{label} must be between zero and one")
    return result


def _optional_fraction(value: Any, label: str) -> float | None:
    return None if value is None else _fraction(value, label)


def _boolean(value: Any, label: str) -> bool:
    if not isinstance(value, bool):
        raise ChartError(f"{label} must be a boolean")
    return value


def _percent(value: Any, label: str) -> float:
    result = _number(value, label)
    if not 0.0 <= result <= 100.0:
        raise ChartError(f"{label} must be between zero and 100")
    return result


def _validate_outcomes(raw: Any, label: str) -> dict[str, int]:
    outcomes = _mapping(raw, label)
    required = (
        "allocation_count",
        "allocation_requested_bytes",
        "tracked_allocations",
        "bypassed_unobserved_allocations",
        *OUTCOME_COUNT_FIELDS.values(),
        "live_right_censored_objects",
        *OUTCOME_BYTE_FIELDS.values(),
        "live_inflight_age_lower_bound_bytes",
    )
    normalized = {
        field: _integer(outcomes.get(field), f"{label}.{field}")
        for field in required
    }
    completed = sum(normalized[field] for field in OUTCOME_COUNT_FIELDS.values())
    if completed <= 0:
        raise ChartError(f"{label} must contain at least one completed outcome")
    completed_bytes = sum(
        normalized[field] for field in OUTCOME_BYTE_FIELDS.values()
    )
    if completed_bytes <= 0:
        raise ChartError(f"{label} must contain positive completed requested bytes")
    if (
        normalized["allocation_count"]
        != normalized["tracked_allocations"]
        + normalized["bypassed_unobserved_allocations"]
    ):
        raise ChartError(f"{label} allocation accounting is inconsistent")
    if (
        normalized["tracked_allocations"]
        != completed + normalized["live_right_censored_objects"]
    ):
        raise ChartError(f"{label} tracked-outcome accounting is inconsistent")
    if completed_bytes > normalized["allocation_requested_bytes"]:
        raise ChartError(f"{label} completed bytes exceed allocated bytes")
    return normalized


def _validate_runtime_sites(
    raw: Any, label: str, expected_count: int
) -> tuple[list[Mapping[str, Any]], int, int]:
    sites = _array(raw, label)
    if len(sites) != expected_count:
        raise ChartError(f"{label} length must equal runtime_exact_site_count")
    actionable = 0
    abstained = 0
    for index, raw_site in enumerate(sites):
        site_label = f"{label}[{index}]"
        site = _mapping(raw_site, site_label)
        joined = _boolean(site.get("static_identity_joined"), f"{site_label}.static_identity_joined")
        compiler = site.get("compiler")
        if compiler is None:
            if joined:
                raise ChartError(f"{site_label} joins a missing compiler record")
            abstained += 1
            continue
        if not joined:
            raise ChartError(f"{site_label} has compiler data without a static join")
        compiler_row = _mapping(compiler, f"{site_label}.compiler")
        semantics = _string(
            compiler_row.get("hint_semantics"),
            f"{site_label}.compiler.hint_semantics",
        )
        if semantics not in KNOWN_HINT_SEMANTICS:
            raise ChartError(
                f"{site_label}.compiler.hint_semantics is unsupported: {semantics!r}"
            )
        if semantics in ACTIONABLE_HINT_SEMANTICS:
            actionable += 1
        else:
            abstained += 1
    return sites, actionable, abstained


def _validate_feature_rows(
    raw: Any, label: str, aggregate_decisive_bytes: int
) -> list[dict[str, Any]]:
    rows = _array(raw, label, nonempty=True)
    normalized: list[dict[str, Any]] = []
    features: set[str] = set()
    for index, raw_row in enumerate(rows):
        row_label = f"{label}[{index}]"
        row = _mapping(raw_row, row_label)
        feature = _string(row.get("feature"), f"{row_label}.feature")
        if feature in features:
            raise ChartError(f"{label} contains duplicate feature {feature!r}")
        features.add(feature)
        sites = _integer(row.get("present_runtime_sites"), f"{row_label}.present_runtime_sites", positive=True)
        decisive = _integer(
            row.get("present_decisive_requested_bytes"),
            f"{row_label}.present_decisive_requested_bytes",
            positive=True,
        )
        long_bytes = _integer(
            row.get("present_long_requested_bytes"),
            f"{row_label}.present_long_requested_bytes",
        )
        share = _fraction(
            row.get("present_long_requested_byte_share"),
            f"{row_label}.present_long_requested_byte_share",
        )
        if long_bytes > decisive:
            raise ChartError(f"{row_label} Long bytes exceed decisive bytes")
        if decisive > aggregate_decisive_bytes:
            raise ChartError(f"{row_label} decisive bytes exceed the campaign total")
        expected_share = long_bytes / decisive
        if not math.isclose(share, expected_share, rel_tol=1e-9, abs_tol=1e-12):
            raise ChartError(f"{row_label} Long requested-byte share is inconsistent")
        _optional_fraction(
            row.get("absent_long_requested_byte_share"),
            f"{row_label}.absent_long_requested_byte_share",
        )
        selection_conditioned = _boolean(
            row.get("selection_conditioned"),
            f"{row_label}.selection_conditioned",
        )
        interpretation = _string(
            row.get("interpretation"), f"{row_label}.interpretation"
        )
        normalized.append(
            {
                "feature": feature,
                "present_runtime_sites": sites,
                "present_decisive_requested_bytes": decisive,
                "present_long_requested_bytes": long_bytes,
                "present_long_requested_byte_share": share,
                "selection_conditioned": selection_conditioned,
                "interpretation": interpretation,
            }
        )
    normalized.sort(
        key=lambda row: (
            row["present_long_requested_byte_share"],
            row["present_long_requested_bytes"],
            row["present_decisive_requested_bytes"],
            row["feature"],
        ),
        reverse=True,
    )
    return normalized[:FEATURE_LIMIT]


def validate_summary(document: Any) -> dict[str, Any]:
    root = _mapping(document, "summary")
    if _integer(root.get("schema_version"), "summary.schema_version", positive=True) != 1:
        raise ChartError("summary.schema_version must be 1")
    if _string(root.get("source"), "summary.source") != "compiler-heap-lifetime-ground-truth-join":
        raise ChartError("summary.source is not a compiler heap-lifetime ground-truth join")
    claim_boundary = _string(root.get("claim_boundary"), "summary.claim_boundary")

    parameters = _mapping(root.get("parameters"), "summary.parameters")
    minimum_outcomes = _integer(
        parameters.get("minimum_decisive_outcomes"),
        "summary.parameters.minimum_decisive_outcomes",
        positive=True,
    )
    minimum_long_share = _fraction(
        parameters.get("minimum_long_requested_byte_share"),
        "summary.parameters.minimum_long_requested_byte_share",
    )

    applications_raw = _array(root.get("applications"), "summary.applications", nonempty=True)
    applications: list[dict[str, Any]] = []
    names: set[str] = set()
    total_actionable = 0
    total_abstained = 0
    for index, raw_app in enumerate(applications_raw):
        app_label = f"summary.applications[{index}]"
        app = _mapping(raw_app, app_label)
        name = _string(app.get("app"), f"{app_label}.app")
        if name in names:
            raise ChartError(f"summary.applications contains duplicate app {name!r}")
        names.add(name)
        outcomes = _validate_outcomes(app.get("outcomes"), f"{app_label}.outcomes")
        runtime_site_count = _integer(
            app.get("runtime_exact_site_count"),
            f"{app_label}.runtime_exact_site_count",
            positive=True,
        )
        sites, actionable, abstained = _validate_runtime_sites(
            app.get("runtime_sites"), f"{app_label}.runtime_sites", runtime_site_count
        )
        total_actionable += actionable
        total_abstained += abstained
        applications.append(
            {
                "app": name,
                "outcomes": outcomes,
                "runtime_exact_site_count": runtime_site_count,
                "runtime_sites": sites,
                "actionable_sites": actionable,
                "abstained_sites": abstained,
            }
        )

    aggregate = _mapping(root.get("aggregate"), "summary.aggregate")
    aggregate_count_fields = (
        "allocation_count",
        "allocation_requested_bytes",
        "decisive_short_outcomes",
        "decisive_long_outcomes",
        "completed_indeterminate_outcomes",
        "live_right_censored_objects",
        "bypassed_unobserved_allocations",
        "runtime_exact_site_count",
        "matched_runtime_exact_site_count",
        "exact_five_field_match_count",
    )
    aggregate_counts = {
        field: _integer(aggregate.get(field), f"summary.aggregate.{field}")
        for field in aggregate_count_fields
    }
    sum_checks = {
        "allocation_count": sum(app["outcomes"]["allocation_count"] for app in applications),
        "allocation_requested_bytes": sum(app["outcomes"]["allocation_requested_bytes"] for app in applications),
        "decisive_short_outcomes": sum(app["outcomes"]["decisive_short_outcomes"] for app in applications),
        "decisive_long_outcomes": sum(app["outcomes"]["decisive_long_outcomes"] for app in applications),
        "completed_indeterminate_outcomes": sum(app["outcomes"]["completed_indeterminate_outcomes"] for app in applications),
        "live_right_censored_objects": sum(app["outcomes"]["live_right_censored_objects"] for app in applications),
        "bypassed_unobserved_allocations": sum(app["outcomes"]["bypassed_unobserved_allocations"] for app in applications),
        "runtime_exact_site_count": sum(app["runtime_exact_site_count"] for app in applications),
    }
    for field, expected in sum_checks.items():
        if aggregate_counts[field] != expected:
            raise ChartError(f"summary.aggregate.{field} disagrees with applications")
    if total_actionable + total_abstained != aggregate_counts["runtime_exact_site_count"]:
        raise ChartError("static classifier site accounting is inconsistent")

    label_collection = _mapping(
        aggregate.get("label_collection"), "summary.aggregate.label_collection"
    )
    force_track_verified = _boolean(
        label_collection.get("force_track_all_verified"),
        "summary.aggregate.label_collection.force_track_all_verified",
    )
    selection_conditioned = _boolean(
        label_collection.get("selection_conditioned"),
        "summary.aggregate.label_collection.selection_conditioned",
    )
    tracked_coverage = _percent(
        label_collection.get("tracked_allocation_coverage_percent"),
        "summary.aggregate.label_collection.tracked_allocation_coverage_percent",
    )
    prevalence_eligible = _boolean(
        label_collection.get("unbiased_prevalence_claim_eligible"),
        "summary.aggregate.label_collection.unbiased_prevalence_claim_eligible",
    )

    aggregate_decisive_bytes = sum(
        app["outcomes"]["decisive_short_requested_bytes"]
        + app["outcomes"]["decisive_long_requested_bytes"]
        for app in applications
    )
    if aggregate_decisive_bytes <= 0:
        raise ChartError("campaign decisive requested bytes must be positive")
    features = _validate_feature_rows(
        aggregate.get("feature_correlations"),
        "summary.aggregate.feature_correlations",
        aggregate_decisive_bytes,
    )

    candidates = _array(
        aggregate.get("long_dominant_candidates"),
        "summary.aggregate.long_dominant_candidates",
    )
    for index, raw_candidate in enumerate(candidates):
        candidate_label = f"summary.aggregate.long_dominant_candidates[{index}]"
        candidate = _mapping(raw_candidate, candidate_label)
        derived = _mapping(candidate.get("derived"), f"{candidate_label}.derived")
        decisive = _integer(
            derived.get("decisive_outcomes"),
            f"{candidate_label}.derived.decisive_outcomes",
        )
        long_share = _fraction(
            derived.get("long_requested_byte_share"),
            f"{candidate_label}.derived.long_requested_byte_share",
        )
        if decisive < minimum_outcomes or long_share < minimum_long_share:
            raise ChartError(f"{candidate_label} does not satisfy the safe profile gate")

    return {
        "applications": applications,
        "features": features,
        "classifier": {
            "total_sites": aggregate_counts["runtime_exact_site_count"],
            "actionable_sites": total_actionable,
            "abstained_sites": total_abstained,
        },
        "gate": {
            "eligible_sites": len(candidates),
            "minimum_decisive_outcomes": minimum_outcomes,
            "minimum_long_requested_byte_share": minimum_long_share,
        },
        "evidence": {
            "claim_boundary": claim_boundary,
            "aggregate_decisive_requested_bytes": aggregate_decisive_bytes,
            "force_track_all_verified": force_track_verified,
            "selection_conditioned": selection_conditioned,
            "tracked_allocation_coverage_percent": tracked_coverage,
            "unbiased_prevalence_claim_eligible": prevalence_eligible,
        },
    }


def _escape(value: object) -> str:
    return html.escape(str(value), quote=True)


def _fmt_share(value: float) -> str:
    return f"{value * 100.0:.2f}%"


def _fmt_count(value: int) -> str:
    return f"{value:,}"


def _truncate(value: str, limit: int) -> str:
    return value if len(value) <= limit else value[: limit - 1].rstrip() + "…"


def _feature_label(value: str) -> str:
    return value.replace("_", " ").replace("=", ": ")


def _svg_text(
    x: float,
    y: float,
    value: object,
    *,
    size: float = 16,
    weight: int = 400,
    anchor: str = "start",
    fill: str = TEXT,
) -> str:
    return (
        f'  <text x="{x:.1f}" y="{y:.1f}" font-size="{size:.1f}" '
        f'font-weight="{weight}" text-anchor="{anchor}" fill="{fill}">'
        f"{_escape(value)}</text>"
    )


def _panel(x: float, y: float, width: float, height: float, panel_id: str) -> str:
    return (
        f'  <rect id="{_escape(panel_id)}" x="{x:.1f}" y="{y:.1f}" '
        f'width="{width:.1f}" height="{height:.1f}" rx="12" '
        f'fill="{PANEL}" stroke="{GRID}"/>'
    )


def _outcome_panel(data: Mapping[str, Any]) -> list[str]:
    applications = data["applications"]
    body = [
        _panel(45, 125, 1510, 225, "outcome-panel"),
        _svg_text(70, 163, "A · Completed pressure-relative outcomes", size=22, weight=700),
        _svg_text(
            1530,
            163,
            "object share within each application",
            size=14,
            anchor="end",
            fill=MUTED,
        ),
    ]
    plot_left = 225.0
    plot_width = 650.0
    first_y = 205.0
    usable_height = 115.0
    gap = usable_height / max(1, len(applications) - 1) if len(applications) > 1 else 0.0
    for index, app in enumerate(applications):
        y = first_y + index * gap
        outcomes = app["outcomes"]
        counts = {category: outcomes[field] for category, field in OUTCOME_COUNT_FIELDS.items()}
        denominator = sum(counts.values())
        body.append(_svg_text(75, y + 5, app["app"], size=17, weight=700))
        cursor = plot_left
        for category, color in (
            ("Short", SHORT_COLOR),
            ("Long", LONG_COLOR),
            ("Indeterminate", INDETERMINATE_COLOR),
        ):
            share = counts[category] / denominator
            width = plot_width * share
            if width > 0:
                body.append(
                    f'  <rect class="outcome-{category.casefold()}" x="{cursor:.2f}" '
                    f'y="{y - 14:.2f}" width="{width:.2f}" height="24" fill="{color}">'
                    f"<title>{_escape(app['app'])} {category}: {_fmt_share(share)} "
                    f"({_fmt_count(counts[category])})</title></rect>"
                )
            cursor += width
        body.append(
            f'  <rect x="{plot_left:.2f}" y="{y - 14:.2f}" width="{plot_width:.2f}" '
            'height="24" fill="none" stroke="#7F8C8D"/>'
        )
        summary = " · ".join(
            f"{category} {_fmt_share(counts[category] / denominator)} ({_fmt_count(counts[category])})"
            for category in ("Short", "Long", "Indeterminate")
        )
        body.append(_svg_text(900, y + 5, summary, size=13.5))
    return body


def _feature_panel(data: Mapping[str, Any]) -> list[str]:
    rows = data["features"]
    total_decisive_bytes = data["evidence"]["aggregate_decisive_requested_bytes"]
    tiny_cutoff = total_decisive_bytes * TINY_DENOMINATOR_FRACTION
    maximum = max(row["present_long_requested_byte_share"] for row in rows)
    axis_high = max(0.05, min(1.0, math.ceil(maximum * 20.0) / 20.0))
    body = [
        _panel(45, 370, 960, 445, "feature-panel"),
        _svg_text(70, 408, "B · Selected MIR feature associations", size=22, weight=700),
        _svg_text(
            975,
            408,
            f"top {len(rows)} observed Long requested-byte shares",
            size=14,
            anchor="end",
            fill=MUTED,
        ),
    ]
    plot_left, plot_right = 445.0, 940.0
    plot_top, plot_bottom = 455.0, 755.0
    for fraction in (0.0, 0.5, 1.0):
        x = plot_left + fraction * (plot_right - plot_left)
        value = fraction * axis_high
        body.extend(
            [
                f'  <line x1="{x:.2f}" y1="{plot_top - 10:.1f}" x2="{x:.2f}" '
                f'y2="{plot_bottom:.1f}" stroke="{GRID}"/>',
                _svg_text(x, 780, _fmt_share(value), size=12, anchor="middle", fill=MUTED),
            ]
        )
    row_gap = (plot_bottom - plot_top) / len(rows)
    for index, row in enumerate(rows):
        center_y = plot_top + (index + 0.5) * row_gap
        share = row["present_long_requested_byte_share"]
        bar_width = (plot_right - plot_left) * share / axis_high
        tiny = row["present_decisive_requested_bytes"] < tiny_cutoff
        marker = " †" if tiny else ""
        display = _truncate(_feature_label(row["feature"]), 47)
        # The exact feature name stays available to presentation editors and
        # assistive tools even when the visible label is shortened.
        body.append(
            f'  <text x="70.0" y="{center_y - 4:.1f}" font-size="14" '
            f'font-weight="650" fill="{TEXT}"><title>{_escape(row["feature"])}</title>'
            f'{_escape(display)}</text>'
        )
        support = (
            f"n={_fmt_count(row['present_decisive_requested_bytes'])} decisive B · "
            f"{_fmt_count(row['present_runtime_sites'])} sites{marker}"
        )
        body.append(_svg_text(70, center_y + 17, support, size=12, fill=MUTED))
        body.append(
            f'  <rect x="{plot_left:.1f}" y="{center_y - 14:.2f}" '
            f'width="{plot_right - plot_left:.1f}" height="24" rx="3" fill="#EAECEE"/>'
        )
        if bar_width > 0:
            body.append(
                f'  <rect x="{plot_left:.1f}" y="{center_y - 14:.2f}" '
                f'width="{bar_width:.2f}" height="24" rx="3" fill="{FEATURE_COLOR}">'
                f'<title>{_escape(row["feature"])}: {_fmt_share(share)} Long bytes; '
                f'n={_fmt_count(row["present_decisive_requested_bytes"])} decisive requested bytes</title></rect>'
            )
        label_x = min(plot_left + bar_width + 8, plot_right - 3)
        anchor = "start" if label_x < plot_right - 45 else "end"
        body.append(
            _svg_text(
                label_x,
                center_y + 4,
                _fmt_share(share),
                size=13,
                weight=700,
                anchor=anchor,
                fill=TEXT,
            )
        )
    body.append(
        _svg_text(
            70,
            802,
            "† decisive-byte denominator <1% of the campaign total",
            size=12.5,
            fill=MUTED,
        )
    )
    return body


def _classifier_panel(data: Mapping[str, Any]) -> list[str]:
    classifier = data["classifier"]
    gate = data["gate"]
    total = classifier["total_sites"]
    actionable = classifier["actionable_sites"]
    abstained = classifier["abstained_sites"]
    actionable_share = actionable / total
    abstained_share = abstained / total
    bar_left, bar_width = 1065.0, 445.0
    action_width = bar_width * actionable_share
    body = [
        _panel(1025, 370, 530, 445, "classifier-panel"),
        _svg_text(1050, 408, "C · Deployed static classifier", size=22, weight=700),
        _svg_text(1050, 442, "Actionable duration output vs abstention", size=14, fill=MUTED),
        f'  <rect x="{bar_left:.1f}" y="462" width="{bar_width:.1f}" height="34" rx="4" fill="{ABSTAIN_COLOR}"/>',
    ]
    if action_width > 0:
        body.append(
            f'  <rect x="{bar_left:.1f}" y="462" width="{action_width:.2f}" '
            f'height="34" rx="4" fill="{ACTIONABLE_COLOR}"/>'
        )
    body.extend(
        [
            _svg_text(
                bar_left,
                523,
                f"Actionable {_fmt_share(actionable_share)} ({_fmt_count(actionable)})",
                size=13.5,
                weight=700,
                fill=ACTIONABLE_COLOR,
            ),
            _svg_text(
                bar_left + bar_width,
                523,
                f"Abstain {_fmt_share(abstained_share)} ({_fmt_count(abstained)})",
                size=13.5,
                weight=700,
                anchor="end",
                fill=MUTED,
            ),
            f'  <rect x="1055" y="555" width="470" height="170" rx="10" fill="#F3F6F7" stroke="{GRID}"/>',
            _svg_text(1075, 588, "Safe profile gate", size=17, weight=700),
            _svg_text(1075, 657, gate["eligible_sites"], size=58, weight=700, fill=LONG_COLOR),
            _svg_text(1150, 638, "eligible Long sites", size=19, weight=700),
            _svg_text(
                1150,
                665,
                f"≥{gate['minimum_decisive_outcomes']} decisive outcomes",
                size=14,
                fill=MUTED,
            ),
            _svg_text(
                1150,
                687,
                f"≥{_fmt_share(gate['minimum_long_requested_byte_share'])} Long byte share",
                size=14,
                fill=MUTED,
            ),
            _svg_text(
                1055,
                756,
                "Unknown and scope-only facts remain abstentions.",
                size=13,
                fill=MUTED,
            ),
            _svg_text(
                1055,
                779,
                "Abstained sites remain unclassified.",
                size=13,
                fill=MUTED,
            ),
        ]
    )
    return body


def render_overview(data: Mapping[str, Any], source_digest: str) -> str:
    app_count = len(data["applications"])
    evidence = data["evidence"]
    coverage = evidence["tracked_allocation_coverage_percent"]
    title = "Rust heap-lifetime inference: runtime truth before placement"
    subtitle = (
        f"{app_count} applications · {coverage:.1f}% tracked allocation coverage · "
        "pressure-relative labels joined to MIR features"
    )
    body = [
        _svg_text(55, 58, title, size=32, weight=700),
        _svg_text(55, 93, subtitle, size=17, fill=MUTED),
        *_outcome_panel(data),
        *_feature_panel(data),
        *_classifier_panel(data),
        _svg_text(
            800,
            852,
            "Outcome bars use completed objects; live right-censored objects stay outside these denominators.",
            size=13,
            anchor="middle",
            fill=MUTED,
        ),
        _svg_text(
            800,
            875,
            "MIR rows are overlapping observational associations; tiny denominators can inflate shares. Held-out prediction and causality remain unevaluated.",
            size=13,
            anchor="middle",
            fill=MUTED,
        ),
        f'  <metadata>source-sha256={_escape(source_digest)}; '
        f'force-track-all-verified={str(evidence["force_track_all_verified"]).lower()}; '
        f'selection-conditioned={str(evidence["selection_conditioned"]).lower()}</metadata>',
    ]
    return "\n".join(
        [
            '<?xml version="1.0" encoding="UTF-8"?>',
            f'<svg xmlns="http://www.w3.org/2000/svg" width="{WIDTH}" height="{HEIGHT}" '
            f'viewBox="0 0 {WIDTH} {HEIGHT}" role="img" aria-labelledby="chart-title chart-description">',
            f'  <title id="chart-title">{_escape(title)}</title>',
            f'  <desc id="chart-description">{_escape(subtitle)}</desc>',
            "  <style>text { font-family: Arial, Helvetica, sans-serif; }</style>",
            f'  <rect id="background" x="0" y="0" width="{WIDTH}" height="{HEIGHT}" fill="{BACKGROUND}"/>',
            *body,
            "</svg>",
            "",
        ]
    )


def csv_rows(data: Mapping[str, Any]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for app in data["applications"]:
        outcomes = app["outcomes"]
        denominator_count = sum(outcomes[field] for field in OUTCOME_COUNT_FIELDS.values())
        denominator_bytes = sum(outcomes[field] for field in OUTCOME_BYTE_FIELDS.values())
        for category in ("Short", "Long", "Indeterminate"):
            count = outcomes[OUTCOME_COUNT_FIELDS[category]]
            requested_bytes = outcomes[OUTCOME_BYTE_FIELDS[category]]
            rows.append(
                {
                    "panel": "outcomes",
                    "group": app["app"],
                    "category": category,
                    "count": count,
                    "denominator_count": denominator_count,
                    "requested_bytes": requested_bytes,
                    "denominator_requested_bytes": denominator_bytes,
                    "share": count / denominator_count,
                    "runtime_sites": app["runtime_exact_site_count"],
                    "caveat": "completed outcomes only; live objects are right-censored",
                }
            )
    for feature in data["features"]:
        tiny = (
            feature["present_decisive_requested_bytes"]
            < data["evidence"]["aggregate_decisive_requested_bytes"]
            * TINY_DENOMINATOR_FRACTION
        )
        rows.append(
            {
                "panel": "mir_feature_association",
                "group": feature["feature"],
                "category": "observed_long_requested_byte_share",
                "requested_bytes": feature["present_long_requested_bytes"],
                "denominator_requested_bytes": feature["present_decisive_requested_bytes"],
                "share": feature["present_long_requested_byte_share"],
                "runtime_sites": feature["present_runtime_sites"],
                "caveat": (
                    "observational; tiny denominator below 1% of campaign decisive bytes"
                    if tiny
                    else "observational association; overlapping feature rows"
                ),
            }
        )
    classifier = data["classifier"]
    for category, count in (
        ("Actionable", classifier["actionable_sites"]),
        ("Abstain", classifier["abstained_sites"]),
    ):
        rows.append(
            {
                "panel": "static_classifier",
                "group": "deployed_duration_output",
                "category": category,
                "count": count,
                "denominator_count": classifier["total_sites"],
                "share": count / classifier["total_sites"],
                "runtime_sites": classifier["total_sites"],
                "caveat": "abstentions remain unclassified and carry no error label",
            }
        )
    gate = data["gate"]
    rows.append(
        {
            "panel": "safe_profile_gate",
            "group": "long_dominant_candidate",
            "category": "Eligible Long site",
            "count": gate["eligible_sites"],
            "denominator_count": classifier["total_sites"],
            "share": gate["eligible_sites"] / classifier["total_sites"],
            "runtime_sites": classifier["total_sites"],
            "threshold_minimum_decisive_outcomes": gate["minimum_decisive_outcomes"],
            "threshold_minimum_long_requested_byte_share": gate[
                "minimum_long_requested_byte_share"
            ],
            "caveat": "preregistered runtime profile gate; no held-out predictive claim",
        }
    )
    return rows


def _atomic_write_text(path: pathlib.Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        mode="w",
        encoding="utf-8",
        newline="",
        dir=path.parent,
        prefix=f".{path.name}.",
        delete=False,
    ) as handle:
        temporary = pathlib.Path(handle.name)
        handle.write(content)
    temporary.replace(path)


def _write_csv(path: pathlib.Path, rows: Sequence[Mapping[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        mode="w",
        encoding="utf-8",
        newline="",
        dir=path.parent,
        prefix=f".{path.name}.",
        delete=False,
    ) as handle:
        temporary = pathlib.Path(handle.name)
        writer = csv.DictWriter(
            handle,
            fieldnames=list(CSV_FIELDS),
            extrasaction="ignore",
            lineterminator="\n",
        )
        writer.writeheader()
        for row in rows:
            writer.writerow({field: row.get(field, "") for field in CSV_FIELDS})
    temporary.replace(path)


def _render_png(svg_path: pathlib.Path, png_path: pathlib.Path) -> str | None:
    convert = shutil.which("convert")
    if convert is None:
        return "ImageMagick convert is unavailable; SVG remains canonical"
    result = subprocess.run(
        [
            convert,
            "-background",
            "white",
            "-density",
            "192",
            str(svg_path),
            "-resize",
            f"{WIDTH * 2}x{HEIGHT * 2}!",
            str(png_path),
        ],
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    if result.returncode == 0:
        return None
    png_path.unlink(missing_ok=True)
    detail = result.stderr.strip().splitlines()
    suffix = f": {detail[-1]}" if detail else ""
    return f"ImageMagick could not render {svg_path.name}{suffix}"


def render_all(
    summary_path: pathlib.Path, output_dir: pathlib.Path, *, make_png: bool = True
) -> list[pathlib.Path]:
    source_bytes = summary_path.read_bytes()
    try:
        document = json.loads(source_bytes)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ChartError(f"summary is not valid UTF-8 JSON: {error}") from error
    data = validate_summary(document)
    digest = hashlib.sha256(source_bytes).hexdigest()

    # Complete validation before creating output paths.  Invalid evidence never
    # leaves a chart that could be mistaken for a publishable result.
    output_dir.mkdir(parents=True, exist_ok=True)
    svg_path = output_dir / "ground-truth-overview.svg"
    csv_path = output_dir / "ground-truth-overview.csv"
    _atomic_write_text(svg_path, render_overview(data, digest))
    _write_csv(csv_path, csv_rows(data))
    outputs = [svg_path, csv_path]
    if make_png:
        png_path = output_dir / "ground-truth-overview.png"
        warning = _render_png(svg_path, png_path)
        if warning is None:
            outputs.append(png_path)
        else:
            print(f"warning: {warning}", file=sys.stderr)
    return outputs


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--summary", type=pathlib.Path, required=True)
    parser.add_argument("--output-dir", type=pathlib.Path, required=True)
    parser.add_argument(
        "--no-png",
        action="store_true",
        help="skip the optional 2x ImageMagick PNG companion",
    )
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        outputs = render_all(
            args.summary.expanduser(),
            args.output_dir.expanduser(),
            make_png=not args.no_png,
        )
    except (ChartError, OSError) as error:
        print(f"error: {error}", file=sys.stderr)
        return 2
    for path in outputs:
        print(path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
