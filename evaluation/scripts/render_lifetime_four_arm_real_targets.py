#!/usr/bin/env python3
"""Render the four-arm real-target lifetime/THP comparison.

The canonical input is a reviewed JSON evidence summary.  The renderer uses
only Python's standard library, writes an editable 16:9 SVG, and optionally
creates a 2x PNG companion with ImageMagick.
"""

from __future__ import annotations

import argparse
import hashlib
import html
import json
import math
from pathlib import Path
import shutil
import subprocess
from collections.abc import Mapping, Sequence
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
WIDTH = 1600
HEIGHT = 900
BACKGROUND = "#FFFFFF"
TEXT = "#17202A"
MUTED = "#566573"
GRID = "#D5DBDB"
PANEL = "#FAFBFC"
ZERO = "#34495E"

VARIANT_ORDER = ("default", "thp_first_fit", "runtime_profile", "compiler_hint")
VARIANT_LABELS = {
    "default": "Default",
    "thp_first_fit": "THP first-fit",
    "runtime_profile": "Runtime profile",
    "compiler_hint": "Compiler hint (ours)",
}
VARIANT_COLORS = {
    "default": "#7F8C8D",
    "thp_first_fit": "#56B4E9",
    "runtime_profile": "#E69F00",
    "compiler_hint": "#0072B2",
}
VARIANT_SHAPES = {
    "default": "diamond",
    "thp_first_fit": "square",
    "runtime_profile": "triangle",
    "compiler_hint": "circle",
}


def _escape(value: object) -> str:
    return html.escape(str(value), quote=True)


def _text(
    x: float,
    y: float,
    value: object,
    *,
    size: float = 14,
    weight: int = 400,
    anchor: str = "start",
    fill: str = TEXT,
    klass: str = "",
) -> str:
    class_attr = f' class="{klass}"' if klass else ""
    return (
        f'  <text x="{x:.1f}" y="{y:.1f}" font-size="{size:.1f}" '
        f'font-weight="{weight}" text-anchor="{anchor}" fill="{fill}"{class_attr}>'
        f"{_escape(value)}</text>"
    )


def _marker(x: float, y: float, variant: str, *, tooltip: str) -> str:
    color = VARIANT_COLORS[variant]
    shape = VARIANT_SHAPES[variant]
    fill = "#FFFFFF" if variant == "default" else color
    stroke_width = 2.6 if variant == "compiler_hint" else 2.0
    title = f"<title>{_escape(tooltip)}</title>"
    if shape == "circle":
        return (
            f'  <circle cx="{x:.1f}" cy="{y:.1f}" r="6.2" fill="{fill}" '
            f'stroke="{color}" stroke-width="{stroke_width}">{title}</circle>'
        )
    if shape == "square":
        return (
            f'  <rect x="{x - 5.8:.1f}" y="{y - 5.8:.1f}" width="11.6" height="11.6" '
            f'rx="1.2" fill="{fill}" stroke="{color}" stroke-width="{stroke_width}">{title}</rect>'
        )
    if shape == "triangle":
        points = f"{x:.1f},{y - 6.8:.1f} {x + 6.5:.1f},{y + 5.3:.1f} {x - 6.5:.1f},{y + 5.3:.1f}"
        return (
            f'  <polygon points="{points}" fill="{fill}" stroke="{color}" '
            f'stroke-width="{stroke_width}" stroke-linejoin="round">{title}</polygon>'
        )
    points = f"{x:.1f},{y - 6.5:.1f} {x + 6.5:.1f},{y:.1f} {x:.1f},{y + 6.5:.1f} {x - 6.5:.1f},{y:.1f}"
    return (
        f'  <polygon points="{points}" fill="{fill}" stroke="{color}" '
        f'stroke-width="{stroke_width}" stroke-linejoin="round">{title}</polygon>'
    )


def _require_number(value: object, context: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{context} must be numeric")
    number = float(value)
    if not math.isfinite(number):
        raise ValueError(f"{context} must be finite")
    return number


def validate(document: Mapping[str, Any]) -> None:
    if document.get("schema_version") != 1:
        raise ValueError("schema_version must be 1")
    targets = document.get("targets")
    if not isinstance(targets, list) or not 4 <= len(targets) <= 7:
        raise ValueError("targets must contain 4 through 7 rows")
    seen: set[str] = set()
    for target in targets:
        if not isinstance(target, dict):
            raise ValueError("every target must be an object")
        target_id = target.get("id")
        if not isinstance(target_id, str) or not target_id or target_id in seen:
            raise ValueError("target ids must be unique non-empty strings")
        seen.add(target_id)
        if not isinstance(target.get("label"), str):
            raise ValueError(f"{target_id}.label must be a string")
        variants = target.get("variants")
        if not isinstance(variants, dict) or set(variants) != set(VARIANT_ORDER):
            raise ValueError(f"{target_id}.variants must contain exactly {VARIANT_ORDER}")
        for variant in VARIANT_ORDER:
            row = variants[variant]
            if not isinstance(row, dict):
                raise ValueError(f"{target_id}.{variant} must be an object")
            available = row.get("available")
            if not isinstance(available, bool):
                raise ValueError(f"{target_id}.{variant}.available must be boolean")
            if available:
                _require_number(
                    row.get("operation_speedup_percent_vs_default"),
                    f"{target_id}.{variant}.operation_speedup_percent_vs_default",
                )
                _require_number(
                    row.get("peak_rss_saving_percent_vs_default"),
                    f"{target_id}.{variant}.peak_rss_saving_percent_vs_default",
                )
    if not isinstance(document.get("measurement_boundary"), str):
        raise ValueError("measurement_boundary must be a string")


def _rounded_domain(values: Sequence[float]) -> float:
    largest = max((abs(value) for value in values), default=0.0)
    step = 5.0 if largest <= 20 else 10.0
    return max(5.0, math.ceil((largest + 1.0) / step) * step)


def _format_effect(value: float) -> str:
    if abs(value) < 0.005:
        return "0.00"
    return f"{value:+.2f}"


def _render_panel(
    document: Mapping[str, Any],
    *,
    x: float,
    y: float,
    width: float,
    height: float,
    panel_label: str,
    title: str,
    subtitle: str,
    field: str,
) -> list[str]:
    targets = document["targets"]
    values = [
        _require_number(row[field], field)
        for target in targets
        for row in target["variants"].values()
        if row["available"]
    ]
    domain = _rounded_domain(values)
    plot_left = x + 155
    plot_right = x + width - 30
    plot_top = y + 105
    plot_bottom = y + height - 54
    row_height = (plot_bottom - plot_top) / len(targets)

    def value_x(value: float) -> float:
        return plot_left + (value + domain) / (2 * domain) * (plot_right - plot_left)

    lines = [
        f'  <rect x="{x:.1f}" y="{y:.1f}" width="{width:.1f}" height="{height:.1f}" rx="12" fill="{PANEL}" stroke="#E5E8E8"/>',
        _text(x + 24, y + 40, f"{panel_label} | {title}", size=22, weight=700),
        _text(x + width - 24, y + 40, subtitle, size=13, anchor="end", fill=MUTED),
    ]
    for tick in (-domain, -domain / 2, 0.0, domain / 2, domain):
        tick_x = value_x(tick)
        stroke = ZERO if tick == 0 else GRID
        dash = ' stroke-dasharray="5 4"' if tick == 0 else ""
        stroke_width = 1.6 if tick == 0 else 1.0
        lines.append(
            f'  <line x1="{tick_x:.1f}" y1="{plot_top - 12:.1f}" x2="{tick_x:.1f}" '
            f'y2="{plot_bottom:.1f}" stroke="{stroke}" stroke-width="{stroke_width}"{dash}/>'
        )
        lines.append(_text(tick_x, plot_bottom + 26, f"{tick:+.0f}%", size=12, anchor="middle", fill=MUTED))

    offsets = (-15.0, -5.0, 5.0, 15.0)
    for target_index, target in enumerate(targets):
        center_y = plot_top + row_height * (target_index + 0.5)
        if target_index:
            sep_y = plot_top + row_height * target_index
            lines.append(
                f'  <line x1="{x + 15:.1f}" y1="{sep_y:.1f}" x2="{x + width - 15:.1f}" '
                f'y2="{sep_y:.1f}" stroke="#ECEFF1"/>'
            )
        lines.append(_text(plot_left - 18, center_y + 5, target["label"], size=13.5, weight=650, anchor="end"))
        for variant_index, variant in enumerate(VARIANT_ORDER):
            row = target["variants"][variant]
            mark_y = center_y + offsets[variant_index]
            if not row["available"]:
                lines.append(_text(value_x(0), mark_y + 4, "N/A", size=10, anchor="middle", fill="#AAB7B8"))
                continue
            value = _require_number(row[field], field)
            mark_x = value_x(value)
            zero_x = value_x(0.0)
            color = VARIANT_COLORS[variant]
            lines.append(
                f'  <line x1="{zero_x:.1f}" y1="{mark_y:.1f}" x2="{mark_x:.1f}" y2="{mark_y:.1f}" '
                f'stroke="{color}" stroke-width="{2.8 if variant == "compiler_hint" else 1.8}" opacity="0.82"/>'
            )
            tooltip = (
                f"{target['label']} | {VARIANT_LABELS[variant]} | {_format_effect(value)}% | "
                f"AHP={row.get('anon_hugepages_mib', 'n/a')} MiB"
            )
            lines.append(_marker(mark_x, mark_y, variant, tooltip=tooltip))
            label_x = mark_x + (9 if value >= 0 else -9)
            anchor = "start" if value >= 0 else "end"
            lines.append(
                _text(
                    label_x,
                    mark_y + 3.8,
                    _format_effect(value),
                    size=10.5,
                    weight=700 if variant == "compiler_hint" else 500,
                    anchor=anchor,
                    fill=color,
                    klass="mono",
                )
            )
    return lines


def render(document: Mapping[str, Any], source_sha256: str) -> str:
    validate(document)
    body = [
        _text(50, 54, "Lifetime/THP policy comparison across real Rust targets", size=31, weight=700),
        _text(
            50,
            88,
            "Fresh processes | fixed work | zero warm-up | default = 0 | positive values are better",
            size=16,
            fill=MUTED,
        ),
    ]
    legend_x = 635.0
    for index, variant in enumerate(VARIANT_ORDER):
        x = legend_x + index * 220
        body.append(_marker(x, 119, variant, tooltip=VARIANT_LABELS[variant]))
        body.append(_text(x + 14, 124, VARIANT_LABELS[variant], size=13.5, weight=650 if variant == "compiler_hint" else 500))
    body.extend(
        _render_panel(
            document,
            x=40,
            y=145,
            width=750,
            height=650,
            panel_label="A",
            title="Operation performance",
            subtitle="speedup vs default",
            field="operation_speedup_percent_vs_default",
        )
    )
    body.extend(
        _render_panel(
            document,
            x=810,
            y=145,
            width=750,
            height=650,
            panel_label="B",
            title="Peak resident memory",
            subtitle="RSS saving vs default",
            field="peak_rss_saving_percent_vs_default",
        )
    )
    body.extend(
        [
            _text(50, 831, "THP first-fit: mmap-wide THP eligibility with no lifetime signal. Runtime profile: policy 11 cold online learning. Ours: policy 9 compiler semantic-scope Long hint.", size=13, fill=MUTED),
            _text(50, 856, document["measurement_boundary"], size=12.5, fill=MUTED),
            _text(1550, 879, f"source sha256 {source_sha256[:16]}", size=10.5, anchor="end", fill="#95A5A6", klass="mono"),
        ]
    )
    metadata = _escape(
        f"source-sha256={source_sha256}; variants={','.join(VARIANT_ORDER)}; "
        f"targets={len(document['targets'])}"
    )
    return "\n".join(
        [
            '<?xml version="1.0" encoding="UTF-8"?>',
            f'<svg xmlns="http://www.w3.org/2000/svg" width="{WIDTH}" height="{HEIGHT}" viewBox="0 0 {WIDTH} {HEIGHT}" role="img">',
            "  <title>Four-way lifetime and THP comparison</title>",
            "  <desc>Operation speedup and peak RSS saving relative to allocator default across real Rust targets.</desc>",
            f"  <metadata>{metadata}</metadata>",
            "  <style>text{font-family:Inter,Segoe UI,Arial,sans-serif}.mono{font-family:ui-monospace,SFMono-Regular,Menlo,monospace}</style>",
            f'  <rect width="{WIDTH}" height="{HEIGHT}" fill="{BACKGROUND}"/>',
            *body,
            "</svg>",
            "",
        ]
    )


def _render_png(svg_path: Path, png_path: Path) -> str | None:
    convert = shutil.which("convert")
    if convert is None:
        return "ImageMagick convert unavailable; SVG remains canonical"
    completed = subprocess.run(
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
    if completed.returncode == 0:
        return None
    png_path.unlink(missing_ok=True)
    detail = completed.stderr.strip().splitlines()
    return f"ImageMagick render failed: {detail[-1] if detail else completed.returncode}"


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--input", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--no-png", action="store_true")
    args = parser.parse_args()

    source = args.input.read_bytes()
    document = json.loads(source)
    if not isinstance(document, dict):
        raise ValueError("input root must be an object")
    digest = hashlib.sha256(source).hexdigest()
    svg = render(document, digest)
    args.output_dir.mkdir(parents=True, exist_ok=True)
    svg_path = args.output_dir / "four-arm-performance-memory.svg"
    svg_path.write_text(svg, encoding="utf-8")
    print(svg_path)
    if not args.no_png:
        png_path = args.output_dir / "four-arm-performance-memory.png"
        warning = _render_png(svg_path, png_path)
        if warning:
            print(warning)
        else:
            print(png_path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
