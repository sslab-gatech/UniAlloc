#!/usr/bin/env python3
"""Run or verify one terminal command plan for the evaluable RustSec scope.

The efficacy report joins the scope-recorded evaluable cases, representative
scenario catalogs, and a live executable-case replay.  Non-evaluable reviewed
candidates remain in a separate exclusion audit whose terminal attempts never
enter the efficacy denominator or CSV.  A command plan may regenerate either
live artifact; a verification plan only revalidates pinned artifacts.
``--dry-run`` validates the same partition without executing commands.

This driver reports observations.  It does not upgrade allocator-signal
observations into source-vulnerability mitigation claims.
"""

from __future__ import annotations

import argparse
import collections
import csv
import json
import pathlib
import re
import subprocess
import sys
import time
from typing import Any, Iterable, Sequence


SCRIPT_DIR = pathlib.Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

import run_rustsec_heap_sweep as sweep  # noqa: E402


ROOT = pathlib.Path(__file__).resolve().parents[2]
DEFAULT_SCOPE = ROOT / "evaluation" / "config" / "rustsec_heap_complete_scope.json"
DEFAULT_CATALOGS = (
    ROOT / "evaluation" / "config" / "rustsec_heap_harnesses.json",
    ROOT / "evaluation" / "config" / "rustsec_heap_expansion_harnesses.json",
    *tuple(
        ROOT
        / "evaluation"
        / "config"
        / f"rustsec_heap_strong_batch_{batch}_harnesses.json"
        for batch in "abcdef"
    ),
    ROOT / "evaluation" / "config" / "rustsec_heap_neon_node_harnesses.json",
)
CASE_RE = re.compile(r"^RSH-[0-9]{3}$")
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
PLAN_JOB_IDS = frozenset(("executable_replay", "blocked_attempts"))
PLAN_ACTIONS = frozenset(("run", "verify"))
STRICT_RECLAIM_CHECKS = frozenset(
    (
        "baseline_reproduced_in_all_repetitions",
        "exact_allocator_signal_in_all_repetitions",
        "feature_matched_reclaim_ablation_valid",
        "patched_reclaim_plain_control_valid",
        "patched_system_control_valid",
        "patched_treatment_signal_free",
        "vulnerable_reclaim_plain_completed_in_all_repetitions",
        "vulnerable_reclaim_plain_exact_signal_absent",
        "vulnerable_treatment_completed_in_all_repetitions",
        "vulnerable_treatment_status_valid",
    )
)
STRICT_RECLAIM_PROVENANCE = frozenset(
    (
        "patched_reclaim_checks",
        "patched_reclaim_plain",
        "vulnerable_reclaim_checks",
        "vulnerable_reclaim_plain",
    )
)
STRICT_TYPEISO_CHECKS = frozenset(
    (
        "claim_scope_present",
        "matched_arms_present",
        "minimum_repetitions_met",
        "orchestration_success",
        "patched_system_control_valid",
        "patched_typed_plain_control_valid",
        "patched_typeiso_control_valid",
        "system_has_no_reuse_denial",
        "system_reuse_observed_in_all_repetitions",
        "typed_allocator_oracle_policy_valid",
        "typed_plain_has_no_reuse_denial",
        "typed_plain_reuse_observed_in_all_repetitions",
        "typeiso_address_reuse_blocked_in_all_repetitions",
        "typeiso_denial_observed_in_all_repetitions",
        "typeiso_report_bound_in_all_repetitions",
        "vulnerable_system_completed_in_all_repetitions",
        "vulnerable_system_oracle_observed",
        "vulnerable_typed_plain_completed_in_all_repetitions",
        "vulnerable_typed_plain_oracle_contract_satisfied",
        "vulnerable_typeiso_completed_in_all_repetitions",
    )
)
STRICT_RECOVERY_LAYOUT_CHECKS = frozenset(
    (
        "matched_arms_present",
        "minimum_repetitions_met",
        "patched_typed_plain_control_signal_free",
        "patched_typeiso_control_signal_free",
        "signal_independent_of_type_isolation_policy",
        "strict_matrix_valid",
        "upstream_miri_baseline_bound",
        "vulnerable_typed_plain_exact_signal_in_all_repetitions",
        "vulnerable_typeiso_exact_signal_in_all_repetitions",
    )
)
SYNTHETIC_TYPEISO_REDUCTION_CASES = frozenset(("RSH-065", "RSH-069"))
NONTERMINAL_ATTEMPT_STATUSES = frozenset(
    ("pending", "planned", "queued", "running", "not_started")
)
CSV_FIELDS = (
    "case_id",
    "advisory_id",
    "package",
    "primary_primitive",
    "root_cause",
    "root_cause_source",
    "scope_status",
    "status",
    "outcome",
    "representative_scenario_id",
    "catalog_path",
    "scenario_source_path",
    "oracle_tool",
    "mechanism",
    "live_arm_count",
    "reclaim_exact_signal_observed",
    "layout_validation_signal_observed",
    "typeiso_reuse_denial_observed",
    "raw_exact_allocator_signal",
    "implementation_digests",
    "scope_review_required",
    "blocker_reason",
    "blocker_code",
    "conclusion",
    "probe_count",
    "source_artifact",
)


class CompleteScopeError(RuntimeError):
    """A fail-closed scope, plan, catalog, or live-artifact error."""


def load_object(path: pathlib.Path, label: str) -> dict[str, Any]:
    try:
        value = sweep.load_json(path)
    except (OSError, json.JSONDecodeError) as error:
        raise CompleteScopeError(f"cannot read {label} {path}: {error}") from error
    if not isinstance(value, dict):
        raise CompleteScopeError(f"{label} must contain a JSON object: {path}")
    return value


def recorded_path(path: pathlib.Path) -> str:
    resolved = path.expanduser().resolve()
    try:
        return resolved.relative_to(ROOT).as_posix()
    except ValueError:
        return str(resolved)


def artifact_record(path: pathlib.Path) -> dict[str, Any]:
    resolved = path.expanduser().resolve()
    if not resolved.is_file():
        raise CompleteScopeError(f"missing artifact: {resolved}")
    return {
        "path": recorded_path(resolved),
        "bytes": resolved.stat().st_size,
        "sha256": sweep.sha256_file(resolved),
    }


def require_string(value: object, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise CompleteScopeError(f"{label} must be a non-empty string")
    return value


def require_string_list(value: object, label: str) -> list[str]:
    if not isinstance(value, list) or not all(
        isinstance(item, str) and item for item in value
    ):
        raise CompleteScopeError(f"{label} must be a list of non-empty strings")
    if len(value) != len(set(value)):
        raise CompleteScopeError(f"{label} contains duplicate values")
    return list(value)


def validate_sha256(value: object, label: str) -> str:
    if not isinstance(value, str) or not SHA256_RE.fullmatch(value):
        raise CompleteScopeError(f"{label} must be a lowercase SHA-256 digest")
    return value


def require_count(value: object, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise CompleteScopeError(f"{label} must be a non-negative integer")
    return value


def indexed_rows(
    rows: object,
    *,
    label: str,
    expected_count: int | None = None,
) -> dict[str, dict[str, Any]]:
    if not isinstance(rows, list):
        raise CompleteScopeError(f"{label} must be a list")
    if expected_count is not None and len(rows) != expected_count:
        raise CompleteScopeError(
            f"{label} must contain exactly {expected_count} cases; found {len(rows)}"
        )
    result: dict[str, dict[str, Any]] = {}
    for row in rows:
        if not isinstance(row, dict):
            raise CompleteScopeError(f"{label} contains a non-object case")
        case_id = row.get("case_id")
        if not isinstance(case_id, str) or not CASE_RE.fullmatch(case_id):
            raise CompleteScopeError(f"invalid case id in {label}: {case_id!r}")
        if case_id in result:
            raise CompleteScopeError(f"duplicate case in {label}: {case_id}")
        result[case_id] = row
    return result


def mechanism_result_has_validated_positive(
    result: dict[str, Any], *, case_id: object
) -> bool:
    """Validate the mechanism-specific semantic field and return its value."""
    mechanism = result.get("mechanism")
    outcome = result.get("outcome")
    if mechanism == "type_isolation":
        if "true_positive" in result:
            raise CompleteScopeError(
                f"Type Isolation result must use validated_mitigation exclusively "
                f"for {case_id}"
            )
        validated = result.get("validated_mitigation")
        if not isinstance(validated, bool):
            raise CompleteScopeError(
                f"Type Isolation result requires explicit validated_mitigation "
                f"for {case_id}"
            )
        expected = outcome == "mitigated"
        if validated is not expected:
            raise CompleteScopeError(
                f"validated_mitigation must be {expected} for outcome {outcome!r} "
                f"in {case_id}"
            )
        return validated

    if "validated_mitigation" in result:
        raise CompleteScopeError(
            f"detector result must use true_positive exclusively for {case_id}"
        )
    true_positive = result.get("true_positive")
    if not isinstance(true_positive, bool):
        raise CompleteScopeError(
            f"detector result requires explicit true_positive for {case_id}"
        )
    expected = outcome == "detected"
    if true_positive is not expected:
        raise CompleteScopeError(
            f"true_positive must be {expected} for outcome {outcome!r} in {case_id}"
        )
    return true_positive


def normalize_historical_base_result(
    result: dict[str, Any], *, case_id: object
) -> None:
    """Bind an archived pre-semantic result to the current boolean contract.

    Historical replay scopes predate ``true_positive`` and
    ``validated_mitigation``.  They remain hash-bound replay provenance, so the
    loader performs the only deterministic migration available: the archived
    mechanism/outcome pair supplies the mechanism-specific boolean.  Current
    rows already carrying either semantic field still pass through the strict
    validator unchanged.
    """
    if "true_positive" in result or "validated_mitigation" in result:
        mechanism_result_has_validated_positive(result, case_id=case_id)
        return

    mechanism = result.get("mechanism")
    outcome = result.get("outcome")
    if mechanism == "type_isolation" and outcome in {
        "mitigated",
        "inconclusive",
        "no_signal",
    }:
        result["validated_mitigation"] = outcome == "mitigated"
    elif mechanism in {"reclaim_checks", "recovery_layout_validation"} and outcome in {
        "detected",
        "inconclusive",
        "no_signal",
    }:
        result["true_positive"] = outcome == "detected"
    else:
        raise CompleteScopeError(
            f"unsupported historical mechanism result for {case_id}: "
            f"{mechanism!r}/{outcome!r}"
        )
    result["historical_schema_normalization"] = (
        "mechanism_specific_boolean_derived_from_archived_outcome"
    )


def positive_mechanism_results(case: dict[str, Any]) -> list[dict[str, Any]]:
    """Return validated positives while rejecting duplicate mechanism claims."""
    raw_results = case.get("mechanism_results")
    if not isinstance(raw_results, list):
        raise CompleteScopeError(
            f"mechanism_results must be a list for {case.get('case_id')}"
        )
    positives: list[dict[str, Any]] = []
    for result in raw_results:
        if not isinstance(result, dict):
            raise CompleteScopeError(
                f"mechanism result must be an object for {case.get('case_id')}"
            )
        if mechanism_result_has_validated_positive(
            result, case_id=case.get("case_id")
        ):
            positives.append(result)
    by_mechanism: collections.Counter[str] = collections.Counter(
        require_string(result.get("mechanism"), "positive mechanism")
        for result in positives
    )
    duplicates = sorted(
        mechanism for mechanism, count in by_mechanism.items() if count > 1
    )
    if duplicates:
        raise CompleteScopeError(
            f"multiple positive results for one mechanism in "
            f"{case.get('case_id')}: {duplicates}"
        )
    return positives


def select_representative_mechanism_result(
    case: dict[str, Any],
) -> dict[str, Any] | None:
    """Select one display result while retaining distinct mechanism positives.

    Exact reclaim and recovery-layout diagnostics remain ahead of a bounded
    Type Isolation mitigation for deterministic case-level display. The
    complete mechanism ledger and overlap fields retain every distinct positive.
    Homogeneous non-positive rows retain the previous first-row selection rule.
    """
    raw_results = case.get("mechanism_results")
    if not isinstance(raw_results, list):
        raise CompleteScopeError(
            f"mechanism_results must be a list for {case.get('case_id')}"
        )
    results: list[dict[str, Any]] = []
    for result in raw_results:
        if not isinstance(result, dict):
            raise CompleteScopeError(
                f"mechanism result must be an object for {case.get('case_id')}"
            )
        results.append(result)
    positives = positive_mechanism_results(case)
    if positives:
        priority = {
            "reclaim_checks": 0,
            "recovery_layout_validation": 1,
            "type_isolation": 2,
        }
        return min(
            positives,
            key=lambda result: (
                priority.get(str(result.get("mechanism")), len(priority)),
                str(result.get("mechanism", "")),
                str(result.get("scenario_id", "")),
            ),
        )
    if not results:
        return None
    outcomes = {
        require_string(result.get("outcome"), "mechanism result outcome")
        for result in results
    }
    mechanisms = {
        require_string(result.get("mechanism"), "mechanism result mechanism")
        for result in results
    }
    if len(outcomes) != 1 or len(mechanisms) != 1:
        raise CompleteScopeError(
            f"representative mechanism result is ambiguous for {case.get('case_id')}"
        )
    return results[0]


def mechanism_result_for_binding(
    case: dict[str, Any],
    *,
    scenario_id: str,
    mechanism: str,
    outcome: str,
) -> dict[str, Any]:
    raw_results = case.get("mechanism_results")
    if not isinstance(raw_results, list):
        raise CompleteScopeError(
            f"mechanism_results must be a list for {case.get('case_id')}"
        )
    matches = [
        result
        for result in raw_results
        if isinstance(result, dict)
        and result.get("scenario_id") == scenario_id
        and result.get("mechanism") == mechanism
        and result.get("outcome") == outcome
    ]
    if len(matches) != 1:
        raise CompleteScopeError(
            f"expected one {scenario_id}/{mechanism}/{outcome} mechanism result "
            f"for {case.get('case_id')}; found {len(matches)}"
        )
    return matches[0]


def scope_outcome(case: dict[str, Any]) -> tuple[str, str | None]:
    integration = case["integration"]
    if integration["status"] == "blocked":
        return "blocked", None
    result = case.get("_representative_mechanism_result")
    if result is None:
        result = select_representative_mechanism_result(case)
    if result is None:
        return "not_yet_attributed", None
    return (
        require_string(result.get("outcome"), "mechanism result outcome"),
        require_string(result.get("mechanism"), "mechanism result mechanism"),
    )


def representative_scenario(case: dict[str, Any]) -> str | None:
    integration = case["integration"]
    scenario_ids = require_string_list(
        integration.get("scenario_ids", []),
        f"{case['case_id']} integration scenario_ids",
    )
    if integration["status"] == "blocked":
        if scenario_ids:
            raise CompleteScopeError(
                f"blocked case unexpectedly has executable scenarios: {case['case_id']}"
            )
        return None
    if not scenario_ids:
        raise CompleteScopeError(
            f"executable case has no scenario: {case['case_id']}"
        )
    selected_result = case.get("_representative_mechanism_result")
    if selected_result is None:
        selected_result = select_representative_mechanism_result(case)
    if selected_result is not None:
        selected = require_string(
            selected_result.get("scenario_id"),
            f"{case['case_id']} mechanism result scenario_id",
        )
        if selected not in scenario_ids:
            raise CompleteScopeError(
                f"mechanism scenario is outside integration scenarios for "
                f"{case['case_id']}: {selected}"
            )
        return selected
    return scenario_ids[0]


def load_scope(
    path: pathlib.Path,
) -> tuple[
    dict[str, Any],
    dict[str, dict[str, Any]],
    dict[str, dict[str, Any]],
]:
    payload = load_object(path, "complete scope")
    if payload.get("schema_version") != 1:
        raise CompleteScopeError("unsupported complete-scope schema")
    counts = payload.get("counts")
    if not isinstance(counts, dict):
        raise CompleteScopeError("complete-scope counts must be an object")
    case_count = require_count(
        counts.get("strong_candidate_count"),
        "counts.strong_candidate_count",
    )
    excluded_count = require_count(
        counts.get("excluded_non_evaluable_count"),
        "counts.excluded_non_evaluable_count",
    )
    reviewed_count = require_count(
        counts.get("reviewed_strong_candidate_count"),
        "counts.reviewed_strong_candidate_count",
    )
    if reviewed_count != case_count + excluded_count:
        raise CompleteScopeError(
            "counts.reviewed_strong_candidate_count must equal "
            "strong_candidate_count plus excluded_non_evaluable_count"
        )
    cases = indexed_rows(
        payload.get("cases"),
        label="complete scope cases for counts.strong_candidate_count",
        expected_count=case_count,
    )
    excluded_cases = indexed_rows(
        payload.get("excluded_cases"),
        label=(
            "complete scope excluded_cases for "
            "counts.excluded_non_evaluable_count"
        ),
        expected_count=excluded_count,
    )
    overlap = sorted(set(cases) & set(excluded_cases))
    if overlap:
        raise CompleteScopeError(
            f"complete-scope cases and excluded_cases overlap: {overlap}"
        )
    status_counts: collections.Counter[str] = collections.Counter()
    for case_id, case in cases.items():
        require_string(case.get("advisory_id"), f"{case_id} advisory_id")
        require_string(case.get("package"), f"{case_id} package")
        require_string(case.get("primary_primitive"), f"{case_id} primary_primitive")
        integration = case.get("integration")
        if not isinstance(integration, dict):
            raise CompleteScopeError(f"missing integration object for {case_id}")
        status = integration.get("status")
        if status != "executable":
            raise CompleteScopeError(
                f"efficacy case must be executable for {case_id}: {status!r}"
            )
        results = case.get("mechanism_results")
        if not isinstance(results, list):
            raise CompleteScopeError(f"mechanism_results must be a list for {case_id}")
        case["_representative_mechanism_result"] = (
            select_representative_mechanism_result(case)
        )
        case["_positive_mechanism_results"] = positive_mechanism_results(case)
        case["_representative_scenario_id"] = representative_scenario(case)
        case["_scope_outcome"], case["_mechanism"] = scope_outcome(case)
        status_counts[status] += 1
    recorded_counts = counts.get("integration_status")
    if recorded_counts != dict(status_counts):
        raise CompleteScopeError("complete-scope integration counts do not match cases")

    excluded_status_counts: collections.Counter[str] = collections.Counter()
    for case_id, case in excluded_cases.items():
        require_string(case.get("advisory_id"), f"{case_id} advisory_id")
        require_string(case.get("package"), f"{case_id} package")
        require_string(case.get("primary_primitive"), f"{case_id} primary_primitive")
        integration = case.get("integration")
        if not isinstance(integration, dict):
            raise CompleteScopeError(
                f"missing excluded integration object for {case_id}"
            )
        if integration.get("status") != "blocked":
            raise CompleteScopeError(
                f"excluded case must be blocked for {case_id}: "
                f"{integration.get('status')!r}"
            )
        require_string(
            integration.get("blocker_reason"), f"{case_id} blocker_reason"
        )
        if require_string_list(
            integration.get("scenario_ids", []),
            f"{case_id} integration scenario_ids",
        ):
            raise CompleteScopeError(
                f"excluded case unexpectedly has executable scenarios: {case_id}"
            )
        results = case.get("mechanism_results")
        if not isinstance(results, list) or results:
            raise CompleteScopeError(
                f"excluded case mechanism_results must be empty for {case_id}"
            )
        exclusion = case.get("scope_exclusion")
        if not isinstance(exclusion, dict):
            raise CompleteScopeError(f"missing scope_exclusion for {case_id}")
        require_string(exclusion.get("code"), f"{case_id} scope_exclusion code")
        require_string(exclusion.get("reason"), f"{case_id} scope_exclusion reason")
        require_string_list(
            exclusion.get("evidence"), f"{case_id} scope_exclusion evidence"
        )
        case["_representative_scenario_id"] = None
        case["_scope_outcome"] = "excluded"
        case["_mechanism"] = None
        excluded_status_counts["blocked"] += 1
    if counts.get("excluded_integration_status") != dict(excluded_status_counts):
        raise CompleteScopeError(
            "complete-scope excluded integration counts do not match excluded_cases"
        )
    return payload, cases, excluded_cases


def load_base_scope(
    path: pathlib.Path,
) -> tuple[dict[str, Any], dict[str, dict[str, Any]]]:
    """Load either the partitioned scope or its archived legacy predecessor.

    Frozen replay artifacts may remain bound to the pre-partition schema.  The
    legacy loader derives its size from the archived count and validates every
    row; it supplies replay provenance only and never defines the current
    efficacy denominator.
    """
    payload = load_object(path, "base complete scope")
    if "excluded_cases" in payload:
        current_payload, cases, _excluded_cases = load_scope(path)
        return current_payload, cases
    if payload.get("schema_version") != 1:
        raise CompleteScopeError("unsupported base complete-scope schema")
    counts = payload.get("counts")
    if not isinstance(counts, dict):
        raise CompleteScopeError("base complete-scope counts must be an object")
    case_count = require_count(
        counts.get("strong_candidate_count"),
        "base counts.strong_candidate_count",
    )
    cases = indexed_rows(
        payload.get("cases"),
        label="base complete scope cases",
        expected_count=case_count,
    )
    status_counts: collections.Counter[str] = collections.Counter()
    for case_id, case in cases.items():
        require_string(case.get("advisory_id"), f"{case_id} advisory_id")
        require_string(case.get("package"), f"{case_id} package")
        require_string(case.get("primary_primitive"), f"{case_id} primary_primitive")
        integration = case.get("integration")
        if not isinstance(integration, dict):
            raise CompleteScopeError(f"missing integration object for {case_id}")
        status = integration.get("status")
        if status not in ("executable", "blocked"):
            raise CompleteScopeError(
                f"invalid base integration status for {case_id}: {status!r}"
            )
        results = case.get("mechanism_results")
        if not isinstance(results, list):
            raise CompleteScopeError(f"mechanism_results must be a list for {case_id}")
        for result in results:
            if not isinstance(result, dict):
                raise CompleteScopeError(
                    f"mechanism result must be an object for {case_id}"
                )
            normalize_historical_base_result(result, case_id=case_id)
        if status == "blocked":
            require_string(
                integration.get("blocker_reason"), f"{case_id} blocker_reason"
            )
            if results:
                raise CompleteScopeError(
                    f"blocked base case mechanism_results must be empty for {case_id}"
                )
        case["_representative_mechanism_result"] = (
            select_representative_mechanism_result(case)
        )
        case["_positive_mechanism_results"] = positive_mechanism_results(case)
        case["_representative_scenario_id"] = representative_scenario(case)
        case["_scope_outcome"], case["_mechanism"] = scope_outcome(case)
        status_counts[status] += 1
    if counts.get("integration_status") != dict(sorted(status_counts.items())):
        raise CompleteScopeError(
            "base complete-scope integration counts do not match cases"
        )
    return payload, cases


def load_catalog_index(
    paths: Sequence[pathlib.Path],
) -> tuple[dict[str, dict[str, Any]], list[dict[str, Any]]]:
    scenarios: dict[str, dict[str, Any]] = {}
    records: list[dict[str, Any]] = []
    for path in paths:
        payload = load_object(path, "harness catalog")
        if payload.get("schema_version") != 1:
            raise CompleteScopeError(f"unsupported harness catalog schema: {path}")
        catalog_artifact = artifact_record(path)
        records.append(catalog_artifact)
        raw_cases = payload.get("cases")
        if not isinstance(raw_cases, list):
            raise CompleteScopeError(f"catalog cases must be a list: {path}")
        for case in raw_cases:
            if not isinstance(case, dict):
                raise CompleteScopeError(f"catalog case must be an object: {path}")
            case_id = case.get("case_id")
            if not isinstance(case_id, str) or not CASE_RE.fullmatch(case_id):
                raise CompleteScopeError(f"invalid catalog case id: {case_id!r}")
            raw_scenarios = case.get("scenarios")
            if not isinstance(raw_scenarios, list):
                raise CompleteScopeError(f"catalog scenarios must be a list: {case_id}")
            for scenario in raw_scenarios:
                if not isinstance(scenario, dict):
                    raise CompleteScopeError(
                        f"catalog scenario must be an object: {case_id}"
                    )
                scenario_id = require_string(
                    scenario.get("scenario_id"), f"{case_id} scenario_id"
                )
                sweep.validate_scenario_id(scenario_id)
                if scenario_id in scenarios:
                    raise CompleteScopeError(
                        f"duplicate scenario across catalogs: {scenario_id}"
                    )
                oracle = scenario.get("oracle")
                if not isinstance(oracle, dict):
                    raise CompleteScopeError(f"missing oracle for {scenario_id}")
                source_path = scenario.get("source_path")
                if source_path is None:
                    source = scenario.get("source")
                    source_path = (
                        source.get("path") if isinstance(source, dict) else None
                    )
                scenarios[scenario_id] = {
                    "case_id": case_id,
                    "catalog_path": recorded_path(path),
                    "catalog_artifact": catalog_artifact,
                    "source_path": require_string(
                        source_path, f"{scenario_id} source path"
                    ),
                    "oracle_tool": require_string(
                        oracle.get("tool"), f"{scenario_id} oracle tool"
                    ),
                    "root_cause": require_string(
                        oracle.get("vulnerable"), f"{scenario_id} vulnerable oracle"
                    ),
                }
    return scenarios, records


def bind_scope_to_catalogs(
    cases: dict[str, dict[str, Any]], scenarios: dict[str, dict[str, Any]]
) -> None:
    for case_id, case in cases.items():
        bound: dict[str, dict[str, Any]] = {}
        integration = case.get("integration")
        scenario_ids = (
            integration.get("scenario_ids", [])
            if isinstance(integration, dict)
            else []
        )
        for integrated_scenario_id in scenario_ids:
            record = scenarios.get(integrated_scenario_id)
            if record is None:
                raise CompleteScopeError(
                    "integrated scenario is absent from catalogs: "
                    f"{integrated_scenario_id}"
                )
            if record["case_id"] != case_id:
                raise CompleteScopeError(
                    "integrated scenario binds another case: "
                    f"{integrated_scenario_id}"
                )
            bound[integrated_scenario_id] = record
        case["_catalog_scenarios"] = bound
        scenario_id = case["_representative_scenario_id"]
        if scenario_id is None:
            continue
        record = bound.get(scenario_id)
        if record is None:
            raise CompleteScopeError(
                f"representative scenario is absent from catalogs: {scenario_id}"
            )
        case["_catalog_scenario"] = record


def resolve_plan_path(value: str, plan_path: pathlib.Path | None) -> pathlib.Path:
    path = pathlib.Path(value).expanduser()
    if path.is_absolute():
        return path.resolve()
    if plan_path is not None:
        candidate = (plan_path.parent / path).resolve()
        if candidate.exists():
            return candidate
    return (ROOT / path).resolve()


def validate_command(value: object, label: str) -> list[str]:
    if not isinstance(value, list) or not value or not all(
        isinstance(item, str) and item and "\0" not in item for item in value
    ):
        raise CompleteScopeError(f"{label} must be a non-empty argv string list")
    return list(value)


def validate_returncodes(value: object, label: str) -> list[int]:
    if not isinstance(value, list) or not value:
        raise CompleteScopeError(f"{label} must be a non-empty integer list")
    result: list[int] = []
    for item in value:
        if isinstance(item, bool) or not isinstance(item, int):
            raise CompleteScopeError(f"{label} must be a non-empty integer list")
        result.append(item)
    if len(result) != len(set(result)):
        raise CompleteScopeError(f"{label} contains duplicate return codes")
    return result


def load_plan(
    path: pathlib.Path,
    *,
    scope_sha256: str,
) -> tuple[dict[str, Any], dict[str, dict[str, Any]]]:
    payload = load_object(path, "command plan")
    if payload.get("schema_version") != 1:
        raise CompleteScopeError("unsupported complete-scope command-plan schema")
    if payload.get("scope_sha256") != scope_sha256:
        raise CompleteScopeError("command plan scope SHA-256 mismatch")
    raw_jobs = payload.get("jobs")
    if not isinstance(raw_jobs, list):
        raise CompleteScopeError("command plan jobs must be a list")
    jobs: dict[str, dict[str, Any]] = {}
    for raw in raw_jobs:
        if not isinstance(raw, dict):
            raise CompleteScopeError("command plan job must be an object")
        job_id = raw.get("job_id")
        if job_id not in PLAN_JOB_IDS:
            raise CompleteScopeError(f"invalid command-plan job id: {job_id!r}")
        if job_id in jobs:
            raise CompleteScopeError(f"duplicate command-plan job: {job_id}")
        action = raw.get("action")
        if action not in PLAN_ACTIONS:
            raise CompleteScopeError(f"invalid command-plan action for {job_id}")
        artifact_path = resolve_plan_path(
            require_string(raw.get("artifact_path"), f"{job_id} artifact_path"),
            path,
        )
        job = {
            "job_id": job_id,
            "action": action,
            "artifact_path": artifact_path,
        }
        expected_sha256 = raw.get("expected_sha256")
        if action == "verify":
            job["expected_sha256"] = validate_sha256(
                expected_sha256, f"{job_id} expected_sha256"
            )
            if raw.get("command") is not None:
                raise CompleteScopeError(f"verify job cannot contain command: {job_id}")
        else:
            job["command"] = validate_command(raw.get("command"), f"{job_id} command")
            job["expected_returncodes"] = validate_returncodes(
                raw.get("expected_returncodes", [0]),
                f"{job_id} expected_returncodes",
            )
            timeout = raw.get("timeout_seconds")
            if timeout is not None and (
                isinstance(timeout, bool) or not isinstance(timeout, int) or timeout <= 0
            ):
                raise CompleteScopeError(
                    f"{job_id} timeout_seconds must be a positive integer"
                )
            if timeout is not None:
                job["timeout_seconds"] = timeout
            if expected_sha256 is not None:
                job["expected_sha256"] = validate_sha256(
                    expected_sha256, f"{job_id} expected_sha256"
                )
        jobs[job_id] = job
    missing = sorted(PLAN_JOB_IDS - set(jobs))
    if missing:
        raise CompleteScopeError(
            f"command plan is missing jobs: {', '.join(missing)}"
        )
    return payload, jobs


def direct_verify_jobs(
    executable_replay: pathlib.Path, blocked_attempts: pathlib.Path
) -> dict[str, dict[str, Any]]:
    result: dict[str, dict[str, Any]] = {}
    for job_id, path in (
        ("executable_replay", executable_replay),
        ("blocked_attempts", blocked_attempts),
    ):
        resolved = path.expanduser().resolve()
        record = artifact_record(resolved)
        result[job_id] = {
            "job_id": job_id,
            "action": "verify",
            "artifact_path": resolved,
            "expected_sha256": record["sha256"],
        }
    return result


def write_command_logs(
    log_dir: pathlib.Path,
    job_id: str,
    stdout: str,
    stderr: str,
) -> dict[str, dict[str, Any]]:
    log_dir.mkdir(parents=True, exist_ok=True)
    result: dict[str, dict[str, Any]] = {}
    for stream, content in (("stdout", stdout), ("stderr", stderr)):
        path = log_dir / f"{job_id}.{stream}.log"
        path.write_text(content, encoding="utf-8")
        result[stream] = artifact_record(path)
    return result


def run_plan_jobs(
    jobs: dict[str, dict[str, Any]],
    *,
    dry_run: bool,
    execute_unsafe: bool,
    default_timeout_seconds: int,
    log_dir: pathlib.Path,
) -> tuple[list[dict[str, Any]], bool]:
    if not dry_run and any(job["action"] == "run" for job in jobs.values()):
        if not execute_unsafe:
            raise CompleteScopeError(
                "run jobs require the explicit --execute-unsafe opt-in"
            )
    records: list[dict[str, Any]] = []
    all_valid = True
    for job_id in sorted(jobs):
        job = jobs[job_id]
        artifact_path = job["artifact_path"]
        base: dict[str, Any] = {
            "job_id": job_id,
            "action": job["action"],
            "artifact_path": recorded_path(artifact_path),
        }
        if job["action"] == "run":
            base["command"] = job["command"]
            base["expected_returncodes"] = job["expected_returncodes"]
        if dry_run:
            records.append({**base, "status": "planned", "valid": True})
            continue
        if job["action"] == "verify":
            try:
                actual = artifact_record(artifact_path)
                valid = actual["sha256"] == job["expected_sha256"]
                record = {
                    **base,
                    "status": "verified" if valid else "failed",
                    "valid": valid,
                    "artifact": actual,
                }
                if not valid:
                    record["error"] = "artifact SHA-256 differs from command plan"
            except CompleteScopeError as error:
                valid = False
                record = {**base, "status": "failed", "valid": False, "error": str(error)}
            records.append(record)
            all_valid &= valid
            continue

        started = time.time()
        timeout = job.get("timeout_seconds", default_timeout_seconds)
        try:
            completed = subprocess.run(
                job["command"],
                cwd=ROOT,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                timeout=timeout,
                check=False,
            )
            logs = write_command_logs(
                log_dir, job_id, completed.stdout, completed.stderr
            )
            returncode: int | None = completed.returncode
            timed_out = False
            valid = completed.returncode in job["expected_returncodes"]
            error = None if valid else "command returned an unexpected status"
        except subprocess.TimeoutExpired as error:
            stdout = error.stdout if isinstance(error.stdout, str) else ""
            stderr = error.stderr if isinstance(error.stderr, str) else ""
            logs = write_command_logs(log_dir, job_id, stdout, stderr)
            returncode = None
            timed_out = True
            valid = False
            error = f"command timed out after {timeout} seconds"
        artifact: dict[str, Any] | None = None
        if valid:
            try:
                artifact = artifact_record(artifact_path)
                expected = job.get("expected_sha256")
                if expected is not None and artifact["sha256"] != expected:
                    valid = False
                    error = "generated artifact SHA-256 differs from command plan"
            except CompleteScopeError as artifact_error:
                valid = False
                error = str(artifact_error)
        record = {
            **base,
            "status": "completed" if valid else ("timed_out" if timed_out else "failed"),
            "valid": valid,
            "returncode": returncode,
            "timed_out": timed_out,
            "wall_seconds": time.time() - started,
            "logs": logs,
            "artifact": artifact,
        }
        if error is not None:
            record["error"] = error
        records.append(record)
        all_valid &= valid
    return records, all_valid


def terminal_status(value: object, label: str) -> str:
    status = require_string(value, label)
    if status in NONTERMINAL_ATTEMPT_STATUSES:
        raise CompleteScopeError(f"nonterminal {label}: {status}")
    return status


def validate_signal_record(value: object, label: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise CompleteScopeError(f"{label} must be an object")
    for name in (
        "address_reuse_observed",
        "asan",
        "miri_ub",
        "pointer_already_released",
        "rust_panic",
    ):
        if not isinstance(value.get(name), bool):
            raise CompleteScopeError(f"{label}.{name} must be boolean")
    denial = value.get("reuse_denial_report")
    if denial is not None and not isinstance(denial, dict):
        raise CompleteScopeError(f"{label}.reuse_denial_report must be an object or null")
    return dict(value)


def typeiso_denial_observed(signals: dict[str, Any]) -> bool:
    denial = signals.get("reuse_denial_report")
    if not isinstance(denial, dict):
        return False
    count = denial.get("typed_cache_wrong_identity_denials", 0)
    return isinstance(count, int) and not isinstance(count, bool) and count > 0


def validate_executable_replay(
    path: pathlib.Path,
    *,
    scope_sha256: str,
    cases: dict[str, dict[str, Any]],
) -> dict[str, dict[str, Any]]:
    payload = load_object(path, "executable replay")
    if payload.get("schema_version") != 1:
        raise CompleteScopeError("unsupported executable-replay schema")
    if payload.get("scope_sha256") != scope_sha256:
        raise CompleteScopeError("executable replay scope SHA-256 mismatch")
    expected = {
        case_id
        for case_id, case in cases.items()
        if case["integration"]["status"] == "executable"
    }
    rows = indexed_rows(payload.get("cases"), label="executable replay")
    if set(rows) != expected:
        missing = sorted(expected - set(rows))
        extra = sorted(set(rows) - expected)
        raise CompleteScopeError(
            "executable replay case coverage mismatch; "
            f"missing={missing}, extra={extra}"
        )
    rendered: dict[str, dict[str, Any]] = {}
    for case_id, row in rows.items():
        scope_case = cases[case_id]
        scenario_id = scope_case["_representative_scenario_id"]
        if row.get("scenario_id") != scenario_id:
            raise CompleteScopeError(
                f"executable replay scenario mismatch for {case_id}"
            )
        for field in ("advisory_id", "package", "primary_primitive"):
            if row.get(field) != scope_case.get(field):
                raise CompleteScopeError(
                    f"executable replay {field} mismatch for {case_id}"
                )
        if row.get("frozen_outcome") != scope_case["_scope_outcome"]:
            raise CompleteScopeError(
                f"executable replay frozen outcome mismatch for {case_id}"
            )
        if row.get("mechanism") != scope_case["_mechanism"]:
            raise CompleteScopeError(
                f"executable replay mechanism mismatch for {case_id}"
            )
        status = terminal_status(
            row.get("live_attempt_status"), f"{case_id} live_attempt_status"
        )
        raw_arms = row.get("live_arms")
        if not isinstance(raw_arms, list) or not raw_arms:
            raise CompleteScopeError(f"executable replay has no live arms: {case_id}")
        observed_pairs: set[tuple[str, str]] = set()
        arms: list[dict[str, Any]] = []
        implementation_digests: collections.Counter[str] = collections.Counter()
        raw_exact_signal = False
        reclaim_exact_signal = False
        layout_validation_signal = False
        typeiso_reuse_denial = False
        for index, arm in enumerate(raw_arms):
            if not isinstance(arm, dict):
                raise CompleteScopeError(f"non-object live arm for {case_id}")
            if arm.get("case_id") != case_id or arm.get("scenario_id") != scenario_id:
                raise CompleteScopeError(f"live arm binding mismatch for {case_id}")
            if arm.get("mechanism") != scope_case["_mechanism"]:
                raise CompleteScopeError(f"live arm mechanism mismatch for {case_id}")
            if arm.get("frozen_outcome") != scope_case["_scope_outcome"]:
                raise CompleteScopeError(f"live arm outcome mismatch for {case_id}")
            archive = require_string(
                arm.get("archive_variant"), f"{case_id} arm archive_variant"
            )
            allocator = require_string(
                arm.get("allocator_variant"), f"{case_id} arm allocator_variant"
            )
            pair = (archive, allocator)
            if pair in observed_pairs:
                raise CompleteScopeError(
                    f"duplicate live arm for {case_id}: {archive}/{allocator}"
                )
            observed_pairs.add(pair)
            attempt_status = terminal_status(
                arm.get("attempt_status"), f"{case_id} arm attempt_status"
            )
            recorded_binary = validate_sha256(
                arm.get("recorded_binary_sha256"),
                f"{case_id} arm recorded_binary_sha256",
            )
            actual_binary = validate_sha256(
                arm.get("actual_binary_sha256"),
                f"{case_id} arm actual_binary_sha256",
            )
            if recorded_binary != actual_binary:
                raise CompleteScopeError(f"frozen binary digest mismatch for {case_id}")
            binary_path = pathlib.Path(
                require_string(arm.get("binary_path"), f"{case_id} arm binary_path")
            ).expanduser()
            if not binary_path.is_absolute():
                binary_path = ROOT / binary_path
            binary_path = binary_path.resolve()
            binary_available = binary_path.is_file()
            if binary_available and sweep.sha256_file(binary_path) != actual_binary:
                raise CompleteScopeError(f"live replay binary hash mismatch for {case_id}")
            implementation = validate_sha256(
                arm.get("frozen_unialloc_implementation_sha256"),
                f"{case_id} arm implementation SHA-256",
            )
            implementation_digests[implementation] += 1
            signals = validate_signal_record(
                arm.get("signals"), f"{case_id} arm signals"
            )
            reclaim_exact_signal |= signals["pointer_already_released"]
            typeiso_reuse_denial |= typeiso_denial_observed(signals)
            raw_exact_signal = (
                reclaim_exact_signal
                or layout_validation_signal
                or typeiso_reuse_denial
            )
            arms.append(
                {
                    "archive_variant": archive,
                    "allocator_variant": allocator,
                    "attempt_status": attempt_status,
                    "exit_code": arm.get("exit_code"),
                    "timed_out": arm.get("timed_out"),
                    "binary_path": recorded_path(binary_path),
                    "binary_sha256": actual_binary,
                    "binary_available": binary_available,
                    "binary_validation": (
                        "local_file_rehashed"
                        if binary_available
                        else "archived_digest_only"
                    ),
                    "frozen_unialloc_implementation_sha256": implementation,
                    "signals": signals,
                }
            )
        catalog = scope_case["_catalog_scenario"]
        rendered[case_id] = {
            "case_id": case_id,
            "advisory_id": scope_case["advisory_id"],
            "package": scope_case["package"],
            "primary_primitive": scope_case["primary_primitive"],
            "root_cause": catalog["root_cause"],
            "root_cause_source": "catalog_oracle.vulnerable",
            "scope_status": "executable",
            "status": status,
            "outcome": scope_case["_scope_outcome"],
            "representative_scenario_id": scenario_id,
            "catalog_path": catalog["catalog_path"],
            "scenario_source_path": catalog["source_path"],
            "oracle_tool": catalog["oracle_tool"],
            "mechanism": scope_case["_mechanism"],
            "live_arm_count": len(arms),
            "reclaim_exact_signal_observed": reclaim_exact_signal,
            "layout_validation_signal_observed": layout_validation_signal,
            "typeiso_reuse_denial_observed": typeiso_reuse_denial,
            "raw_exact_allocator_signal": raw_exact_signal,
            "implementation_digests": dict(sorted(implementation_digests.items())),
            "scope_review_required": False,
            "blocker_reason": None,
            "blocker_code": None,
            "conclusion": row.get("frozen_reason"),
            "probe_count": 0,
            "arms": arms,
            "source_artifact": recorded_path(path),
        }
    return rendered


def bind_replay_to_current_scope(
    replay: dict[str, dict[str, Any]],
    *,
    cases: dict[str, dict[str, Any]],
    supplemental_case_ids: set[str],
) -> None:
    expected = {
        case_id
        for case_id, case in cases.items()
        if case["integration"]["status"] == "executable"
        and case_id not in supplemental_case_ids
    }
    if set(replay) != expected:
        raise CompleteScopeError(
            "base replay does not match current executable scope after supplements; "
            f"missing={sorted(expected - set(replay))}, "
            f"extra={sorted(set(replay) - expected)}"
        )
    for case_id, record in replay.items():
        case = cases[case_id]
        expected_fields = {
            "advisory_id": case["advisory_id"],
            "package": case["package"],
            "primary_primitive": case["primary_primitive"],
        }
        for field, expected_value in expected_fields.items():
            if record.get(field) != expected_value:
                raise CompleteScopeError(
                    f"base replay {field} changed in current scope for {case_id}"
                )
        expected_binding = (
            case["_scope_outcome"],
            case["_mechanism"],
            case["_representative_scenario_id"],
        )
        record_binding = (
            record.get("outcome"),
            record.get("mechanism"),
            record.get("representative_scenario_id"),
        )
        if record_binding != expected_binding:
            apply_strict_result_override(case=case, record=record)
        final_binding = (
            record.get("outcome"),
            record.get("mechanism"),
            record.get("representative_scenario_id"),
        )
        if final_binding != expected_binding:
            raise CompleteScopeError(
                f"base replay binding changed without a strict result override "
                f"for {case_id}"
            )
        validate_and_bind_positive_mechanism_results(case=case, record=record)


def embedded_artifact_record(
    value: object,
    label: str,
    *,
    require_file: bool,
    verify_if_present: bool = True,
) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise CompleteScopeError(f"{label} must be an artifact object")
    path_text = require_string(value.get("path"), f"{label}.path")
    expected_sha256 = validate_sha256(value.get("sha256"), f"{label}.sha256")
    expected_bytes = value.get("bytes")
    if (
        isinstance(expected_bytes, bool)
        or not isinstance(expected_bytes, int)
        or expected_bytes < 0
    ):
        raise CompleteScopeError(f"{label}.bytes must be non-negative")
    path = pathlib.Path(path_text).expanduser()
    if not path.is_absolute():
        path = ROOT / path
    path = path.resolve()
    if require_file or (verify_if_present and path.is_file()):
        if not path.is_file():
            raise CompleteScopeError(f"missing artifact: {path}")
        actual = artifact_record(path)
        if actual["sha256"] != expected_sha256 or actual["bytes"] != expected_bytes:
            raise CompleteScopeError(f"{label} hash/size mismatch")
    return {
        "path": recorded_path(path),
        "bytes": expected_bytes,
        "sha256": expected_sha256,
    }


def strict_typeiso_evidence_implementation_digest(
    *,
    case_id: str,
    result: dict[str, Any],
) -> str:
    """Bind a TypeIso result to the implementation used by its raw matrix.

    The mechanism ledger points at the compact derived-reuse summary.  That
    summary, in turn, authenticates the raw experiment containing per-arm and
    toolchain implementation digests.  Following both links keeps replay-arm
    provenance separate from the strict TypeIso evidence provenance.
    """

    summary_reference = result.get("evidence")
    embedded_artifact_record(
        summary_reference,
        f"{case_id} strict Type Isolation summary evidence",
        require_file=True,
    )
    if not isinstance(summary_reference, dict):
        raise CompleteScopeError(
            f"strict Type Isolation summary evidence is invalid for {case_id}"
        )
    summary_path = pathlib.Path(
        require_string(
            summary_reference.get("path"),
            f"{case_id} strict Type Isolation summary evidence.path",
        )
    ).expanduser()
    if not summary_path.is_absolute():
        summary_path = ROOT / summary_path
    summary = load_object(summary_path.resolve(), f"{case_id} Type Isolation summary")
    if summary.get("claim_grade") is not False:
        raise CompleteScopeError(
            f"strict Type Isolation summary claim grade is invalid for {case_id}"
        )
    raw_inputs = summary.get("raw_experiment_inputs")
    if not isinstance(raw_inputs, list):
        raise CompleteScopeError(
            f"strict Type Isolation summary raw inputs are missing for {case_id}"
        )
    matching_inputs = [
        value
        for value in raw_inputs
        if isinstance(value, dict) and value.get("case_id") == case_id
    ]
    if len(matching_inputs) != 1:
        raise CompleteScopeError(
            f"strict Type Isolation summary must bind one raw matrix for {case_id}"
        )
    raw_reference = matching_inputs[0]
    embedded_artifact_record(
        raw_reference,
        f"{case_id} strict Type Isolation raw matrix",
        require_file=True,
    )
    raw_path = pathlib.Path(
        require_string(
            raw_reference.get("path"),
            f"{case_id} strict Type Isolation raw matrix.path",
        )
    ).expanduser()
    if not raw_path.is_absolute():
        raw_path = ROOT / raw_path
    experiment = load_object(raw_path.resolve(), f"{case_id} Type Isolation matrix")
    if experiment.get("claim_grade") is not False:
        raise CompleteScopeError(
            f"strict Type Isolation matrix claim grade is invalid for {case_id}"
        )
    edge = experiment.get("type_isolation_reuse_edge_evaluation")
    if not isinstance(edge, dict) or edge.get("validated") is not True:
        raise CompleteScopeError(
            f"strict Type Isolation raw matrix is not validated for {case_id}"
        )

    implementation_digests: set[str] = set()
    arms = experiment.get("arms")
    if not isinstance(arms, list) or not arms:
        raise CompleteScopeError(
            f"strict Type Isolation raw matrix has no arms for {case_id}"
        )
    for index, arm in enumerate(arms):
        if not isinstance(arm, dict):
            raise CompleteScopeError(
                f"strict Type Isolation raw arm is invalid for {case_id}: {index}"
            )
        provenance = arm.get("allocator_provenance")
        if not isinstance(provenance, dict):
            raise CompleteScopeError(
                f"strict Type Isolation raw provenance is missing for {case_id}: {index}"
            )
        implementation_digests.add(
            validate_sha256(
                provenance.get("unialloc_implementation_sha256"),
                f"{case_id} strict Type Isolation arm {index} implementation",
            )
        )
    typeiso_toolchain = experiment.get("typeiso_toolchain")
    force_build = (
        typeiso_toolchain.get("force_build")
        if isinstance(typeiso_toolchain, dict)
        else None
    )
    if not isinstance(force_build, dict):
        raise CompleteScopeError(
            f"strict Type Isolation toolchain provenance is missing for {case_id}"
        )
    implementation_digests.add(
        validate_sha256(
            force_build.get("implementation_sha256"),
            f"{case_id} strict Type Isolation toolchain implementation",
        )
    )
    if len(implementation_digests) != 1:
        raise CompleteScopeError(
            f"strict Type Isolation implementation drift for {case_id}"
        )
    return next(iter(implementation_digests))


def strict_typeiso_identity_provenance(
    *, case_id: str, result: dict[str, Any]
) -> dict[str, Any]:
    """Validate and normalize manual or compiler-automatic edge provenance."""

    compiler_automatic = result.get("compiler_automatic_victim_coverage")
    if not isinstance(compiler_automatic, bool):
        raise CompleteScopeError(
            f"strict Type Isolation compiler coverage is invalid for {case_id}"
        )
    if result.get("source_vulnerability_detection_validated") is not False:
        raise CompleteScopeError(
            f"strict Type Isolation source boundary is invalid for {case_id}"
        )
    if result.get("vulnerability_specific_detection_signal") not in (None, False):
        raise CompleteScopeError(
            f"strict Type Isolation vulnerability-specific detection is invalid for {case_id}"
        )
    if result.get("full_source_vulnerability_detection") not in (None, False):
        raise CompleteScopeError(
            f"strict Type Isolation full-source detection is invalid for {case_id}"
        )
    if "source_level_true_positive" in result:
        raise CompleteScopeError(
            f"strict Type Isolation result uses legacy true-positive terminology "
            f"for {case_id}"
        )
    automatic_value = result.get("automatic_edge_identity_probe", False)
    if not isinstance(automatic_value, bool):
        raise CompleteScopeError(
            f"strict Type Isolation automatic probe marker is invalid for {case_id}"
        )
    manual_value = result.get("manual_victim_identity_annotation")
    if automatic_value:
        if compiler_automatic is not True or manual_value is not False:
            raise CompleteScopeError(
                f"strict Type Isolation automatic provenance is invalid for {case_id}"
            )
        for field in ("automatic_source_coverage",):
            if result.get(field) is not False:
                raise CompleteScopeError(
                    f"strict Type Isolation {field} is invalid for {case_id}"
                )
    elif compiler_automatic is not False or manual_value not in (None, True):
        raise CompleteScopeError(
            f"strict Type Isolation manual provenance is invalid for {case_id}"
        )

    synthetic = case_id in SYNTHETIC_TYPEISO_REDUCTION_CASES
    if (
        "synthetic_reduction" in result
        and result["synthetic_reduction"] is not synthetic
    ):
        raise CompleteScopeError(
            f"strict Type Isolation synthetic marker is invalid for {case_id}"
        )
    expected_fidelity = (
        "compiler_automatic_synthetic_reduction"
        if automatic_value and synthetic
        else "compiler_automatic_derived_reduction"
        if automatic_value
        else "synthetic_manual_reduction"
        if synthetic
        else "manual_derived_reduction"
    )
    fidelity = result.get("reduction_fidelity")
    if (automatic_value and fidelity != expected_fidelity) or (
        fidelity is not None and fidelity != expected_fidelity
    ):
        raise CompleteScopeError(
            f"strict Type Isolation reduction fidelity is invalid for {case_id}"
        )
    checks = result.get("checks")
    if automatic_value and (
        not isinstance(checks, dict)
        or checks.get("automatic_edge_identity_probe_mode_valid") is not True
        or checks.get("edge_identity_contract_valid") is not True
        or checks.get("vulnerability_specific_detection_boundary_valid") is not True
    ):
        raise CompleteScopeError(
            f"strict Type Isolation automatic checks are incomplete for {case_id}"
        )
    return {
        "automatic_edge_identity_probe": automatic_value,
        "manual_victim_identity_annotation": not automatic_value,
        "compiler_automatic_victim_coverage": compiler_automatic,
        "source_vulnerability_detection_validated": False,
        "vulnerability_specific_detection_signal": False,
        "full_source_vulnerability_detection": False,
        "automatic_source_coverage": False,
        "claim_grade": False,
        "reduction_fidelity": expected_fidelity,
        "synthetic_reduction": synthetic,
    }


def validate_strict_current_result(
    *,
    case: dict[str, Any],
    result: dict[str, Any] | None = None,
    require_representative_binding: bool = True,
) -> tuple[dict[str, Any], dict[str, Any], int]:
    """Validate one current positive result and its immutable evidence record."""
    case_id = case["case_id"]
    selected_result = case.get("_representative_mechanism_result")
    if result is None:
        result = selected_result
    raw_results = case.get("mechanism_results")
    if not isinstance(raw_results, list) or not isinstance(result, dict):
        raise CompleteScopeError(
            f"strict result override requires a selected mechanism result for {case_id}"
        )
    result_indexes = [index for index, candidate in enumerate(raw_results) if candidate is result]
    if len(result_indexes) != 1:
        raise CompleteScopeError(
            f"strict result override cannot locate selected result for {case_id}"
        )
    result_index = result_indexes[0]
    if require_representative_binding:
        if result is not selected_result:
            raise CompleteScopeError(
                f"strict result override is not the representative result for {case_id}"
            )
        expected_binding = {
            "mechanism": case.get("_mechanism"),
            "outcome": case.get("_scope_outcome"),
            "scenario_id": case.get("_representative_scenario_id"),
        }
        for field, expected in expected_binding.items():
            if result.get(field) != expected:
                raise CompleteScopeError(
                    f"strict result override {field} mismatch for {case_id}"
                )
    for field in ("case_id", "advisory_id"):
        if field in result and result.get(field) != case[field]:
            raise CompleteScopeError(
                f"strict result override {field} mismatch for {case_id}"
            )
    require_string(
        result.get("claim_scope"),
        f"{case_id} strict result override claim_scope",
    )
    repetitions = require_count(
        result.get("repetitions"), f"{case_id} strict result override repetitions"
    )
    if repetitions == 0:
        raise CompleteScopeError(
            f"strict result override repetitions must be positive for {case_id}"
        )
    evidence = embedded_artifact_record(
        result.get("evidence"),
        f"{case_id} strict result override evidence",
        require_file=True,
    )
    mechanism = result.get("mechanism")
    outcome = result.get("outcome")
    validated_positive = mechanism_result_has_validated_positive(
        result, case_id=case_id
    )
    reason = require_string(
        result.get("reason"), f"{case_id} strict result override reason"
    )
    semantics = require_string(
        result.get("result_semantics"),
        f"{case_id} strict result override result_semantics",
    )
    checks = result.get("checks")
    if not isinstance(checks, dict):
        raise CompleteScopeError(
            f"strict result override checks are missing for {case_id}"
        )

    if (mechanism, outcome, reason, semantics) == (
        "reclaim_checks",
        "detected",
        "feature_matched_exact_allocator_diagnostic",
        "exact_diagnostic_true_positive",
    ):
        if validated_positive is not True:
            raise CompleteScopeError(
                f"strict result override detected row is not positive for {case_id}"
            )
        require_string(
            result.get("positive_scope"),
            f"{case_id} strict result override positive_scope",
        )
        if result.get("negative_boundary") is not None:
            raise CompleteScopeError(
                f"strict result override positive row has a negative boundary for {case_id}"
            )
        if any(checks.get(name) is not True for name in STRICT_RECLAIM_CHECKS):
            raise CompleteScopeError(
                f"strict result override checks are incomplete for {case_id}"
            )
        signatures = result.get("allocator_signal_signatures")
        if not isinstance(signatures, dict) or signatures.get(
            "unialloc_pointer_already_released_check"
        ) != repetitions:
            raise CompleteScopeError(
                f"strict result override signal count mismatch for {case_id}"
            )
        provenance = result.get("feature_provenance")
        if not isinstance(provenance, dict) or set(provenance) != set(
            STRICT_RECLAIM_PROVENANCE
        ):
            raise CompleteScopeError(
                f"strict result override feature provenance mismatch for {case_id}"
            )
        implementation_digests: set[str] = set()
        for name in sorted(STRICT_RECLAIM_PROVENANCE):
            entry = provenance[name]
            if not isinstance(entry, dict):
                raise CompleteScopeError(
                    f"strict result override provenance is invalid for {case_id}: {name}"
                )
            implementation_digests.add(
                validate_sha256(
                    entry.get("unialloc_implementation_sha256"),
                    f"{case_id} strict result override {name} implementation",
                )
            )
        if len(implementation_digests) != 1:
            raise CompleteScopeError(
                f"strict result override implementation drift for {case_id}"
            )
        if result.get("feature_provenance_failures") != []:
            raise CompleteScopeError(
                f"strict result override has feature provenance failures for {case_id}"
            )
    elif (mechanism, outcome, reason, semantics) == (
        "reclaim_checks",
        "no_signal",
        "feature_matched_execution_without_exact_allocator_diagnostic",
        "matched_negative_without_exact_reclaim_diagnostic",
    ):
        if validated_positive is not False or result.get("positive_scope") is not None:
            raise CompleteScopeError(
                f"strict result override no-signal row has positive semantics for {case_id}"
            )
        require_string(
            result.get("negative_boundary"),
            f"{case_id} strict result override negative_boundary",
        )
        expected_checks = {
            name: True for name in STRICT_RECLAIM_CHECKS
        }
        expected_checks["exact_allocator_signal_in_all_repetitions"] = False
        if any(
            checks.get(name) is not expected
            for name, expected in expected_checks.items()
        ):
            raise CompleteScopeError(
                f"strict result override no-signal checks are incomplete for {case_id}"
            )
        if result.get("allocator_signal_signatures") != {}:
            raise CompleteScopeError(
                f"strict result override no-signal signatures are invalid for {case_id}"
            )
        provenance = result.get("feature_provenance")
        if not isinstance(provenance, dict) or set(provenance) != set(
            STRICT_RECLAIM_PROVENANCE
        ):
            raise CompleteScopeError(
                f"strict result override feature provenance mismatch for {case_id}"
            )
        implementation_digests = {
            validate_sha256(
                provenance[name].get("unialloc_implementation_sha256")
                if isinstance(provenance[name], dict)
                else None,
                f"{case_id} strict result override {name} implementation",
            )
            for name in sorted(STRICT_RECLAIM_PROVENANCE)
        }
        if len(implementation_digests) != 1 or result.get(
            "feature_provenance_failures"
        ) != []:
            raise CompleteScopeError(
                f"strict result override no-signal provenance is invalid for {case_id}"
            )
    elif (mechanism, outcome, reason, semantics) == (
        "recovery_layout_validation",
        "detected",
        "matched_exact_recovery_layout_diagnostic",
        "exact_diagnostic_true_positive",
    ):
        if validated_positive is not True:
            raise CompleteScopeError(
                f"strict recovery-layout result is not positive for {case_id}"
            )
        if result.get("positive_scope") != (
            "allocation_deallocation_layout_mismatch_edge"
        ):
            raise CompleteScopeError(
                f"strict recovery-layout positive scope is invalid for {case_id}"
            )
        if result.get("negative_boundary") is not None:
            raise CompleteScopeError(
                f"strict recovery-layout result has a negative boundary for {case_id}"
            )
        if any(
            checks.get(name) is not True for name in STRICT_RECOVERY_LAYOUT_CHECKS
        ):
            raise CompleteScopeError(
                f"strict recovery-layout checks are incomplete for {case_id}"
            )
        signatures = result.get("allocator_signal_signatures")
        if not isinstance(signatures, dict) or signatures.get(
            "unialloc_recovery_deallocation_layout_mismatch"
        ) != repetitions:
            raise CompleteScopeError(
                f"strict recovery-layout signal count mismatch for {case_id}"
            )
        baseline_signatures = result.get("baseline_finding_signatures")
        baseline_finding_count = (
            baseline_signatures.get("miri_undefined_behavior")
            if isinstance(baseline_signatures, dict)
            else None
        )
        if (
            repetitions < 3
            or isinstance(baseline_finding_count, bool)
            or not isinstance(baseline_finding_count, int)
            or baseline_finding_count < 3
        ):
            raise CompleteScopeError(
                f"strict recovery-layout baseline count mismatch for {case_id}"
            )
        ground_truth = result.get("ground_truth_validation")
        if (
            not isinstance(ground_truth, dict)
            or ground_truth.get("valid") is not True
            or ground_truth.get("reason") != "matched_miri_source_controls"
            or ground_truth.get("vulnerable_finding_count") != baseline_finding_count
            or not isinstance(ground_truth.get("patched_clean_exit_count"), int)
            or ground_truth.get("patched_clean_exit_count") < 3
        ):
            raise CompleteScopeError(
                f"strict recovery-layout ground truth is invalid for {case_id}"
            )
        embedded_artifact_record(
            result.get("ground_truth_evidence"),
            f"{case_id} strict recovery-layout ground truth evidence",
            require_file=True,
        )
    elif (mechanism, outcome, reason, semantics) == (
        "type_isolation",
        "mitigated",
        "matched_cross_identity_reuse_edge_blocked_and_reported",
        "causal_compiler_bound_reuse_edge_mitigation",
    ):
        if validated_positive is not True:
            raise CompleteScopeError(
                f"strict result override mitigated row is not positive for {case_id}"
            )
        if result.get("positive_scope") != (
            "exploit_enabling_cross_identity_reuse_edge"
        ):
            raise CompleteScopeError(
                f"strict result override positive scope is invalid for {case_id}"
            )
        if result.get("negative_boundary") is not None:
            raise CompleteScopeError(
                f"strict result override positive row has a negative boundary for {case_id}"
            )
        if any(checks.get(name) is not True for name in STRICT_TYPEISO_CHECKS):
            raise CompleteScopeError(
                f"strict result override Type Isolation checks are incomplete for {case_id}"
            )
        for field in ("failed_checks", "missing_arms", "evidence_gaps"):
            if result.get(field) != []:
                raise CompleteScopeError(
                    f"strict result override {field} is nonempty for {case_id}"
                )
        strict_typeiso_identity_provenance(case_id=case_id, result=result)
        strict_typeiso_evidence_implementation_digest(
            case_id=case_id,
            result=result,
        )
    else:
        raise CompleteScopeError(
            f"unsupported strict result override contract for {case_id}: "
            f"{mechanism!r}/{outcome!r}/{reason!r}/{semantics!r}"
        )
    return result, evidence, result_index


def validate_and_bind_positive_mechanism_results(
    *,
    case: dict[str, Any],
    record: dict[str, Any],
) -> None:
    """Validate every explicit positive and expose nonexclusive case signals."""
    positives = case.get("_positive_mechanism_results")
    if positives is None:
        positives = positive_mechanism_results(case)
    if not isinstance(positives, list):
        raise CompleteScopeError(
            f"positive mechanism results must be a list for {case.get('case_id')}"
        )
    if not positives:
        return

    representative = case.get("_representative_mechanism_result")
    bound: list[dict[str, Any]] = []
    for candidate in positives:
        result, evidence, result_index = validate_strict_current_result(
            case=case,
            result=candidate,
            require_representative_binding=candidate is representative,
        )
        mechanism = result["mechanism"]
        outcome = result["outcome"]
        if mechanism == "reclaim_checks" and outcome == "detected":
            record["reclaim_exact_signal_observed"] = True
        elif mechanism == "recovery_layout_validation" and outcome == "detected":
            record["layout_validation_signal_observed"] = True
        elif mechanism == "type_isolation" and outcome == "mitigated":
            record["typeiso_reuse_denial_observed"] = True
        else:
            raise CompleteScopeError(
                f"unsupported positive mechanism binding for {case.get('case_id')}: "
                f"{mechanism!r}/{outcome!r}"
            )
        bound_result = {
            "source": f"scope.mechanism_results[{result_index}]",
            "mechanism": mechanism,
            "outcome": outcome,
            "scenario_id": result["scenario_id"],
            "reason": result["reason"],
            "result_semantics": result["result_semantics"],
            "positive_scope": result.get("positive_scope"),
            "claim_scope": result["claim_scope"],
            "repetitions": result["repetitions"],
            "evidence": evidence,
        }
        if mechanism == "type_isolation":
            bound_result["validated_mitigation"] = True
            bound_result.update(
                strict_typeiso_identity_provenance(
                    case_id=case["case_id"], result=result
                )
            )
            bound_result["implementation_sha256"] = (
                strict_typeiso_evidence_implementation_digest(
                    case_id=case["case_id"],
                    result=result,
                )
            )
        else:
            bound_result["true_positive"] = True
        if mechanism == "recovery_layout_validation":
            bound_result["allocator_signal_signatures"] = dict(
                result["allocator_signal_signatures"]
            )
            bound_result["ground_truth_evidence"] = embedded_artifact_record(
                result["ground_truth_evidence"],
                f"{case.get('case_id')} recovery-layout ground truth evidence",
                require_file=True,
            )
            bound_result["ground_truth_validation"] = dict(
                result["ground_truth_validation"]
            )
        bound.append(bound_result)

    record["raw_exact_allocator_signal"] = bool(
        record.get("reclaim_exact_signal_observed")
        or record.get("layout_validation_signal_observed")
        or record.get("typeiso_reuse_denial_observed")
    )
    record["strict_positive_mechanism_results"] = bound


def apply_strict_result_override(
    *,
    case: dict[str, Any],
    record: dict[str, Any],
) -> None:
    """Overlay one evidence-bound current result on historical observations.

    The raw replay or supplemental arms stay unchanged.  The rendered case-level
    binding moves to the current positive result, while explicit frozen fields
    retain the provenance of the historical observation that supplied the arms.
    """
    case_id = case["case_id"]
    frozen_outcome = record.get("outcome")
    if frozen_outcome not in {"inconclusive", "no_signal"}:
        raise CompleteScopeError(
            f"unsupported strict result override for {case_id}: "
            f"{frozen_outcome!r} -> {case.get('_scope_outcome')!r}"
        )
    result, evidence, result_index = validate_strict_current_result(case=case)
    mechanism = result["mechanism"]
    outcome = result["outcome"]
    scenario_id = result["scenario_id"]
    reason = result["reason"]
    semantics = result["result_semantics"]
    repetitions = result["repetitions"]

    frozen_fields = {
        "outcome": frozen_outcome,
        "mechanism": record.get("mechanism"),
        "representative_scenario_id": record.get("representative_scenario_id"),
        "conclusion": record.get("conclusion"),
        "root_cause": record.get("root_cause"),
        "root_cause_source": record.get("root_cause_source"),
        "catalog_path": record.get("catalog_path"),
        "scenario_source_path": record.get("scenario_source_path"),
        "oracle_tool": record.get("oracle_tool"),
        "reclaim_exact_signal_observed": record.get(
            "reclaim_exact_signal_observed"
        ),
        "layout_validation_signal_observed": record.get(
            "layout_validation_signal_observed"
        ),
        "typeiso_reuse_denial_observed": record.get(
            "typeiso_reuse_denial_observed"
        ),
        "raw_exact_allocator_signal": record.get("raw_exact_allocator_signal"),
        "source_artifact": record.get("source_artifact"),
    }
    record["historical_observation"] = frozen_fields
    record["frozen_replay_outcome"] = frozen_outcome
    record["frozen_replay_mechanism"] = frozen_fields["mechanism"]
    record["frozen_replay_representative_scenario_id"] = frozen_fields[
        "representative_scenario_id"
    ]
    record["frozen_replay_conclusion"] = frozen_fields["conclusion"]

    record["outcome"] = outcome
    record["mechanism"] = mechanism
    record["representative_scenario_id"] = scenario_id
    record["conclusion"] = reason
    catalog_scenario = case.get("_catalog_scenario")
    if not isinstance(catalog_scenario, dict):
        raise CompleteScopeError(
            f"strict result override lacks current catalog binding for {case_id}"
        )
    record["root_cause"] = catalog_scenario["root_cause"]
    record["root_cause_source"] = "catalog_oracle.vulnerable"
    record["catalog_path"] = catalog_scenario["catalog_path"]
    record["scenario_source_path"] = catalog_scenario["source_path"]
    record["oracle_tool"] = catalog_scenario["oracle_tool"]
    if mechanism == "reclaim_checks" and outcome == "detected":
        record["reclaim_exact_signal_observed"] = True
    elif mechanism == "recovery_layout_validation" and outcome == "detected":
        record["layout_validation_signal_observed"] = True
    elif mechanism == "type_isolation":
        record["typeiso_reuse_denial_observed"] = True
    record["raw_exact_allocator_signal"] = bool(
        record.get("reclaim_exact_signal_observed")
        or record.get("layout_validation_signal_observed")
        or record.get("typeiso_reuse_denial_observed")
    )

    override: dict[str, Any] = {
        "source": f"scope.mechanism_results[{result_index}]",
        "frozen_outcome": frozen_outcome,
        "outcome": outcome,
        "mechanism": mechanism,
        "scenario_id": scenario_id,
        "reason": reason,
        "result_semantics": semantics,
        "positive_scope": result.get("positive_scope"),
        "repetitions": repetitions,
        "evidence": evidence,
        "checks": dict(result["checks"]),
    }
    if mechanism == "reclaim_checks":
        override["true_positive"] = result["true_positive"]
        override["allocator_signal_signatures"] = dict(
            result["allocator_signal_signatures"]
        )
        override["feature_provenance"] = {
            name: {
                "unialloc_implementation_sha256": result["feature_provenance"][
                    name
                ]["unialloc_implementation_sha256"]
            }
            for name in sorted(STRICT_RECLAIM_PROVENANCE)
        }
    elif mechanism == "recovery_layout_validation":
        override["true_positive"] = result["true_positive"]
        override["allocator_signal_signatures"] = dict(
            result["allocator_signal_signatures"]
        )
        override["baseline_finding_signatures"] = dict(
            result["baseline_finding_signatures"]
        )
        override["ground_truth_evidence"] = embedded_artifact_record(
            result["ground_truth_evidence"],
            f"{case_id} strict recovery-layout ground truth evidence",
            require_file=True,
        )
        override["ground_truth_validation"] = dict(
            result["ground_truth_validation"]
        )
    else:
        override["validated_mitigation"] = result["validated_mitigation"]
        override.update(
            strict_typeiso_identity_provenance(case_id=case_id, result=result)
        )
    record["strict_result_override"] = override

def validate_supplemental_execution(
    execution: object,
    *,
    case_id: str,
    allocator: str,
    repetition: int,
) -> dict[str, Any]:
    if not isinstance(execution, dict):
        raise CompleteScopeError(
            f"{case_id} {allocator} repetition {repetition} must be an object"
        )
    if execution.get("repetition") != repetition:
        raise CompleteScopeError(
            f"{case_id} {allocator} repetition sequence mismatch"
        )
    if execution.get("exact_reclaim_signal_observed") is not False:
        raise CompleteScopeError(f"unexpected reclaim signal in {case_id} {allocator}")
    if execution.get("reuse_denial_signal_observed") is not False:
        raise CompleteScopeError(f"unexpected reuse denial in {case_id} {allocator}")
    oracle = execution.get("oracle")
    if not isinstance(oracle, dict) or oracle != {
        "after_churn": oracle.get("after_churn") if isinstance(oracle, dict) else None,
        "before": oracle.get("before") if isinstance(oracle, dict) else None,
        "byte_length": 4,
        "expected": [0, 1, 2, 3],
        "observed": True,
        "status": "corrupt_bytes_observed",
    }:
        raise CompleteScopeError(
            f"invalid corrupt-byte oracle in {case_id} {allocator} repetition {repetition}"
        )
    for field in ("before", "after_churn"):
        values = oracle[field]
        if not isinstance(values, list) or len(values) != 4 or not all(
            isinstance(item, int) and not isinstance(item, bool) and 0 <= item <= 255
            for item in values
        ):
            raise CompleteScopeError(
                f"invalid {field} bytes in {case_id} {allocator} repetition {repetition}"
            )
    command = execution.get("execution")
    if not isinstance(command, dict):
        raise CompleteScopeError(
            f"missing execution record in {case_id} {allocator} repetition {repetition}"
        )
    if command.get("returncode") != 0 or command.get("timed_out") is not False:
        raise CompleteScopeError(
            f"nonterminal execution in {case_id} {allocator} repetition {repetition}"
        )
    embedded_artifact_record(
        command.get("stdout"),
        f"{case_id} {allocator} repetition {repetition} stdout",
        require_file=False,
    )
    embedded_artifact_record(
        command.get("stderr"),
        f"{case_id} {allocator} repetition {repetition} stderr",
        require_file=False,
    )
    return {
        "repetition": repetition,
        "oracle_status": oracle["status"],
        "exact_reclaim_signal_observed": False,
        "reuse_denial_signal_observed": False,
    }


def validate_supplemental_executable(
    path: pathlib.Path,
    *,
    cases: dict[str, dict[str, Any]],
) -> tuple[str, dict[str, Any]]:
    payload = load_object(path, "supplemental executable")
    if payload.get("schema_version") != 1:
        raise CompleteScopeError("unsupported supplemental-executable schema")
    case_id = payload.get("case_id")
    if case_id != "RSH-064":
        raise CompleteScopeError(
            "supplemental executable must bind the dedicated RSH-064 experiment"
        )
    case = cases.get(case_id)
    if case is None or case["integration"]["status"] != "executable":
        raise CompleteScopeError("RSH-064 is not executable in the current scope")
    node_scenario_id = "RSH-064-node-addon"
    if (
        payload.get("advisory_id") != case["advisory_id"]
        or payload.get("scenario_id") != node_scenario_id
    ):
        raise CompleteScopeError("supplemental RSH-064 case/scenario binding mismatch")
    mechanism_result = mechanism_result_for_binding(
        case,
        scenario_id=node_scenario_id,
        mechanism="type_isolation",
        outcome="inconclusive",
    )
    if (
        mechanism_result.get("validated_mitigation") is not False
        or "true_positive" in mechanism_result
        or mechanism_result.get("result_semantics") != "evidence_gap"
        or mechanism_result.get("positive_scope") is not None
    ):
        raise CompleteScopeError(
            "current RSH-064 Node result must remain a non-positive evidence gap"
        )
    scope_evidence = embedded_artifact_record(
        mechanism_result.get("evidence"),
        "current RSH-064 mechanism evidence",
        require_file=True,
    )
    if scope_evidence != artifact_record(path):
        raise CompleteScopeError(
            "supplemental RSH-064 artifact differs from current scope evidence"
        )
    if (
        payload.get("claim_grade") is not False
        or payload.get("unsafe_execution_requested") is not True
        or payload.get("orchestration_success") is not True
        or payload.get("unexpected_arm_count") != 0
        or payload.get("repetitions_requested") != 3
    ):
        raise CompleteScopeError("supplemental RSH-064 terminal markers are invalid")
    variants = ["system", "reclaim_plain", "reclaim_checks", "typed_plain", "typeiso"]
    if payload.get("variants") != variants:
        raise CompleteScopeError("supplemental RSH-064 must contain five vulnerable variants")
    if payload.get("input_drift") != {}:
        raise CompleteScopeError("supplemental RSH-064 reports input drift")
    before = payload.get("input_fingerprint_before")
    after = payload.get("input_fingerprint_after")
    fingerprint_fields = {
        "catalog_sha256",
        "runner_sha256",
        "unialloc_implementation_sha256",
    }
    if (
        not isinstance(before, dict)
        or not isinstance(after, dict)
        or set(before) != fingerprint_fields
        or before != after
    ):
        raise CompleteScopeError("supplemental RSH-064 input fingerprints are invalid")
    for field in fingerprint_fields:
        validate_sha256(before[field], f"RSH-064 input fingerprint {field}")
    catalog = case["_catalog_scenario"]["catalog_artifact"]
    embedded_catalog = embedded_artifact_record(
        payload.get("catalog"),
        "RSH-064 catalog",
        require_file=False,
        verify_if_present=False,
    )
    if (
        embedded_catalog["path"] != catalog["path"]
        or before["catalog_sha256"] != embedded_catalog["sha256"]
    ):
        raise CompleteScopeError("supplemental RSH-064 catalog binding mismatch")
    checks = payload.get("checks")
    if checks != {
        "exact_reclaim_signal_count": 0,
        "feature_matched_reclaim_arms_reproduced_uaf": True,
        "patched_static_bound_compile_rejection_observed": True,
        "vulnerable_system_corrupt_bytes_reproduced": True,
    }:
        raise CompleteScopeError("supplemental RSH-064 checks are invalid")
    reclaim = payload.get("mechanism_evaluation")
    if not isinstance(reclaim, dict) or (
        reclaim.get("mechanism"), reclaim.get("outcome")
    ) != ("reclaim_checks", "no_signal"):
        raise CompleteScopeError("supplemental RSH-064 reclaim result is invalid")
    typeiso = payload.get("type_isolation_evaluation")
    if not isinstance(typeiso, dict) or {
        "outcome": typeiso.get("outcome"),
        "reason": typeiso.get("reason"),
        "reuse_denial_signal_count": typeiso.get("reuse_denial_signal_count"),
        "typed_plain_and_typeiso_uaf_reproduced": typeiso.get(
            "typed_plain_and_typeiso_uaf_reproduced"
        ),
        "compiler_critical_site_coverage_validated": typeiso.get(
            "compiler_critical_site_coverage_validated"
        ),
    } != {
        "outcome": "inconclusive",
        "reason": "compiler_critical_site_coverage_not_validated",
        "reuse_denial_signal_count": 0,
        "typed_plain_and_typeiso_uaf_reproduced": True,
        "compiler_critical_site_coverage_validated": False,
    }:
        raise CompleteScopeError("supplemental RSH-064 Type Isolation result is invalid")
    raw_arms = payload.get("arms")
    if not isinstance(raw_arms, list):
        raise CompleteScopeError("supplemental RSH-064 arms must be a list")
    expected_pairs = {("patched", "system")} | {
        ("vulnerable", allocator) for allocator in variants
    }
    arms_by_pair: dict[tuple[str, str], dict[str, Any]] = {}
    for arm in raw_arms:
        if not isinstance(arm, dict):
            raise CompleteScopeError("supplemental RSH-064 contains a non-object arm")
        pair = (arm.get("archive_variant"), arm.get("allocator_variant"))
        if pair in arms_by_pair:
            raise CompleteScopeError(f"duplicate supplemental RSH-064 arm: {pair}")
        if pair not in expected_pairs:
            raise CompleteScopeError(f"unexpected supplemental RSH-064 arm: {pair}")
        arms_by_pair[pair] = arm
    if set(arms_by_pair) != expected_pairs:
        raise CompleteScopeError("supplemental RSH-064 arm matrix is incomplete")
    patched = arms_by_pair[("patched", "system")]
    patched_build = patched.get("build")
    patched_oracle = patched.get("oracle_validation")
    if (
        patched.get("execution_status") != "completed"
        or patched.get("executions") != []
        or not isinstance(patched_build, dict)
        or patched_build.get("returncode") != 101
        or patched_build.get("timed_out") is not False
        or not isinstance(patched_oracle, dict)
        or patched_oracle.get("observed") is not True
        or patched_oracle.get("static_bound_observed") is not True
        or patched_oracle.get("status")
        != "patched_control_compile_rejection_observed"
        or set(patched_oracle.get("error_codes", [])) != {"E0505", "E0597"}
    ):
        raise CompleteScopeError("supplemental RSH-064 patched oracle is invalid")
    for stream in ("stdout", "stderr"):
        embedded_artifact_record(
            patched_build.get(stream),
            f"RSH-064 patched build {stream}",
            require_file=False,
        )
    embedded_artifact_record(
        patched.get("source"), "RSH-064 patched source", require_file=False
    )
    rendered_arms: list[dict[str, Any]] = []
    for allocator in variants:
        arm = arms_by_pair[("vulnerable", allocator)]
        build = arm.get("build")
        oracle = arm.get("oracle_validation")
        summary = arm.get("repetition_summary")
        if (
            arm.get("execution_status") != "completed"
            or arm.get("claim_grade") is not False
            or not isinstance(build, dict)
            or build.get("returncode") != 0
            or build.get("timed_out") is not False
            or oracle
            != {
                "expected_payload": [0, 1, 2, 3],
                "observed": True,
                "status": "corrupt_bytes_observed_in_all_repetitions",
            }
            or summary
            != {
                "completed": 3,
                "corrupt_bytes_observed": 3,
                "exact_reclaim_signal_observed": 0,
                "requested": 3,
                "reuse_denial_signal_observed": 0,
            }
        ):
            raise CompleteScopeError(
                f"supplemental RSH-064 vulnerable oracle is invalid for {allocator}"
            )
        for stream in ("stdout", "stderr"):
            embedded_artifact_record(
                build.get(stream),
                f"RSH-064 {allocator} build {stream}",
                require_file=False,
            )
        embedded_artifact_record(
            arm.get("source"), f"RSH-064 {allocator} source", require_file=False
        )
        executions = arm.get("executions")
        if not isinstance(executions, list) or len(executions) != 3:
            raise CompleteScopeError(
                f"supplemental RSH-064 requires three repetitions for {allocator}"
            )
        rendered_executions = [
            validate_supplemental_execution(
                execution,
                case_id=case_id,
                allocator=allocator,
                repetition=repetition,
            )
            for repetition, execution in enumerate(executions, start=1)
        ]
        compiler = arm.get("compiler_evidence")
        if allocator in {"typed_plain", "typeiso"}:
            if not isinstance(compiler, dict):
                raise CompleteScopeError(f"missing compiler evidence for {allocator}")
            for field in ("audit_files", "pass_log_files"):
                records = compiler.get(field)
                if not isinstance(records, list) or not records:
                    raise CompleteScopeError(
                        f"missing RSH-064 {allocator} compiler {field}"
                    )
                for index, record in enumerate(records, start=1):
                    embedded_artifact_record(
                        record,
                        f"RSH-064 {allocator} {field} {index}",
                        require_file=False,
                    )
        rendered_arms.append(
            {
                "archive_variant": "vulnerable",
                "allocator_variant": allocator,
                "attempt_status": "completed",
                "repetition_count": 3,
                "frozen_unialloc_implementation_sha256": before[
                    "unialloc_implementation_sha256"
                ],
                "executions": rendered_executions,
            }
        )
    catalog_scenarios = case.get("_catalog_scenarios")
    catalog_scenario = (
        catalog_scenarios.get(node_scenario_id)
        if isinstance(catalog_scenarios, dict)
        else None
    )
    if not isinstance(catalog_scenario, dict):
        raise CompleteScopeError("RSH-064 Node catalog binding is missing")
    implementation = before["unialloc_implementation_sha256"]
    return case_id, {
        "case_id": case_id,
        "advisory_id": case["advisory_id"],
        "package": case["package"],
        "primary_primitive": case["primary_primitive"],
        "root_cause": catalog_scenario["root_cause"],
        "root_cause_source": "catalog_oracle.vulnerable",
        "scope_status": "executable",
        "status": "completed",
        "outcome": "inconclusive",
        "representative_scenario_id": node_scenario_id,
        "catalog_path": catalog_scenario["catalog_path"],
        "scenario_source_path": catalog_scenario["source_path"],
        "oracle_tool": catalog_scenario["oracle_tool"],
        "mechanism": "type_isolation",
        "live_arm_count": 5,
        "reclaim_exact_signal_observed": False,
        "layout_validation_signal_observed": False,
        "typeiso_reuse_denial_observed": False,
        "raw_exact_allocator_signal": False,
        "implementation_digests": {implementation: 5},
        "scope_review_required": False,
        "blocker_reason": None,
        "blocker_code": None,
        "conclusion": typeiso["reason"],
        "probe_count": 0,
        "arms": rendered_arms,
        "supplemental_checks": checks,
        "source_artifact": recorded_path(path),
    }


def validate_probe(
    probe: object,
    case_id: str,
    index: int,
    *,
    evidence_root: pathlib.Path,
) -> dict[str, Any]:
    if not isinstance(probe, dict):
        raise CompleteScopeError(f"non-object blocked probe for {case_id}")
    if probe.get("case_id") != case_id:
        raise CompleteScopeError(f"blocked probe case binding mismatch for {case_id}")
    label = require_string(probe.get("label"), f"{case_id} probe {index} label")
    command = require_string(
        probe.get("command"), f"{case_id} probe {index} command"
    )
    exit_code = probe.get("exit_code")
    if isinstance(exit_code, bool) or not isinstance(exit_code, int):
        raise CompleteScopeError(
            f"{case_id} probe {index} exit_code must be an integer"
        )
    outcome = require_string(
        probe.get("outcome"), f"{case_id} probe {index} outcome"
    )
    raw_evidence = probe.get("evidence")
    if not isinstance(raw_evidence, list) or not raw_evidence:
        raise CompleteScopeError(f"{case_id} probe {index} has no evidence")
    evidence: list[dict[str, Any]] = []
    for evidence_index, raw in enumerate(raw_evidence, start=1):
        if not isinstance(raw, dict):
            raise CompleteScopeError(
                f"{case_id} probe {index} evidence {evidence_index} must be an object"
            )
        path_text = require_string(
            raw.get("path"),
            f"{case_id} probe {index} evidence {evidence_index} path",
        )
        expected_sha256 = validate_sha256(
            raw.get("sha256"),
            f"{case_id} probe {index} evidence {evidence_index} sha256",
        )
        expected_bytes = raw.get("bytes")
        if (
            isinstance(expected_bytes, bool)
            or not isinstance(expected_bytes, int)
            or expected_bytes < 0
        ):
            raise CompleteScopeError(
                f"{case_id} probe {index} evidence bytes must be non-negative"
            )
        path = pathlib.Path(path_text).expanduser()
        if not path.is_absolute():
            path = evidence_root / path
        path = path.resolve()
        if path != evidence_root and evidence_root not in path.parents:
            raise CompleteScopeError(
                f"blocked probe evidence escapes artifact root: {path_text}"
            )
        actual = artifact_record(path)
        if actual["sha256"] != expected_sha256 or actual["bytes"] != expected_bytes:
            raise CompleteScopeError(
                f"blocked probe evidence hash/size mismatch: {path_text}"
            )
        evidence.append(raw)
    return {
        "case_id": case_id,
        "label": label,
        "command": command,
        "exit_code": exit_code,
        "status": "completed",
        "outcome": outcome,
        "evidence": evidence,
    }


def validate_blocked_attempts(
    path: pathlib.Path,
    *,
    excluded_cases: dict[str, dict[str, Any]],
    historical_transition_cases: dict[str, dict[str, Any]] | None = None,
) -> dict[str, dict[str, Any]]:
    """Validate excluded attempts and explicitly allowlisted legacy transitions.

    The historical artifact name remains ``blocked attempts`` for compatibility.
    Its default coverage binds exactly to ``excluded_cases``.  A supplemental
    case may contribute separate historical transition metadata; neither class
    can supply an efficacy row.
    """
    payload = load_object(path, "blocked attempts")
    if payload.get("schema_version") != 1:
        raise CompleteScopeError("unsupported blocked-attempt schema")
    evidence_root = pathlib.Path(
        require_string(payload.get("root"), "blocked-attempt root")
    ).expanduser()
    if not evidence_root.is_absolute():
        evidence_root = ROOT / evidence_root
    evidence_root = evidence_root.resolve()
    if not evidence_root.is_dir():
        raise CompleteScopeError(
            f"blocked-attempt evidence root is missing: {evidence_root}"
        )
    transition_cases = historical_transition_cases or {}
    overlap = sorted(set(excluded_cases) & set(transition_cases))
    if overlap:
        raise CompleteScopeError(
            f"blocked-attempt exclusion/transition overlap: {overlap}"
        )
    expected = set(excluded_cases) | set(transition_cases)
    rows = indexed_rows(payload.get("cases"), label="blocked attempts")
    if set(rows) != expected:
        missing = sorted(expected - set(rows))
        extra = sorted(set(rows) - expected)
        raise CompleteScopeError(
            "blocked-attempt exclusion coverage mismatch; "
            f"missing={missing}, extra={extra}"
        )
    rendered: dict[str, dict[str, Any]] = {}
    for case_id, row in rows.items():
        is_transition = case_id in transition_cases
        scope_case = (
            transition_cases[case_id] if is_transition else excluded_cases[case_id]
        )
        if row.get("advisory_id") != scope_case["advisory_id"]:
            raise CompleteScopeError(
                f"blocked-attempt advisory mismatch for {case_id}"
            )
        row_package = row.get("package", row.get("crate"))
        if row_package != scope_case["package"]:
            raise CompleteScopeError(f"blocked-attempt package mismatch for {case_id}")
        if row.get("primary_primitive") != scope_case["primary_primitive"]:
            raise CompleteScopeError(
                f"blocked-attempt primary primitive mismatch for {case_id}"
            )
        prior_status = row.get("prior_status", "blocked")
        if prior_status != "blocked":
            raise CompleteScopeError(f"blocked-attempt prior status mismatch for {case_id}")
        current_status = terminal_status(
            row.get("current_status", row.get("status")),
            f"{case_id} blocked-attempt current_status",
        )
        raw_probes = row.get("probes", row.get("attempts"))
        if not isinstance(raw_probes, list) or not raw_probes:
            raise CompleteScopeError(f"blocked case has no attempted probes: {case_id}")
        probes = [
            validate_probe(
                probe,
                case_id,
                index,
                evidence_root=evidence_root,
            )
            for index, probe in enumerate(raw_probes, start=1)
        ]
        blocker_reason = scope_case["integration"].get("blocker_reason")
        root_blocker = row.get("root_blocker")
        if root_blocker is not None and not isinstance(root_blocker, str):
            raise CompleteScopeError(f"invalid root_blocker for {case_id}")
        conclusion = row.get("conclusion")
        if not isinstance(conclusion, str) or not conclusion.strip():
            raise CompleteScopeError(f"blocked-attempt conclusion missing for {case_id}")
        blocker_code = row.get("blocker_code")
        if blocker_code is not None and not isinstance(blocker_code, str):
            raise CompleteScopeError(f"invalid blocker_code for {case_id}")
        if is_transition and current_status != "newly_executable":
            raise CompleteScopeError(
                f"historical transition must be newly_executable for {case_id}"
            )
        if not is_transition and current_status != "blocked":
            raise CompleteScopeError(
                f"excluded case changed status in blocked-attempt artifact: {case_id}"
            )
        scope_status = "historical_transition" if is_transition else "excluded"
        outcome = current_status if is_transition else "excluded"
        record = {
            "case_id": case_id,
            "advisory_id": scope_case["advisory_id"],
            "package": scope_case["package"],
            "primary_primitive": scope_case["primary_primitive"],
            "root_cause": root_blocker or blocker_reason,
            "root_cause_source": "blocked_attempt.root_blocker",
            "scope_status": scope_status,
            "status": current_status,
            "outcome": outcome,
            "representative_scenario_id": (
                scope_case["_representative_scenario_id"] if is_transition else None
            ),
            "catalog_path": None,
            "scenario_source_path": None,
            "oracle_tool": None,
            "mechanism": scope_case["_mechanism"] if is_transition else None,
            "live_arm_count": 0,
            "reclaim_exact_signal_observed": False,
            "layout_validation_signal_observed": False,
            "typeiso_reuse_denial_observed": False,
            "raw_exact_allocator_signal": False,
            "implementation_digests": {},
            "scope_review_required": False,
            "blocker_reason": blocker_reason,
            "blocker_code": blocker_code,
            "conclusion": conclusion,
            "probe_count": len(probes),
            "probes": probes,
            "evidence_files": row.get("evidence_files", []),
            "source_artifact": recorded_path(path),
        }
        if is_transition:
            record["transition_target"] = {
                "scope_status": "executable",
                "outcome": scope_case["_scope_outcome"],
                "mechanism": scope_case["_mechanism"],
                "representative_scenario_id": scope_case[
                    "_representative_scenario_id"
                ],
            }
        else:
            record["scope_exclusion"] = scope_case["scope_exclusion"]
        rendered[case_id] = record
    return rendered


def planned_exclusion_records(
    excluded_cases: dict[str, dict[str, Any]],
) -> list[dict[str, Any]]:
    return [
        {
            "case_id": case_id,
            "advisory_id": case["advisory_id"],
            "package": case["package"],
            "primary_primitive": case["primary_primitive"],
            "root_cause": case["integration"]["blocker_reason"],
            "root_cause_source": "scope.integration.blocker_reason",
            "scope_status": "excluded",
            "status": "planned",
            "outcome": "planned_exclusion_probe",
            "representative_scenario_id": None,
            "catalog_path": None,
            "scenario_source_path": None,
            "oracle_tool": None,
            "mechanism": None,
            "live_arm_count": 0,
            "reclaim_exact_signal_observed": False,
            "layout_validation_signal_observed": False,
            "typeiso_reuse_denial_observed": False,
            "raw_exact_allocator_signal": False,
            "implementation_digests": {},
            "scope_review_required": False,
            "blocker_reason": case["integration"]["blocker_reason"],
            "blocker_code": case["scope_exclusion"]["code"],
            "conclusion": case["scope_exclusion"]["reason"],
            "probe_count": 0,
            "scope_exclusion": case["scope_exclusion"],
            "source_artifact": None,
        }
        for case_id, case in sorted(excluded_cases.items())
    ]


def planned_case_records(cases: dict[str, dict[str, Any]]) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    for case_id in sorted(cases):
        case = cases[case_id]
        integration = case["integration"]
        scenario_id = case["_representative_scenario_id"]
        catalog = case.get("_catalog_scenario")
        blocked = integration["status"] == "blocked"
        result.append(
            {
                "case_id": case_id,
                "advisory_id": case["advisory_id"],
                "package": case["package"],
                "primary_primitive": case["primary_primitive"],
                "root_cause": (
                    integration.get("blocker_reason")
                    if blocked
                    else catalog["root_cause"]
                ),
                "root_cause_source": (
                    "scope.integration.blocker_reason"
                    if blocked
                    else "catalog_oracle.vulnerable"
                ),
                "scope_status": integration["status"],
                "status": "planned",
                "outcome": (
                    "planned_blocked_probe" if blocked else "planned_executable_replay"
                ),
                "representative_scenario_id": scenario_id,
                "catalog_path": None if blocked else catalog["catalog_path"],
                "scenario_source_path": None if blocked else catalog["source_path"],
                "oracle_tool": None if blocked else catalog["oracle_tool"],
                "mechanism": case["_mechanism"],
                "live_arm_count": 0,
                "reclaim_exact_signal_observed": False,
                "layout_validation_signal_observed": False,
                "typeiso_reuse_denial_observed": False,
                "raw_exact_allocator_signal": False,
                "implementation_digests": {},
                "scope_review_required": False,
                "blocker_reason": integration.get("blocker_reason"),
                "blocker_code": None,
                "conclusion": None,
                "probe_count": 0,
                "source_artifact": None,
            }
        )
    return result


def count_values(records: Iterable[dict[str, Any]], field: str) -> dict[str, int]:
    counts: collections.Counter[str] = collections.Counter()
    for record in records:
        value = record.get(field)
        counts[str(value)] += 1
    return dict(sorted(counts.items()))


def write_csv(path: pathlib.Path, records: Sequence[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=CSV_FIELDS, lineterminator="\n")
        writer.writeheader()
        for record in records:
            row: dict[str, Any] = {}
            for field in CSV_FIELDS:
                value = record.get(field)
                if isinstance(value, (dict, list)):
                    value = json.dumps(value, sort_keys=True, separators=(",", ":"))
                elif isinstance(value, bool):
                    value = "true" if value else "false"
                elif value is None:
                    value = ""
                row[field] = value
            writer.writerow(row)


def build_report(
    *,
    scope_path: pathlib.Path,
    catalog_paths: Sequence[pathlib.Path],
    jobs: dict[str, dict[str, Any]],
    plan_path: pathlib.Path | None,
    output_json: pathlib.Path,
    output_csv: pathlib.Path,
    log_dir: pathlib.Path,
    dry_run: bool,
    execute_unsafe: bool,
    default_timeout_seconds: int,
    base_scope_path: pathlib.Path | None = None,
    supplemental_paths: Sequence[pathlib.Path] = (),
) -> tuple[dict[str, Any], bool]:
    scope_path = scope_path.expanduser().resolve()
    scope_payload, cases, excluded_cases = load_scope(scope_path)
    scope_sha256 = sweep.sha256_file(scope_path)
    base_scope_path = (
        base_scope_path.expanduser().resolve()
        if base_scope_path is not None
        else scope_path
    )
    if base_scope_path == scope_path:
        _base_scope_payload, base_cases = scope_payload, cases
    else:
        _base_scope_payload, base_cases = load_base_scope(base_scope_path)
    base_scope_sha256 = sweep.sha256_file(base_scope_path)
    supplemental_paths = tuple(path.expanduser().resolve() for path in supplemental_paths)
    if len(supplemental_paths) != len(set(supplemental_paths)):
        raise CompleteScopeError("duplicate --supplemental-executable path")
    scenarios, catalog_records = load_catalog_index(catalog_paths)
    bind_scope_to_catalogs(cases, scenarios)
    bind_scope_to_catalogs(base_cases, scenarios)
    job_records, jobs_valid = run_plan_jobs(
        jobs,
        dry_run=dry_run,
        execute_unsafe=execute_unsafe,
        default_timeout_seconds=default_timeout_seconds,
        log_dir=log_dir,
    )
    if dry_run:
        case_records = planned_case_records(cases)
        exclusion_records = planned_exclusion_records(excluded_cases)
        transition_records: list[dict[str, Any]] = []
        expected_transition_ids: set[str] = set()
        excluded_source_artifact = None
        supplemental_artifacts: list[dict[str, Any]] = []
    else:
        executable_path = jobs["executable_replay"]["artifact_path"]
        blocked_path = jobs["blocked_attempts"]["artifact_path"]
        executable = validate_executable_replay(
            executable_path,
            scope_sha256=base_scope_sha256,
            cases=base_cases,
        )
        supplemental: dict[str, dict[str, Any]] = {}
        supplemental_artifacts = []
        for path in supplemental_paths:
            case_id, record = validate_supplemental_executable(path, cases=cases)
            if case_id in supplemental:
                raise CompleteScopeError(
                    f"duplicate supplemental executable case: {case_id}"
                )
            expected_binding = (
                cases[case_id]["_scope_outcome"],
                cases[case_id]["_mechanism"],
                cases[case_id]["_representative_scenario_id"],
            )
            observed_binding = (
                record.get("outcome"),
                record.get("mechanism"),
                record.get("representative_scenario_id"),
            )
            if observed_binding != expected_binding:
                apply_strict_result_override(case=cases[case_id], record=record)
            final_binding = (
                record.get("outcome"),
                record.get("mechanism"),
                record.get("representative_scenario_id"),
            )
            if final_binding != expected_binding:
                raise CompleteScopeError(
                    f"supplemental binding changed without a strict result "
                    f"override for {case_id}"
                )
            validate_and_bind_positive_mechanism_results(
                case=cases[case_id], record=record
            )
            supplemental[case_id] = record
            supplemental_artifacts.append(artifact_record(path))
        bind_replay_to_current_scope(
            executable,
            cases=cases,
            supplemental_case_ids=set(supplemental),
        )
        expected_transition_ids = set(supplemental)
        attempt_records = validate_blocked_attempts(
            blocked_path,
            excluded_cases=excluded_cases,
            historical_transition_cases={
                case_id: cases[case_id] for case_id in expected_transition_ids
            },
        )
        combined = {**executable, **supplemental}
        if len(combined) != len(cases) or set(combined) != set(cases):
            raise CompleteScopeError(
                "joined live artifacts do not cover the recorded evaluable scope"
            )
        case_records = [combined[case_id] for case_id in sorted(combined)]
        exclusion_records = [
            attempt_records[case_id] for case_id in sorted(excluded_cases)
        ]
        transition_records = [
            attempt_records[case_id] for case_id in sorted(expected_transition_ids)
        ]
        excluded_source_artifact = artifact_record(blocked_path)
    implementation_counts: collections.Counter[str] = collections.Counter()
    strict_typeiso_implementation_counts: collections.Counter[str] = (
        collections.Counter()
    )
    strict_typeiso_automatic_case_ids: set[str] = set()
    strict_typeiso_manual_case_ids: set[str] = set()
    binary_available_arm_count = 0
    archived_binary_unavailable_arm_count = 0
    for record in case_records:
        implementation_counts.update(record.get("implementation_digests", {}))
        strict_positive_results = record.get("strict_positive_mechanism_results", [])
        if isinstance(strict_positive_results, list):
            for result in strict_positive_results:
                if (
                    isinstance(result, dict)
                    and result.get("mechanism") == "type_isolation"
                    and result.get("outcome") == "mitigated"
                    and result.get("validated_mitigation") is True
                    and "true_positive" not in result
                ):
                    if result.get("compiler_automatic_victim_coverage") is True:
                        strict_typeiso_automatic_case_ids.add(record["case_id"])
                    else:
                        strict_typeiso_manual_case_ids.add(record["case_id"])
                    strict_typeiso_implementation_counts.update(
                        [
                            validate_sha256(
                                result.get("implementation_sha256"),
                                "strict Type Isolation evidence implementation",
                            )
                        ]
                    )
        arms = record.get("arms", [])
        if isinstance(arms, list):
            binary_available_arm_count += sum(
                arm.get("binary_available") is True
                for arm in arms
                if isinstance(arm, dict)
            )
            archived_binary_unavailable_arm_count += sum(
                arm.get("binary_available") is False
                for arm in arms
                if isinstance(arm, dict)
            )
    efficacy_ids = [record.get("case_id") for record in case_records]
    exclusion_ids = [record.get("case_id") for record in exclusion_records]
    transition_ids = [record.get("case_id") for record in transition_records]
    report_valid = (
        jobs_valid
        and len(efficacy_ids) == len(cases)
        and set(efficacy_ids) == set(cases)
        and len(set(efficacy_ids)) == len(efficacy_ids)
        and len(exclusion_ids) == len(excluded_cases)
        and set(exclusion_ids) == set(excluded_cases)
        and len(set(exclusion_ids)) == len(exclusion_ids)
        and set(efficacy_ids).isdisjoint(exclusion_ids)
        and len(transition_ids) == len(expected_transition_ids)
        and set(transition_ids) == expected_transition_ids
        and len(set(transition_ids)) == len(transition_ids)
    )
    report = {
        "schema_version": 1,
        "source": "unialloc-rustsec-complete-scope-run",
        "claim_grade": False,
        "valid": report_valid,
        "dry_run": dry_run,
        "generated_at_unix": int(time.time()),
        "scope": artifact_record(scope_path),
        "base_scope": artifact_record(base_scope_path),
        "scope_source": scope_payload.get("source"),
        "plan": artifact_record(plan_path) if plan_path is not None else None,
        "catalogs": catalog_records,
        "jobs": job_records,
        "supplemental_executables": supplemental_artifacts,
        "counts": {
            "case_count": len(case_records),
            "scope_status": count_values(case_records, "scope_status"),
            "status": count_values(case_records, "status"),
            "outcome": count_values(case_records, "outcome"),
            "raw_exact_allocator_signal_case_count": sum(
                record["raw_exact_allocator_signal"] for record in case_records
            ),
            "reclaim_exact_signal_case_count": sum(
                record["reclaim_exact_signal_observed"] for record in case_records
            ),
            "layout_validation_signal_case_count": sum(
                record["layout_validation_signal_observed"]
                for record in case_records
            ),
            "typeiso_reuse_denial_case_count": sum(
                record["typeiso_reuse_denial_observed"] for record in case_records
            ),
            "typeiso_compiler_automatic_victim_coverage_case_count": (
                len(strict_typeiso_automatic_case_ids)
            ),
            "typeiso_manual_victim_annotation_case_count": (
                len(strict_typeiso_manual_case_ids)
            ),
            "typeiso_vulnerability_specific_detection_case_count": 0,
            "typeiso_full_source_vulnerability_detection_case_count": 0,
            "scope_review_required_count": sum(
                record["scope_review_required"] for record in case_records
            ),
            "live_arm_count": sum(record["live_arm_count"] for record in case_records),
            "binary_available_arm_count": binary_available_arm_count,
            "archived_binary_unavailable_arm_count": (
                archived_binary_unavailable_arm_count
            ),
            "probe_count": sum(record["probe_count"] for record in case_records),
            "replay_arm_implementation_digest_counts": dict(
                sorted(implementation_counts.items())
            ),
            "strict_typeiso_evidence_implementation_digest_counts": dict(
                sorted(strict_typeiso_implementation_counts.items())
            ),
        },
        "cases": case_records,
        "excluded_audit": {
            "source_artifact": excluded_source_artifact,
            "counts": {
                "case_count": len(exclusion_records),
                "scope_status": count_values(exclusion_records, "scope_status"),
                "status": count_values(exclusion_records, "status"),
                "outcome": count_values(exclusion_records, "outcome"),
                "probe_count": sum(
                    record["probe_count"] for record in exclusion_records
                ),
            },
            "cases": exclusion_records,
        },
        "historical_transitions": {
            "source_artifact": excluded_source_artifact,
            "case_count": len(transition_records),
            "cases": transition_records,
        },
        "outputs": {
            "json": recorded_path(output_json),
            "csv": recorded_path(output_csv),
        },
        "claim_boundary": (
            "The report preserves each base-replay arm's frozen UniAlloc "
            "implementation digest and the supplemental experiment's own input "
            "fingerprint. Multiple digests are separate observations and do not "
            "describe one current allocator snapshot. Archived replay arms with "
            "binary_available=false preserve recorded digests and are not local "
            "live-binary verification; every present binary is rehashed and any "
            "mismatch fails validation. Exact allocator signals remain "
            "detection observations; clean exits do not establish mitigation. "
            "Type Isolation positives cover manually attributed derived "
            "cross-identity reuse edges; the automatic source-vulnerability "
            "Type Isolation true-positive count is zero. Their strict matrix "
            "implementation digests are reported separately from historical "
            "replay-arm digests and remain evidence-snapshot provenance rather "
            "than a claim about the current working tree. RSH-065 and RSH-069 "
            "are synthetic manual reductions. "
            "Excluded audit rows remain outside the efficacy cases and CSV."
        ),
    }
    report["report_sha256"] = sweep.canonical_sha256(
        {key: value for key, value in report.items() if key != "report_sha256"}
    )
    output_json.parent.mkdir(parents=True, exist_ok=True)
    sweep.write_json(output_json, report)
    write_csv(output_csv, case_records)
    return report, report_valid


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--scope", type=pathlib.Path, default=DEFAULT_SCOPE)
    parser.add_argument(
        "--base-scope",
        type=pathlib.Path,
        help="archived predecessor scope that binds the frozen executable replay",
    )
    parser.add_argument(
        "--catalog",
        action="append",
        type=pathlib.Path,
        dest="catalogs",
        help=(
            "harness catalog; repeatable (defaults to base, expansion, batches a-f, "
            "and the Neon Node catalog)"
        ),
    )
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--plan", type=pathlib.Path)
    source.add_argument(
        "--executable-replay",
        type=pathlib.Path,
        help="verify this replay directly; requires --blocked-attempts",
    )
    parser.add_argument("--blocked-attempts", type=pathlib.Path)
    parser.add_argument(
        "--supplemental-executable",
        action="append",
        type=pathlib.Path,
        default=[],
        help="supplement the frozen replay with a current-scope experiment; repeatable",
    )
    parser.add_argument("--output-json", type=pathlib.Path, required=True)
    parser.add_argument("--output-csv", type=pathlib.Path, required=True)
    parser.add_argument("--log-dir", type=pathlib.Path)
    parser.add_argument("--default-timeout-seconds", type=int, default=3600)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--execute-unsafe", action="store_true")
    return parser


def normalize_paths(args: argparse.Namespace) -> None:
    args.scope = args.scope.expanduser().resolve()
    args.base_scope = (
        args.base_scope.expanduser().resolve()
        if args.base_scope is not None
        else None
    )
    args.supplemental_executable = tuple(
        path.expanduser().resolve() for path in args.supplemental_executable
    )
    args.catalogs = tuple(
        path.expanduser().resolve() for path in (args.catalogs or DEFAULT_CATALOGS)
    )
    args.output_json = args.output_json.expanduser().resolve()
    args.output_csv = args.output_csv.expanduser().resolve()
    if args.output_json == args.output_csv:
        raise CompleteScopeError("JSON and CSV outputs must use different paths")
    args.log_dir = (
        args.log_dir.expanduser().resolve()
        if args.log_dir is not None
        else args.output_json.with_suffix("").with_name(
            args.output_json.stem + "-logs"
        )
    )
    if args.default_timeout_seconds <= 0:
        raise CompleteScopeError("default timeout must be positive")
    if args.plan is not None:
        args.plan = args.plan.expanduser().resolve()
        if args.blocked_attempts is not None:
            raise CompleteScopeError("--blocked-attempts cannot accompany --plan")
    else:
        if args.blocked_attempts is None:
            raise CompleteScopeError(
                "--executable-replay requires --blocked-attempts"
            )
        args.executable_replay = args.executable_replay.expanduser().resolve()
        args.blocked_attempts = args.blocked_attempts.expanduser().resolve()


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        normalize_paths(args)
        scope_sha256 = sweep.sha256_file(args.scope)
        if args.plan is not None:
            _plan, jobs = load_plan(args.plan, scope_sha256=scope_sha256)
        else:
            jobs = direct_verify_jobs(args.executable_replay, args.blocked_attempts)
        report, valid = build_report(
            scope_path=args.scope,
            base_scope_path=args.base_scope,
            catalog_paths=args.catalogs,
            jobs=jobs,
            plan_path=args.plan,
            output_json=args.output_json,
            output_csv=args.output_csv,
            log_dir=args.log_dir,
            dry_run=args.dry_run,
            execute_unsafe=args.execute_unsafe,
            default_timeout_seconds=args.default_timeout_seconds,
            supplemental_paths=args.supplemental_executable,
        )
        print(json.dumps(report, indent=2, sort_keys=True))
        return 0 if valid else 1
    except (
        CompleteScopeError,
        OSError,
        json.JSONDecodeError,
        subprocess.SubprocessError,
        sweep.SweepError,
    ) as error:
        print(f"error: {error}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
