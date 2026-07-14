#!/usr/bin/env python3
"""Render slide-ready cross-allocator large-page charts from a summary JSON file.

The renderer intentionally uses only the Python standard library.  SVG is the
canonical output so that every label, line, and data mark remains editable in
presentation software.  When ImageMagick is available, a 2x PNG companion is
also produced for convenient copy/paste workflows.
"""

from __future__ import annotations

import argparse
import csv
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
LIGHT_GRID = "#EDF1F2"
FRONTIER = "#4D4D4D"

# Okabe-Ito-derived palette.  The yellow entry is darkened so that it remains
# legible on a white slide while retaining a color-blind-safe hue separation.
PALETTE = (
    "#0072B2",
    "#D55E00",
    "#009E73",
    "#CC79A7",
    "#E69F00",
    "#56B4E9",
    "#7A6A00",
    "#000000",
)

TOGGLE_FIELDS = (
    "label",
    "mechanism",
    "speedup_pct",
    "speedup_ci_low_pct",
    "speedup_ci_high_pct",
    "max_resident_change_pct",
    "max_resident_ci_low_pct",
    "max_resident_ci_high_pct",
)
ENDPOINT_FIELDS = (
    "label",
    "mechanism",
    "ns_per_touch",
    "max_effective_resident_mib",
    "large_page_backing_mib",
    "pair",
    "mode",
)
BACKING_FIELDS = (
    "label",
    "anon_thp_mib",
    "hugetlb_mib",
)


class ChartError(RuntimeError):
    """Input data cannot be represented without weakening the evidence."""


def _escape(value: object) -> str:
    return html.escape(str(value), quote=True)


def _require_mapping(value: Any, label: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise ChartError(f"{label} must be an object")
    return value


def _require_nonempty_string(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ChartError(f"{label} must be a non-empty string")
    if any(ord(character) < 32 for character in value):
        raise ChartError(f"{label} contains a control character")
    return value.strip()


def _require_finite_number(value: Any, label: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ChartError(f"{label} must be a number")
    result = float(value)
    if not math.isfinite(result):
        raise ChartError(f"{label} must be finite")
    return result


def _require_rows(
    value: Any,
    label: str,
    fields: Sequence[str],
    numeric_fields: Sequence[str],
    nonnegative_fields: Sequence[str] = (),
) -> list[dict[str, Any]]:
    if not isinstance(value, list) or not value:
        raise ChartError(f"{label} must be a non-empty array")
    rows: list[dict[str, Any]] = []
    labels: set[str] = set()
    for index, raw_row in enumerate(value):
        row = _require_mapping(raw_row, f"{label}[{index}]")
        missing = [field for field in fields if field not in row]
        if missing:
            raise ChartError(
                f"{label}[{index}] is missing required field(s): {', '.join(missing)}"
            )
        normalized: dict[str, Any] = {}
        for field in fields:
            field_label = f"{label}[{index}].{field}"
            if field in numeric_fields:
                normalized[field] = _require_finite_number(row[field], field_label)
            else:
                normalized[field] = _require_nonempty_string(row[field], field_label)
        if normalized["label"] in labels:
            raise ChartError(f"{label} contains duplicate label {normalized['label']!r}")
        labels.add(normalized["label"])
        for field in nonnegative_fields:
            if normalized[field] < 0:
                raise ChartError(f"{label}[{index}].{field} must be non-negative")
        rows.append(normalized)
    return rows


def validate_summary(document: Any) -> dict[str, list[dict[str, Any]]]:
    root = _require_mapping(document, "summary")
    slide_data = _require_mapping(root.get("slide_data"), "summary.slide_data")
    toggle_effects = _require_rows(
        slide_data.get("toggle_effects"),
        "summary.slide_data.toggle_effects",
        TOGGLE_FIELDS,
        TOGGLE_FIELDS[2:],
    )
    for index, row in enumerate(toggle_effects):
        for point, low, high, metric in (
            (
                "speedup_pct",
                "speedup_ci_low_pct",
                "speedup_ci_high_pct",
                "speedup",
            ),
            (
                "max_resident_change_pct",
                "max_resident_ci_low_pct",
                "max_resident_ci_high_pct",
                "maximum resident change",
            ),
        ):
            if row[low] > row[point] or row[point] > row[high]:
                raise ChartError(
                    "summary.slide_data.toggle_effects"
                    f"[{index}] {metric} confidence interval must contain its estimate"
                )

    endpoints = _require_rows(
        slide_data.get("endpoints"),
        "summary.slide_data.endpoints",
        ENDPOINT_FIELDS,
        ENDPOINT_FIELDS[2:5],
        ENDPOINT_FIELDS[2:5],
    )
    backing = _require_rows(
        slide_data.get("backing"),
        "summary.slide_data.backing",
        BACKING_FIELDS,
        BACKING_FIELDS[1:],
        BACKING_FIELDS[1:],
    )
    return {
        "toggle_effects": toggle_effects,
        "endpoints": endpoints,
        "backing": backing,
    }


def _format_number(value: float, decimals: int = 1) -> str:
    if abs(value) < 0.5 * (10 ** -decimals):
        value = 0.0
    return f"{value:.{decimals}f}"


def _format_percent(value: float) -> str:
    if abs(value) < 0.05:
        return "0.0%"
    sign = "+" if value > 0 else "−"
    return f"{sign}{abs(value):.1f}%"


def _format_tick(value: float, suffix: str = "") -> str:
    if abs(value) < 1e-12:
        value = 0.0
    if abs(value) >= 100 or math.isclose(value, round(value), abs_tol=1e-9):
        rendered = f"{value:.0f}"
    elif abs(value) >= 10:
        rendered = f"{value:.1f}"
    else:
        rendered = f"{value:.2f}".rstrip("0").rstrip(".")
    return rendered + suffix


def _nice_ticks(
    values: Sequence[float], *, include_zero: bool, target_count: int = 6
) -> tuple[float, float, list[float]]:
    minimum = min(values)
    maximum = max(values)
    if include_zero:
        minimum = min(minimum, 0.0)
        maximum = max(maximum, 0.0)
    if math.isclose(minimum, maximum, rel_tol=1e-12, abs_tol=1e-12):
        padding = max(abs(minimum) * 0.2, 1.0)
        minimum -= padding
        maximum += padding
    raw_step = (maximum - minimum) / max(target_count - 1, 1)
    magnitude = 10 ** math.floor(math.log10(raw_step))
    normalized = raw_step / magnitude
    if normalized <= 1:
        nice = 1
    elif normalized <= 2:
        nice = 2
    elif normalized <= 5:
        nice = 5
    else:
        nice = 10
    step = nice * magnitude
    lower = math.floor(minimum / step) * step
    upper = math.ceil(maximum / step) * step
    if include_zero:
        lower = min(lower, 0.0)
        upper = max(upper, 0.0)
    ticks: list[float] = []
    value = lower
    while value <= upper + step * 1e-9:
        ticks.append(0.0 if abs(value) < step * 1e-10 else value)
        value += step
    return lower, upper, ticks


def _scale(value: float, domain_low: float, domain_high: float, low: float, high: float) -> float:
    return low + (value - domain_low) / (domain_high - domain_low) * (high - low)


def _truncate(value: str, length: int) -> str:
    if len(value) <= length:
        return value
    return value[: max(length - 1, 1)].rstrip() + "…"


def _color_map(mechanisms: Sequence[str]) -> dict[str, str]:
    unique = list(dict.fromkeys(mechanisms))
    return {value: PALETTE[index % len(PALETTE)] for index, value in enumerate(unique)}


def _svg_document(title: str, description: str, body: Sequence[str]) -> str:
    lines = [
        '<?xml version="1.0" encoding="UTF-8"?>',
        (
            f'<svg xmlns="http://www.w3.org/2000/svg" width="{WIDTH}" '
            f'height="{HEIGHT}" viewBox="0 0 {WIDTH} {HEIGHT}" '
            'role="img" aria-labelledby="chart-title chart-description">'
        ),
        f'  <title id="chart-title">{_escape(title)}</title>',
        f'  <desc id="chart-description">{_escape(description)}</desc>',
        "  <style>",
        "    text { font-family: Arial, Helvetica, sans-serif; fill: #17202A; }",
        "    .axis { stroke: #566573; stroke-width: 1.5; }",
        "    .grid { stroke: #D5DBDB; stroke-width: 1; }",
        "    .small { font-size: 15px; }",
        "    .label { font-size: 17px; font-weight: 600; }",
        "  </style>",
        f'  <rect id="background" x="0" y="0" width="{WIDTH}" height="{HEIGHT}" fill="{BACKGROUND}"/>',
        *body,
        "</svg>",
        "",
    ]
    return "\n".join(lines)


def _title_block(title: str, subtitle: str) -> list[str]:
    return [
        f'  <text x="70" y="62" font-size="34" font-weight="700">{_escape(title)}</text>',
        f'  <text x="70" y="102" font-size="18" fill="{MUTED}">{_escape(subtitle)}</text>',
    ]


def _render_effect_panel(
    rows: Sequence[Mapping[str, Any]],
    *,
    x: float,
    y: float,
    width: float,
    height: float,
    title: str,
    point_field: str,
    low_field: str,
    high_field: str,
    favorable: str,
    colors: Mapping[str, str],
) -> list[str]:
    values = [0.0]
    for row in rows:
        values.extend((float(row[low_field]), float(row[high_field])))
    domain_low, domain_high, ticks = _nice_ticks(values, include_zero=True)
    label_width = 220.0
    plot_left = x + label_width
    plot_right = x + width - 30
    plot_top = y + 85
    plot_bottom = y + height - 55
    row_gap = (plot_bottom - plot_top) / len(rows)
    font_size = max(11.0, min(16.0, row_gap * 0.26))
    body = [
        f'  <g class="effect-panel" data-metric="{_escape(point_field)}">',
        f'    <rect x="{x:.1f}" y="{y:.1f}" width="{width:.1f}" height="{height:.1f}" rx="12" fill="#FAFBFC" stroke="{GRID}"/>',
        f'    <text x="{x + 24:.1f}" y="{y + 38:.1f}" font-size="22" font-weight="700">{_escape(title)}</text>',
        f'    <text x="{x + 24:.1f}" y="{y + 65:.1f}" font-size="14" fill="{MUTED}">{_escape(favorable)}</text>',
    ]
    for tick in ticks:
        tick_x = _scale(tick, domain_low, domain_high, plot_left, plot_right)
        class_name = "axis" if abs(tick) < 1e-12 else "grid"
        body.append(
            f'    <line class="{class_name}" x1="{tick_x:.2f}" y1="{plot_top - 12:.1f}" x2="{tick_x:.2f}" y2="{plot_bottom + 6:.1f}"/>'
        )
        body.append(
            f'    <text x="{tick_x:.2f}" y="{plot_bottom + 32:.1f}" font-size="13" text-anchor="middle" fill="{MUTED}">{_escape(_format_tick(tick, "%"))}</text>'
        )
    for index, row in enumerate(rows):
        center_y = plot_top + (index + 0.5) * row_gap
        point = float(row[point_field])
        low = float(row[low_field])
        high = float(row[high_field])
        point_x = _scale(point, domain_low, domain_high, plot_left, plot_right)
        low_x = _scale(low, domain_low, domain_high, plot_left, plot_right)
        high_x = _scale(high, domain_low, domain_high, plot_left, plot_right)
        color = colors[str(row["mechanism"])]
        body.extend(
            [
                f'    <line x1="{plot_left:.1f}" y1="{center_y + row_gap * 0.42:.2f}" x2="{plot_right:.1f}" y2="{center_y + row_gap * 0.42:.2f}" stroke="{LIGHT_GRID}"/>',
                f'    <text x="{x + 20:.1f}" y="{center_y - 3:.2f}" font-size="{font_size:.1f}" font-weight="600"><title>{_escape(row["label"])}</title>{_escape(_truncate(str(row["label"]), 24))}</text>',
                f'    <text x="{x + 20:.1f}" y="{center_y + font_size + 2:.2f}" font-size="{max(font_size - 3, 9):.1f}" fill="{MUTED}">{_escape(_truncate(str(row["mechanism"]), 29))}</text>',
                f'    <line x1="{low_x:.2f}" y1="{center_y:.2f}" x2="{high_x:.2f}" y2="{center_y:.2f}" stroke="{color}" stroke-width="4" stroke-linecap="round"/>',
                f'    <line x1="{low_x:.2f}" y1="{center_y - 7:.2f}" x2="{low_x:.2f}" y2="{center_y + 7:.2f}" stroke="{color}" stroke-width="2"/>',
                f'    <line x1="{high_x:.2f}" y1="{center_y - 7:.2f}" x2="{high_x:.2f}" y2="{center_y + 7:.2f}" stroke="{color}" stroke-width="2"/>',
                f'    <circle cx="{point_x:.2f}" cy="{center_y:.2f}" r="7" fill="{color}" stroke="#FFFFFF" stroke-width="2"><title>{_escape(row["label"])}: {_escape(_format_percent(point))} [{_escape(_format_percent(low))}, {_escape(_format_percent(high))}]</title></circle>',
            ]
        )
        anchor = "start" if point_x < plot_right - 65 else "end"
        label_x = point_x + 11 if anchor == "start" else point_x - 11
        body.append(
            f'    <text x="{label_x:.2f}" y="{center_y - 10:.2f}" font-size="13" font-weight="700" text-anchor="{anchor}" fill="{color}">{_escape(_format_percent(point))}</text>'
        )
    body.append("  </g>")
    return body


def render_incremental_effect(rows: Sequence[Mapping[str, Any]]) -> str:
    title = "Incremental effect of enabling large-page allocation"
    subtitle = "Paired change within each allocator; points are estimates and whiskers are confidence intervals"
    colors = _color_map([str(row["mechanism"]) for row in rows])
    body = _title_block(title, subtitle)
    body.extend(
        _render_effect_panel(
            rows,
            x=55,
            y=145,
            width=730,
            height=650,
            title="Touch speedup",
            point_field="speedup_pct",
            low_field="speedup_ci_low_pct",
            high_field="speedup_ci_high_pct",
            favorable="Positive values mean faster dependent-pointer touch execution",
            colors=colors,
        )
    )
    body.extend(
        _render_effect_panel(
            rows,
            x=815,
            y=145,
            width=730,
            height=650,
            title="Maximum effective resident memory",
            point_field="max_resident_change_pct",
            low_field="max_resident_ci_low_pct",
            high_field="max_resident_ci_high_pct",
            favorable="Negative values mean a smaller resident footprint",
            colors=colors,
        )
    )
    body.extend(
        [
            f'  <text x="800" y="835" font-size="13" text-anchor="middle" fill="{MUTED}">Within-pair deltas only: UniAlloc uses a semantic Rust probe; others share a neutral C probe. 20 paired blocks; 95% bootstrap CI.</text>',
            f'  <text x="800" y="859" font-size="13" text-anchor="middle" fill="{MUTED}">gperftools uses explicit pre-reserved HugeTLB; resident delta excludes unused pool capacity. Zero = matched off/default.</text>',
        ]
    )
    return _svg_document(title, subtitle, body)


def _pareto_frontier(rows: Sequence[Mapping[str, Any]]) -> list[Mapping[str, Any]]:
    frontier: list[Mapping[str, Any]] = []
    for candidate in rows:
        candidate_x = float(candidate["ns_per_touch"])
        candidate_y = float(candidate["max_effective_resident_mib"])
        dominated = any(
            float(other["ns_per_touch"]) <= candidate_x
            and float(other["max_effective_resident_mib"]) <= candidate_y
            and (
                float(other["ns_per_touch"]) < candidate_x
                or float(other["max_effective_resident_mib"]) < candidate_y
            )
            for other in rows
            if other is not candidate
        )
        if not dominated:
            frontier.append(candidate)
    return sorted(frontier, key=lambda row: float(row["ns_per_touch"]))


def _point_shape(
    mode: str, x: float, y: float, radius: float, color: str, title: str
) -> str:
    normalized = mode.casefold()
    common = f'fill="{color}" fill-opacity="0.88" stroke="#FFFFFF" stroke-width="2"'
    tooltip = f"<title>{_escape(title)}</title>"
    if normalized in {"off", "disabled", "never"}:
        return (
            f'<rect x="{x - radius:.2f}" y="{y - radius:.2f}" width="{2 * radius:.2f}" '
            f'height="{2 * radius:.2f}" rx="2" {common}>{tooltip}</rect>'
        )
    if normalized in {"default", "baseline"}:
        points = (
            f"{x:.2f},{y - radius:.2f} {x + radius:.2f},{y:.2f} "
            f"{x:.2f},{y + radius:.2f} {x - radius:.2f},{y:.2f}"
        )
        return f'<polygon points="{points}" {common}>{tooltip}</polygon>'
    if normalized in {"on", "enabled", "always"}:
        return f'<circle cx="{x:.2f}" cy="{y:.2f}" r="{radius:.2f}" {common}>{tooltip}</circle>'
    points = (
        f"{x:.2f},{y - radius:.2f} {x + radius:.2f},{y + radius:.2f} "
        f"{x - radius:.2f},{y + radius:.2f}"
    )
    return f'<polygon points="{points}" {common}>{tooltip}</polygon>'


def render_endpoint_frontier(rows: Sequence[Mapping[str, Any]]) -> str:
    title = "Allocator endpoint frontier"
    subtitle = "Lower-left is better; connected marks are matched modes and bubble area reflects actual large-page backing"
    body = _title_block(title, subtitle)
    plot_left, plot_right = 145.0, 1190.0
    plot_top, plot_bottom = 165.0, 735.0
    x_values = [float(row["ns_per_touch"]) for row in rows]
    y_values = [float(row["max_effective_resident_mib"]) for row in rows]
    x_low, x_high, x_ticks = _nice_ticks(x_values, include_zero=False)
    y_low, y_high, y_ticks = _nice_ticks(y_values, include_zero=False)
    colors = _color_map([str(row["mechanism"]) for row in rows])
    backing_max = max(float(row["large_page_backing_mib"]) for row in rows)

    body.append('  <g id="endpoint-plot">')
    body.append(
        f'    <rect x="{plot_left:.1f}" y="{plot_top:.1f}" width="{plot_right - plot_left:.1f}" height="{plot_bottom - plot_top:.1f}" fill="#FAFBFC" stroke="{GRID}"/>'
    )
    for tick in x_ticks:
        x = _scale(tick, x_low, x_high, plot_left, plot_right)
        body.extend(
            [
                f'    <line class="grid" x1="{x:.2f}" y1="{plot_top:.1f}" x2="{x:.2f}" y2="{plot_bottom:.1f}"/>',
                f'    <text x="{x:.2f}" y="{plot_bottom + 30:.1f}" font-size="14" text-anchor="middle" fill="{MUTED}">{_escape(_format_tick(tick))}</text>',
            ]
        )
    for tick in y_ticks:
        y = _scale(tick, y_low, y_high, plot_bottom, plot_top)
        body.extend(
            [
                f'    <line class="grid" x1="{plot_left:.1f}" y1="{y:.2f}" x2="{plot_right:.1f}" y2="{y:.2f}"/>',
                f'    <text x="{plot_left - 14:.1f}" y="{y + 5:.2f}" font-size="14" text-anchor="end" fill="{MUTED}">{_escape(_format_tick(tick))}</text>',
            ]
        )
    body.extend(
        [
            f'    <text x="{(plot_left + plot_right) / 2:.1f}" y="815" font-size="18" font-weight="600" text-anchor="middle">Nanoseconds per touch</text>',
            f'    <text x="0" y="0" font-size="18" font-weight="600" text-anchor="middle" transform="translate(42 {(plot_top + plot_bottom) / 2:.1f}) rotate(-90)">Maximum effective resident memory (MiB)</text>',
            f'    <text x="{plot_left + 18:.1f}" y="{plot_bottom - 18:.1f}" font-size="14" font-weight="700" fill="{MUTED}">preferred direction ↙</text>',
        ]
    )

    pair_groups: dict[str, list[Mapping[str, Any]]] = {}
    for row in rows:
        pair_groups.setdefault(str(row["pair"]), []).append(row)
    for pair, pair_rows in pair_groups.items():
        if len(pair_rows) < 2:
            continue
        ordered = sorted(pair_rows, key=lambda row: float(row["ns_per_touch"]))
        points = " ".join(
            f'{_scale(float(row["ns_per_touch"]), x_low, x_high, plot_left, plot_right):.2f},'
            f'{_scale(float(row["max_effective_resident_mib"]), y_low, y_high, plot_bottom, plot_top):.2f}'
            for row in ordered
        )
        body.append(
            f'    <polyline points="{points}" fill="none" stroke="#AAB7B8" stroke-width="2" stroke-dasharray="5 5"><title>Matched pair: {_escape(pair)}</title></polyline>'
        )

    frontier = _pareto_frontier(rows)
    if len(frontier) >= 2:
        frontier_points = " ".join(
            f'{_scale(float(row["ns_per_touch"]), x_low, x_high, plot_left, plot_right):.2f},'
            f'{_scale(float(row["max_effective_resident_mib"]), y_low, y_high, plot_bottom, plot_top):.2f}'
            for row in frontier
        )
        body.append(
            f'    <polyline id="pareto-frontier" points="{frontier_points}" fill="none" stroke="{FRONTIER}" stroke-width="3" stroke-dasharray="10 6"><title>Pareto frontier</title></polyline>'
        )

    point_layouts: list[dict[str, Any]] = []
    for index, row in enumerate(rows):
        x = _scale(float(row["ns_per_touch"]), x_low, x_high, plot_left, plot_right)
        y = _scale(
            float(row["max_effective_resident_mib"]),
            y_low,
            y_high,
            plot_bottom,
            plot_top,
        )
        backing = float(row["large_page_backing_mib"])
        radius = 8.0 if backing_max <= 0 else 8.0 + 10.0 * math.sqrt(backing / backing_max)
        color = colors[str(row["mechanism"])]
        right_side = x < plot_right - 210
        point_layouts.append(
            {
                "index": index,
                "row": row,
                "x": x,
                "y": y,
                "radius": radius,
                "color": color,
                "right_side": right_side,
                "label_x": x + radius + 24 if right_side else x - radius - 24,
                "anchor": "start" if right_side else "end",
                "desired_label_y": y - 11 if index % 2 == 0 else y + 25,
            }
        )

    # Resolve labels independently on each side of the marks.  Dense allocator
    # endpoints often share almost the same latency and RSS; fixed alternating
    # offsets make those labels overlap exactly.  A deterministic vertical
    # packing pass keeps both text lines readable and leader lines preserve the
    # point-to-label association.
    for right_side in (True, False):
        group = sorted(
            (layout for layout in point_layouts if layout["right_side"] is right_side),
            key=lambda layout: float(layout["desired_label_y"]),
        )
        minimum_y = plot_top + 22
        maximum_y = plot_bottom - 26
        gap = 46.0
        next_y = minimum_y
        for layout in group:
            layout["label_y"] = max(float(layout["desired_label_y"]), next_y)
            next_y = float(layout["label_y"]) + gap
        next_y = maximum_y
        for layout in reversed(group):
            layout["label_y"] = min(float(layout["label_y"]), next_y)
            next_y = float(layout["label_y"]) - gap
        next_y = minimum_y
        for layout in group:
            layout["label_y"] = max(float(layout["label_y"]), next_y)
            next_y = float(layout["label_y"]) + gap

    for layout in point_layouts:
        row = layout["row"]
        x = float(layout["x"])
        y = float(layout["y"])
        radius = float(layout["radius"])
        color = str(layout["color"])
        backing = float(row["large_page_backing_mib"])
        label_x = float(layout["label_x"])
        label_y = float(layout["label_y"])
        anchor = str(layout["anchor"])
        tooltip = (
            f'{row["label"]}: {float(row["ns_per_touch"]):.2f} ns/touch, '
            f'{float(row["max_effective_resident_mib"]):.1f} MiB resident, '
            f'{backing:.1f} MiB large-page backing; mode={row["mode"]}, pair={row["pair"]}'
        )
        body.append("    " + _point_shape(str(row["mode"]), x, y, radius, color, tooltip))
        leader_end_x = label_x - 4 if anchor == "start" else label_x + 4
        body.append(
            f'    <line x1="{x:.2f}" y1="{y:.2f}" x2="{leader_end_x:.2f}" '
            f'y2="{label_y - 5:.2f}" stroke="{color}" stroke-opacity="0.38" '
            'stroke-width="1.5"/>'
        )
        body.extend(
            [
                f'    <text x="{label_x:.2f}" y="{label_y:.2f}" font-size="14" font-weight="700" text-anchor="{anchor}" fill="{color}">{_escape(_truncate(str(row["label"]), 28))}</text>',
                f'    <text x="{label_x:.2f}" y="{label_y + 18:.2f}" font-size="12" text-anchor="{anchor}" fill="{MUTED}">{float(row["ns_per_touch"]):.2f} ns/touch · {float(row["max_effective_resident_mib"]):.1f} MiB</text>',
            ]
        )
    body.append("  </g>")

    legend_x, legend_y = 1240.0, 180.0
    body.extend(
        [
            f'  <g id="mechanism-legend">',
            f'    <text x="{legend_x:.1f}" y="{legend_y:.1f}" font-size="18" font-weight="700">Mechanism</text>',
        ]
    )
    for index, (mechanism, color) in enumerate(colors.items()):
        y = legend_y + 32 + index * 31
        body.extend(
            [
                f'    <circle cx="{legend_x + 8:.1f}" cy="{y - 5:.1f}" r="7" fill="{color}"/>',
                f'    <text x="{legend_x + 24:.1f}" y="{y:.1f}" font-size="14"><title>{_escape(mechanism)}</title>{_escape(_truncate(mechanism, 28))}</text>',
            ]
        )
    mode_y = legend_y + 65 + len(colors) * 31
    body.extend(
        [
            f'    <text x="{legend_x:.1f}" y="{mode_y:.1f}" font-size="18" font-weight="700">Mode shape</text>',
            f'    <circle cx="{legend_x + 8:.1f}" cy="{mode_y + 25:.1f}" r="7" fill="{MUTED}"/><text x="{legend_x + 24:.1f}" y="{mode_y + 30:.1f}" font-size="14">on / enabled</text>',
            f'    <rect x="{legend_x + 1:.1f}" y="{mode_y + 43:.1f}" width="14" height="14" fill="{MUTED}"/><text x="{legend_x + 24:.1f}" y="{mode_y + 56:.1f}" font-size="14">off / disabled</text>',
            f'    <polygon points="{legend_x + 8:.1f},{mode_y + 70:.1f} {legend_x + 15:.1f},{mode_y + 77:.1f} {legend_x + 8:.1f},{mode_y + 84:.1f} {legend_x + 1:.1f},{mode_y + 77:.1f}" fill="{MUTED}"/><text x="{legend_x + 24:.1f}" y="{mode_y + 82:.1f}" font-size="14">default</text>',
            f'    <text x="{legend_x:.1f}" y="{mode_y + 124:.1f}" font-size="13" fill="{MUTED}">Bubble area encodes</text>',
            f'    <text x="{legend_x:.1f}" y="{mode_y + 142:.1f}" font-size="13" fill="{MUTED}">large-page backing.</text>',
            "  </g>",
        ]
    )
    return _svg_document(title, subtitle, body)


def render_actual_backing(rows: Sequence[Mapping[str, Any]]) -> str:
    title = "Actual large-page backing"
    subtitle = "Process evidence: anonymous THP from smaps; explicit HugeTLB from status"
    body = _title_block(title, subtitle)
    plot_left, plot_right = 110.0, 1505.0
    plot_top, plot_bottom = 175.0, 670.0
    totals = [float(row["anon_thp_mib"]) + float(row["hugetlb_mib"]) for row in rows]
    domain_low, domain_high, ticks = _nice_ticks(totals + [0.0], include_zero=True)
    domain_low = 0.0
    ticks = [tick for tick in ticks if tick >= 0]
    slot = (plot_right - plot_left) / len(rows)
    bar_width = min(105.0, slot * 0.58)
    body.append('  <g id="backing-plot">')
    for tick in ticks:
        y = _scale(tick, domain_low, domain_high, plot_bottom, plot_top)
        body.extend(
            [
                f'    <line class="grid" x1="{plot_left:.1f}" y1="{y:.2f}" x2="{plot_right:.1f}" y2="{y:.2f}"/>',
                f'    <text x="{plot_left - 14:.1f}" y="{y + 5:.2f}" font-size="14" text-anchor="end" fill="{MUTED}">{_escape(_format_tick(tick))}</text>',
            ]
        )
    body.extend(
        [
            f'    <line class="axis" x1="{plot_left:.1f}" y1="{plot_bottom:.1f}" x2="{plot_right:.1f}" y2="{plot_bottom:.1f}"/>',
            f'    <text x="0" y="0" font-size="18" font-weight="600" text-anchor="middle" transform="translate(42 {(plot_top + plot_bottom) / 2:.1f}) rotate(-90)">Large-page backing (MiB)</text>',
        ]
    )
    for index, row in enumerate(rows):
        center = plot_left + (index + 0.5) * slot
        anon = float(row["anon_thp_mib"])
        hugetlb = float(row["hugetlb_mib"])
        total = anon + hugetlb
        zero_y = _scale(0.0, domain_low, domain_high, plot_bottom, plot_top)
        anon_y = _scale(anon, domain_low, domain_high, plot_bottom, plot_top)
        total_y = _scale(total, domain_low, domain_high, plot_bottom, plot_top)
        anon_height = max(0.0, zero_y - anon_y)
        huge_height = max(0.0, anon_y - total_y)
        body.extend(
            [
                f'    <rect x="{center - bar_width / 2:.2f}" y="{anon_y:.2f}" width="{bar_width:.2f}" height="{anon_height:.2f}" fill="#0072B2"><title>{_escape(row["label"])} anonymous THP: {anon:.1f} MiB</title></rect>',
                f'    <rect x="{center - bar_width / 2:.2f}" y="{total_y:.2f}" width="{bar_width:.2f}" height="{huge_height:.2f}" fill="#D55E00"><title>{_escape(row["label"])} HugeTLB: {hugetlb:.1f} MiB</title></rect>',
                f'    <text x="{center:.2f}" y="{max(total_y - 12, plot_top + 14):.2f}" font-size="14" font-weight="700" text-anchor="middle">{total:.1f} MiB</text>',
                f'    <text x="{center:.2f}" y="{plot_bottom + 30:.1f}" font-size="14" font-weight="700" text-anchor="middle"><title>{_escape(row["label"])}</title>{_escape(_truncate(str(row["label"]), 24))}</text>',
                f'    <text x="{center:.2f}" y="{plot_bottom + 52:.1f}" font-size="12" text-anchor="middle" fill="{MUTED}">THP {anon:.1f} · HugeTLB {hugetlb:.1f}</text>',
            ]
        )
    body.extend(
        [
            "  </g>",
            '  <g id="backing-legend">',
            '    <rect x="590" y="805" width="18" height="18" fill="#0072B2"/>',
            '    <text x="618" y="820" font-size="16">Anonymous THP</text>',
            '    <rect x="805" y="805" width="18" height="18" fill="#D55E00"/>',
            '    <text x="833" y="820" font-size="16">Explicit HugeTLB</text>',
            "  </g>",
            f'  <text x="800" y="860" font-size="14" text-anchor="middle" fill="{MUTED}">Same 512 MiB peak requested payload; cross-family comparison is backing-only.</text>',
        ]
    )
    return _svg_document(title, subtitle, body)


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


def _write_csv(
    path: pathlib.Path, rows: Sequence[Mapping[str, Any]], fields: Sequence[str]
) -> None:
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
            fieldnames=list(fields),
            extrasaction="ignore",
            lineterminator="\n",
        )
        writer.writeheader()
        writer.writerows(rows)
    temporary.replace(path)


def _render_png(svg_path: pathlib.Path, png_path: pathlib.Path) -> str | None:
    convert = shutil.which("convert")
    if convert is None:
        return "ImageMagick convert is unavailable; SVG remains the canonical output"
    command = [
        convert,
        "-background",
        "white",
        "-density",
        "192",
        str(svg_path),
        "-resize",
        f"{WIDTH * 2}x{HEIGHT * 2}!",
        str(png_path),
    ]
    result = subprocess.run(
        command,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    if result.returncode != 0:
        png_path.unlink(missing_ok=True)
        detail = result.stderr.strip().splitlines()
        suffix = f": {detail[-1]}" if detail else ""
        return f"ImageMagick could not render {svg_path.name}{suffix}"
    return None


def render_all(
    summary_path: pathlib.Path, output_dir: pathlib.Path, *, make_png: bool = True
) -> list[pathlib.Path]:
    try:
        document = json.loads(summary_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as error:
        raise ChartError(f"summary is not valid JSON: {error}") from error
    data = validate_summary(document)

    # Validation completes before the output directory is created, so malformed
    # evidence cannot leave charts that look publishable.
    output_dir.mkdir(parents=True, exist_ok=True)
    renderers = (
        (
            "incremental-effect",
            data["toggle_effects"],
            TOGGLE_FIELDS,
            render_incremental_effect,
        ),
        (
            "endpoint-frontier",
            data["endpoints"],
            ENDPOINT_FIELDS,
            render_endpoint_frontier,
        ),
        (
            "actual-backing",
            data["backing"],
            BACKING_FIELDS,
            render_actual_backing,
        ),
    )
    outputs: list[pathlib.Path] = []
    warnings: list[str] = []
    for stem, rows, fields, renderer in renderers:
        svg_path = output_dir / f"{stem}.svg"
        csv_path = output_dir / f"{stem}.csv"
        _atomic_write_text(svg_path, renderer(rows))
        _write_csv(csv_path, rows, fields)
        outputs.extend((svg_path, csv_path))
        if make_png:
            png_path = output_dir / f"{stem}.png"
            warning = _render_png(svg_path, png_path)
            if warning is None:
                outputs.append(png_path)
            else:
                warnings.append(warning)
    for warning in warnings:
        print(f"warning: {warning}", file=sys.stderr)
    return outputs


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--summary", type=pathlib.Path, required=True)
    parser.add_argument("--output-dir", type=pathlib.Path, required=True)
    parser.add_argument(
        "--no-png",
        action="store_true",
        help="skip the optional 2x ImageMagick PNG companions",
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
