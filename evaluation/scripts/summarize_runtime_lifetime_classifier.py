#!/usr/bin/env python3
"""Summarize feature-parity runtime lifetime classifier evidence."""

from __future__ import annotations

import argparse
import json
import math
import pathlib
import random
import statistics
from typing import Any, Sequence


class SummaryError(RuntimeError):
    pass


def load_result(path: pathlib.Path) -> dict[str, Any]:
    result = json.loads(path.read_text(encoding="utf-8"))
    if result.get("success") is not True:
        raise SummaryError(f"evaluation is incomplete: {path}")
    return result


def measured_rows(result: dict[str, Any], app: str) -> list[dict[str, Any]]:
    rows = [
        row
        for row in result.get("measurements", ())
        if row.get("app") == app and not row.get("warmup")
    ]
    if not rows:
        raise SummaryError(f"no measured rows for {app}")
    return rows


def median_ratio_interval(
    baseline: Sequence[float],
    adaptive: Sequence[float],
    *,
    iterations: int = 10_000,
    seed: int = 0xA11C,
) -> tuple[float, float]:
    if not baseline or not adaptive:
        raise SummaryError("bootstrap inputs must be nonempty")
    rng = random.Random(seed)
    ratios: list[float] = []
    for _ in range(iterations):
        sampled_baseline = [rng.choice(baseline) for _ in baseline]
        sampled_adaptive = [rng.choice(adaptive) for _ in adaptive]
        ratios.append(
            statistics.median(sampled_adaptive) / statistics.median(sampled_baseline)
        )
    ratios.sort()
    return ratios[int(iterations * 0.025)], ratios[int(iterations * 0.975)]


def build_features(result: dict[str, Any], app: str) -> tuple[str, ...]:
    for build in result.get("builds", ()):
        if build.get("app") != app:
            continue
        force_load = build.get("force_load")
        if isinstance(force_load, dict):
            return tuple(map(str, force_load.get("features", ())))
    raise SummaryError(f"missing force-load feature evidence for {app}")


def summarize_performance(
    baseline: dict[str, Any], adaptive: dict[str, Any]
) -> dict[str, Any]:
    apps = sorted(set(map(str, baseline.get("apps", ()))) & set(map(str, adaptive.get("apps", ()))))
    if not apps:
        raise SummaryError("performance results have no shared applications")
    rows: list[dict[str, Any]] = []
    wall_ratios: list[float] = []
    baseline_wall_sum = 0.0
    adaptive_wall_sum = 0.0
    for app in apps:
        baseline_rows = measured_rows(baseline, app)
        adaptive_rows = measured_rows(adaptive, app)
        if {row["output_sha256"] for row in baseline_rows} != {
            row["output_sha256"] for row in adaptive_rows
        }:
            raise SummaryError(f"output hashes differ for {app}")
        if build_features(baseline, app) != build_features(adaptive, app):
            raise SummaryError(f"allocator feature sets differ for {app}")
        baseline_wall = [float(row["wall_seconds"]) for row in baseline_rows]
        adaptive_wall = [float(row["wall_seconds"]) for row in adaptive_rows]
        baseline_rss = [int(row["peak_rss_kib"]) for row in baseline_rows]
        adaptive_rss = [int(row["peak_rss_kib"]) for row in adaptive_rows]
        baseline_median = statistics.median(baseline_wall)
        adaptive_median = statistics.median(adaptive_wall)
        ratio = adaptive_median / baseline_median
        low, high = median_ratio_interval(baseline_wall, adaptive_wall, seed=0xA11C + len(rows))
        baseline_rss_median = statistics.median(baseline_rss)
        adaptive_rss_median = statistics.median(adaptive_rss)
        rows.append(
            {
                "app": app,
                "samples_per_mode": len(baseline_rows),
                "features": list(build_features(baseline, app)),
                "output_sha256": baseline_rows[0]["output_sha256"],
                "baseline_median_wall_seconds": baseline_median,
                "adaptive_median_wall_seconds": adaptive_median,
                "wall_overhead_percent": (ratio - 1.0) * 100.0,
                "bootstrap_95_percent_wall_overhead": [
                    (low - 1.0) * 100.0,
                    (high - 1.0) * 100.0,
                ],
                "baseline_wall_mad_seconds": statistics.median(
                    abs(value - baseline_median) for value in baseline_wall
                ),
                "adaptive_wall_mad_seconds": statistics.median(
                    abs(value - adaptive_median) for value in adaptive_wall
                ),
                "baseline_median_peak_rss_kib": baseline_rss_median,
                "adaptive_median_peak_rss_kib": adaptive_rss_median,
                "peak_rss_delta_kib": adaptive_rss_median - baseline_rss_median,
                "peak_rss_delta_percent": (
                    (adaptive_rss_median / baseline_rss_median - 1.0) * 100.0
                    if baseline_rss_median
                    else None
                ),
            }
        )
        wall_ratios.append(ratio)
        baseline_wall_sum += baseline_median
        adaptive_wall_sum += adaptive_median
    return {
        "applications": rows,
        "geometric_mean_wall_overhead_percent":
            (math.prod(wall_ratios) ** (1.0 / len(wall_ratios)) - 1.0) * 100.0,
        "aggregate_median_time_weighted_overhead_percent":
            (adaptive_wall_sum / baseline_wall_sum - 1.0) * 100.0,
        "execution_order": "separate sequential off and adaptive campaigns",
    }


def sum_fields(stats: Sequence[dict[str, Any]], *fields: str) -> int:
    return sum(int(row.get(field, 0)) for row in stats for field in fields)


def summarize_classification(coverage: dict[str, Any]) -> dict[str, Any]:
    measurements = list(coverage.get("measurements", ()))
    stats = [row["runtime_lifetime_stats"] for row in measurements]
    if not stats or any(not isinstance(row, dict) for row in stats):
        raise SummaryError("coverage result lacks runtime classifier telemetry")
    sites = sum_fields(stats, "adaptive_site_count")
    short_sites = sum_fields(stats, "adaptive_short_sites")
    long_sites = sum_fields(stats, "adaptive_long_sites")
    cold_sites = sum_fields(stats, "adaptive_cold_sites")
    eligible = sum_fields(stats, "adaptive_eligible_allocations")
    learned_decisions = sum_fields(
        stats,
        "adaptive_short_bypassed_allocations",
        "adaptive_short_routed_allocations",
        "adaptive_long_routed_allocations",
    )
    decisive = sum_fields(stats, "adaptive_short_observations", "adaptive_long_observations")
    predicted = sum_fields(stats, "predictor_tp", "predictor_tn", "predictor_fp", "predictor_fn")
    correct = sum_fields(stats, "predictor_tp", "predictor_tn")
    static_classified = sum_fields(
        stats,
        "static_hint_tp",
        "static_hint_tn",
        "static_hint_fp",
        "static_hint_fn",
    )
    return {
        "applications": [
            {
                "app": row["app"],
                "stats": row["runtime_lifetime_stats"],
            }
            for row in measurements
        ],
        "totals": {
            "sites": sites,
            "cold_sites": cold_sites,
            "short_sites": short_sites,
            "long_sites": long_sites,
            "site_classification_coverage_percent":
                ((short_sites + long_sites) / sites * 100.0 if sites else 0.0),
            "eligible_allocations": eligible,
            "learned_allocation_decisions": learned_decisions,
            "learned_allocation_decision_coverage_percent":
                (learned_decisions / eligible * 100.0 if eligible else 0.0),
            "decisive_runtime_observations": decisive,
            "short_runtime_observations": sum_fields(stats, "adaptive_short_observations"),
            "long_runtime_observations": sum_fields(stats, "adaptive_long_observations"),
            "censored_runtime_observations": sum_fields(stats, "adaptive_censored_observations"),
            "learned_predictions_with_runtime_truth": predicted,
            "conditional_learned_prediction_accuracy_percent":
                (correct / predicted * 100.0 if predicted else None),
            "static_hints_classified": static_classified,
            "static_hints_abstained": sum_fields(stats, "static_hint_abstained"),
            "static_hint_coverage_percent":
                (static_classified / decisive * 100.0 if decisive else 0.0),
            "semantic_phase_advances": sum_fields(stats, "phase_advances"),
            "thp_extent_mappings": sum_fields(stats, "thp_extent_mappings"),
            "thp_advice_attempts": sum_fields(stats, "thp_advice_attempts"),
            "missing_identity_bypasses": sum_fields(stats, "adaptive_missing_identity_bypasses"),
            "site_table_bypasses": sum_fields(stats, "adaptive_site_table_bypasses"),
            "trailer_corruptions": sum_fields(stats, "adaptive_trailer_corruptions"),
        },
        "claim_boundary": (
            "The quick real-app corpus learned only Short sites. It validates marker-free "
            "runtime learning and sampled short prediction, while providing no real-app "
            "Long precision or THP benefit claim."
        ),
    }


def summarize(
    baseline: dict[str, Any], adaptive: dict[str, Any], coverage: dict[str, Any]
) -> dict[str, Any]:
    return {
        "schema_version": 1,
        "performance": summarize_performance(baseline, adaptive),
        "classification": summarize_classification(coverage),
        "source": {
            "performance_off_implementation_sha256": baseline["implementation_sha256"],
            "performance_adaptive_implementation_sha256": adaptive["implementation_sha256"],
            "coverage_implementation_sha256": coverage["implementation_sha256"],
        },
    }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--performance-off", type=pathlib.Path, required=True)
    parser.add_argument("--performance-adaptive", type=pathlib.Path, required=True)
    parser.add_argument("--coverage", type=pathlib.Path, required=True)
    parser.add_argument("--output", type=pathlib.Path, required=True)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    record = summarize(
        load_result(args.performance_off),
        load_result(args.performance_adaptive),
        load_result(args.coverage),
    )
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(record, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(json.dumps(record, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
