#!/usr/bin/env python3
"""Render retained real-program lifetime-prior evidence as slide-ready charts.

The input is a compact, tracked JSON summary.  The canonical outputs are two
editable 16:9 SVG files and two long-form CSV files.  ImageMagick may add 2x
PNG companions.  Python's standard library is the only required dependency.

Stage A remains a force-tracked, process-wide-pressure diagnostic.  Stage B
renders every measured value even when a provenance, backing, or classifier
gate blocks a performance claim; the blocked reasons remain in the SVG and
CSV output.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import html
import json
import math
from pathlib import Path
import re
import shutil
import statistics
import subprocess
import sys
import tempfile
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

SHORT_COLOR = "#0072B2"
LONG_COLOR = "#D55E00"
CENSORED_COLOR = "#999999"
PRECISION_COLOR = "#009E73"
RECALL_COLOR = "#CC79A7"

STATUS_COLORS = {
    "CLAIM ELIGIBLE": "#009E73",
    "PRELIMINARY": "#E69F00",
    "BLOCKED": "#C0392B",
    "THP CAUSAL CLAIM BLOCKED": "#C0392B",
    "PERFORMANCE CLAIM BLOCKED": "#C0392B",
}

TARGET_ORDER = (
    "oxipng",
    "redb",
    "polars",
    "swc",
    "rustpython",
    "actix_web",
)
TARGET_LABELS = {
    "oxipng": "Oxipng",
    "redb": "redb",
    "polars": "Polars",
    "swc": "SWC",
    "rustpython": "RustPython",
    "actix_web": "Actix Web",
}
PRE_GATE_TARGETS = frozenset({"oxipng", "redb"})
TARGET_SOURCE_ROLES = {
    "oxipng": "stage-a-oxipng-redb",
    "redb": "stage-a-oxipng-redb",
    "polars": "stage-a-polars",
    "swc": "stage-a-swc",
    "rustpython": "stage-a-rustpython",
    "actix_web": "stage-a-actix",
}
CANONICAL_SOURCE_ROLES = frozenset(
    {
        *TARGET_SOURCE_ROLES.values(),
        "stage-b-polars",
    }
)
OPTIONAL_SOURCE_ROLES = frozenset({"stage-b-swc-diagnostic"})

ARM_NAMES = (
    "default",
    "adaptive-ordinary-all-unknown",
    "adaptive-ordinary-compiler-prior",
    "adaptive-selective-thp-all-unknown",
    "adaptive-selective-thp-compiler-prior",
)
ARM_LABELS = {
    "default": "Default",
    "adaptive-ordinary-all-unknown": "Runtime learning / ordinary",
    "adaptive-ordinary-compiler-prior": "Compiler prior / ordinary",
    "adaptive-selective-thp-all-unknown": "Runtime learning / selective THP",
    "adaptive-selective-thp-compiler-prior": "Compiler prior / selective THP",
}
ARM_COLORS = {
    "default": "#7F8C8D",
    "adaptive-ordinary-all-unknown": "#56B4E9",
    "adaptive-ordinary-compiler-prior": "#009E73",
    "adaptive-selective-thp-all-unknown": "#E69F00",
    "adaptive-selective-thp-compiler-prior": "#D55E00",
}
THP_ARMS = frozenset(
    {
        "adaptive-selective-thp-all-unknown",
        "adaptive-selective-thp-compiler-prior",
    }
)
COMPILER_ARMS = frozenset(
    {
        "adaptive-ordinary-compiler-prior",
        "adaptive-selective-thp-compiler-prior",
    }
)
ALL_UNKNOWN_ARMS = frozenset(set(ARM_NAMES) - set(COMPILER_ARMS))

COMPARISON_SPECS = (
    (
        "runtime_only_vs_default",
        "default",
        "adaptive-ordinary-all-unknown",
        "Runtime ordinary / Default",
    ),
    (
        "compiler_prior_vs_runtime_only",
        "adaptive-ordinary-all-unknown",
        "adaptive-ordinary-compiler-prior",
        "Compiler prior / Runtime ordinary",
    ),
    (
        "runtime_thp_vs_runtime_ordinary",
        "adaptive-ordinary-all-unknown",
        "adaptive-selective-thp-all-unknown",
        "Runtime THP / Runtime ordinary",
    ),
    (
        "compiler_prior_thp_vs_runtime_thp",
        "adaptive-selective-thp-all-unknown",
        "adaptive-selective-thp-compiler-prior",
        "Compiler prior / Runtime THP",
    ),
    (
        "selective_thp_vs_compiler_ordinary",
        "adaptive-ordinary-compiler-prior",
        "adaptive-selective-thp-compiler-prior",
        "Compiler THP / Compiler ordinary",
    ),
    (
        "compiler_thp_end_to_end_vs_default",
        "default",
        "adaptive-selective-thp-compiler-prior",
        "Compiler THP / Default",
    ),
)
THP_COMPARISON_NAMES = frozenset(
    {
        "runtime_thp_vs_runtime_ordinary",
        "compiler_prior_thp_vs_runtime_thp",
        "selective_thp_vs_compiler_ordinary",
        "compiler_thp_end_to_end_vs_default",
    }
)

STAGE_A_CSV_FIELDS = (
    "target_id",
    "target",
    "allocator_revision",
    "source_commit",
    "exact_layout_gate",
    "wall_seconds",
    "runtime_exact_site_count",
    "category",
    "requested_bytes",
    "denominator_requested_bytes",
    "share",
    "long_gate_passed",
    "short_gate_passed",
    "matched_prior_site_count",
    "prior_long_requested_bytes",
    "prior_short_requested_bytes",
    "prior_censored_requested_bytes",
    "prior_byte_precision",
    "long_byte_recall",
    "caveat",
)

STAGE_B_CSV_FIELDS = (
    "row_type",
    "arm",
    "arm_label",
    "comparison",
    "comparison_label",
    "repeat",
    "baseline_arm",
    "candidate_arm",
    "seconds_per_work_unit",
    "normalized_time_percent",
    "peak_rss_kib",
    "speed_improvement_percent",
    "peak_rss_reduction_percent",
    "diagnostic_eligible",
    "preliminary_eligible",
    "claim_eligible",
    "comparison_status",
    "campaign_status",
    "blocked_reasons",
    "caveat",
)

HEX_40_OR_64 = re.compile(r"(?:[0-9a-f]{40}|[0-9a-f]{64})")
SHA256 = re.compile(r"[0-9a-f]{64}")


class ChartError(RuntimeError):
    """The compact summary cannot support a faithful evidence figure."""


def _mapping(value: Any, label: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise ChartError(f"{label} must be an object")
    return value


def _array(value: Any, label: str, *, nonempty: bool = False) -> list[Any]:
    if not isinstance(value, list) or (nonempty and not value):
        qualifier = "a non-empty" if nonempty else "an"
        raise ChartError(f"{label} must be {qualifier} array")
    return value


def _string(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ChartError(f"{label} must be a non-empty string")
    result = value.strip()
    if any(ord(character) < 32 for character in result):
        raise ChartError(f"{label} contains a control character")
    return result


def _boolean(value: Any, label: str) -> bool:
    if not isinstance(value, bool):
        raise ChartError(f"{label} must be a boolean")
    return value


def _integer(value: Any, label: str, *, positive: bool = False) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ChartError(f"{label} must be an integer")
    minimum = 1 if positive else 0
    if value < minimum:
        qualifier = "positive" if positive else "non-negative"
        raise ChartError(f"{label} must be {qualifier}")
    return value


def _number(value: Any, label: str, *, positive: bool = False) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ChartError(f"{label} must be a number")
    result = float(value)
    if not math.isfinite(result):
        raise ChartError(f"{label} must be finite")
    if positive and result <= 0:
        raise ChartError(f"{label} must be positive")
    return result


def _nullable_number(value: Any, label: str) -> float | None:
    return None if value is None else _number(value, label)


def _nullable_boolean(value: Any, label: str) -> bool | None:
    return None if value is None else _boolean(value, label)


def _close(actual: float, expected: float) -> bool:
    return math.isclose(actual, expected, rel_tol=1e-9, abs_tol=1e-9)


def _percent_improvement(baseline: float, candidate: float) -> float:
    return 100.0 * (baseline - candidate) / baseline


def _median(values: Sequence[float]) -> float:
    if not values:
        raise ChartError("cannot calculate a median from zero values")
    return float(statistics.median(values))


def _validate_sources(raw: Any) -> list[dict[str, str]]:
    rows = _array(raw, "summary.sources", nonempty=True)
    normalized: list[dict[str, str]] = []
    roles: set[str] = set()
    paths: set[str] = set()
    for index, raw_row in enumerate(rows):
        label = f"summary.sources[{index}]"
        row = _mapping(raw_row, label)
        role = _string(row.get("role"), f"{label}.role")
        path = _string(row.get("path"), f"{label}.path")
        digest = _string(row.get("sha256"), f"{label}.sha256")
        if role in roles:
            raise ChartError(f"summary.sources contains duplicate role {role!r}")
        if path in paths:
            raise ChartError(f"summary.sources contains duplicate path {path!r}")
        if Path(path).is_absolute() or ".." in Path(path).parts:
            raise ChartError(f"{label}.path must be repository-relative")
        if SHA256.fullmatch(digest) is None:
            raise ChartError(f"{label}.sha256 must be a lowercase SHA-256 digest")
        roles.add(role)
        paths.add(path)
        normalized.append({"role": role, "path": path, "sha256": digest})
    missing = sorted(CANONICAL_SOURCE_ROLES - roles)
    extra = sorted(roles - CANONICAL_SOURCE_ROLES - OPTIONAL_SOURCE_ROLES)
    if missing or extra:
        raise ChartError(
            "summary.sources must contain the six canonical roles and may add "
            "only stage-b-swc-diagnostic; "
            f"missing={missing}, extra={extra}"
        )
    normalized.sort(key=lambda row: (row["role"], row["path"]))
    return normalized


def _verify_source_files(
    sources: Sequence[Mapping[str, str]], repo_root: Path
) -> None:
    resolved_root = repo_root.expanduser().resolve()
    if not resolved_root.is_dir():
        raise ChartError(f"repository root is not a directory: {resolved_root}")
    for source in sources:
        candidate = (resolved_root / source["path"]).resolve()
        try:
            candidate.relative_to(resolved_root)
        except ValueError as error:
            raise ChartError(
                f"source {source['role']!r} escapes repository root"
            ) from error
        if not candidate.is_file():
            raise ChartError(
                f"source {source['role']!r} is missing under repository root: "
                f"{source['path']}"
            )
        digest = hashlib.sha256(candidate.read_bytes()).hexdigest()
        if digest != source["sha256"]:
            raise ChartError(
                f"source {source['role']!r} SHA-256 mismatch: {source['path']}"
            )


def _validate_stage_a(
    raw: Any, source_roles: frozenset[str]
) -> dict[str, Any]:
    stage = _mapping(raw, "summary.stage_a")
    if (
        _string(stage.get("pressure_basis"), "summary.stage_a.pressure_basis")
        != "process_wide_requested_generation_bytes"
    ):
        raise ChartError("summary.stage_a.pressure_basis is not the Stage-A clock")
    if _boolean(stage.get("force_track_all"), "summary.stage_a.force_track_all") is not True:
        raise ChartError("summary.stage_a.force_track_all must be true")
    long_minimum = _integer(
        stage.get("minimum_long_requested_bytes"),
        "summary.stage_a.minimum_long_requested_bytes",
        positive=True,
    )
    short_minimum = _integer(
        stage.get("minimum_confirmed_short_requested_bytes"),
        "summary.stage_a.minimum_confirmed_short_requested_bytes",
        positive=True,
    )
    rows = _array(stage.get("targets"), "summary.stage_a.targets", nonempty=True)
    by_target: dict[str, dict[str, Any]] = {}
    for index, raw_row in enumerate(rows):
        label = f"summary.stage_a.targets[{index}]"
        row = _mapping(raw_row, label)
        target_id = _string(row.get("target_id"), f"{label}.target_id")
        if target_id not in TARGET_LABELS:
            raise ChartError(f"{label}.target_id is unsupported: {target_id!r}")
        if target_id in by_target:
            raise ChartError(f"summary.stage_a.targets contains duplicate {target_id!r}")
        source_role = _string(row.get("source_role"), f"{label}.source_role")
        expected_source_role = TARGET_SOURCE_ROLES[target_id]
        if source_role != expected_source_role or source_role not in source_roles:
            raise ChartError(
                f"{label}.source_role must bind {target_id!r} to "
                f"{expected_source_role!r}"
            )
        allocator_revision = _string(
            row.get("allocator_revision"), f"{label}.allocator_revision"
        )
        source_commit = _string(row.get("source_commit"), f"{label}.source_commit")
        if HEX_40_OR_64.fullmatch(allocator_revision) is None:
            raise ChartError(f"{label}.allocator_revision must be a lowercase commit digest")
        if HEX_40_OR_64.fullmatch(source_commit) is None:
            raise ChartError(f"{label}.source_commit must be a lowercase commit digest")
        exact_layout_gate = _boolean(
            row.get("exact_layout_gate"), f"{label}.exact_layout_gate"
        )
        if exact_layout_gate != (target_id not in PRE_GATE_TARGETS):
            raise ChartError(f"{label}.exact_layout_gate disagrees with the retained campaign boundary")
        wall_seconds = _number(row.get("wall_seconds"), f"{label}.wall_seconds", positive=True)
        if not 20.0 <= wall_seconds <= 60.0:
            raise ChartError(f"{label}.wall_seconds lies outside the 20--60 second Stage-A window")
        runtime_sites = _integer(
            row.get("runtime_exact_site_count"),
            f"{label}.runtime_exact_site_count",
            positive=True,
        )
        byte_fields = (
            "allocation_requested_bytes",
            "long_requested_bytes",
            "short_requested_bytes",
            "censored_requested_bytes",
            "live_right_censored_requested_bytes",
            "confirmed_short_requested_bytes",
        )
        counts = {
            field: _integer(row.get(field), f"{label}.{field}")
            for field in byte_fields
        }
        if counts["allocation_requested_bytes"] <= 0:
            raise ChartError(f"{label}.allocation_requested_bytes must be positive")
        if counts["allocation_requested_bytes"] != (
            counts["long_requested_bytes"]
            + counts["short_requested_bytes"]
            + counts["censored_requested_bytes"]
            + counts["live_right_censored_requested_bytes"]
        ):
            raise ChartError(f"{label} requested-byte outcome accounting is inconsistent")
        if counts["confirmed_short_requested_bytes"] > counts["short_requested_bytes"]:
            raise ChartError(f"{label} confirmed Short bytes exceed Short bytes")
        long_gate = _boolean(row.get("long_gate_passed"), f"{label}.long_gate_passed")
        short_gate = _boolean(row.get("short_gate_passed"), f"{label}.short_gate_passed")
        if long_gate != (counts["long_requested_bytes"] >= long_minimum):
            raise ChartError(f"{label}.long_gate_passed is inconsistent")
        if short_gate != (
            counts["confirmed_short_requested_bytes"] >= short_minimum
        ):
            raise ChartError(f"{label}.short_gate_passed is inconsistent")

        prior_raw = _mapping(row.get("prior_quality"), f"{label}.prior_quality")
        matched_sites = _integer(
            prior_raw.get("matched_site_count"),
            f"{label}.prior_quality.matched_site_count",
            positive=True,
        )
        if matched_sites > runtime_sites:
            raise ChartError(f"{label} matched prior sites exceed runtime sites")
        prior = {
            field: _integer(
                prior_raw.get(field), f"{label}.prior_quality.{field}"
            )
            for field in (
                "long_requested_bytes",
                "short_requested_bytes",
                "censored_requested_bytes",
            )
        }
        if prior["long_requested_bytes"] > counts["long_requested_bytes"]:
            raise ChartError(f"{label} prior Long bytes exceed runtime Long bytes")
        if prior["short_requested_bytes"] > counts["short_requested_bytes"]:
            raise ChartError(f"{label} prior Short bytes exceed runtime Short bytes")
        if prior["censored_requested_bytes"] > counts["censored_requested_bytes"]:
            raise ChartError(f"{label} prior censored bytes exceed runtime censored bytes")
        decisive_prior = prior["long_requested_bytes"] + prior["short_requested_bytes"]
        if decisive_prior <= 0:
            raise ChartError(f"{label}.prior_quality must contain decisive requested bytes")
        if counts["long_requested_bytes"] <= 0:
            raise ChartError(f"{label} must contain positive runtime Long bytes")
        by_target[target_id] = {
            "target_id": target_id,
            "target": TARGET_LABELS[target_id],
            "source_role": source_role,
            "allocator_revision": allocator_revision,
            "source_commit": source_commit,
            "exact_layout_gate": exact_layout_gate,
            "wall_seconds": wall_seconds,
            "runtime_exact_site_count": runtime_sites,
            **counts,
            "completed_requested_bytes": (
                counts["long_requested_bytes"]
                + counts["short_requested_bytes"]
                + counts["censored_requested_bytes"]
            ),
            "long_gate_passed": long_gate,
            "short_gate_passed": short_gate,
            "prior_quality": {
                "matched_site_count": matched_sites,
                **prior,
                "byte_precision": prior["long_requested_bytes"] / decisive_prior,
                "long_byte_recall": prior["long_requested_bytes"]
                / counts["long_requested_bytes"],
            },
        }
    if set(by_target) != set(TARGET_ORDER):
        missing = sorted(set(TARGET_ORDER) - set(by_target))
        extra = sorted(set(by_target) - set(TARGET_ORDER))
        raise ChartError(
            f"summary.stage_a.targets must contain the six-program set; missing={missing}, extra={extra}"
        )
    return {
        "pressure_basis": "process_wide_requested_generation_bytes",
        "force_track_all": True,
        "minimum_long_requested_bytes": long_minimum,
        "minimum_confirmed_short_requested_bytes": short_minimum,
        "targets": [by_target[target] for target in TARGET_ORDER],
    }


def _reason_array(value: Any, label: str) -> list[str]:
    rows = _array(value, label)
    return [_string(row, f"{label}[{index}]") for index, row in enumerate(rows)]


def _validate_gate(
    raw: Any,
    label: str,
    *,
    required_children: Sequence[str] = (),
) -> dict[str, Any]:
    gate = _mapping(raw, label)
    passed = _boolean(gate.get("passed"), f"{label}.passed")
    reasons: list[str] = []
    child_results: dict[str, dict[str, Any]] = {}
    if "reasons" in gate:
        reasons.extend(_reason_array(gate.get("reasons"), f"{label}.reasons"))
    for child_name in required_children:
        if child_name not in gate:
            raise ChartError(f"{label}.{child_name} is required")
    for child_name in ("backing", "allocator_activity"):
        if child_name in gate:
            child = _mapping(gate.get(child_name), f"{label}.{child_name}")
            child_passed = _boolean(child.get("passed"), f"{label}.{child_name}.passed")
            child_reasons: list[str] = []
            if "reasons" in child:
                child_reasons = _reason_array(
                    child.get("reasons"), f"{label}.{child_name}.reasons"
                )
                reasons.extend(child_reasons)
            if child_passed and child_reasons:
                raise ChartError(
                    f"{label}.{child_name} passes while retaining failure reasons"
                )
            child_results[child_name] = {
                "passed": child_passed,
                "reasons": child_reasons,
            }
    if required_children:
        expected_passed = all(
            child_results[child_name]["passed"] for child_name in required_children
        )
        if passed != expected_passed:
            raise ChartError(
                f"{label}.passed must equal the conjunction of backing and "
                "allocator_activity"
            )
    if passed and reasons:
        raise ChartError(f"{label} passes while retaining failure reasons")
    return {
        "passed": passed,
        "reasons": reasons,
        "backing_reasons": child_results.get("backing", {}).get("reasons", []),
        "allocator_activity_reasons": child_results.get(
            "allocator_activity", {}
        ).get("reasons", []),
    }


def _sample_diagnostic_eligibility(sample: Mapping[str, Any]) -> bool:
    return bool(
        sample["backing_gate"]["passed"]
        and sample["duration_eligible"]
        and sample["cross_arm_output_equivalent"]
        and (
            sample["full_executed_prior_coverage"] is True
            and sample["compiler_prior_effect_observed"] is True
            if sample["arm"] in COMPILER_ARMS
            else True
        )
        and (
            sample["all_unknown_control_clean"] is True
            if sample["arm"] in ALL_UNKNOWN_ARMS
            else True
        )
    )


def _validate_stage_b_sample(
    raw: Any, label: str, *, repeats: int
) -> dict[str, Any]:
    row = _mapping(raw, label)
    arm = _string(row.get("arm"), f"{label}.arm")
    if arm not in ARM_NAMES:
        raise ChartError(f"{label}.arm is unsupported: {arm!r}")
    repeat = _integer(row.get("repeat"), f"{label}.repeat")
    if repeat >= repeats:
        raise ChartError(f"{label}.repeat lies outside the campaign")
    seconds = _number(
        row.get("primary_metric_value_seconds"),
        f"{label}.primary_metric_value_seconds",
        positive=True,
    )
    procfs = _mapping(row.get("procfs"), f"{label}.procfs")
    peak_rss = _number(
        procfs.get("peak_rss_kib"), f"{label}.procfs.peak_rss_kib", positive=True
    )
    duration_eligible = _boolean(
        row.get("duration_eligible"), f"{label}.duration_eligible"
    )
    output_equivalent = _boolean(
        row.get("cross_arm_output_equivalent"),
        f"{label}.cross_arm_output_equivalent",
    )
    performance_eligible = _boolean(
        row.get("performance_claim_eligible"),
        f"{label}.performance_claim_eligible",
    )
    full_coverage = _nullable_boolean(
        row.get("full_executed_prior_coverage"),
        f"{label}.full_executed_prior_coverage",
    )
    prior_effect = _nullable_boolean(
        row.get("compiler_prior_effect_observed"),
        f"{label}.compiler_prior_effect_observed",
    )
    control_clean = _nullable_boolean(
        row.get("all_unknown_control_clean"),
        f"{label}.all_unknown_control_clean",
    )
    if arm in COMPILER_ARMS and (full_coverage is None or prior_effect is None):
        raise ChartError(f"{label} compiler-prior sample lacks prior gates")
    if arm in ALL_UNKNOWN_ARMS and control_clean is None:
        raise ChartError(f"{label} all-Unknown sample lacks its contamination gate")
    gate_name = "thp_pair_backing_gate" if arm in THP_ARMS else "ordinary_backing_gate"
    backing_gate = _validate_gate(
        row.get(gate_name),
        f"{label}.{gate_name}",
        required_children=("backing", "allocator_activity")
        if arm in THP_ARMS
        else (),
    )
    normalized = {
        "arm": arm,
        "repeat": repeat,
        "seconds": seconds,
        "peak_rss_kib": peak_rss,
        "duration_eligible": duration_eligible,
        "cross_arm_output_equivalent": output_equivalent,
        "performance_claim_eligible": performance_eligible,
        "full_executed_prior_coverage": full_coverage,
        "compiler_prior_effect_observed": prior_effect,
        "all_unknown_control_clean": control_clean,
        "backing_gate": backing_gate,
    }
    diagnostic = _sample_diagnostic_eligibility(normalized)
    if performance_eligible and not diagnostic:
        raise ChartError(f"{label} is performance eligible while a required gate fails")
    return normalized


def _validate_comparison(
    raw: Any,
    label: str,
    *,
    name: str,
    display: str,
    baseline_arm: str,
    candidate_arm: str,
    repeats: int,
    samples: Mapping[tuple[str, int], Mapping[str, Any]],
) -> dict[str, Any]:
    row = _mapping(raw, label)
    if _string(row.get("baseline_arm"), f"{label}.baseline_arm") != baseline_arm:
        raise ChartError(f"{label}.baseline_arm disagrees with the registered comparison")
    if _string(row.get("candidate_arm"), f"{label}.candidate_arm") != candidate_arm:
        raise ChartError(f"{label}.candidate_arm disagrees with the registered comparison")
    pairs_raw = _array(row.get("pairs"), f"{label}.pairs", nonempty=True)
    if len(pairs_raw) != repeats:
        raise ChartError(f"{label}.pairs must contain one row per repeat")
    pairs: list[dict[str, Any]] = []
    observed_repeats: set[int] = set()
    for index, raw_pair in enumerate(pairs_raw):
        pair_label = f"{label}.pairs[{index}]"
        pair = _mapping(raw_pair, pair_label)
        repeat = _integer(pair.get("repeat"), f"{pair_label}.repeat")
        if repeat >= repeats or repeat in observed_repeats:
            raise ChartError(f"{pair_label}.repeat is duplicate or outside the campaign")
        observed_repeats.add(repeat)
        baseline = samples[(baseline_arm, repeat)]
        candidate = samples[(candidate_arm, repeat)]
        baseline_seconds = _number(
            pair.get("baseline_seconds"), f"{pair_label}.baseline_seconds", positive=True
        )
        candidate_seconds = _number(
            pair.get("candidate_seconds"), f"{pair_label}.candidate_seconds", positive=True
        )
        baseline_rss = _number(
            pair.get("baseline_peak_rss_kib"),
            f"{pair_label}.baseline_peak_rss_kib",
            positive=True,
        )
        candidate_rss = _number(
            pair.get("candidate_peak_rss_kib"),
            f"{pair_label}.candidate_peak_rss_kib",
            positive=True,
        )
        if not _close(baseline_seconds, float(baseline["seconds"])):
            raise ChartError(f"{pair_label}.baseline_seconds disagrees with the sample")
        if not _close(candidate_seconds, float(candidate["seconds"])):
            raise ChartError(f"{pair_label}.candidate_seconds disagrees with the sample")
        if not _close(baseline_rss, float(baseline["peak_rss_kib"])):
            raise ChartError(f"{pair_label}.baseline_peak_rss_kib disagrees with the sample")
        if not _close(candidate_rss, float(candidate["peak_rss_kib"])):
            raise ChartError(f"{pair_label}.candidate_peak_rss_kib disagrees with the sample")
        improvement = _number(
            pair.get("percent_improvement"), f"{pair_label}.percent_improvement"
        )
        rss_reduction = _number(
            pair.get("peak_rss_percent_reduction"),
            f"{pair_label}.peak_rss_percent_reduction",
        )
        expected_improvement = _percent_improvement(
            baseline_seconds, candidate_seconds
        )
        expected_rss = _percent_improvement(baseline_rss, candidate_rss)
        if not _close(improvement, expected_improvement):
            raise ChartError(f"{pair_label}.percent_improvement is inconsistent")
        if not _close(rss_reduction, expected_rss):
            raise ChartError(f"{pair_label}.peak_rss_percent_reduction is inconsistent")

        backing_eligible = _boolean(
            pair.get("backing_eligible"), f"{pair_label}.backing_eligible"
        )
        compiler_eligible = _boolean(
            pair.get("compiler_prior_effect_eligible"),
            f"{pair_label}.compiler_prior_effect_eligible",
        )
        controls_eligible = _boolean(
            pair.get("runtime_controls_eligible"),
            f"{pair_label}.runtime_controls_eligible",
        )
        expected_backing = bool(
            baseline["backing_gate"]["passed"]
            and candidate["backing_gate"]["passed"]
        )
        expected_compiler = all(
            sample["full_executed_prior_coverage"] is True
            and sample["compiler_prior_effect_observed"] is True
            for sample in (baseline, candidate)
            if sample["arm"] in COMPILER_ARMS
        )
        expected_controls = all(
            sample["all_unknown_control_clean"] is True
            for sample in (baseline, candidate)
            if sample["arm"] in ALL_UNKNOWN_ARMS
        )
        if backing_eligible != expected_backing:
            raise ChartError(f"{pair_label}.backing_eligible is inconsistent")
        if compiler_eligible != expected_compiler:
            raise ChartError(f"{pair_label}.compiler_prior_effect_eligible is inconsistent")
        if controls_eligible != expected_controls:
            raise ChartError(f"{pair_label}.runtime_controls_eligible is inconsistent")
        expected_diagnostic = bool(
            expected_backing
            and baseline["duration_eligible"]
            and candidate["duration_eligible"]
            and baseline["cross_arm_output_equivalent"]
            and candidate["cross_arm_output_equivalent"]
            and expected_compiler
            and expected_controls
        )
        expected_preliminary = bool(
            expected_diagnostic
            and baseline["performance_claim_eligible"]
            and candidate["performance_claim_eligible"]
        )
        expected_claim = expected_preliminary and repeats >= 4
        diagnostic = _boolean(
            pair.get("diagnostic_eligible"), f"{pair_label}.diagnostic_eligible"
        )
        preliminary = _boolean(
            pair.get("preliminary_eligible"), f"{pair_label}.preliminary_eligible"
        )
        claim = _boolean(pair.get("claim_eligible"), f"{pair_label}.claim_eligible")
        if diagnostic != expected_diagnostic:
            raise ChartError(f"{pair_label}.diagnostic_eligible is inconsistent")
        if preliminary != expected_preliminary:
            raise ChartError(f"{pair_label}.preliminary_eligible is inconsistent")
        if claim != expected_claim:
            raise ChartError(f"{pair_label}.claim_eligible is inconsistent")
        pairs.append(
            {
                "repeat": repeat,
                "speed_improvement": improvement,
                "rss_reduction": rss_reduction,
                "diagnostic_eligible": diagnostic,
                "preliminary_eligible": preliminary,
                "claim_eligible": claim,
            }
        )
    pairs.sort(key=lambda pair: pair["repeat"])

    def validate_count(field: str, key: str) -> None:
        expected = sum(1 for pair in pairs if pair[key])
        actual = _integer(row.get(field), f"{label}.{field}")
        if actual != expected:
            raise ChartError(f"{label}.{field} is inconsistent")

    validate_count("diagnostic_eligible_pair_count", "diagnostic_eligible")
    validate_count("preliminary_eligible_pair_count", "preliminary_eligible")
    validate_count("claim_eligible_pair_count", "claim_eligible")

    diagnostic_pairs = [pair for pair in pairs if pair["diagnostic_eligible"]]
    preliminary_pairs = [pair for pair in pairs if pair["preliminary_eligible"]]
    claim_pairs = [pair for pair in pairs if pair["claim_eligible"]]

    def validate_optional_median(
        field: str, selected: Sequence[Mapping[str, Any]], value_key: str
    ) -> float | None:
        actual = _nullable_number(row.get(field), f"{label}.{field}")
        expected = (
            _median([float(pair[value_key]) for pair in selected])
            if selected
            else None
        )
        if actual is None and expected is None:
            return None
        if actual is None or expected is None or not _close(actual, expected):
            raise ChartError(f"{label}.{field} is inconsistent")
        return actual

    speed_median = validate_optional_median(
        "median_percent_improvement", diagnostic_pairs, "speed_improvement"
    )
    rss_median = validate_optional_median(
        "median_peak_rss_percent_reduction", diagnostic_pairs, "rss_reduction"
    )
    validate_optional_median(
        "preliminary_median_percent_improvement",
        preliminary_pairs,
        "speed_improvement",
    )
    validate_optional_median(
        "claim_median_percent_improvement", claim_pairs, "speed_improvement"
    )
    aggregate_diagnostic = _boolean(
        row.get("diagnostic_eligible"), f"{label}.diagnostic_eligible"
    )
    aggregate_preliminary = _boolean(
        row.get("preliminary_eligible"), f"{label}.preliminary_eligible"
    )
    aggregate_claim = _boolean(
        row.get("claim_eligible"), f"{label}.claim_eligible"
    )
    if aggregate_diagnostic != (len(diagnostic_pairs) == repeats):
        raise ChartError(f"{label}.diagnostic_eligible is inconsistent")
    if aggregate_preliminary != (len(preliminary_pairs) == repeats):
        raise ChartError(f"{label}.preliminary_eligible is inconsistent")
    if aggregate_claim != (len(claim_pairs) == repeats):
        raise ChartError(f"{label}.claim_eligible is inconsistent")
    if aggregate_claim:
        comparison_status = "CLAIM ELIGIBLE"
    elif repeats < 4 and aggregate_preliminary:
        comparison_status = "PRELIMINARY"
    else:
        comparison_status = "BLOCKED"
    return {
        "name": name,
        "display": display,
        "baseline_arm": baseline_arm,
        "candidate_arm": candidate_arm,
        "pairs": pairs,
        "median_speed_improvement": speed_median,
        "median_rss_reduction": rss_median,
        "diagnostic_eligible": aggregate_diagnostic,
        "preliminary_eligible": aggregate_preliminary,
        "claim_eligible": aggregate_claim,
        "status": comparison_status,
    }


def _canonical_gate_reason(reason: str) -> str:
    if "no resident AnonHugePages" in reason:
        return "no resident AnonHugePages in measured samples"
    if "ordinary run has resident AnonHugePages" in reason:
        return "ordinary run has resident AnonHugePages"
    return re.sub(r"\bsample \d+\b", "sample", reason)


def _blocked_reasons(
    samples: Sequence[Mapping[str, Any]], comparisons: Sequence[Mapping[str, Any]]
) -> list[str]:
    backing_causes: dict[str, set[tuple[str, int]]] = {}
    activity_causes: dict[str, set[tuple[str, int]]] = {}
    ordinary_causes: dict[str, set[tuple[str, int]]] = {}
    provenance_reasons: set[str] = set()
    for sample in samples:
        prefix = f"repeat {sample['repeat']} {ARM_LABELS[sample['arm']]}"
        if not sample["duration_eligible"]:
            provenance_reasons.add(f"{prefix}: duration outside 20--60 seconds")
        if not sample["cross_arm_output_equivalent"]:
            provenance_reasons.add(f"{prefix}: cross-arm output differs")
        if sample["arm"] in COMPILER_ARMS:
            if sample["full_executed_prior_coverage"] is not True:
                provenance_reasons.add(f"{prefix}: exact-prior coverage incomplete")
            if sample["compiler_prior_effect_observed"] is not True:
                provenance_reasons.add(f"{prefix}: no executed compiler-prior effect")
        if (
            sample["arm"] in ALL_UNKNOWN_ARMS
            and sample["all_unknown_control_clean"] is not True
        ):
            provenance_reasons.add(f"{prefix}: all-Unknown control contaminated")
        if not sample["backing_gate"]["passed"]:
            context = (sample["arm"], sample["repeat"])
            if sample["arm"] in THP_ARMS:
                for reason in sample["backing_gate"]["backing_reasons"]:
                    backing_causes.setdefault(
                        _canonical_gate_reason(reason), set()
                    ).add(context)
                for reason in sample["backing_gate"][
                    "allocator_activity_reasons"
                ]:
                    activity_causes.setdefault(
                        _canonical_gate_reason(reason), set()
                    ).add(context)
                if (
                    not sample["backing_gate"]["backing_reasons"]
                    and not sample["backing_gate"]["allocator_activity_reasons"]
                ):
                    backing_causes.setdefault(
                        "resident backing or allocator-activity gate failed", set()
                    ).add(context)
            else:
                for reason in sample["backing_gate"]["reasons"] or [
                    "ordinary backing gate failed"
                ]:
                    ordinary_causes.setdefault(
                        _canonical_gate_reason(reason), set()
                    ).add(context)
    reasons: list[str] = []
    for prefix, groups in (
        ("THP backing", backing_causes),
        ("THP allocator activity", activity_causes),
        ("Ordinary backing", ordinary_causes),
    ):
        for reason in sorted(groups):
            run_count = len(groups[reason])
            reasons.append(
                f"{prefix}: {reason} ({run_count} measured run"
                f"{'s' if run_count != 1 else ''})"
            )
    reasons.extend(sorted(provenance_reasons))
    for comparison in comparisons:
        if not comparison["diagnostic_eligible"]:
            reasons.append(f"{comparison['display']}: diagnostic gate failed")
        elif not comparison["preliminary_eligible"]:
            reasons.append(f"{comparison['display']}: performance gate failed")
    return reasons


def _validate_stage_b(raw: Any) -> dict[str, Any]:
    stage = _mapping(raw, "summary.stage_b")
    source_role = _string(
        stage.get("source_role"), "summary.stage_b.source_role"
    )
    if source_role != "stage-b-polars":
        raise ChartError("summary.stage_b.source_role must be stage-b-polars")
    if _string(stage.get("target_id"), "summary.stage_b.target_id") != "polars":
        raise ChartError("summary.stage_b.target_id must be polars")
    if (
        _string(stage.get("pressure_basis"), "summary.stage_b.pressure_basis")
        != "eligible_exact_site_payload_capacity"
    ):
        raise ChartError("summary.stage_b.pressure_basis is not the production clock")
    if _boolean(stage.get("force_track_all"), "summary.stage_b.force_track_all") is not False:
        raise ChartError("summary.stage_b.force_track_all must be false")
    repeats = _integer(stage.get("repeats"), "summary.stage_b.repeats", positive=True)
    if repeats % 2 != 0:
        raise ChartError("summary.stage_b.repeats must be even for balanced order")
    presentation_eligible = _boolean(
        stage.get("presentation_repeat_count_eligible"),
        "summary.stage_b.presentation_repeat_count_eligible",
    )
    if presentation_eligible != (repeats >= 4):
        raise ChartError("summary.stage_b.presentation_repeat_count_eligible is inconsistent")
    arms = [
        _string(value, f"summary.stage_b.arms[{index}]")
        for index, value in enumerate(
            _array(stage.get("arms"), "summary.stage_b.arms", nonempty=True)
        )
    ]
    if arms != list(ARM_NAMES):
        raise ChartError("summary.stage_b.arms must match the registered five-arm order")

    raw_samples = _array(stage.get("samples"), "summary.stage_b.samples", nonempty=True)
    if len(raw_samples) != repeats * len(ARM_NAMES):
        raise ChartError("summary.stage_b.samples must contain every arm and repeat")
    samples: list[dict[str, Any]] = []
    samples_by_key: dict[tuple[str, int], dict[str, Any]] = {}
    for index, raw_sample in enumerate(raw_samples):
        sample = _validate_stage_b_sample(
            raw_sample, f"summary.stage_b.samples[{index}]", repeats=repeats
        )
        key = (sample["arm"], sample["repeat"])
        if key in samples_by_key:
            raise ChartError(f"summary.stage_b.samples contains duplicate {key!r}")
        samples_by_key[key] = sample
        samples.append(sample)
    expected_keys = {
        (arm, repeat) for arm in ARM_NAMES for repeat in range(repeats)
    }
    if set(samples_by_key) != expected_keys:
        raise ChartError("summary.stage_b.samples does not cover every arm/repeat key")
    samples.sort(key=lambda sample: (ARM_NAMES.index(sample["arm"]), sample["repeat"]))

    comparisons_raw = _mapping(
        stage.get("paired_comparisons"), "summary.stage_b.paired_comparisons"
    )
    expected_comparisons = {spec[0] for spec in COMPARISON_SPECS}
    if set(comparisons_raw) != expected_comparisons:
        raise ChartError("summary.stage_b.paired_comparisons must contain the registered six comparisons")
    comparisons = [
        _validate_comparison(
            comparisons_raw[name],
            f"summary.stage_b.paired_comparisons.{name}",
            name=name,
            display=display,
            baseline_arm=baseline,
            candidate_arm=candidate,
            repeats=repeats,
            samples=samples_by_key,
        )
        for name, baseline, candidate, display in COMPARISON_SPECS
    ]
    all_thp_backed = _boolean(
        stage.get("all_thp_pairs_backed"), "summary.stage_b.all_thp_pairs_backed"
    )
    expected_thp_backed = all(
        sample["backing_gate"]["passed"]
        for sample in samples
        if sample["arm"] in THP_ARMS
    )
    if all_thp_backed != expected_thp_backed:
        raise ChartError("summary.stage_b.all_thp_pairs_backed is inconsistent")
    claim_scope = _mapping(stage.get("claim_scope"), "summary.stage_b.claim_scope")
    if (
        _boolean(
            claim_scope.get("per_sample_thp_backing_required"),
            "summary.stage_b.claim_scope.per_sample_thp_backing_required",
        )
        is not True
    ):
        raise ChartError("Stage B must require per-sample THP backing")
    if (
        _boolean(
            claim_scope.get("allocator_vma_residency_attribution"),
            "summary.stage_b.claim_scope.allocator_vma_residency_attribution",
        )
        is not False
    ):
        raise ChartError("allocator-VMA residency attribution must remain false")
    causality_scope = _string(
        claim_scope.get("thp_causality_scope"),
        "summary.stage_b.claim_scope.thp_causality_scope",
    )
    if causality_scope != "process-level backing correlated with positive allocator activity":
        raise ChartError("summary.stage_b.claim_scope.thp_causality_scope changed")

    if repeats >= 4 and all(comparison["claim_eligible"] for comparison in comparisons):
        status = "CLAIM ELIGIBLE"
    elif repeats < 4 and all(
        comparison["preliminary_eligible"] for comparison in comparisons
    ):
        status = "PRELIMINARY"
    elif any(
        comparison["name"] in THP_COMPARISON_NAMES
        and comparison["status"] == "BLOCKED"
        for comparison in comparisons
    ):
        status = "THP CAUSAL CLAIM BLOCKED"
    else:
        status = "PERFORMANCE CLAIM BLOCKED"
    reasons = _blocked_reasons(samples, comparisons)
    if status == "PRELIMINARY":
        reasons = ["presentation requires at least four paired repeats", *reasons]
    if status == "CLAIM ELIGIBLE" and reasons:
        raise ChartError("claim-eligible Stage B evidence retains blocked reasons")

    arm_rows: list[dict[str, Any]] = []
    default_median = _median(
        [sample["seconds"] for sample in samples if sample["arm"] == "default"]
    )
    for arm in ARM_NAMES:
        arm_samples = [sample for sample in samples if sample["arm"] == arm]
        median_seconds = _median([sample["seconds"] for sample in arm_samples])
        arm_rows.append(
            {
                "arm": arm,
                "display": ARM_LABELS[arm],
                "color": ARM_COLORS[arm],
                "samples": arm_samples,
                "median_seconds": median_seconds,
                "normalized_median_percent": 100.0 * median_seconds / default_median,
                "median_peak_rss_kib": _median(
                    [sample["peak_rss_kib"] for sample in arm_samples]
                ),
            }
        )
    return {
        "target_id": "polars",
        "source_role": source_role,
        "pressure_basis": "eligible_exact_site_payload_capacity",
        "force_track_all": False,
        "repeats": repeats,
        "presentation_repeat_count_eligible": presentation_eligible,
        "samples": samples,
        "arms": arm_rows,
        "comparisons": comparisons,
        "all_thp_pairs_backed": all_thp_backed,
        "status": status,
        "blocked_reasons": reasons,
        "claim_scope": {
            "per_sample_thp_backing_required": True,
            "allocator_vma_residency_attribution": False,
            "thp_causality_scope": causality_scope,
        },
    }


def validate_summary(document: Any) -> dict[str, Any]:
    root = _mapping(document, "summary")
    if _integer(root.get("schema_version"), "summary.schema_version", positive=True) != 1:
        raise ChartError("summary.schema_version must be 1")
    if (
        _string(root.get("source"), "summary.source")
        != "lifetime-prior-real-program-evidence-v1"
    ):
        raise ChartError("summary.source is unsupported")
    sources = _validate_sources(root.get("sources"))
    source_roles = frozenset(source["role"] for source in sources)
    return {
        "sources": sources,
        "stage_a": _validate_stage_a(root.get("stage_a"), source_roles),
        "stage_b": _validate_stage_b(root.get("stage_b")),
    }


def _escape(value: object) -> str:
    return html.escape(str(value), quote=True)


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


def _fmt_percent(value: float) -> str:
    return f"{value * 100.0:.2f}%"


def _fmt_signed_percent(value: float) -> str:
    return f"{value:+.2f}%"


def _fmt_mib(value: int) -> str:
    return f"{value / 1048576.0:.2f} MiB"


def _fmt_seconds(value: float) -> str:
    if value < 0.001:
        return f"{value * 1_000_000.0:.2f} us/work"
    if value < 1.0:
        return f"{value * 1000.0:.3f} ms/work"
    return f"{value:.3f} s/work"


def _truncate(value: str, limit: int) -> str:
    return value if len(value) <= limit else value[: limit - 3].rstrip() + "..."


def _svg_document(
    *, title: str, description: str, body: Sequence[str], metadata: str
) -> str:
    return "\n".join(
        [
            '<?xml version="1.0" encoding="UTF-8"?>',
            f'<svg xmlns="http://www.w3.org/2000/svg" width="{WIDTH}" height="{HEIGHT}" '
            f'viewBox="0 0 {WIDTH} {HEIGHT}" role="img" aria-labelledby="chart-title chart-description">',
            f'  <title id="chart-title">{_escape(title)}</title>',
            f'  <desc id="chart-description">{_escape(description)}</desc>',
            "  <style>text { font-family: Arial, Helvetica, sans-serif; }</style>",
            f'  <rect id="background" x="0" y="0" width="{WIDTH}" height="{HEIGHT}" fill="{BACKGROUND}"/>',
            *body,
            f"  <metadata>{_escape(metadata)}</metadata>",
            "</svg>",
            "",
        ]
    )


def render_stage_a(data: Mapping[str, Any], source_digest: str) -> str:
    title = "Rust lifetime priors expose structure in real programs"
    description = (
        "Six pinned 20--60 second Stage-A screens using force tracking and "
        "process-wide requested-byte pressure."
    )
    body = [
        _svg_text(55, 58, title, size=32, weight=700),
        _svg_text(
            55,
            93,
            "Six pinned programs | 20--60 s Stage-A screens | process-wide requested-byte pressure",
            size=17,
            fill=MUTED,
        ),
        _panel(45, 125, 1510, 355, "stage-a-runtime-mix"),
        _svg_text(70, 163, "A | Runtime lifetime mix (completed requested bytes)", size=22, weight=700),
        _svg_text(
            1530,
            163,
            "Long >=8 MiB later pressure | Short <2 MiB | Censored 2--8 MiB",
            size=13.5,
            anchor="end",
            fill=MUTED,
        ),
    ]
    bar_left = 245.0
    bar_width = 650.0
    for index, row in enumerate(data["targets"]):
        y = 205.0 + index * 43.0
        label = row["target"]
        if not row["exact_layout_gate"]:
            label += " [pre-gate triage]"
        body.append(_svg_text(70, y + 5, label, size=15.5, weight=700))
        denominator = float(row["completed_requested_bytes"])
        cursor = bar_left
        for category, field, color in (
            ("Short", "short_requested_bytes", SHORT_COLOR),
            ("Censored", "censored_requested_bytes", CENSORED_COLOR),
            ("Long", "long_requested_bytes", LONG_COLOR),
        ):
            share = row[field] / denominator
            width = bar_width * share
            if width > 0:
                body.append(
                    f'  <rect class="stage-a-{category.casefold()}" x="{cursor:.2f}" '
                    f'y="{y - 13:.2f}" width="{width:.2f}" height="22" fill="{color}">'
                    f'<title>{_escape(row["target"])} {category}: {_fmt_percent(share)}; '
                    f'{_escape(_fmt_mib(row[field]))}</title></rect>'
                )
            cursor += width
        body.append(
            f'  <rect x="{bar_left:.1f}" y="{y - 13:.1f}" width="{bar_width:.1f}" '
            'height="22" fill="none" stroke="#7F8C8D"/>'
        )
        gates = (
            f"Long {'PASS' if row['long_gate_passed'] else 'below gate'} | "
            f"Short {'PASS' if row['short_gate_passed'] else 'below gate'}"
        )
        detail = (
            f"L {_fmt_mib(row['long_requested_bytes'])} | "
            f"S {_fmt_mib(row['short_requested_bytes'])} | "
            f"{row['runtime_exact_site_count']} sites | {gates}"
        )
        body.append(_svg_text(925, y + 5, detail, size=12.5))

    body.extend(
        [
            _panel(45, 500, 1510, 285, "stage-a-prior-quality"),
            _svg_text(70, 538, "B | Exact executed Long-prior quality", size=22, weight=700),
            _svg_text(70, 565, "Current exact-layout-gated rows", size=13.5, fill=MUTED),
            _svg_text(1180, 538, "Byte precision", size=13, weight=700, fill=PRECISION_COLOR),
            _svg_text(1320, 538, "Long-byte recall", size=13, weight=700, fill=RECALL_COLOR),
        ]
    )
    plot_left, plot_width = 355.0, 740.0
    for fraction in (0.0, 0.5, 1.0):
        x = plot_left + plot_width * fraction
        body.extend(
            [
                f'  <line x1="{x:.1f}" y1="570" x2="{x:.1f}" y2="757" stroke="{GRID}"/>',
                _svg_text(x, 774, f"{fraction * 100:.0f}%", size=11.5, anchor="middle", fill=MUTED),
            ]
        )
    quality_rows = [row for row in data["targets"] if row["exact_layout_gate"]]
    for index, row in enumerate(quality_rows):
        y = 605.0 + index * 45.0
        prior = row["prior_quality"]
        tiny = prior["matched_site_count"] == 1
        opacity = "0.42" if tiny else "1.0"
        body.append(_svg_text(70, y + 5, row["target"], size=16, weight=700))
        for offset, metric, color, css_class in (
            (-8.0, prior["byte_precision"], PRECISION_COLOR, "byte-precision"),
            (9.0, prior["long_byte_recall"], RECALL_COLOR, "long-byte-recall"),
        ):
            body.append(
                f'  <rect class="{css_class}" x="{plot_left:.1f}" y="{y + offset - 5:.1f}" '
                f'width="{plot_width * metric:.2f}" height="10" rx="3" fill="{color}" '
                f'opacity="{opacity}"><title>{_escape(row["target"])} {css_class}: '
                f'{_escape(_fmt_percent(metric))}</title></rect>'
            )
        support = (
            f"{_fmt_percent(prior['byte_precision'])} precision | "
            f"{_fmt_percent(prior['long_byte_recall'])} recall | "
            f"{prior['matched_site_count']} matched site"
            f"{'s' if prior['matched_site_count'] != 1 else ''}"
            f"{' [one-site support]' if tiny else ''}"
        )
        body.append(_svg_text(1120, y + 5, support, size=12.5))

    body.extend(
        [
            _svg_text(
                800,
                825,
                "Stage A uses force tracking and the process-wide requested-generation clock.",
                size=13,
                anchor="middle",
                fill=MUTED,
            ),
            _svg_text(
                800,
                850,
                "Completed outcomes form the bars; live right-censored bytes stay outside their denominators.",
                size=13,
                anchor="middle",
                fill=MUTED,
            ),
            _svg_text(
                800,
                875,
                "Opportunity and classifier diagnostics stay in Stage A; speed, RSS, fragmentation, and resident-THP effects require Stage B.",
                size=13,
                anchor="middle",
                fill=MUTED,
            ),
        ]
    )
    return _svg_document(
        title=title,
        description=description,
        body=body,
        metadata=(
            f"source-sha256={source_digest}; stage=stage-a; "
            "pressure-basis=process_wide_requested_generation_bytes; force-track-all=true"
        ),
    )


def _effect_domain(comparisons: Sequence[Mapping[str, Any]]) -> float:
    values = [0.0]
    for comparison in comparisons:
        for pair in comparison["pairs"]:
            values.extend([pair["speed_improvement"], pair["rss_reduction"]])
    maximum = max(abs(value) for value in values)
    return max(5.0, math.ceil(maximum / 5.0) * 5.0)


def render_stage_b(data: Mapping[str, Any], source_digest: str) -> str:
    status = data["status"]
    reasons = data["blocked_reasons"]
    title = "Polars production-clock lifetime/THP ablation"
    description = (
        f"Five fixed-work arms and six paired comparisons over {data['repeats']} repeats. "
        f"Campaign status: {status}. " + "; ".join(reasons)
    )
    status_color = STATUS_COLORS[status]
    body = [
        _svg_text(55, 58, title, size=32, weight=700),
        _svg_text(
            55,
            93,
            f"Fixed work | {data['repeats']} paired repeats | lower time and RSS are better | production clock",
            size=17,
            fill=MUTED,
        ),
        f'  <rect x="1165" y="30" width="385" height="48" rx="8" fill="{status_color}"/>',
        _svg_text(1357.5, 61, status, size=15.5, weight=700, anchor="middle", fill="#FFFFFF"),
        _panel(45, 125, 720, 580, "stage-b-endpoints"),
        _svg_text(70, 163, "A | Five-arm endpoint", size=22, weight=700),
        _svg_text(735, 163, "median and every repeat; Default = 100", size=13.5, anchor="end", fill=MUTED),
        _panel(790, 125, 765, 580, "stage-b-paired-effects"),
        _svg_text(815, 163, "B | Registered paired effects", size=22, weight=700),
        _svg_text(1525, 163, "positive means improvement / reduction", size=13.5, anchor="end", fill=MUTED),
    ]

    normalized_values = [100.0]
    default_median = next(
        row["median_seconds"] for row in data["arms"] if row["arm"] == "default"
    )
    for row in data["arms"]:
        normalized_values.append(row["normalized_median_percent"])
        normalized_values.extend(
            100.0 * sample["seconds"] / default_median for sample in row["samples"]
        )
    low_value, high_value = min(normalized_values), max(normalized_values)
    padding = max(2.0, (high_value - low_value) * 0.2)
    domain_low = max(0.0, math.floor(low_value - padding))
    domain_high = math.ceil(high_value + padding)
    if domain_high <= domain_low:
        domain_high = domain_low + 4.0
    plot_left, plot_right = 275.0, 700.0

    def time_x(value: float) -> float:
        return plot_left + (value - domain_low) / (domain_high - domain_low) * (
            plot_right - plot_left
        )

    for fraction in (0.0, 0.5, 1.0):
        value = domain_low + fraction * (domain_high - domain_low)
        x = time_x(value)
        body.extend(
            [
                f'  <line x1="{x:.1f}" y1="188" x2="{x:.1f}" y2="650" stroke="{GRID}"/>',
                _svg_text(x, 675, f"{value:.1f}", size=11.5, anchor="middle", fill=MUTED),
            ]
        )
    default_x = time_x(100.0)
    body.append(
        f'  <line x1="{default_x:.1f}" y1="188" x2="{default_x:.1f}" y2="650" '
        'stroke="#2C3E50" stroke-width="1.5" stroke-dasharray="5 4"/>'
    )
    for index, row in enumerate(data["arms"]):
        y = 230.0 + index * 86.0
        body.append(_svg_text(70, y + 5, row["display"], size=14.5, weight=700))
        body.append(
            f'  <line x1="{plot_left:.1f}" y1="{y:.1f}" x2="{plot_right:.1f}" '
            f'y2="{y:.1f}" stroke="#E5E7E9" stroke-width="5"/>'
        )
        for sample in row["samples"]:
            value = 100.0 * sample["seconds"] / default_median
            body.append(
                f'  <circle class="endpoint-sample" cx="{time_x(value):.2f}" cy="{y:.1f}" '
                f'r="4" fill="{row["color"]}" opacity="0.38"><title>repeat '
                f'{sample["repeat"]}: {value:.3f}; {_escape(_fmt_seconds(sample["seconds"]))}'
                "</title></circle>"
            )
        median_x = time_x(row["normalized_median_percent"])
        body.append(
            f'  <circle class="endpoint-median" cx="{median_x:.2f}" cy="{y:.1f}" '
            f'r="8" fill="{row["color"]}" stroke="#FFFFFF" stroke-width="2"><title>'
            f'{_escape(row["display"])} median {row["normalized_median_percent"]:.3f}; '
            f'{_escape(_fmt_seconds(row["median_seconds"]))}</title></circle>'
        )
        body.append(
            _svg_text(
                735,
                y + 5,
                f"{row['normalized_median_percent']:.2f} | {_fmt_seconds(row['median_seconds'])}",
                size=12,
                anchor="end",
            )
        )

    effect_high = _effect_domain(data["comparisons"])
    effect_left, effect_right = 1110.0, 1510.0

    def effect_x(value: float) -> float:
        return effect_left + (value + effect_high) / (2.0 * effect_high) * (
            effect_right - effect_left
        )

    for value in (-effect_high, 0.0, effect_high):
        x = effect_x(value)
        body.extend(
            [
                f'  <line x1="{x:.1f}" y1="188" x2="{x:.1f}" y2="650" '
                f'stroke="{TEXT if value == 0 else GRID}" '
                f'stroke-width="{1.5 if value == 0 else 1.0}"/>',
                _svg_text(x, 675, f"{value:+.0f}%", size=11.5, anchor="middle", fill=MUTED),
            ]
        )
    body.extend(
        [
            _svg_text(825, 195, "Speed", size=12.5, weight=700, fill=PRECISION_COLOR),
            _svg_text(900, 195, "Peak RSS", size=12.5, weight=700, fill=RECALL_COLOR),
        ]
    )
    for index, comparison in enumerate(data["comparisons"]):
        y = 235.0 + index * 67.0
        body.append(
            _svg_text(815, y, _truncate(comparison["display"], 36), size=13.5, weight=700)
        )
        body.append(
            _svg_text(
                815,
                y + 18,
                comparison["status"],
                size=10.5,
                weight=700,
                fill=STATUS_COLORS[comparison["status"]],
            )
        )
        for pair in comparison["pairs"]:
            body.extend(
                [
                    f'  <circle class="paired-speed-sample" cx="{effect_x(pair["speed_improvement"]):.2f}" '
                    f'cy="{y - 8:.1f}" r="3.5" fill="{PRECISION_COLOR}" opacity="0.35">'
                    f'<title>repeat {pair["repeat"]}: speed {_escape(_fmt_signed_percent(pair["speed_improvement"]))}</title></circle>',
                    f'  <rect class="paired-rss-sample" x="{effect_x(pair["rss_reduction"]) - 3.5:.2f}" '
                    f'y="{y + 5.5:.1f}" width="7" height="7" fill="{RECALL_COLOR}" opacity="0.35">'
                    f'<title>repeat {pair["repeat"]}: peak RSS {_escape(_fmt_signed_percent(pair["rss_reduction"]))}</title></rect>',
                ]
            )
        if comparison["median_speed_improvement"] is not None:
            body.append(
                f'  <circle class="paired-speed-median" '
                f'cx="{effect_x(comparison["median_speed_improvement"]):.2f}" cy="{y - 8:.1f}" '
                f'r="6" fill="{PRECISION_COLOR}" stroke="#FFFFFF" stroke-width="1.5"><title>'
                f'median speed {_escape(_fmt_signed_percent(comparison["median_speed_improvement"]))}</title></circle>'
            )
        if comparison["median_rss_reduction"] is not None:
            body.append(
                f'  <rect class="paired-rss-median" '
                f'x="{effect_x(comparison["median_rss_reduction"]) - 5.5:.2f}" y="{y + 3.5:.1f}" '
                f'width="11" height="11" fill="{RECALL_COLOR}" stroke="#FFFFFF" stroke-width="1.5"><title>'
                f'median peak RSS {_escape(_fmt_signed_percent(comparison["median_rss_reduction"]))}</title></rect>'
            )
        medians = (
            f"speed {_fmt_signed_percent(comparison['median_speed_improvement']) if comparison['median_speed_improvement'] is not None else 'blocked'} | "
            f"RSS {_fmt_signed_percent(comparison['median_rss_reduction']) if comparison['median_rss_reduction'] is not None else 'blocked'}"
        )
        body.append(_svg_text(1515, y + 4, medians, size=11.5, anchor="end"))

    body.extend(
        [
            _panel(45, 725, 1510, 120, "stage-b-claim-gate"),
            _svg_text(70, 760, f"Claim gate: {status}", size=19, weight=700, fill=status_color),
        ]
    )
    if reasons:
        displayed = reasons[:2]
        for index, reason in enumerate(displayed):
            body.append(
                _svg_text(
                    70,
                    790 + index * 23,
                    _truncate(reason, 190),
                    size=12.5,
                    fill=MUTED,
                )
            )
        if len(reasons) > len(displayed):
            body.append(
                _svg_text(
                    1515,
                    813,
                    f"+{len(reasons) - len(displayed)} additional reasons in CSV and SVG description",
                    size=12,
                    anchor="end",
                    fill=MUTED,
                )
            )
    else:
        body.append(
            _svg_text(
                70,
                798,
                "All paired provenance, exact-prior, runtime-control, output, duration, backing, and allocator-activity gates passed.",
                size=12.5,
                fill=MUTED,
            )
        )
    body.extend(
        [
            _svg_text(
                800,
                870,
                "Production eligible-site payload-capacity clock; force tracking disabled. AnonHugePages is process-level backing correlated with positive UniAlloc activity; allocator-VMA attribution is unmeasured.",
                size=12.5,
                anchor="middle",
                fill=MUTED,
            ),
        ]
    )
    return _svg_document(
        title=title,
        description=description,
        body=body,
        metadata=(
            f"source-sha256={source_digest}; stage=stage-b; status={status}; "
            f"blocked-reasons={json.dumps(reasons, ensure_ascii=True, separators=(',', ':'))}"
        ),
    )


def stage_a_csv_rows(data: Mapping[str, Any]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for target in data["targets"]:
        prior = target["prior_quality"]
        for category, field in (
            ("Short", "short_requested_bytes"),
            ("Censored", "censored_requested_bytes"),
            ("Long", "long_requested_bytes"),
        ):
            rows.append(
                {
                    "target_id": target["target_id"],
                    "target": target["target"],
                    "allocator_revision": target["allocator_revision"],
                    "source_commit": target["source_commit"],
                    "exact_layout_gate": target["exact_layout_gate"],
                    "wall_seconds": target["wall_seconds"],
                    "runtime_exact_site_count": target["runtime_exact_site_count"],
                    "category": category,
                    "requested_bytes": target[field],
                    "denominator_requested_bytes": target[
                        "completed_requested_bytes"
                    ],
                    "share": target[field] / target["completed_requested_bytes"],
                    "long_gate_passed": target["long_gate_passed"],
                    "short_gate_passed": target["short_gate_passed"],
                    "matched_prior_site_count": prior["matched_site_count"],
                    "prior_long_requested_bytes": prior["long_requested_bytes"],
                    "prior_short_requested_bytes": prior["short_requested_bytes"],
                    "prior_censored_requested_bytes": prior[
                        "censored_requested_bytes"
                    ],
                    "prior_byte_precision": prior["byte_precision"],
                    "long_byte_recall": prior["long_byte_recall"],
                    "caveat": (
                        "pre-exact-layout-gate triage; classifier quality excluded from panel B"
                        if not target["exact_layout_gate"]
                        else (
                            "descriptive one-site support"
                            if prior["matched_site_count"] == 1
                            else "completed outcomes only; live objects are right-censored"
                        )
                    ),
                }
            )
    return rows


def stage_b_csv_rows(data: Mapping[str, Any]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    reasons = " | ".join(data["blocked_reasons"])
    default_median = next(
        arm["median_seconds"] for arm in data["arms"] if arm["arm"] == "default"
    )
    for arm in data["arms"]:
        for sample in arm["samples"]:
            rows.append(
                {
                    "row_type": "arm_sample",
                    "arm": arm["arm"],
                    "arm_label": arm["display"],
                    "repeat": sample["repeat"],
                    "seconds_per_work_unit": sample["seconds"],
                    "normalized_time_percent": 100.0
                    * sample["seconds"]
                    / default_median,
                    "peak_rss_kib": sample["peak_rss_kib"],
                    "diagnostic_eligible": _sample_diagnostic_eligibility(sample),
                    "preliminary_eligible": sample["performance_claim_eligible"],
                    "claim_eligible": "",
                    "comparison_status": "",
                    "campaign_status": data["status"],
                    "blocked_reasons": reasons,
                    "caveat": "measured endpoint; backing remains process-level",
                }
            )
    for comparison in data["comparisons"]:
        for pair in comparison["pairs"]:
            rows.append(
                {
                    "row_type": "paired_effect",
                    "comparison": comparison["name"],
                    "comparison_label": comparison["display"],
                    "repeat": pair["repeat"],
                    "baseline_arm": comparison["baseline_arm"],
                    "candidate_arm": comparison["candidate_arm"],
                    "speed_improvement_percent": pair["speed_improvement"],
                    "peak_rss_reduction_percent": pair["rss_reduction"],
                    "diagnostic_eligible": pair["diagnostic_eligible"],
                    "preliminary_eligible": pair["preliminary_eligible"],
                    "claim_eligible": pair["claim_eligible"],
                    "comparison_status": comparison["status"],
                    "campaign_status": data["status"],
                    "blocked_reasons": reasons,
                    "caveat": "paired repeat; positive values indicate improvement or reduction",
                }
            )
        rows.append(
            {
                "row_type": "paired_median",
                "comparison": comparison["name"],
                "comparison_label": comparison["display"],
                "baseline_arm": comparison["baseline_arm"],
                "candidate_arm": comparison["candidate_arm"],
                "speed_improvement_percent": comparison[
                    "median_speed_improvement"
                ],
                "peak_rss_reduction_percent": comparison[
                    "median_rss_reduction"
                ],
                "diagnostic_eligible": comparison["diagnostic_eligible"],
                "preliminary_eligible": comparison["preliminary_eligible"],
                "claim_eligible": comparison["claim_eligible"],
                "comparison_status": comparison["status"],
                "campaign_status": data["status"],
                "blocked_reasons": reasons,
                "caveat": "median of eligible paired repeats",
            }
        )
    return rows


def _atomic_write_text(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        mode="w",
        encoding="utf-8",
        newline="",
        dir=path.parent,
        prefix=f".{path.name}.",
        delete=False,
    ) as handle:
        temporary = Path(handle.name)
        handle.write(content)
    temporary.replace(path)


def _write_csv(
    path: Path, rows: Sequence[Mapping[str, Any]], fields: Sequence[str]
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
        temporary = Path(handle.name)
        writer = csv.DictWriter(
            handle, fieldnames=list(fields), extrasaction="ignore", lineterminator="\n"
        )
        writer.writeheader()
        for row in rows:
            writer.writerow({field: row.get(field, "") for field in fields})
    temporary.replace(path)


def _render_png(svg_path: Path, png_path: Path) -> str | None:
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
    summary_path: Path,
    output_dir: Path,
    *,
    repo_root: Path = ROOT,
    make_png: bool = True,
) -> list[Path]:
    source_bytes = summary_path.read_bytes()
    try:
        document = json.loads(source_bytes)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ChartError(f"summary is not valid UTF-8 JSON: {error}") from error
    data = validate_summary(document)
    _verify_source_files(data["sources"], repo_root)
    source_digest = hashlib.sha256(source_bytes).hexdigest()

    # Complete evidence validation before creating any publishable output.
    stage_a_svg = render_stage_a(data["stage_a"], source_digest)
    stage_b_svg = render_stage_b(data["stage_b"], source_digest)
    stage_a_rows = stage_a_csv_rows(data["stage_a"])
    stage_b_rows = stage_b_csv_rows(data["stage_b"])

    output_dir.mkdir(parents=True, exist_ok=True)
    outputs: list[Path] = []
    for stem, svg, rows, fields in (
        (
            "stage-a-opportunity",
            stage_a_svg,
            stage_a_rows,
            STAGE_A_CSV_FIELDS,
        ),
        (
            "polars-stage-b-ablation",
            stage_b_svg,
            stage_b_rows,
            STAGE_B_CSV_FIELDS,
        ),
    ):
        svg_path = output_dir / f"{stem}.svg"
        csv_path = output_dir / f"{stem}.csv"
        _atomic_write_text(svg_path, svg)
        _write_csv(csv_path, rows, fields)
        outputs.extend((svg_path, csv_path))
        if make_png:
            png_path = output_dir / f"{stem}.png"
            warning = _render_png(svg_path, png_path)
            if warning is None:
                outputs.append(png_path)
            else:
                print(f"warning: {warning}", file=sys.stderr)
    return outputs


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--summary", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument(
        "--repo-root",
        type=Path,
        default=ROOT,
        help="repository root used to verify every retained source path and SHA-256",
    )
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
            repo_root=args.repo_root.expanduser(),
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
