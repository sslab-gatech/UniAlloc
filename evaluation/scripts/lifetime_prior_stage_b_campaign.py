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
import ast
import contextlib
import json
import math
import os
from pathlib import Path
import re
import signal
import statistics
import subprocess
import sys
import time
import uuid
from typing import Any, Iterator, Mapping, Sequence


SCRIPT_DIR = Path(__file__).resolve().parent
ROOT = SCRIPT_DIR.parents[1]
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import lifetime_prior_six_program_campaign as stage_a  # noqa: E402
from evaluation.scripts import immutable_evidence  # noqa: E402
from evaluation.scripts import type_isolation_suite_contract as suite_contract  # noqa: E402


FIXED_WORK_TARGETS = frozenset({"oxipng", "redb", "polars"})
TRANSPORT_POLICY_DISABLED_ARM = "lifetime_transport_policy_disabled"
ARM_NAMES = (
    TRANSPORT_POLICY_DISABLED_ARM,
    "adaptive-ordinary-all-unknown",
    "adaptive-ordinary-compiler-prior",
    "adaptive-selective-thp-all-unknown",
    "adaptive-selective-thp-compiler-prior",
)
STAGE_A_ARM_NAMES = {
    TRANSPORT_POLICY_DISABLED_ARM: "default",
    **{name: name for name in ARM_NAMES if name != TRANSPORT_POLICY_DISABLED_ARM},
}
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
DEFAULT_MEASUREMENT_LOCK = suite_contract.HOST_PRIMARY_MEASUREMENT_LOCK
STAGE_B_PROTOCOL_SCHEMA_VERSION = 1
STAGE_B_EVIDENCE_SCHEMA_VERSION = 1
GNU_TIME_KEYS = {
    "User time (seconds)": "user_seconds",
    "System time (seconds)": "system_seconds",
    "Elapsed (wall clock) time (h:mm:ss or m:ss)": "wall_time_text",
    "Maximum resident set size (kbytes)": "peak_rss_kib",
    "Exit status": "time_exit_status",
}


def transitive_evaluator_digests(
    seed_paths: Sequence[Path], *, search_root: Path = SCRIPT_DIR
) -> dict[str, str]:
    """Digest the local Python import closure used by an evaluator."""

    root = search_root.resolve()
    pending = [path.resolve() for path in seed_paths]
    visited: set[Path] = set()
    while pending:
        path = pending.pop()
        if path in visited or not path.is_file():
            continue
        visited.add(path)
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        except (OSError, SyntaxError) as error:
            raise stage_a.CampaignContractError(
                f"cannot inspect evaluator dependency: {path}"
            ) from error
        module_names: set[str] = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                module_names.update(alias.name.rsplit(".", 1)[-1] for alias in node.names)
            elif isinstance(node, ast.ImportFrom):
                module = (node.module or "").rsplit(".", 1)[-1]
                if module and module != "scripts":
                    module_names.add(module)
                if node.module in {"evaluation.scripts", "scripts"}:
                    module_names.update(alias.name for alias in node.names)
        for module_name in module_names:
            candidate = root / f"{module_name}.py"
            if candidate.is_file() and candidate.resolve() not in visited:
                pending.append(candidate.resolve())
    result: dict[str, str] = {}
    for path in sorted(visited):
        try:
            key = str(path.relative_to(ROOT))
        except ValueError:
            key = str(path.relative_to(root))
        result[key] = immutable_evidence.sha256_file(path)
    return result


def _parse_wall_seconds(value: str) -> float:
    fields = value.split(":")
    if len(fields) == 2:
        minutes, seconds = fields
        return float(minutes) * 60.0 + float(seconds)
    if len(fields) == 3:
        hours, minutes, seconds = fields
        return float(hours) * 3600.0 + float(minutes) * 60.0 + float(seconds)
    raise stage_a.CampaignContractError(
        f"unexpected GNU time wall format: {value!r}"
    )


def parse_gnu_time(path: Path) -> dict[str, Any]:
    parsed: dict[str, Any] = {}
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        for source, destination in GNU_TIME_KEYS.items():
            prefix = f"{source}: "
            if line.startswith(prefix):
                parsed[destination] = line[len(prefix) :].strip()
                break
    missing = sorted(set(GNU_TIME_KEYS.values()) - set(parsed))
    if missing:
        raise stage_a.CampaignContractError(
            f"GNU time evidence is missing {missing}"
        )
    for field in ("user_seconds", "system_seconds"):
        parsed[field] = float(parsed[field])
    parsed["peak_rss_kib"] = int(parsed["peak_rss_kib"])
    parsed["time_exit_status"] = int(parsed["time_exit_status"])
    parsed["wall_seconds"] = _parse_wall_seconds(str(parsed["wall_time_text"]))
    if (
        parsed["wall_seconds"] <= 0
        or parsed["peak_rss_kib"] <= 0
        or parsed["user_seconds"] < 0
        or parsed["system_seconds"] < 0
    ):
        raise stage_a.CampaignContractError("GNU time evidence is invalid")
    return parsed


def terminate_and_wait_process_group(process: subprocess.Popen[Any]) -> None:
    """Terminate a tracked process group and always reap its leader."""

    if process.poll() is None:
        try:
            os.killpg(process.pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
        try:
            process.wait(timeout=2)
        except subprocess.TimeoutExpired:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            process.wait(timeout=2)
    else:
        process.wait()


def execute_monitored_process_safely(
    command: Sequence[str],
    *,
    cwd: Path,
    env: Mapping[str, str],
    artifact_dir: Path,
    timeout: float,
    sample_interval: float = 0.5,
) -> dict[str, Any]:
    """Stage-A-compatible process runner with unconditional group cleanup."""

    artifact_dir.mkdir(parents=True, exist_ok=True)
    stdout_path = artifact_dir / "stdout.bin"
    stderr_path = artifact_dir / "stderr.bin"
    started = time.monotonic()
    timed_out = False
    samples: list[dict[str, Any]] = []
    observed_descendants: dict[int, dict[str, Any]] = {}
    workload_pid: int | None = None
    time_wrapper = bool(command and command[0] == "/usr/bin/time")
    process: subprocess.Popen[Any] | None = None
    try:
        with stdout_path.open("wb") as stdout_handle, stderr_path.open("wb") as stderr_handle:
            process = subprocess.Popen(
                list(command),
                cwd=cwd,
                env=dict(env),
                stdout=stdout_handle,
                stderr=stderr_handle,
                start_new_session=True,
            )
            while process.poll() is None:
                elapsed = time.monotonic() - started
                descendants = stage_a._descendant_processes(process.pid)  # noqa: SLF001
                if time_wrapper and workload_pid is None:
                    direct = [
                        row
                        for row in descendants
                        if row.get("ppid") == process.pid
                    ]
                    if direct:
                        workload_pid = int(direct[0]["pid"])
                for descendant in descendants:
                    pid = int(descendant["pid"])
                    if not time_wrapper or pid != workload_pid:
                        observed_descendants[pid] = descendant
                sampled_pid = workload_pid if time_wrapper else process.pid
                sample = (
                    stage_a._read_smaps(sampled_pid, elapsed)  # noqa: SLF001
                    if sampled_pid is not None
                    else None
                )
                if sample is not None:
                    samples.append(sample)
                if elapsed > timeout:
                    timed_out = True
                    terminate_and_wait_process_group(process)
                    break
                time.sleep(sample_interval)
            return_code = process.wait()
    finally:
        if process is not None:
            terminate_and_wait_process_group(process)
    elapsed = time.monotonic() - started
    return {
        "command": list(command),
        "cwd": str(cwd.resolve()),
        "exit_code": return_code,
        "timed_out": timed_out,
        "wall_seconds": elapsed,
        "stdout_path": str(stdout_path.resolve()),
        "stderr_path": str(stderr_path.resolve()),
        "stdout_sha256": stage_a.sha256_file(stdout_path),
        "stderr_sha256": stage_a.sha256_file(stderr_path),
        "smaps_samples": samples,
        "single_process_guard": {
            "passed": not observed_descendants and (
                not time_wrapper or workload_pid is not None
            ),
            "root_pid": workload_pid or process.pid,
            "observed_descendant_count": len(observed_descendants),
            "observed_descendants": list(observed_descendants.values()),
            "poll_interval_seconds": sample_interval,
            "guard_kind": "procfs-parent-closure-poll",
        },
    }


@contextlib.contextmanager
def stage_a_safe_process_runner() -> Iterator[None]:
    original = stage_a.execute_monitored_process
    stage_a.execute_monitored_process = execute_monitored_process_safely
    try:
        yield
    finally:
        stage_a.execute_monitored_process = original


@contextlib.contextmanager
def cleanup_on_sigterm() -> Iterator[None]:
    """Translate SIGTERM to stack unwinding so tracked-process finally blocks run."""

    previous = signal.getsignal(signal.SIGTERM)

    def handler(_signum: int, _frame: Any) -> None:
        raise KeyboardInterrupt("SIGTERM")

    signal.signal(signal.SIGTERM, handler)
    try:
        yield
    finally:
        signal.signal(signal.SIGTERM, previous)


def _stage_a_arm_name(arm_name: str) -> str:
    try:
        return STAGE_A_ARM_NAMES[arm_name]
    except KeyError as error:
        raise stage_a.CampaignContractError(
            f"unknown Stage-B arm: {arm_name}"
        ) from error


def _stage_a_arm(arm_name: str) -> Any:
    return stage_a.ARM_BY_NAME[_stage_a_arm_name(arm_name)]


def stage_b_runtime_environment(raw_dir: Path, arm_name: str) -> dict[str, str]:
    arm = _stage_a_arm(arm_name)
    return suite_contract.clean_runtime_environment(
        raw_dir / "tmp" / "runtime" / arm_name,
        overrides={
            stage_a.RUNTIME_ARM_ENV: stage_a.runtime_arm_selector(arm),
            "POLARS_MAX_THREADS": "1",
            "RAYON_NUM_THREADS": "1",
            "TOKIO_WORKER_THREADS": "1",
        },
    )


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
        "long_prior_site_count",
        "short_prior_site_count",
        *stage_a.RUNTIME_OUTCOME_AGGREGATE_FIELDS,
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
        "long_prior_site_count",
        "short_prior_site_count",
        "missing_or_invalid_field_site_count",
        *stage_a.RUNTIME_OUTCOME_AGGREGATE_FIELDS,
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


def measurement_phase_smaps(
    samples: Sequence[Mapping[str, Any]], *, start_seconds: float
) -> dict[str, Any]:
    if (
        isinstance(start_seconds, bool)
        or not isinstance(start_seconds, (int, float))
        or not math.isfinite(float(start_seconds))
        or start_seconds < 0
    ):
        raise stage_a.CampaignContractError(
            "measurement-phase start must be a finite non-negative number"
        )
    selected: list[Mapping[str, Any]] = []
    previous_elapsed = -1.0
    for index, sample in enumerate(samples):
        elapsed = sample.get("elapsed_seconds")
        if (
            isinstance(elapsed, bool)
            or not isinstance(elapsed, (int, float))
            or not math.isfinite(float(elapsed))
            or elapsed < 0
            or elapsed < previous_elapsed
        ):
            raise stage_a.CampaignContractError(
                f"smaps sample {index} has an invalid elapsed time"
            )
        previous_elapsed = float(elapsed)
        if elapsed >= start_seconds:
            selected.append(sample)
    if not selected:
        raise stage_a.CampaignContractError(
            "no smaps samples fall inside the measured phase"
        )
    return {
        "samples": selected,
        "measurement_phase_start_seconds": float(start_seconds),
        "process_sample_count": len(samples),
        "measurement_phase_sample_count": len(selected),
        "excluded_pre_measurement_sample_count": len(samples) - len(selected),
        "claim_boundary": (
            "Criterion warm-up samples are excluded before per-sample backing "
            "validation; every sample in the timed measurement phase remains "
            "subject to the ordinary or selective-THP backing gate"
        ),
    }


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
    expected_revision = snapshot.get("allocator_revision")
    expected_manifest = snapshot.get("campaign_snapshot_sha256")
    for field, value, length in (
        ("allocator_revision", expected_revision, 40),
        ("unialloc_implementation_sha256", expected_implementation, 64),
        ("campaign_snapshot_sha256", expected_manifest, 64),
    ):
        if (
            not isinstance(value, str)
            or re.fullmatch(rf"[0-9a-f]{{{length}}}", value) is None
        ):
            raise stage_a.CampaignContractError(
                f"Stage-A allocator snapshot has an invalid {field}"
            )
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
            or build.get("implementation_revision") != expected_revision
            or build.get("campaign_snapshot_sha256") != expected_manifest
            or build.get("base_campaign_snapshot_sha256")
            != expected_manifest
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


STAGE_B_ARTIFACT_FIELDS = {
    "stdout": "stdout_path",
    "stderr": "stderr_path",
    "gnu_time": "gnu_time_path",
    "runtime_stats": "runtime_stats_path",
    "runtime_sites": "runtime_sites_path",
    "smaps_samples": "smaps_samples_path",
    "compiler_runtime_exact_join": "compiler_runtime_exact_join_path",
    "output_identity": "output_identity_path",
    "output_manifest": "output_manifest_path",
}


def _validate_stage_b_repeats(repeats: int) -> None:
    if isinstance(repeats, bool) or repeats < 4 or repeats % 2 != 0:
        raise stage_a.CampaignContractError(
            "Stage-B claim collection requires an even repeat count of at least four"
        )


def build_stage_b_protocol(
    *,
    document: Mapping[str, Any],
    stage_a_results: Path,
    target_id: str,
    repeats: int,
    cpu: int,
    measurement_seconds: float,
    timeout: float,
    sample_interval: float,
    lock_path: Path,
    criterion_warm_up_seconds: float,
    work_units: int | None,
    input_path: Path | None,
) -> dict[str, Any]:
    """Build the complete immutable compatibility contract for one run root."""

    _validate_stage_b_repeats(repeats)
    snapshot = document["allocator_snapshot"]
    stage_a_path = stage_a_results.resolve()
    evaluator = Path(__file__).resolve()
    compatibility = {
        "campaign": "rust-lifetime-prior-stage-b-fast-v3",
        "target_id": target_id,
        "target_source_commit": stage_a.TARGETS[target_id].source_commit,
        "stage_a_results_sha256": stage_a.sha256_file(stage_a_path),
        "allocator_implementation_revision": snapshot["allocator_revision"],
        "allocator_implementation_sha256": snapshot[
            "unialloc_implementation_sha256"
        ],
        "allocator_manifest_sha256": snapshot["campaign_snapshot_sha256"],
        "evaluator_sha256": stage_a.sha256_file(evaluator),
        "transitive_evaluator_sha256": transitive_evaluator_digests(
            (
                evaluator,
                Path(stage_a.__file__).resolve(),
                Path(immutable_evidence.__file__).resolve(),
                Path(suite_contract.__file__).resolve(),
            )
        ),
        "arms": [
            {
                "arm": arm_name,
                "stage_a_arm": _stage_a_arm_name(arm_name),
                "build_group": _stage_a_arm(arm_name).build_group,
                "runtime_selector": stage_a.runtime_arm_selector(
                    _stage_a_arm(arm_name)
                ),
            }
            for arm_name in ARM_NAMES
        ],
        "repeats": repeats,
        "pinned_cpu": cpu,
        "measurement_seconds": measurement_seconds,
        "timeout_seconds": timeout,
        "sample_interval_seconds": sample_interval,
        "criterion_warm_up_seconds": (
            criterion_warm_up_seconds
            if target_id not in FIXED_WORK_TARGETS
            else None
        ),
        "work_units": work_units,
        "measurement_lock_path": str(lock_path.resolve()),
        "runtime_environment_policy": suite_contract.RUNTIME_ENVIRONMENT_POLICY,
        "rseq_policy": suite_contract.PRODUCTION_RSEQ_POLICY,
        "minimum_claim_repeats": 4,
        "balanced_repeat_order": True,
        "measurement_session_scope": "one-fresh-session-per-repeat",
        "thp_host_state": _thp_host_state(),
        "cell_artifact_contract": (
            "stdout,stderr,gnu-time,smaps,runtime-stats,runtime-sites,"
            "exact-join,output-identity,output-manifest under one attempt"
        ),
        "oxipng_input_sha256": (
            stage_a.sha256_file(input_path) if input_path is not None else None
        ),
    }
    protocol: dict[str, Any] = {
        "schema_version": STAGE_B_PROTOCOL_SCHEMA_VERSION,
        "compatibility": compatibility,
        "provenance": {
            "stage_a_results": str(stage_a_path),
            "evaluator": str(evaluator),
            "oxipng_input": str(input_path.resolve()) if input_path else None,
        },
    }
    protocol["protocol_sha256"] = immutable_evidence.sha256_bytes(
        immutable_evidence.canonical_json_bytes(protocol)
    )
    return protocol


def ensure_stage_b_protocol(
    raw_dir: Path, protocol: Mapping[str, Any]
) -> dict[str, Any]:
    expected = dict(protocol)
    observed_digest = expected.get("protocol_sha256")
    unsigned = dict(expected)
    unsigned.pop("protocol_sha256", None)
    if (
        expected.get("schema_version") != STAGE_B_PROTOCOL_SCHEMA_VERSION
        or observed_digest
        != immutable_evidence.sha256_bytes(
            immutable_evidence.canonical_json_bytes(unsigned)
        )
    ):
        raise stage_a.CampaignContractError("Stage-B protocol digest is invalid")
    try:
        committed = immutable_evidence.persist_immutable_json(
            raw_dir / "stage-b-protocol.json", expected
        )
    except immutable_evidence.ImmutableEvidenceError as error:
        raise stage_a.CampaignContractError(
            "Stage-B protocol differs from the immutable run-root contract"
        ) from error
    value = committed.value
    if not isinstance(value, dict):
        raise stage_a.CampaignContractError("Stage-B protocol is not a JSON object")
    return dict(value)


def ensure_stage_b_measurement_session(
    raw_dir: Path,
    *,
    protocol: Mapping[str, Any],
    target_id: str,
    repeat: int,
) -> dict[str, Any]:
    """Create or reuse the one immutable measurement session for a repeat."""

    compatibility = protocol["compatibility"]
    if (
        compatibility.get("target_id") != target_id
        or isinstance(repeat, bool)
        or not isinstance(repeat, int)
        or repeat < 0
        or repeat >= compatibility.get("repeats", 0)
    ):
        raise stage_a.CampaignContractError(
            "Stage-B session identity differs from its protocol"
    )
    path = raw_dir / "sessions" / target_id / f"repeat-{repeat:02d}.json"
    if path.is_file():
        immutable_evidence.validate_committed_file(path)
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as error:
            raise stage_a.CampaignContractError(
                f"Stage-B session record is unreadable: {path}"
            ) from error
    else:
        value = {
            "schema_version": 1,
            "protocol_sha256": protocol["protocol_sha256"],
            "target_id": target_id,
            "repeat": repeat,
            "measurement_session_id": str(uuid.uuid4()),
            "anchor_arm": TRANSPORT_POLICY_DISABLED_ARM,
        }
        immutable_evidence.persist_immutable_json(path, value)
    expected = {
        "schema_version": 1,
        "protocol_sha256": protocol["protocol_sha256"],
        "target_id": target_id,
        "repeat": repeat,
        "anchor_arm": TRANSPORT_POLICY_DISABLED_ARM,
    }
    if any(value.get(field) != expected_value for field, expected_value in expected.items()):
        raise stage_a.CampaignContractError(
            "Stage-B session record differs from its protocol"
        )
    session_id = value.get("measurement_session_id")
    try:
        parsed = uuid.UUID(str(session_id))
    except (ValueError, AttributeError) as error:
        raise stage_a.CampaignContractError(
            "Stage-B measurement session id is invalid"
        ) from error
    if str(parsed) != session_id:
        raise stage_a.CampaignContractError(
            "Stage-B measurement session id is not canonical"
        )
    return dict(value)


def stage_b_cell_path(
    raw_dir: Path, *, target_id: str, repeat: int, arm_name: str
) -> Path:
    if target_id not in stage_a.TARGET_ORDER or arm_name not in ARM_NAMES:
        raise stage_a.CampaignContractError("invalid Stage-B cell identity")
    if isinstance(repeat, bool) or repeat < 0:
        raise stage_a.CampaignContractError("invalid Stage-B repeat index")
    return (
        raw_dir
        / "cells"
        / target_id
        / f"repeat-{repeat:02d}"
        / arm_name
        / "record.json"
    )


def stage_b_cell_identity(
    protocol: Mapping[str, Any],
    *,
    target_id: str,
    repeat: int,
    arm_name: str,
    measurement_session_id: str,
) -> dict[str, Any]:
    compatibility = protocol["compatibility"]
    if compatibility.get("target_id") != target_id:
        raise stage_a.CampaignContractError(
            "Stage-B cell target differs from its protocol"
        )
    if (
        isinstance(repeat, bool)
        or not isinstance(repeat, int)
        or repeat < 0
        or repeat >= compatibility.get("repeats", 0)
    ):
        raise stage_a.CampaignContractError(
            "Stage-B cell repeat lies outside its protocol"
        )
    expected_arm = {
        row.get("arm"): row
        for row in compatibility.get("arms", [])
        if isinstance(row, dict)
    }.get(arm_name)
    arm = _stage_a_arm(arm_name)
    if expected_arm != {
        "arm": arm_name,
        "stage_a_arm": _stage_a_arm_name(arm_name),
        "build_group": arm.build_group,
        "runtime_selector": stage_a.runtime_arm_selector(arm),
    }:
        raise stage_a.CampaignContractError(
            "Stage-B cell arm differs from its protocol"
        )
    return {
        "protocol_sha256": protocol["protocol_sha256"],
        "target_id": target_id,
        "target_source_commit": compatibility["target_source_commit"],
        "stage_a_results_sha256": compatibility["stage_a_results_sha256"],
        "allocator_implementation_revision": compatibility[
            "allocator_implementation_revision"
        ],
        "allocator_implementation_sha256": compatibility[
            "allocator_implementation_sha256"
        ],
        "allocator_manifest_sha256": compatibility[
            "allocator_manifest_sha256"
        ],
        "arm": arm_name,
        "stage_a_arm": _stage_a_arm_name(arm_name),
        "build_group": arm.build_group,
        "repeat": repeat,
        "measurement_session_id": measurement_session_id,
        "work_units": compatibility["work_units"],
        "pinned_cpu": compatibility["pinned_cpu"],
        "criterion_warm_up_seconds": compatibility[
            "criterion_warm_up_seconds"
        ],
    }


def _stage_b_evidence_record(
    identity: Mapping[str, Any], collected: Mapping[str, Any]
) -> dict[str, Any]:
    artifacts: dict[str, Any] = {}
    for artifact_name, path_field in STAGE_B_ARTIFACT_FIELDS.items():
        path_value = collected.get(path_field)
        if not isinstance(path_value, str) or not path_value:
            raise stage_a.CampaignContractError(
                f"Stage-B sample lacks {path_field}"
            )
        artifacts[artifact_name] = immutable_evidence.artifact_ref(
            Path(path_value)
        )
    sample, metrics = derive_stage_b_cell(identity, artifacts)
    return {
        "evidence_schema_version": STAGE_B_EVIDENCE_SCHEMA_VERSION,
        "identity": dict(identity),
        "metrics": metrics,
        "artifacts": artifacts,
        "sample": sample,
    }


def _json_artifact(path: Path, expected_type: type[Any], label: str) -> Any:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise immutable_evidence.ImmutableEvidenceError(
            f"Stage-B {label} artifact is invalid"
        ) from error
    if not isinstance(value, expected_type):
        raise immutable_evidence.ImmutableEvidenceError(
            f"Stage-B {label} artifact has the wrong type"
        )
    return value


def _validate_stage_b_artifact_containment(
    artifacts: Mapping[str, Any], *, attempts_root: Path
) -> tuple[dict[str, Path], Path]:
    if set(artifacts) != set(STAGE_B_ARTIFACT_FIELDS):
        raise immutable_evidence.ImmutableEvidenceError(
            "Stage-B artifact set differs from its protocol"
        )
    paths: dict[str, Path] = {}
    attempt_directories: set[Path] = set()
    root = attempts_root.resolve()
    for name in STAGE_B_ARTIFACT_FIELDS:
        path = immutable_evidence.validate_artifact_ref(
            artifacts[name], context=f"Stage-B {name}"
        )
        try:
            relative = path.relative_to(root)
        except ValueError as error:
            raise immutable_evidence.ImmutableEvidenceError(
                f"Stage-B {name} is outside its attempt directory"
            ) from error
        if len(relative.parts) < 2:
            raise immutable_evidence.ImmutableEvidenceError(
                f"Stage-B {name} has no unique attempt directory"
            )
        attempt_directories.add(root / relative.parts[0])
        paths[name] = path
    if len(attempt_directories) != 1:
        raise immutable_evidence.ImmutableEvidenceError(
            "Stage-B artifacts mix multiple attempts"
        )
    return paths, attempt_directories.pop()


def derive_stage_b_cell(
    identity: Mapping[str, Any], artifacts: Mapping[str, Any]
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Rederive every metric and pre-gate input from committed artifacts."""

    paths = {
        name: immutable_evidence.validate_artifact_ref(
            artifacts[name], context=f"Stage-B {name}"
        )
        for name in STAGE_B_ARTIFACT_FIELDS
    }
    stdout = paths["stdout"].read_text(encoding="utf-8", errors="replace")
    stderr = paths["stderr"].read_text(encoding="utf-8", errors="replace")
    gnu_time = parse_gnu_time(paths["gnu_time"])
    if gnu_time["time_exit_status"] != 0:
        raise immutable_evidence.ImmutableEvidenceError(
            "Stage-B GNU time reports a failed process"
        )
    smaps_value = _json_artifact(paths["smaps_samples"], list, "smaps")
    if not smaps_value or any(not isinstance(row, dict) for row in smaps_value):
        raise immutable_evidence.ImmutableEvidenceError(
            "Stage-B smaps artifact contains no samples"
        )
    runtime_stats = _json_artifact(
        paths["runtime_stats"], dict, "runtime stats"
    )
    runtime_sites = _json_artifact(
        paths["runtime_sites"], list, "runtime sites"
    )
    if any(not isinstance(row, dict) for row in runtime_sites):
        raise immutable_evidence.ImmutableEvidenceError(
            "Stage-B runtime-site artifact is malformed"
        )
    exact_join = _json_artifact(
        paths["compiler_runtime_exact_join"], dict, "exact join"
    )
    try:
        stage_a.validate_runtime_evidence(
            _stage_a_arm(str(identity["arm"])), runtime_stats, runtime_sites
        )
        fragmentation = stage_a.parse_fragmentation(stderr)
    except stage_a.CampaignContractError as error:
        raise immutable_evidence.ImmutableEvidenceError(str(error)) from error
    target_id = str(identity["target_id"])
    work_units = identity.get("work_units")
    if target_id in FIXED_WORK_TARGETS:
        if isinstance(work_units, bool) or not isinstance(work_units, int) or work_units <= 0:
            raise immutable_evidence.ImmutableEvidenceError(
                "Stage-B fixed-work identity has no work count"
            )
        primary_metric = {
            "source": "gnu-time-fixed-work-wall-clock",
            "seconds_per_work_unit": gnu_time["wall_seconds"] / work_units,
            "work_units_per_second": work_units / gnu_time["wall_seconds"],
            "diagnostic_only": False,
        }
        performance_unit = "seconds_per_work_unit"
    else:
        primary_metric = parse_criterion_estimate(stdout)
        performance_unit = "seconds_per_iteration"
    performance = _metric_value(primary_metric)
    duration_eligible = (
        stage_a.DEFAULT_SCREEN_MIN_SECONDS
        <= gnu_time["wall_seconds"]
        <= stage_a.DEFAULT_SCREEN_MAX_SECONDS
    )
    arm = _stage_a_arm(str(identity["arm"]))
    prior_coverage_complete = (
        exact_prior_coverage_complete(exact_join)
        if arm.build_group == "compiler-prior"
        else None
    )
    prior_effect_observed = (
        exact_prior_effect_observed(exact_join)
        if arm.build_group == "compiler-prior"
        else None
    )
    runtime_control_clean = (
        all_unknown_control_clean(exact_join)
        if arm.build_group == "all-unknown"
        else None
    )
    pre_gate_eligible = (
        target_id in FIXED_WORK_TARGETS
        and duration_eligible
        and prior_coverage_complete is not False
        and prior_effect_observed is not False
        and runtime_control_clean is not False
    )
    classification = stage_a.runtime_classification_summary(
        runtime_stats, evidence_stage="production"
    )
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
            "pre_gate_eligible": pre_gate_eligible,
        }
    )
    for forbidden in ("performance_claim_eligible", "final_eligible"):
        classification.pop(forbidden, None)
    output_identity = _json_artifact(
        paths["output_identity"], dict, "output identity"
    )
    output_manifest = _json_artifact(
        paths["output_manifest"], dict, "output manifest"
    )
    if (
        output_manifest.get("target_id") != target_id
        or not isinstance(output_manifest.get("files"), list)
    ):
        raise immutable_evidence.ImmutableEvidenceError(
            "Stage-B output manifest is malformed"
        )
    attempt_dir = paths["output_manifest"].parent.resolve()
    manifest_paths: list[Path] = []
    for index, reference in enumerate(output_manifest["files"]):
        output_path = immutable_evidence.validate_artifact_ref(
            reference, context=f"Stage-B workload output {index}"
        )
        try:
            output_path.relative_to(attempt_dir)
        except ValueError as error:
            raise immutable_evidence.ImmutableEvidenceError(
                "Stage-B workload output is outside its attempt"
            ) from error
        manifest_paths.append(output_path)
    work_dir = (
        attempt_dir
        / "workloads"
        / target_id
        / str(identity["build_group"])
        / "sample"
    )
    expected_output_paths = (
        sorted((work_dir / "out").glob("*.png"))
        if target_id == "oxipng"
        else []
    )
    if manifest_paths != [path.resolve() for path in expected_output_paths]:
        raise immutable_evidence.ImmutableEvidenceError(
            "Stage-B workload output manifest differs from retained outputs"
        )
    try:
        derived_output_identity = stage_a.workload_output_identity(
            target_id,
            stdout=stdout,
            work_dir=work_dir,
            work_units=work_units,
        )
    except stage_a.CampaignContractError as error:
        raise immutable_evidence.ImmutableEvidenceError(str(error)) from error
    if output_identity != derived_output_identity:
        raise immutable_evidence.ImmutableEvidenceError(
            "Stage-B output identity differs from retained workload evidence"
        )
    sample = {
        "target_id": target_id,
        "arm": identity["arm"],
        "stage_a_arm": identity["stage_a_arm"],
        "build_group": identity["build_group"],
        "repeat": identity["repeat"],
        "measurement_session_id": identity["measurement_session_id"],
        "work_units": work_units,
        "primary_metric": primary_metric,
        "primary_metric_value_seconds": performance,
        "gnu_time": gnu_time,
        "wall_seconds": gnu_time["wall_seconds"],
        "duration_eligible": duration_eligible,
        "duration_status": (
            "inside-fast-boundary"
            if duration_eligible
            else "retained-outside-fast-boundary"
        ),
        "pinned_cpu": identity["pinned_cpu"],
        "full_executed_prior_coverage": prior_coverage_complete,
        "compiler_prior_effect_observed": prior_effect_observed,
        "all_unknown_control_clean": runtime_control_clean,
        "force_track_enabled": False,
        "classification": classification,
        "pre_gate_eligible": pre_gate_eligible,
        "fragmentation": fragmentation,
        "procfs": stage_a.summarize_proc_samples(smaps_value),
        "runtime_stats": runtime_stats,
        "compiler_runtime_exact_join": stage_a.compact_compiler_runtime_exact_join(
            exact_join
        ),
        "output_identity": output_identity,
        **{
            path_field: str(paths[artifact_name])
            for artifact_name, path_field in STAGE_B_ARTIFACT_FIELDS.items()
        },
        "stdout_sha256": artifacts["stdout"]["sha256"],
        "stderr_sha256": artifacts["stderr"]["sha256"],
    }
    metrics = {
        "performance": performance,
        "performance_unit": performance_unit,
        "peak_rss_mib": gnu_time["peak_rss_kib"] / 1024.0,
    }
    return sample, metrics


def validate_stage_b_cell_record(
    record: Mapping[str, Any],
    *,
    expected_identity: Mapping[str, Any],
    attempts_root: Path,
) -> None:
    immutable_evidence.validate_measurement_record(
        record, expected_identity=expected_identity
    )
    observed_sample = record.get("sample")
    if not isinstance(observed_sample, dict):
        raise immutable_evidence.ImmutableEvidenceError(
            "Stage-B evidence has no sample object"
        )
    artifacts = record["artifacts"]
    _validate_stage_b_artifact_containment(
        artifacts, attempts_root=attempts_root
    )
    derived_sample, derived_metrics = derive_stage_b_cell(
        expected_identity, artifacts
    )
    if observed_sample != derived_sample or record.get("metrics") != derived_metrics:
        raise immutable_evidence.ImmutableEvidenceError(
            "Stage-B sample or metrics differ from artifact-derived evidence"
        )
    if "performance_claim_eligible" in observed_sample:
        raise immutable_evidence.ImmutableEvidenceError(
            "Stage-B cells may store only pre-gate eligibility"
        )


def run_or_reuse_stage_b_cell(
    *,
    raw_dir: Path,
    protocol: Mapping[str, Any],
    target_id: str,
    repeat: int,
    arm_name: str,
    measurement_session_id: str,
    collect_sample: Any,
) -> dict[str, Any]:
    """Collect one attempt or reuse the exact previously committed cell."""

    identity = stage_b_cell_identity(
        protocol,
        target_id=target_id,
        repeat=repeat,
        arm_name=arm_name,
        measurement_session_id=measurement_session_id,
    )
    commit_path = stage_b_cell_path(
        raw_dir, target_id=target_id, repeat=repeat, arm_name=arm_name
    )

    def collect(attempt_dir: Path) -> Mapping[str, Any]:
        sample = collect_sample(attempt_dir)
        if not isinstance(sample, dict):
            raise stage_a.CampaignContractError(
                "Stage-B cell collector returned no sample object"
            )
        return _stage_b_evidence_record(identity, sample)

    committed = immutable_evidence.run_or_reuse_json(
        commit_path,
        collect,
        lambda value: validate_stage_b_cell_record(
            value,
            expected_identity=identity,
            attempts_root=commit_path.parent / "attempts" / commit_path.stem,
        ),
    )
    sample = dict(committed.value["sample"])
    sample["record_path"] = str(
        committed.path.relative_to(raw_dir.resolve())
    )
    sample["record_sha256"] = committed.record_sha256
    return sample


def _committed_reference(raw_dir: Path, path_value: str, digest: str) -> dict[str, str]:
    path = (raw_dir / path_value).resolve()
    immutable_evidence.validate_committed_file(path)
    if immutable_evidence.sha256_file(path) != digest:
        raise stage_a.CampaignContractError(
            f"committed Stage-B reference digest changed: {path}"
        )
    return {"path": str(path.relative_to(raw_dir.resolve())), "sha256": digest}


def persist_stage_b_repeat_result(
    raw_dir: Path,
    *,
    protocol: Mapping[str, Any],
    target_id: str,
    repeat: int,
    measurement_session_id: str,
    by_arm: Mapping[str, Mapping[str, Any]],
    thp_gates: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    """Publish all post-gate decisions for one exact set of cell digests."""

    if set(by_arm) != set(ARM_NAMES):
        raise stage_a.CampaignContractError(
            "Stage-B repeat result does not cover every arm"
        )
    cells: list[dict[str, Any]] = []
    arms: dict[str, Any] = {}
    for arm_name in ARM_NAMES:
        row = by_arm[arm_name]
        if row.get("measurement_session_id") != measurement_session_id:
            raise stage_a.CampaignContractError(
                "Stage-B repeat mixes measurement sessions"
            )
        reference = _committed_reference(
            raw_dir, str(row["record_path"]), str(row["record_sha256"])
        )
        cells.append({"arm": arm_name, **reference})
        arms[arm_name] = {
            "cell": reference,
            "pre_gate_eligible": row["pre_gate_eligible"],
            "performance_claim_eligible": row["performance_claim_eligible"],
            "primary_metric_value_seconds": row[
                "primary_metric_value_seconds"
            ],
            "peak_rss_kib": row["gnu_time"]["peak_rss_kib"],
            "cross_arm_output_equivalent": row.get(
                "cross_arm_output_equivalent"
            ),
            "ordinary_backing_gate": row.get("ordinary_backing_gate"),
            "thp_pair_backing_gate": row.get("thp_pair_backing_gate"),
            "smaps_measurement_phase": row["smaps_measurement_phase"],
        }
    value = {
        "schema_version": 1,
        "protocol_sha256": protocol["protocol_sha256"],
        "target_id": target_id,
        "repeat": repeat,
        "measurement_session_id": measurement_session_id,
        "cells": cells,
        "arms": arms,
        "thp_pair_backing_gates": [dict(gate) for gate in thp_gates],
        "all_thp_pairs_backed": all(
            gate.get("passed") is True for gate in thp_gates
        ),
    }
    path = raw_dir / "repeat-results" / target_id / f"repeat-{repeat:02d}.json"
    committed = immutable_evidence.persist_immutable_json(path, value)
    return {
        "repeat": repeat,
        "measurement_session_id": measurement_session_id,
        "path": str(committed.path.relative_to(raw_dir.resolve())),
        "sha256": committed.record_sha256,
    }


def persist_stage_b_pair_result(
    raw_dir: Path,
    *,
    protocol: Mapping[str, Any],
    target_id: str,
    name: str,
    comparison: Mapping[str, Any],
    samples_by_arm: Mapping[str, Sequence[Mapping[str, Any]]],
    repeat_records: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    baseline_arm = str(comparison["baseline_arm"])
    candidate_arm = str(comparison["candidate_arm"])
    pairs: list[dict[str, Any]] = []
    for repeat_record in repeat_records:
        repeat = int(repeat_record["repeat"])
        baseline = next(
            row for row in samples_by_arm[baseline_arm] if row["repeat"] == repeat
        )
        candidate = next(
            row for row in samples_by_arm[candidate_arm] if row["repeat"] == repeat
        )
        if (
            baseline["measurement_session_id"]
            != candidate["measurement_session_id"]
            or baseline["measurement_session_id"]
            != repeat_record["measurement_session_id"]
        ):
            raise stage_a.CampaignContractError(
                f"Stage-B paired result mixes sessions: {name}/{repeat}"
            )
        pairs.append(
            {
                "repeat": repeat,
                "measurement_session_id": baseline[
                    "measurement_session_id"
                ],
                "repeat_result": _committed_reference(
                    raw_dir,
                    str(repeat_record["path"]),
                    str(repeat_record["sha256"]),
                ),
                "baseline_cell": _committed_reference(
                    raw_dir,
                    str(baseline["record_path"]),
                    str(baseline["record_sha256"]),
                ),
                "candidate_cell": _committed_reference(
                    raw_dir,
                    str(candidate["record_path"]),
                    str(candidate["record_sha256"]),
                ),
            }
        )
    value = {
        "schema_version": 1,
        "protocol_sha256": protocol["protocol_sha256"],
        "target_id": target_id,
        "comparison_name": name,
        "baseline_arm": baseline_arm,
        "candidate_arm": candidate_arm,
        "cell_pairs": pairs,
        "comparison": dict(comparison),
    }
    path = raw_dir / "pair-results" / target_id / f"{name}.json"
    committed = immutable_evidence.persist_immutable_json(path, value)
    return {
        "comparison_name": name,
        "path": str(committed.path.relative_to(raw_dir.resolve())),
        "sha256": committed.record_sha256,
    }


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
    criterion_warm_up_seconds: float,
) -> dict[str, Any]:
    _validate_stage_b_repeats(repeats)
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
    if lock_path.resolve() != DEFAULT_MEASUREMENT_LOCK.resolve():
        raise stage_a.CampaignContractError(
            "Stage-B requires the host-wide primary measurement lock"
        )
    protocol = ensure_stage_b_protocol(
        raw_dir,
        build_stage_b_protocol(
            document=document,
            stage_a_results=stage_a_results,
            target_id=target_id,
            repeats=repeats,
            cpu=cpu,
            measurement_seconds=measurement_seconds,
            timeout=timeout,
            sample_interval=sample_interval,
            lock_path=lock_path,
            criterion_warm_up_seconds=criterion_warm_up_seconds,
            work_units=work_units,
            input_path=input_path,
        ),
    )
    samples: list[dict[str, Any]] = []
    thp_pair_gates: list[dict[str, Any]] = []
    repeat_records: list[dict[str, Any]] = []

    with cleanup_on_sigterm(), suite_contract.primary_measurement_lock(
        runner=Path(__file__).name,
        raw_root=raw_dir,
        target_id=target_id,
        phase="warmup-and-measured",
    ) as measurement_lock:
        for repeat in range(repeats):
            thp_gate_start = len(thp_pair_gates)
            session = ensure_stage_b_measurement_session(
                raw_dir,
                protocol=protocol,
                target_id=target_id,
                repeat=repeat,
            )
            by_arm: dict[str, dict[str, Any]] = {}
            for arm_name in _arm_order(repeat):
                arm = _stage_a_arm(arm_name)

                def collect_sample(attempt_dir: Path) -> dict[str, Any]:
                    runtime_environment = stage_b_runtime_environment(
                        attempt_dir, arm_name
                    )
                    if target_id == "rustpython":
                        dependency = builds[arm.build_group].get(
                            "runtime_dependency"
                        )
                        if not isinstance(dependency, dict):
                            raise stage_a.CampaignContractError(
                                "RustPython Stage-B runtime dependency is missing"
                            )
                        runtime_environment = stage_a.psr.apply_runtime_environment(
                            stage_a.psr.TARGET_SPECS["rustpython"],
                            runtime_environment,
                            dependency,
                        )
                    gnu_time_path = attempt_dir / "gnu-time.txt"
                    with stage_a_safe_process_runner():
                        sample = stage_a.run_stage_a_sample(
                            target_id,
                            arm.build_group,
                            build=builds[arm.build_group],
                            raw_dir=attempt_dir,
                            input_path=input_path,
                            work_units=work_units,
                            measurement_seconds=measurement_seconds,
                            timeout=timeout,
                            sample_interval=sample_interval,
                            validate_duration=False,
                            run_label="sample",
                            arm_name=_stage_a_arm_name(arm_name),
                            command_prefix=(
                                "/usr/bin/time",
                                "-v",
                                "-o",
                                str(gnu_time_path),
                                "taskset",
                                "-c",
                                str(cpu),
                            ),
                            evidence_stage="production",
                            criterion_warm_up_seconds=criterion_warm_up_seconds,
                            runtime_environment_override=runtime_environment,
                        )
                    runtime_sites_path = Path(sample["runtime_sites_path"])
                    runtime_stats_path = runtime_sites_path.with_name(
                        "runtime-stats.json"
                    )
                    work_dir = (
                        attempt_dir
                        / "workloads"
                        / target_id
                        / arm.build_group
                        / "sample"
                    )
                    output_files = (
                        sorted((work_dir / "out").glob("*.png"))
                        if target_id == "oxipng"
                        else []
                    )
                    output_manifest_path = attempt_dir / "output-manifest.json"
                    output_identity_path = attempt_dir / "output-identity.json"
                    immutable_evidence.atomic_write_bytes(
                        output_manifest_path,
                        immutable_evidence.canonical_json_bytes(
                            {
                                "target_id": target_id,
                                "files": [
                                    immutable_evidence.artifact_ref(path)
                                    for path in output_files
                                ],
                            }
                        ),
                    )
                    immutable_evidence.atomic_write_bytes(
                        output_identity_path,
                        immutable_evidence.canonical_json_bytes(
                            sample["output_identity"]
                        ),
                    )
                    return {
                        "stdout_path": sample["stdout_path"],
                        "stderr_path": sample["stderr_path"],
                        "gnu_time_path": str(gnu_time_path.resolve()),
                        "runtime_stats_path": str(runtime_stats_path.resolve()),
                        "runtime_sites_path": sample["runtime_sites_path"],
                        "smaps_samples_path": sample["smaps_samples_path"],
                        "compiler_runtime_exact_join_path": sample[
                            "compiler_runtime_exact_join_path"
                        ],
                        "output_identity_path": str(
                            output_identity_path.resolve()
                        ),
                        "output_manifest_path": str(
                            output_manifest_path.resolve()
                        ),
                    }

                row = run_or_reuse_stage_b_cell(
                    raw_dir=raw_dir,
                    protocol=protocol,
                    target_id=target_id,
                    repeat=repeat,
                    arm_name=arm_name,
                    measurement_session_id=session["measurement_session_id"],
                    collect_sample=collect_sample,
                )
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
            measurement_phase_start = (
                0.0
                if target_id in FIXED_WORK_TARGETS
                else criterion_warm_up_seconds
            )
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
                measured_phase = measurement_phase_smaps(
                    smaps_value, start_seconds=measurement_phase_start
                )
                smaps_by_arm[arm_name] = list(measured_phase.pop("samples"))
                row["smaps_measurement_phase"] = measured_phase

            for arm_name in (
                TRANSPORT_POLICY_DISABLED_ARM,
                "adaptive-ordinary-all-unknown",
                "adaptive-ordinary-compiler-prior",
            ):
                row = by_arm[arm_name]
                ordinary_gate = strict_ordinary_backing_gate(
                    smaps_by_arm[arm_name]
                )
                row["ordinary_backing_gate"] = ordinary_gate
                row["performance_claim_eligible"] = (
                    row["pre_gate_eligible"]
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
                    thp_row["pre_gate_eligible"]
                    and gate["passed"] is True
                )
                thp_row["classification"]["performance_claim_eligible"] = (
                    thp_row["performance_claim_eligible"]
                )
            repeat_records.append(
                persist_stage_b_repeat_result(
                    raw_dir,
                    protocol=protocol,
                    target_id=target_id,
                    repeat=repeat,
                    measurement_session_id=session[
                        "measurement_session_id"
                    ],
                    by_arm=by_arm,
                    thp_gates=thp_pair_gates[thp_gate_start:],
                )
            )

    if len({row["measurement_session_id"] for row in repeat_records}) != repeats:
        raise stage_a.CampaignContractError(
            "Stage-B repeats do not use distinct measurement sessions"
        )

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
                [float(row["gnu_time"]["peak_rss_kib"]) for row in rows]
            ),
            "all_performance_claim_eligible": all(
                row["performance_claim_eligible"] for row in rows
            ),
            "all_full_executed_prior_coverage": (
                all(row["full_executed_prior_coverage"] is True for row in rows)
                if _stage_a_arm(arm).build_group == "compiler-prior"
                else None
            ),
            "all_compiler_prior_effect_observed": (
                all(row["compiler_prior_effect_observed"] is True for row in rows)
                if _stage_a_arm(arm).build_group == "compiler-prior"
                else None
            ),
            "all_unknown_controls_clean": (
                all(row["all_unknown_control_clean"] is True for row in rows)
                if _stage_a_arm(arm).build_group == "all-unknown"
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
            TRANSPORT_POLICY_DISABLED_ARM,
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
            TRANSPORT_POLICY_DISABLED_ARM,
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
                    "baseline_peak_rss_kib": baseline["gnu_time"][
                        "peak_rss_kib"
                    ],
                    "candidate_peak_rss_kib": candidate["gnu_time"][
                        "peak_rss_kib"
                    ],
                    "peak_rss_percent_reduction": _optional_percent_reduction(
                        baseline["gnu_time"]["peak_rss_kib"],
                        candidate["gnu_time"]["peak_rss_kib"],
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

    pair_records = [
        persist_stage_b_pair_result(
            raw_dir,
            protocol=protocol,
            target_id=target_id,
            name=name,
            comparison=comparison,
            samples_by_arm=samples_by_arm,
            repeat_records=repeat_records,
        )
        for name, comparison in paired_comparisons.items()
    ]
    result = {
        "schema_version": 3,
        "campaign": "rust-lifetime-prior-stage-b-fast-v3",
        "target_id": target_id,
        "target_source_commit": stage_a.TARGETS[target_id].source_commit,
        "allocator_snapshot": document["allocator_snapshot"],
        "stage_a_results": str(stage_a_results.resolve()),
        "stage_a_results_sha256": stage_a.sha256_file(stage_a_results),
        "stage_b_protocol": str((raw_dir / "stage-b-protocol.json").resolve()),
        "stage_b_protocol_sha256": protocol["protocol_sha256"],
        "stage_b_evaluator": {
            "path": str(Path(__file__).resolve()),
            "sha256": stage_a.sha256_file(Path(__file__).resolve()),
        },
        "allocator_implementation_sha256": builds["compiler-prior"][
            "implementation_sha256"
        ],
        "allocator_implementation_revision": builds["compiler-prior"][
            "implementation_revision"
        ],
        "allocator_manifest_sha256": builds["compiler-prior"][
            "campaign_snapshot_sha256"
        ],
        "repeats": repeats,
        "repeat_order_balanced": repeats % 2 == 0,
        "presentation_repeat_count_eligible": repeats >= 4,
        "pinned_cpu": cpu,
        "measurement_lock_path": str(lock_path.resolve()),
        "measurement_lock_held_during_collection": True,
        "runtime_environment_policy": (
            suite_contract.RUNTIME_ENVIRONMENT_POLICY
        ),
        "rseq_policy": suite_contract.PRODUCTION_RSEQ_POLICY,
        "measurement_seconds": measurement_seconds,
        "criterion_warm_up_seconds": (
            criterion_warm_up_seconds
            if target_id not in FIXED_WORK_TARGETS
            else None
        ),
        "work_units": work_units,
        "thp_host_state": protocol["compatibility"]["thp_host_state"],
        "arms": list(ARM_NAMES),
        "samples": samples,
        "repeat_results": repeat_records,
        "pair_results": pair_records,
        "cell_records": [
            {
                "target_id": row["target_id"],
                "repeat": row["repeat"],
                "arm": row["arm"],
                "record_path": row["record_path"],
                "record_sha256": row["record_sha256"],
            }
            for row in samples
        ],
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
    immutable_evidence.persist_immutable_json(
        raw_dir / "stage-b-results.json", result
    )
    return result


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--stage-a-results", type=Path, required=True)
    parser.add_argument("--target", choices=stage_a.TARGET_ORDER, required=True)
    parser.add_argument("--raw-dir", type=Path, required=True)
    parser.add_argument("--repeats", type=int, default=4)
    parser.add_argument("--cpu", type=int, default=18)
    parser.add_argument("--measurement-seconds", type=float, default=30.0)
    parser.add_argument(
        "--run-timeout", type=float, default=stage_a.HARD_PROCESS_CAP_SECONDS
    )
    parser.add_argument("--sample-interval", type=float, default=0.5)
    parser.add_argument("--criterion-warm-up-seconds", type=float, default=5.0)
    parser.add_argument(
        "--lock-path",
        type=Path,
        default=DEFAULT_MEASUREMENT_LOCK,
    )
    args = parser.parse_args(argv)
    if (
        args.repeats < 4
        or args.repeats % 2 != 0
        or args.cpu < 0
        or not stage_a.DEFAULT_SCREEN_MIN_SECONDS
        <= args.measurement_seconds
        <= stage_a.DEFAULT_SCREEN_MAX_SECONDS
        or args.run_timeout <= 0
        or args.sample_interval <= 0
        or not 0.0 < args.criterion_warm_up_seconds <= 20.0
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
        criterion_warm_up_seconds=args.criterion_warm_up_seconds,
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
