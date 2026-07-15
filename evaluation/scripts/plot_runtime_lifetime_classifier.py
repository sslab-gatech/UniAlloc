#!/usr/bin/env python3
"""Render a slide-ready SVG from the runtime lifetime classifier summary."""

from __future__ import annotations

import argparse
import html
import json
import pathlib
from typing import Any


WIDTH = 1600
HEIGHT = 900


def text(x: float, y: float, value: str, size: int, *, weight: int = 400, fill: str = "#172033") -> str:
    return (
        f'<text x="{x}" y="{y}" font-family="Inter,Arial,sans-serif" '
        f'font-size="{size}" font-weight="{weight}" fill="{fill}">{html.escape(value)}</text>'
    )


def render(summary: dict[str, Any]) -> str:
    performance = summary["performance"]
    classification = summary["classification"]["totals"]
    apps = performance["applications"]
    parts = [
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{WIDTH}" height="{HEIGHT}" viewBox="0 0 {WIDTH} {HEIGHT}">',
        '<rect width="1600" height="900" fill="#F7F9FC"/>',
        '<rect x="48" y="40" width="1504" height="820" rx="28" fill="#FFFFFF" stroke="#DCE3EE"/>',
        text(96, 112, "Marker-free lifetime learning in real Rust applications", 40, weight=700),
        text(96, 152, "Feature-parity classifier off/on; pinned quick workloads; 15 measured runs per mode", 22, fill="#526078"),
        text(96, 218, "Runtime overhead", 27, weight=700),
        text(96, 249, "Median wall-time delta; lower is better", 18, fill="#68758A"),
    ]

    chart_x = 250.0
    chart_width = 480.0
    domain_low = -2.0
    domain_high = 20.0

    def scale(value: float) -> float:
        return chart_x + (value - domain_low) / (domain_high - domain_low) * chart_width

    zero_x = scale(0.0)
    parts.append(f'<line x1="{zero_x}" y1="278" x2="{zero_x}" y2="574" stroke="#9AA7BA" stroke-width="2"/>')
    for tick in (0, 5, 10, 15, 20):
        x = scale(float(tick))
        parts.append(f'<line x1="{x}" y1="278" x2="{x}" y2="574" stroke="#E7ECF3"/>')
        parts.append(text(x - 8, 602, f"{tick}%", 15, fill="#68758A"))

    colors = {"ripgrep": "#4C78A8", "fd": "#F58518", "oxipng": "#54A24B"}
    for index, row in enumerate(sorted(apps, key=lambda item: item["app"])):
        y = 315 + index * 92
        value = float(row["wall_overhead_percent"])
        low, high = map(float, row["bootstrap_95_percent_wall_overhead"])
        parts.append(text(96, y + 28, str(row["app"]), 22, weight=600))
        bar_x = min(zero_x, scale(value))
        bar_width = max(3.0, abs(scale(value) - zero_x))
        parts.append(
            f'<rect x="{bar_x}" y="{y}" width="{bar_width}" height="38" rx="8" fill="{colors.get(str(row["app"]), "#4C78A8")}"/>'
        )
        parts.append(
            f'<line x1="{scale(low)}" y1="{y + 19}" x2="{scale(high)}" y2="{y + 19}" stroke="#172033" stroke-width="3"/>'
        )
        for endpoint in (low, high):
            x = scale(endpoint)
            parts.append(f'<line x1="{x}" y1="{y + 11}" x2="{x}" y2="{y + 27}" stroke="#172033" stroke-width="3"/>')
        label_x = max(scale(value), scale(high)) + 12
        parts.append(text(label_x, y + 28, f"{value:.2f}%", 20, weight=700))

    parts.extend(
        [
            text(830, 218, "Classification evidence", 27, weight=700),
            text(830, 249, "Coverage and held-out sampled outcomes", 18, fill="#68758A"),
        ]
    )
    metrics = [
        (
            "Allocation decisions learned",
            float(classification["learned_allocation_decision_coverage_percent"]),
            "#4C78A8",
        ),
        (
            "Exact sites learned",
            float(classification["site_classification_coverage_percent"]),
            "#72B7B2",
        ),
        (
            "Sampled learned accuracy",
            float(classification["conditional_learned_prediction_accuracy_percent"]),
            "#54A24B",
        ),
        (
            "Compiler/static hint coverage",
            float(classification["static_hint_coverage_percent"]),
            "#B8C2D1",
        ),
    ]
    for index, (label, value, color) in enumerate(metrics):
        y = 290 + index * 92
        bar_y = y + 17
        parts.append(text(830, y, label, 20, weight=600))
        parts.append(
            f'<rect x="830" y="{bar_y}" width="600" height="24" rx="12" fill="#EEF2F7"/>'
        )
        parts.append(f'<rect x="830" y="{bar_y}" width="{6 * value}" height="24" rx="12" fill="{color}"/>')
        parts.append(text(1445, bar_y + 20, f"{value:.1f}%", 20, weight=700))

    short_obs = int(classification["short_runtime_observations"])
    long_obs = int(classification["long_runtime_observations"])
    parts.extend(
        [
            '<rect x="96" y="655" width="1408" height="148" rx="20" fill="#F0F5FB"/>',
            text(126, 704, "Observed boundary", 21, weight=700, fill="#315B82"),
            text(
                126,
                746,
                f'0 semantic epoch calls  •  {short_obs} Short outcomes  •  {long_obs} Long outcomes  •  0 stable Long sites  •  0 THP routes',
                22,
                weight=600,
            ),
            text(
                126,
                780,
                "The real-app result supports marker-free learning and low-frequency Short sampling; Long/THP benefit remains synthetic-only.",
                19,
                fill="#526078",
            ),
        ]
    )
    parts.append("</svg>")
    return "\n".join(parts) + "\n"


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--summary", type=pathlib.Path, required=True)
    parser.add_argument("--output", type=pathlib.Path, required=True)
    args = parser.parse_args()
    summary = json.loads(args.summary.read_text(encoding="utf-8"))
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(render(summary), encoding="utf-8")
    print(args.output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
