#!/usr/bin/env python3
"""Run matched production-clock lifetime/THP arms from a Stage-A build.

The five arms reuse two compiler-matched binaries from one frozen allocator
snapshot.  Fixed-work targets produce performance evidence.  Criterion targets
produce normalized per-iteration diagnostics and remain outside the defended
macrobenchmark claim.  Every compiler-prior/THP run retains its exact-site join
and every ordinary/THP pair is checked sample by sample for resident THP
backing.
"""

from __future__ import annotations

import argparse
import fcntl
import json
import math
import os
from pathlib import Path
import re
import statistics
import sys
from typing import Any, Mapping, Sequence


SCRIPT_DIR = Path(__file__).resolve().parent
ROOT = SCRIPT_DIR.parents[1]
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

import lifetime_prior_six_program_campaign as stage_a  # noqa: E402


FIXED_WORK_TARGETS = frozenset({"oxipng", "redb", "polars"})
ARM_NAMES = (
    "default",
    "adaptive-ordinary-all-unknown",
    "adaptive-ordinary-compiler-prior",
    "adaptive-selective-thp-all-unknown",
    "adaptive-selective-thp-compiler-prior",
)
THP_ARM_PAIRS = (
    (
        "adaptive-ordinary-all-unknown",
        "adaptive-selective-thp-all-unknown",
    ),
    (
        "adaptive-ordinary-compiler-prior",
        "adaptive-selective-thp-compiler-prior",
    ),
)
NUMBER_RE = r"[0-9]+(?:\.[0-9]+)?(?:[eE][+-]?[0-9]+)?"
CRITERION_TIME_RE = re.compile(
    r"time:\s*\["
    rf"(?P<low>{NUMBER_RE})\s+(?P<low_unit>ns|us|µs|ms|s)\s+"
    rf"(?P<estimate>{NUMBER_RE})\s+(?P<estimate_unit>ns|us|µs|ms|s)\s+"
    rf"(?P<high>{NUMBER_RE})\s+(?P<high_unit>ns|us|µs|ms|s)\]"
)
ANSI_ESCAPE_RE = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]")
UNIT_SECONDS = {
    "ns": 1e-9,
    "us": 1e-6,
    "µs": 1e-6,
    "ms": 1e-3,
    "s": 1.0,
}


def _read_json(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise stage_a.CampaignContractError(f"expected JSON object: {path}")
    return value


def _finite_positive(value: Any) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise stage_a.CampaignContractError("performance metric is not numeric")
    result = float(value)
    if not math.isfinite(result) or result <= 0:
        raise stage_a.CampaignContractError("performance metric is not positive")
    return result


def parse_criterion_estimate(stdout: str) -> dict[str, Any]:
    matches = list(CRITERION_TIME_RE.finditer(ANSI_ESCAPE_RE.sub("", stdout)))
    if len(matches) != 1:
        raise stage_a.CampaignContractError(
            f"expected one Criterion time estimate, observed {len(matches)}"
        )
    match = matches[0]

    def seconds(field: str) -> float:
        value = _finite_positive(float(match.group(field)))
        return value * UNIT_SECONDS[match.group(f"{field}_unit")]

    low = seconds("low")
    estimate = seconds("estimate")
    high = seconds("high")
    if not low <= estimate <= high:
        raise stage_a.CampaignContractError(
            "Criterion estimate lies outside its reported interval"
        )
    return {
        "source": "criterion-three-point-time-estimate",
        "low_seconds_per_iteration": low,
        "estimate_seconds_per_iteration": estimate,
        "high_seconds_per_iteration": high,
        "diagnostic_only": True,
    }


def exact_prior_coverage_complete(join: Mapping[str, Any]) -> bool:
    matched = join.get("matched_applied_prior_outcomes")
    executed = join.get("executed_static_prior_outcomes")
    resolution = join.get("resolution_status_counts")
    if (
        not isinstance(matched, dict)
        or not isinstance(executed, dict)
        or not isinstance(resolution, dict)
    ):
        return False
    fields = (
        "site_count",
        "exact_site_key_digest",
        "allocation_count",
        "allocation_requested_bytes",
        "long_outcomes",
        "short_outcomes",
        "censored_outcomes",
        "long_requested_bytes",
        "short_requested_bytes",
        "censored_requested_bytes",
        "long_prior_site_count",
        "short_prior_site_count",
    )
    integer_fields = tuple(
        field for field in fields if field != "exact_site_key_digest"
    )
    expected_key = list(stage_a.runtime_lifetime.RUNTIME_SITE_KEY_FIELDS)

    def aggregate_valid(aggregate: Mapping[str, Any]) -> bool:
        digest = aggregate.get("exact_site_key_digest")
        if (
            aggregate.get("evidence_complete") is not True
            or aggregate.get("deduplicated_by") != expected_key
            or not isinstance(digest, str)
            or re.fullmatch(r"[0-9a-f]{64}", digest) is None
            or any(
                isinstance(aggregate.get(field), bool)
                or not isinstance(aggregate.get(field), int)
                or aggregate[field] < 0
                for field in integer_fields
            )
            or aggregate.get("missing_or_invalid_field_site_count") != 0
        ):
            return False
        return (
            aggregate["allocation_count"]
            >= aggregate["long_outcomes"]
            + aggregate["short_outcomes"]
            + aggregate["censored_outcomes"]
            and aggregate["allocation_requested_bytes"]
            >= aggregate["long_requested_bytes"]
            + aggregate["short_requested_bytes"]
            + aggregate["censored_requested_bytes"]
            and aggregate["site_count"]
            == aggregate["long_prior_site_count"]
            + aggregate["short_prior_site_count"]
        )

    return (
        join.get("source") == "unialloc-compiler-runtime-exact-site-join-v2"
        and join.get("static_prior_join_success") is True
        and isinstance(join.get("applied_prior_classified_count"), int)
        and join["applied_prior_classified_count"] > 0
        and join.get("applied_prior_incomplete_key_count") == 0
        and isinstance(join.get("matched_applied_prior_site_count"), int)
        and join["matched_applied_prior_site_count"] > 0
        and aggregate_valid(matched)
        and aggregate_valid(executed)
        and matched.get("site_count") == join["matched_applied_prior_site_count"]
        and all(matched.get(field) == executed.get(field) for field in fields)
        and resolution.get("rejected_static_prior_transport_mismatch", 0) == 0
    )


def all_unknown_control_clean(join: Mapping[str, Any]) -> bool:
    executed = join.get("executed_static_prior_outcomes")
    if not isinstance(executed, dict):
        return False
    zero_fields = (
        "site_count",
        "allocation_count",
        "allocation_requested_bytes",
        "long_outcomes",
        "short_outcomes",
        "censored_outcomes",
        "long_requested_bytes",
        "short_requested_bytes",
        "censored_requested_bytes",
        "long_prior_site_count",
        "short_prior_site_count",
        "missing_or_invalid_field_site_count",
    )
    return (
        join.get("source") == "unialloc-compiler-runtime-exact-site-join-v2"
        and join.get("applied_prior_classified_count") == 0
        and executed.get("evidence_complete") is True
        and executed.get("deduplicated_by")
        == list(stage_a.runtime_lifetime.RUNTIME_SITE_KEY_FIELDS)
        and all(executed.get(field) == 0 for field in zero_fields)
    )


def exact_prior_effect_observed(join: Mapping[str, Any]) -> bool:
    if not exact_prior_coverage_complete(join):
        return False
    executed = join["executed_static_prior_outcomes"]
    return (
        isinstance(executed.get("allocation_count"), int)
        and executed["allocation_count"] > 0
        and isinstance(executed.get("allocation_requested_bytes"), int)
        and executed["allocation_requested_bytes"] > 0
    )


def strict_thp_pair_gate(
    ordinary_samples: Sequence[Mapping[str, Any]],
    thp_samples: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    reasons: list[str] = []
    sample_eligibility: list[dict[str, Any]] = []
    if not ordinary_samples:
        reasons.append("ordinary run has no measured smaps samples")
    if not thp_samples:
        reasons.append("selective-THP run has no measured smaps samples")
    for arm, samples, expect_thp in (
        ("ordinary", ordinary_samples, False),
        ("selective-thp", thp_samples, True),
    ):
        for index, sample in enumerate(samples):
            backing = sample.get("anon_hugepages_kib")
            valid = (
                isinstance(backing, int)
                and not isinstance(backing, bool)
                and backing >= 0
            )
            sample_reasons: list[str] = []
            if not valid:
                sample_reasons.append("invalid resident AnonHugePages value")
            elif expect_thp and backing == 0:
                sample_reasons.append("no resident AnonHugePages")
            elif not expect_thp and backing != 0:
                sample_reasons.append("ordinary run has resident AnonHugePages")
            sample_eligibility.append(
                {
                    "arm": arm,
                    "sample_index": index,
                    "elapsed_seconds": sample.get("elapsed_seconds"),
                    "anon_hugepages_kib": backing,
                    "eligible": not sample_reasons,
                    "reasons": sample_reasons,
                }
            )
            reasons.extend(
                f"{arm} sample {index}: {reason}" for reason in sample_reasons
            )
    excluded = [row for row in sample_eligibility if not row["eligible"]]
    return {
        "passed": not reasons,
        "ordinary_sample_count": len(ordinary_samples),
        "selective_thp_sample_count": len(thp_samples),
        "eligible_sample_count": len(sample_eligibility) - len(excluded),
        "excluded_sample_count": len(excluded),
        "sample_eligibility": sample_eligibility,
        "reasons": reasons,
        "pairing_mode": "independent-per-sample-sustained-backing",
        "claim_boundary": (
            "every measured sample is checked independently; unequal run durations "
            "do not force index pairing, and any zero-backed THP sample fails the "
            "whole-run causal gate"
        ),
    }


def selective_thp_runtime_gate(stats: Mapping[str, Any]) -> dict[str, Any]:
    fields = (
        "adaptive_long_routed_allocations",
        "thp_extent_mappings",
        "thp_advice_attempts",
        "thp_advice_successes",
        "thp_advice_errors",
        "mapping_failures",
    )
    counters: dict[str, int] = {}
    reasons: list[str] = []
    for field in fields:
        value = stats.get(field)
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            reasons.append(f"invalid allocator counter: {field}")
        else:
            counters[field] = value
    if not reasons:
        for field in (
            "adaptive_long_routed_allocations",
            "thp_extent_mappings",
            "thp_advice_attempts",
            "thp_advice_successes",
        ):
            if counters[field] == 0:
                reasons.append(f"allocator THP activity is zero: {field}")
        if counters["thp_advice_errors"] != 0:
            reasons.append("allocator THP advice reported errors")
        if counters["mapping_failures"] != 0:
            reasons.append("allocator mapping failures are nonzero")
        if counters["thp_advice_attempts"] != (
            counters["thp_advice_successes"] + counters["thp_advice_errors"]
        ):
            reasons.append("allocator THP advice accounting is inconsistent")
    return {
        "passed": not reasons,
        "counters": counters,
        "reasons": reasons,
        "claim_boundary": (
            "process-level resident AnonHugePages is correlated with UniAlloc THP "
            "activity only when routing, extent, and successful advice counters "
            "are all positive; allocator-VMA residency attribution is unmeasured"
        ),
    }


def strict_ordinary_backing_gate(
    samples: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    reasons: list[str] = []
    if not samples:
        reasons.append("ordinary run has no measured smaps samples")
    for index, sample in enumerate(samples):
        backing = sample.get("anon_hugepages_kib")
        if (
            isinstance(backing, bool)
            or not isinstance(backing, int)
            or backing < 0
        ):
            reasons.append(f"ordinary sample {index}: invalid AnonHugePages value")
        elif backing != 0:
            reasons.append(
                f"ordinary sample {index}: resident AnonHugePages={backing} KiB"
            )
    return {
        "passed": not reasons,
        "sample_count": len(samples),
        "reasons": reasons,
        "claim_boundary": "every ordinary-arm sample must have zero AnonHugePages",
    }


def _sample_primary_metric(
    target_id: str, sample: Mapping[str, Any]
) -> dict[str, Any]:
    if target_id in FIXED_WORK_TARGETS:
        work_units = sample.get("work_units")
        if (
            isinstance(work_units, bool)
            or not isinstance(work_units, int)
            or work_units <= 0
        ):
            raise stage_a.CampaignContractError("fixed-work sample has no work count")
        wall_seconds = _finite_positive(sample.get("wall_seconds"))
        return {
            "source": "fixed-work-wall-clock",
            "seconds_per_work_unit": wall_seconds / work_units,
            "work_units_per_second": work_units / wall_seconds,
            "diagnostic_only": False,
        }
    stdout = Path(str(sample["stdout_path"])).read_text(
        encoding="utf-8", errors="replace"
    )
    return parse_criterion_estimate(stdout)


def _metric_value(metric: Mapping[str, Any]) -> float:
    if metric.get("diagnostic_only") is True:
        return _finite_positive(metric.get("estimate_seconds_per_iteration"))
    return _finite_positive(metric.get("seconds_per_work_unit"))


def _median(values: Sequence[float]) -> float:
    if not values:
        raise stage_a.CampaignContractError("cannot summarize an empty metric set")
    return float(statistics.median(values))


def _percent_improvement(baseline: float, candidate: float) -> float:
    baseline = _finite_positive(baseline)
    candidate = _finite_positive(candidate)
    return (baseline - candidate) / baseline * 100.0


def _optional_percent_reduction(baseline: Any, candidate: Any) -> float | None:
    if (
        isinstance(baseline, bool)
        or not isinstance(baseline, (int, float))
        or isinstance(candidate, bool)
        or not isinstance(candidate, (int, float))
    ):
        raise stage_a.CampaignContractError("comparison metric is not numeric")
    baseline_value = float(baseline)
    candidate_value = float(candidate)
    if (
        not math.isfinite(baseline_value)
        or not math.isfinite(candidate_value)
        or baseline_value < 0
        or candidate_value < 0
    ):
        raise stage_a.CampaignContractError("comparison metric is invalid")
    if baseline_value == 0:
        return None
    return (baseline_value - candidate_value) / baseline_value * 100.0


def _arm_order(repeat: int) -> tuple[str, ...]:
    if repeat % 2 == 0:
        return ARM_NAMES
    return tuple(reversed(ARM_NAMES))


def _thp_host_state() -> dict[str, Any]:
    values: dict[str, Any] = {}
    for name in ("enabled", "defrag", "shmem_enabled"):
        path = Path("/sys/kernel/mm/transparent_hugepage") / name
        values[name] = (
            path.read_text(encoding="utf-8").strip() if path.is_file() else None
        )
    return values


def _selected_work_units(target: Mapping[str, Any], target_id: str) -> int | None:
    if target_id not in FIXED_WORK_TARGETS:
        return None
    runs = target.get("runs")
    if not isinstance(runs, dict):
        raise stage_a.CampaignContractError("Stage-A result has no completed runs")
    observed = {
        row.get("work_units")
        for row in runs.values()
        if isinstance(row, dict) and isinstance(row.get("work_units"), int)
    }
    if len(observed) != 1:
        raise stage_a.CampaignContractError(
            "Stage-A build groups do not share one fixed work count"
        )
    return int(observed.pop())


def _stage_a_target_contract(
    document: Mapping[str, Any], target_id: str
) -> dict[str, Any]:
    targets = document.get("targets")
    if not isinstance(targets, dict) or not isinstance(targets.get(target_id), dict):
        raise stage_a.CampaignContractError("Stage-A result lacks the requested target")
    target = dict(targets[target_id])
    if target.get("status") not in {"complete", "complete-no-static-coverage"}:
        raise stage_a.CampaignContractError("Stage-A target did not complete")
    gate = target.get("opportunity_gate")
    if not isinstance(gate, dict) or gate.get("passed") is not True:
        raise stage_a.CampaignContractError("Stage-A opportunity gate did not pass")
    builds = target.get("builds")
    if not isinstance(builds, dict) or set(stage_a.BUILD_GROUPS) - set(builds):
        raise stage_a.CampaignContractError(
            "Stage-B requires all-Unknown and compiler-prior builds"
        )
    implementation = {
        builds[group].get("implementation_sha256") for group in stage_a.BUILD_GROUPS
    }
    if len(implementation) != 1 or None in implementation:
        raise stage_a.CampaignContractError("Stage-B build snapshots differ")
    snapshot = document.get("allocator_snapshot")
    if not isinstance(snapshot, dict):
        raise stage_a.CampaignContractError("Stage-A allocator snapshot is missing")
    expected_implementation = snapshot.get("unialloc_implementation_sha256")
    if implementation != {expected_implementation}:
        raise stage_a.CampaignContractError(
            "Stage-A build implementation differs from its frozen allocator snapshot"
        )
    paired_fields = (
        "campaign_snapshot_sha256",
        "base_campaign_snapshot_sha256",
        "runtime_instrumentation_sha256",
        "generated_source_sha256",
    )
    for group in stage_a.BUILD_GROUPS:
        build = builds[group]
        if not isinstance(build, dict) or build.get("build_group") != group:
            raise stage_a.CampaignContractError(
                f"Stage-A build-group record is invalid: {group}"
            )
        expected_prior = group == "compiler-prior"
        if build.get("automatic_rust_lifetime_prior") is not expected_prior:
            raise stage_a.CampaignContractError(
                f"Stage-A compiler mode is invalid: {group}"
            )
        if (
            build.get("success") is not True
            or build.get("target_id") != target_id
            or build.get("source_commit") != stage_a.TARGETS[target_id].source_commit
            or build.get("toolchain") != stage_a.TOOLCHAIN
            or build.get("implementation_revision")
            != snapshot.get("allocator_revision")
            or build.get("base_campaign_snapshot_sha256")
            != snapshot.get("campaign_snapshot_sha256")
            or build.get("activation", {}).get("success") is not True
            or build.get("rewrite_claim_success") is not True
            or build.get("requested_target_crates")
            != list(stage_a.stage_a_target_crates(target_id))
        ):
            raise stage_a.CampaignContractError(
                f"Stage-A retained build provenance failed: {group}"
            )
        compiler_sites = build.get("compiler_sites")
        if (
            not isinstance(compiler_sites, dict)
            or compiler_sites.get("source") != stage_a.COMPILER_SITE_EXPORT_SOURCE
        ):
            raise stage_a.CampaignContractError(
                f"Stage-A compiler-site schema failed: {group}"
            )
        for path_field, digest_field in (
            ("binary", "binary_sha256"),
            ("compiler_sites_path", "compiler_sites_sha256"),
        ):
            path = Path(str(build.get(path_field, "")))
            digest = build.get(digest_field)
            if (
                not path.is_file()
                or not isinstance(digest, str)
                or stage_a.sha256_file(path) != digest
            ):
                raise stage_a.CampaignContractError(
                    f"Stage-A retained artifact identity failed: {group}/{path_field}"
                )
    for field in paired_fields:
        values = {builds[group].get(field) for group in stage_a.BUILD_GROUPS}
        if len(values) != 1:
            raise stage_a.CampaignContractError(
                f"Stage-A build-pair provenance differs: {field}"
            )
    dependency_provenance = {
        builds[group].get("allocator_dependency_compatibility", {}).get(
            "provenance_digest"
        )
        for group in stage_a.BUILD_GROUPS
    }
    if len(dependency_provenance) != 1 or None in dependency_provenance:
        raise stage_a.CampaignContractError(
            "Stage-A build-pair dependency provenance differs"
        )
    return target


def run_campaign(
    *,
    stage_a_results: Path,
    target_id: str,
    raw_dir: Path,
    repeats: int,
    cpu: int,
    measurement_seconds: float,
    timeout: float,
    sample_interval: float,
    lock_path: Path,
) -> dict[str, Any]:
    document = _read_json(stage_a_results)
    target = _stage_a_target_contract(document, target_id)
    builds = target["builds"]
    work_units = _selected_work_units(target, target_id)
    if cpu not in os.sched_getaffinity(0):
        raise stage_a.CampaignContractError(f"CPU {cpu} is outside process affinity")
    input_path = None
    if target_id == "oxipng":
        preflight_path = stage_a_results.parent / "stage-a-preflight.json"
        preflight = _read_json(preflight_path)
        input_path = Path(str(preflight["oxipng_input"]["path"]))
        if (
            not input_path.is_file()
            or stage_a.sha256_file(input_path) != stage_a.OXIPNG_INPUT_SHA256
        ):
            raise stage_a.CampaignContractError(
                "Stage-A retained Oxipng fixture identity failed"
            )
    raw_dir.mkdir(parents=True, exist_ok=True)
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    samples: list[dict[str, Any]] = []
    thp_pair_gates: list[dict[str, Any]] = []

    with lock_path.open("a+", encoding="utf-8") as lock_file:
        fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
        for repeat in range(repeats):
            by_arm: dict[str, dict[str, Any]] = {}
            for arm_name in _arm_order(repeat):
                arm = stage_a.ARM_BY_NAME[arm_name]
                run_label = f"stage-b-repeat-{repeat:02d}-{arm_name}"
                sample = stage_a.run_stage_a_sample(
                    target_id,
                    arm.build_group,
                    build=builds[arm.build_group],
                    raw_dir=raw_dir,
                    input_path=input_path,
                    work_units=work_units,
                    measurement_seconds=measurement_seconds,
                    timeout=timeout,
                    sample_interval=sample_interval,
                    validate_duration=False,
                    run_label=run_label,
                    arm_name=arm_name,
                    command_prefix=("taskset", "-c", str(cpu)),
                    evidence_stage="production",
                )
                wall_seconds = _finite_positive(sample["wall_seconds"])
                duration_eligible = (
                    stage_a.DEFAULT_SCREEN_MIN_SECONDS
                    <= wall_seconds
                    <= stage_a.DEFAULT_SCREEN_MAX_SECONDS
                )
                metric = _sample_primary_metric(target_id, sample)
                if not isinstance(sample.get("procfs"), dict):
                    raise stage_a.CampaignContractError(
                        f"{target_id}/{arm_name} emitted no procfs samples"
                    )
                join = sample["compiler_runtime_exact_join"]
                prior_coverage_complete = (
                    exact_prior_coverage_complete(join)
                    if arm.build_group == "compiler-prior"
                    else None
                )
                prior_effect_observed = (
                    exact_prior_effect_observed(join)
                    if arm.build_group == "compiler-prior"
                    else None
                )
                runtime_control_clean = (
                    all_unknown_control_clean(join)
                    if arm.build_group == "all-unknown"
                    else None
                )
                performance_claim_eligible = (
                    target_id in FIXED_WORK_TARGETS
                    and duration_eligible
                    and prior_coverage_complete is not False
                    and prior_effect_observed is not False
                    and runtime_control_clean is not False
                )
                classification = dict(sample["classification"])
                classification.update(
                    {
                        "production_accuracy_claim_eligible": (
                            prior_coverage_complete is True
                            and prior_effect_observed is True
                            and classification["production_accuracy"] is not None
                        ),
                        "runtime_predictor_accuracy_claim_eligible": (
                            classification["runtime_predictor_accuracy"] is not None
                        ),
                        "affinity_pinned": True,
                        "measurement_lock_held": True,
                        "performance_claim_eligible": performance_claim_eligible,
                    }
                )
                row = {
                    **sample,
                    "repeat": repeat,
                    "primary_metric": metric,
                    "primary_metric_value_seconds": _metric_value(metric),
                    "duration_eligible": duration_eligible,
                    "duration_status": (
                        "inside-fast-boundary"
                        if duration_eligible
                        else "retained-outside-fast-boundary"
                    ),
                    "affinity_pinned": True,
                    "pinned_cpu": cpu,
                    "measurement_lock_held": True,
                    "measurement_lock_path": str(lock_path.resolve()),
                    "full_executed_prior_coverage": prior_coverage_complete,
                    "compiler_prior_effect_observed": prior_effect_observed,
                    "all_unknown_control_clean": runtime_control_clean,
                    "force_track_enabled": False,
                    "classification": classification,
                    "performance_claim_eligible": performance_claim_eligible,
                }
                samples.append(row)
                by_arm[arm_name] = row

            if target_id in FIXED_WORK_TARGETS:
                output_values = {
                    stage_a._output_comparison_value(  # noqa: SLF001
                        target_id, row["output_identity"]
                    )
                    for row in by_arm.values()
                }
                if len(output_values) != 1:
                    raise stage_a.CampaignContractError(
                        f"{target_id} output differs across Stage-B arms"
                    )
                for row in by_arm.values():
                    row["cross_arm_output_equivalent"] = True
            smaps_by_arm: dict[str, list[dict[str, Any]]] = {}
            for arm_name, row in by_arm.items():
                smaps_value = json.loads(
                    Path(row["smaps_samples_path"]).read_text(encoding="utf-8")
                )
                if not isinstance(smaps_value, list) or any(
                    not isinstance(sample, dict) for sample in smaps_value
                ):
                    raise stage_a.CampaignContractError(
                        f"{target_id}/{arm_name} smaps evidence is invalid"
                    )
                smaps_by_arm[arm_name] = smaps_value

            for arm_name in (
                "default",
                "adaptive-ordinary-all-unknown",
                "adaptive-ordinary-compiler-prior",
            ):
                row = by_arm[arm_name]
                ordinary_gate = strict_ordinary_backing_gate(
                    smaps_by_arm[arm_name]
                )
                row["ordinary_backing_gate"] = ordinary_gate
                row["performance_claim_eligible"] = (
                    row["performance_claim_eligible"]
                    and ordinary_gate["passed"] is True
                )
                row["classification"]["performance_claim_eligible"] = row[
                    "performance_claim_eligible"
                ]

            for ordinary_name, thp_name in THP_ARM_PAIRS:
                thp_row = by_arm[thp_name]
                backing_gate = strict_thp_pair_gate(
                    smaps_by_arm[ordinary_name], smaps_by_arm[thp_name]
                )
                activity_gate = selective_thp_runtime_gate(
                    thp_row["runtime_stats"]
                )
                gate = {
                    "repeat": repeat,
                    "ordinary_arm": ordinary_name,
                    "selective_thp_arm": thp_name,
                    "backing": backing_gate,
                    "allocator_activity": activity_gate,
                    "passed": (
                        backing_gate["passed"] is True
                        and activity_gate["passed"] is True
                    ),
                }
                thp_pair_gates.append(gate)
                thp_row["thp_pair_backing_gate"] = gate
                thp_row["performance_claim_eligible"] = (
                    thp_row["performance_claim_eligible"]
                    and gate["passed"] is True
                )
                thp_row["classification"]["performance_claim_eligible"] = (
                    thp_row["performance_claim_eligible"]
                )

        fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)

    samples_by_arm = {
        arm: [row for row in samples if row["arm"] == arm] for arm in ARM_NAMES
    }
    arm_summary: dict[str, Any] = {}
    for arm, rows in samples_by_arm.items():
        values = [float(row["primary_metric_value_seconds"]) for row in rows]
        arm_summary[arm] = {
            "sample_count": len(rows),
            "median_primary_seconds": _median(values),
            "minimum_primary_seconds": min(values),
            "maximum_primary_seconds": max(values),
            "median_peak_rss_kib": _median(
                [float(row["procfs"]["peak_rss_kib"]) for row in rows]
            ),
            "all_performance_claim_eligible": all(
                row["performance_claim_eligible"] for row in rows
            ),
            "all_full_executed_prior_coverage": (
                all(row["full_executed_prior_coverage"] is True for row in rows)
                if stage_a.ARM_BY_NAME[arm].build_group == "compiler-prior"
                else None
            ),
            "all_compiler_prior_effect_observed": (
                all(row["compiler_prior_effect_observed"] is True for row in rows)
                if stage_a.ARM_BY_NAME[arm].build_group == "compiler-prior"
                else None
            ),
            "all_unknown_controls_clean": (
                all(row["all_unknown_control_clean"] is True for row in rows)
                if stage_a.ARM_BY_NAME[arm].build_group == "all-unknown"
                else None
            ),
            "all_backing_gates_passed": all(
                (
                    row.get("thp_pair_backing_gate")
                    or row.get("ordinary_backing_gate")
                    or {}
                ).get("passed")
                is True
                for row in rows
            ),
        }

    paired_comparisons: dict[str, Any] = {}
    for name, baseline_arm, candidate_arm in (
        (
            "runtime_only_vs_default",
            "default",
            "adaptive-ordinary-all-unknown",
        ),
        (
            "compiler_prior_vs_runtime_only",
            "adaptive-ordinary-all-unknown",
            "adaptive-ordinary-compiler-prior",
        ),
        (
            "runtime_thp_vs_runtime_ordinary",
            "adaptive-ordinary-all-unknown",
            "adaptive-selective-thp-all-unknown",
        ),
        (
            "compiler_prior_thp_vs_runtime_thp",
            "adaptive-selective-thp-all-unknown",
            "adaptive-selective-thp-compiler-prior",
        ),
        (
            "selective_thp_vs_compiler_ordinary",
            "adaptive-ordinary-compiler-prior",
            "adaptive-selective-thp-compiler-prior",
        ),
        (
            "compiler_thp_end_to_end_vs_default",
            "default",
            "adaptive-selective-thp-compiler-prior",
        ),
    ):
        pairs: list[dict[str, Any]] = []
        for repeat in range(repeats):
            baseline = next(
                row
                for row in samples_by_arm[baseline_arm]
                if row["repeat"] == repeat
            )
            candidate = next(
                row
                for row in samples_by_arm[candidate_arm]
                if row["repeat"] == repeat
            )
            backing_eligible = all(
                (
                    row.get("thp_pair_backing_gate")
                    or row.get("ordinary_backing_gate")
                    or {}
                ).get("passed")
                is True
                for row in (baseline, candidate)
            )
            diagnostic_eligible = (
                backing_eligible
                and baseline["duration_eligible"]
                and candidate["duration_eligible"]
            )
            compiler_prior_effect_eligible = all(
                row["full_executed_prior_coverage"] is True
                and row["compiler_prior_effect_observed"] is True
                for row in (baseline, candidate)
                if row["build_group"] == "compiler-prior"
            )
            runtime_controls_eligible = all(
                row["all_unknown_control_clean"] is True
                for row in (baseline, candidate)
                if row["build_group"] == "all-unknown"
            )
            diagnostic_eligible = (
                diagnostic_eligible
                and compiler_prior_effect_eligible
                and runtime_controls_eligible
            )
            preliminary_eligible = (
                diagnostic_eligible
                and baseline["performance_claim_eligible"]
                and candidate["performance_claim_eligible"]
            )
            claim_eligible = preliminary_eligible and repeats >= 4
            pairs.append(
                {
                    "repeat": repeat,
                    "diagnostic_eligible": diagnostic_eligible,
                    "preliminary_eligible": preliminary_eligible,
                    "claim_eligible": claim_eligible,
                    "backing_eligible": backing_eligible,
                    "compiler_prior_effect_eligible": (
                        compiler_prior_effect_eligible
                    ),
                    "runtime_controls_eligible": runtime_controls_eligible,
                    "baseline_seconds": baseline["primary_metric_value_seconds"],
                    "candidate_seconds": candidate["primary_metric_value_seconds"],
                    "percent_improvement": _percent_improvement(
                        baseline["primary_metric_value_seconds"],
                        candidate["primary_metric_value_seconds"],
                    ),
                    "baseline_peak_rss_kib": baseline["procfs"]["peak_rss_kib"],
                    "candidate_peak_rss_kib": candidate["procfs"]["peak_rss_kib"],
                    "peak_rss_percent_reduction": _optional_percent_reduction(
                        baseline["procfs"]["peak_rss_kib"],
                        candidate["procfs"]["peak_rss_kib"],
                    ),
                    "retained_bytes_percent_reduction": _optional_percent_reduction(
                        baseline["fragmentation"]["retained_bytes"],
                        candidate["fragmentation"]["retained_bytes"],
                    ),
                    "stranded_bytes_percent_reduction": _optional_percent_reduction(
                        baseline["fragmentation"]["stranded_bytes"],
                        candidate["fragmentation"]["stranded_bytes"],
                    ),
                }
            )
        diagnostic_improvements = [
            float(pair["percent_improvement"])
            for pair in pairs
            if pair["diagnostic_eligible"]
        ]
        claim_improvements = [
            float(pair["percent_improvement"])
            for pair in pairs
            if pair["claim_eligible"]
        ]
        preliminary_improvements = [
            float(pair["percent_improvement"])
            for pair in pairs
            if pair["preliminary_eligible"]
        ]

        def eligible_reductions(field: str) -> list[float]:
            return [
                float(pair[field])
                for pair in pairs
                if pair["diagnostic_eligible"] and pair[field] is not None
            ]

        rss_reductions = eligible_reductions("peak_rss_percent_reduction")
        retained_reductions = eligible_reductions(
            "retained_bytes_percent_reduction"
        )
        stranded_reductions = eligible_reductions(
            "stranded_bytes_percent_reduction"
        )
        paired_comparisons[name] = {
            "baseline_arm": baseline_arm,
            "candidate_arm": candidate_arm,
            "pairs": pairs,
            "diagnostic_eligible_pair_count": len(diagnostic_improvements),
            "preliminary_eligible_pair_count": len(preliminary_improvements),
            "claim_eligible_pair_count": len(claim_improvements),
            "median_percent_improvement": (
                _median(diagnostic_improvements)
                if diagnostic_improvements
                else None
            ),
            "claim_median_percent_improvement": (
                _median(claim_improvements) if claim_improvements else None
            ),
            "preliminary_median_percent_improvement": (
                _median(preliminary_improvements)
                if preliminary_improvements
                else None
            ),
            "median_peak_rss_percent_reduction": (
                _median(rss_reductions) if rss_reductions else None
            ),
            "median_retained_bytes_percent_reduction": (
                _median(retained_reductions) if retained_reductions else None
            ),
            "median_stranded_bytes_percent_reduction": (
                _median(stranded_reductions) if stranded_reductions else None
            ),
            "claim_eligible": len(claim_improvements) == repeats,
            "preliminary_eligible": len(preliminary_improvements) == repeats,
            "diagnostic_eligible": len(diagnostic_improvements) == repeats,
        }

    result = {
        "schema_version": 1,
        "campaign": "rust-lifetime-prior-stage-b-fast-v1",
        "target_id": target_id,
        "target_source_commit": stage_a.TARGETS[target_id].source_commit,
        "allocator_snapshot": document["allocator_snapshot"],
        "stage_a_results": str(stage_a_results.resolve()),
        "stage_a_results_sha256": stage_a.sha256_file(stage_a_results),
        "stage_b_evaluator": {
            "path": str(Path(__file__).resolve()),
            "sha256": stage_a.sha256_file(Path(__file__).resolve()),
        },
        "allocator_implementation_sha256": builds["compiler-prior"][
            "implementation_sha256"
        ],
        "repeats": repeats,
        "repeat_order_balanced": repeats % 2 == 0,
        "presentation_repeat_count_eligible": repeats >= 4,
        "pinned_cpu": cpu,
        "measurement_lock_path": str(lock_path.resolve()),
        "measurement_seconds": measurement_seconds,
        "work_units": work_units,
        "thp_host_state": _thp_host_state(),
        "arms": list(ARM_NAMES),
        "samples": samples,
        "arm_summary": arm_summary,
        "thp_pair_backing_gates": thp_pair_gates,
        "all_thp_pairs_backed": all(
            gate.get("passed") is True for gate in thp_pair_gates
        ),
        "paired_comparisons": paired_comparisons,
        "claim_scope": {
            "fixed_work_targets": sorted(FIXED_WORK_TARGETS),
            "criterion_targets_diagnostic_only": [
                "swc",
                "rustpython",
                "actix_web",
            ],
            "stage_a_force_track_excluded": True,
            "production_force_track_disabled": True,
            "per_sample_thp_backing_required": True,
            "runtime_only_selective_thp_control_present": True,
            "allocator_vma_residency_attribution": False,
            "thp_causality_scope": (
                "process-level backing correlated with positive allocator activity"
            ),
            "zero_backed_thp_sample_excluded": True,
            "max_or_total_anon_hugepages_never_sufficient": True,
        },
    }
    stage_a.write_json(raw_dir / "stage-b-results.json", result)
    return result


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--stage-a-results", type=Path, required=True)
    parser.add_argument("--target", choices=stage_a.TARGET_ORDER, required=True)
    parser.add_argument("--raw-dir", type=Path, required=True)
    parser.add_argument("--repeats", type=int, default=2)
    parser.add_argument("--cpu", type=int, default=18)
    parser.add_argument("--measurement-seconds", type=float, default=30.0)
    parser.add_argument(
        "--run-timeout", type=float, default=stage_a.HARD_PROCESS_CAP_SECONDS
    )
    parser.add_argument("--sample-interval", type=float, default=0.5)
    parser.add_argument(
        "--lock-path",
        type=Path,
        default=Path("/tmp/unialloc-lifetime-stage-b.lock"),
    )
    args = parser.parse_args(argv)
    if (
        args.repeats < 2
        or args.repeats % 2 != 0
        or args.cpu < 0
        or not stage_a.DEFAULT_SCREEN_MIN_SECONDS
        <= args.measurement_seconds
        <= stage_a.DEFAULT_SCREEN_MAX_SECONDS
        or args.run_timeout <= 0
        or args.sample_interval <= 0
    ):
        parser.error("invalid Stage-B experiment boundary or unbalanced repeat count")
    return args


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    result = run_campaign(
        stage_a_results=args.stage_a_results.resolve(),
        target_id=args.target,
        raw_dir=args.raw_dir.resolve(),
        repeats=args.repeats,
        cpu=args.cpu,
        measurement_seconds=args.measurement_seconds,
        timeout=args.run_timeout,
        sample_interval=args.sample_interval,
        lock_path=args.lock_path,
    )
    print(
        json.dumps(
            {
                "success": True,
                "target_id": result["target_id"],
                "result": str((args.raw_dir / "stage-b-results.json").resolve()),
                "all_thp_pairs_backed": result["all_thp_pairs_backed"],
            },
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
