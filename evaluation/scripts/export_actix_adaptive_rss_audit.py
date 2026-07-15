#!/usr/bin/env python3
"""Export the fixed-schema Actix adaptive-RSS audit evidence."""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import statistics
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[2]
DEFAULT_RAW_ROOT = ROOT / "evaluation/raw/type-isolation-redb-actix-f5d0c19"
DEFAULT_OUTPUT = (
    ROOT
    / "docs/evidence/type-isolation-primary-suite-20260714"
    / "actix-async-service-direct-adaptive-rss-audit.json"
)
VARIANTS = ("unialloc", "typed_plain", "typeiso_perf")
PHASES = (("warmup", 0), *((f"round-{index:02d}", index) for index in range(1, 6)))


class AuditError(RuntimeError):
    """Raised when retained evidence cannot support the audit."""


def load_object(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise AuditError(f"invalid JSON evidence: {path}") from error
    if not isinstance(value, dict):
        raise AuditError(f"JSON evidence must be an object: {path}")
    return value


def sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def relative(path: Path) -> str:
    try:
        return path.resolve().relative_to(ROOT).as_posix()
    except ValueError as error:
        raise AuditError(f"evidence path is outside the repository: {path}") from error


def positive_number(value: Any, context: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise AuditError(f"{context} must be numeric")
    result = float(value)
    if not math.isfinite(result) or result <= 0.0:
        raise AuditError(f"{context} must be finite and positive")
    return result


def linear_fit(xs: list[float], ys: list[float]) -> dict[str, float]:
    if len(xs) != len(ys) or len(xs) < 2:
        raise AuditError("linear fit needs paired observations")
    x_mean = statistics.fmean(xs)
    y_mean = statistics.fmean(ys)
    x_variance = math.fsum((value - x_mean) ** 2 for value in xs)
    if x_variance <= 0.0:
        raise AuditError("linear fit needs varying iteration counts")
    slope = (
        math.fsum((x - x_mean) * (y - y_mean) for x, y in zip(xs, ys, strict=True))
        / x_variance
    )
    intercept = y_mean - slope * x_mean
    predicted = [intercept + slope * value for value in xs]
    residual = math.fsum(
        (actual - estimate) ** 2 for actual, estimate in zip(ys, predicted, strict=True)
    )
    total = math.fsum((value - y_mean) ** 2 for value in ys)
    return {
        "intercept_mib": intercept,
        "r_squared": 1.0 - residual / total,
        "slope_bytes_per_measured_iteration": slope * 1024.0 * 1024.0,
    }


def collect_rows(raw_root: Path) -> list[dict[str, Any]]:
    run_root = raw_root / "runs/actix_web/async_service_direct"
    rows: list[dict[str, Any]] = []
    for phase_name, round_number in PHASES:
        for variant in VARIANTS:
            base = run_root / phase_name / variant
            measurement_path = base / "measurement.json"
            sample_path = (
                base / "work/target/criterion/async_service_direct/base/sample.json"
            )
            measurement = load_object(measurement_path)
            sample = load_object(sample_path)
            iterations = sample.get("iters")
            if not isinstance(iterations, list) or len(iterations) != 10:
                raise AuditError(f"unexpected Criterion sample shape: {sample_path}")
            iteration_values = [
                positive_number(value, f"{sample_path}.iters") for value in iterations
            ]
            expected_phase = "warmup" if round_number == 0 else "measurement"
            if (
                measurement.get("target_id") != "actix_web"
                or measurement.get("harness_id") != "async_service_direct"
                or measurement.get("variant") != variant
                or measurement.get("round") != round_number
                or measurement.get("phase") != expected_phase
            ):
                raise AuditError(f"measurement identity mismatch: {measurement_path}")
            rows.append(
                {
                    "measurement_path": relative(measurement_path),
                    "measurement_sha256": sha256_file(measurement_path),
                    "measured_iteration_count": int(sum(iteration_values)),
                    "peak_rss_mib": positive_number(
                        measurement.get("peak_rss_mib"),
                        f"{measurement_path}.peak_rss_mib",
                    ),
                    "performance_seconds": positive_number(
                        measurement.get("performance"),
                        f"{measurement_path}.performance",
                    ),
                    "phase": expected_phase,
                    "round": round_number,
                    "sample_path": relative(sample_path),
                    "sample_sha256": sha256_file(sample_path),
                    "variant": variant,
                }
            )
    return rows


def paired_median_ratio(
    rows: list[dict[str, Any]], subject: str, reference: str, field: str
) -> float:
    measured = [row for row in rows if row["round"] > 0]
    by_identity = {(row["round"], row["variant"]): row for row in measured}
    return float(
        statistics.median(
            float(by_identity[(round_number, subject)][field])
            / float(by_identity[(round_number, reference)][field])
            for round_number in range(1, 6)
        )
    )


def export(raw_root: Path, output: Path) -> Path:
    rows = collect_rows(raw_root.resolve())
    summaries: dict[str, Any] = {}
    for variant in VARIANTS:
        measured = [
            row for row in rows if row["variant"] == variant and int(row["round"]) > 0
        ]
        iteration_counts = [int(row["measured_iteration_count"]) for row in measured]
        summaries[variant] = {
            "measured_iteration_count_max": max(iteration_counts),
            "measured_iteration_count_median": float(
                statistics.median(iteration_counts)
            ),
            "measured_iteration_count_min": min(iteration_counts),
            "performance_seconds_median": float(
                statistics.median(float(row["performance_seconds"]) for row in measured)
            ),
            "peak_rss_mib_median": float(
                statistics.median(float(row["peak_rss_mib"]) for row in measured)
            ),
        }
    fit = linear_fit(
        [float(row["measured_iteration_count"]) for row in rows],
        [float(row["peak_rss_mib"]) for row in rows],
    )
    result = {
        "schema_version": 1,
        "audit_id": "actix-async-service-direct-adaptive-rss-20260714",
        "source_commit": "696b1fed9c5b0147c37c70e0808cae5f63a5a4a0",
        "implementation_revision": "f5d0c19c1cc5b56fdac3282d69333dd8c85d4cf2",
        "implementation_sha256": (
            "cab1e580c08e2b16308bae75501716049ba428b040fb04bf305269c9ba9eaf01"
        ),
        "target_id": "actix_web",
        "harness_id": "async_service_direct",
        "criterion_protocol": {
            "measurement_time_seconds": 0.3,
            "sample_size": 10,
            "sampling_mode": "Linear",
            "warmup_time_seconds": 0.1,
        },
        "comparison_ratios": {
            "compiler_route_execution_cost": paired_median_ratio(
                rows, "typed_plain", "unialloc", "performance_seconds"
            ),
            "end_to_end_execution_cost": paired_median_ratio(
                rows, "typeiso_perf", "unialloc", "performance_seconds"
            ),
            "end_to_end_peak_rss_process_observed": paired_median_ratio(
                rows, "typeiso_perf", "unialloc", "peak_rss_mib"
            ),
            "policy_increment_execution_cost": paired_median_ratio(
                rows, "typeiso_perf", "typed_plain", "performance_seconds"
            ),
        },
        "iteration_rss_linear_fit": fit,
        "interpretation": {
            "equal_work_rss_eligible": False,
            "execution_claim": (
                "The large cost is reproducible for this exact microbenchmark and "
                "belongs to the common typed compiler/runtime route."
            ),
            "rss_claim": (
                "Criterion adapts measured iteration counts to variant speed; peak "
                "RSS is a process-volume observation and cannot support an equal-work "
                "memory-efficiency claim."
            ),
        },
        "variant_summaries": summaries,
        "observations": rows,
    }
    output = output.resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    temporary = output.with_suffix(output.suffix + ".tmp")
    temporary.write_text(
        json.dumps(result, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    temporary.replace(output)
    return output


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--raw-root", type=Path, default=DEFAULT_RAW_ROOT)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    try:
        path = export(args.raw_root, args.output)
    except AuditError as error:
        print(f"error: {error}")
        return 2
    print(path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
