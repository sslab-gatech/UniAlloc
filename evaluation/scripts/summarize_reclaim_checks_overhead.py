#!/usr/bin/env python3
"""Summarize bounded runtime/RSS overhead for the opt-in reclaim checks."""

from __future__ import annotations

import argparse
import hashlib
import json
import pathlib
import sys
from typing import Any


ROOT = pathlib.Path(__file__).resolve().parents[2]
BASE = "unialloc_no_optional"
TREATMENT = "unialloc_reclaim_checks"


class OverheadError(RuntimeError):
    """Raised when a matrix cannot support the requested comparison."""


def load(path: pathlib.Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise OverheadError(f"cannot read matrix {path}: {error}") from error
    if not isinstance(value, dict) or value.get("success") is not True:
        raise OverheadError(f"matrix is missing a successful result: {path}")
    return value


def artifact(path: pathlib.Path) -> dict[str, Any]:
    resolved = path.resolve()
    payload = resolved.read_bytes()
    try:
        display = str(resolved.relative_to(ROOT))
    except ValueError:
        display = str(resolved)
    return {
        "path": display,
        "bytes": len(payload),
        "sha256": hashlib.sha256(payload).hexdigest(),
    }


def summary_row(matrix: dict[str, Any], app: str, variant: str) -> dict[str, Any]:
    rows = matrix.get("summaries")
    if not isinstance(rows, list):
        raise OverheadError("matrix summaries must be a list")
    matches = [
        row
        for row in rows
        if isinstance(row, dict)
        and row.get("app") == app
        and row.get("variant") == variant
    ]
    if len(matches) != 1:
        raise OverheadError(f"expected one {app}/{variant} summary")
    return matches[0]


def positive_number(value: object, label: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or value <= 0:
        raise OverheadError(f"{label} must be positive")
    return float(value)


def compare(matrix: dict[str, Any], app: str) -> dict[str, Any]:
    base = summary_row(matrix, app, BASE)
    treatment = summary_row(matrix, app, TREATMENT)
    if base.get("output_sha256") != treatment.get("output_sha256"):
        raise OverheadError(f"workload outputs differ for {app}")
    base_wall = positive_number(base.get("median_wall_seconds"), "base wall time")
    treatment_wall = positive_number(
        treatment.get("median_wall_seconds"), "treatment wall time"
    )
    base_rss = positive_number(base.get("median_peak_rss_kib"), "base RSS")
    treatment_rss = positive_number(
        treatment.get("median_peak_rss_kib"), "treatment RSS"
    )
    return {
        "app": app,
        "sample_count": base.get("sample_count"),
        "work_amount": base.get("work_amount"),
        "work_unit": base.get("work_unit"),
        "output_sha256": base.get("output_sha256"),
        "baseline": {
            "variant": BASE,
            "median_wall_seconds": base_wall,
            "wall_mad_seconds": base.get("wall_mad_seconds"),
            "median_peak_rss_kib": base_rss,
            "rss_mad_kib": base.get("rss_mad_kib"),
        },
        "reclaim_checks": {
            "variant": TREATMENT,
            "median_wall_seconds": treatment_wall,
            "wall_mad_seconds": treatment.get("wall_mad_seconds"),
            "median_peak_rss_kib": treatment_rss,
            "rss_mad_kib": treatment.get("rss_mad_kib"),
        },
        "delta": {
            "median_wall_percent": (treatment_wall / base_wall - 1.0) * 100.0,
            "median_peak_rss_kib": treatment_rss - base_rss,
            "median_peak_rss_percent": (treatment_rss / base_rss - 1.0)
            * 100.0,
        },
    }


def summarize(
    standard: dict[str, Any],
    amplified: dict[str, Any],
    *,
    standard_artifact: dict[str, Any],
    amplified_artifact: dict[str, Any],
) -> dict[str, Any]:
    return {
        "schema_version": 1,
        "source": "unialloc-reclaim-checks-overhead-summary",
        "claim_grade": False,
        "boundary": (
            "Single-host medians bound observed overhead for two pinned workloads. "
            "They do not establish a workload-independent zero-overhead claim."
        ),
        "feature_contract": (
            "reclaim_checks is opt-in; default and type_isolation-only builds do not "
            "enable this lifecycle path"
        ),
        "inputs": [standard_artifact, amplified_artifact],
        "comparisons": [compare(standard, "oxipng"), compare(amplified, "ripgrep")],
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--standard", type=pathlib.Path, required=True)
    parser.add_argument("--ripgrep-amplified", type=pathlib.Path, required=True)
    parser.add_argument("--output", type=pathlib.Path)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        standard = args.standard.expanduser().resolve()
        amplified = args.ripgrep_amplified.expanduser().resolve()
        result = summarize(
            load(standard),
            load(amplified),
            standard_artifact=artifact(standard),
            amplified_artifact=artifact(amplified),
        )
        encoded = json.dumps(result, indent=2, sort_keys=True) + "\n"
        if args.output is not None:
            args.output.parent.mkdir(parents=True, exist_ok=True)
            args.output.write_text(encoded, encoding="utf-8")
        print(encoded, end="")
        return 0
    except (OSError, OverheadError) as error:
        print(f"error: {error}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
