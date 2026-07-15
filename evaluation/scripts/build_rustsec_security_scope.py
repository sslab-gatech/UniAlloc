#!/usr/bin/env python3
"""Build the complete, mechanism-attributed RustSec evaluation-scope ledger."""

from __future__ import annotations

import argparse
import collections
import json
import pathlib
from typing import Any, Iterable, Sequence


ROOT = pathlib.Path(__file__).resolve().parents[2]
DEFAULT_INVENTORY = ROOT / "evaluation/config/rustsec_heap_candidate_inventory.json"
DEFAULT_REVIEW = ROOT / "evaluation/config/rustsec_temporal_reclaim_review.json"
DEFAULT_CORPUS = ROOT / "evaluation/config/rustsec_heap_security_corpus.json"
DEFAULT_PRIMITIVE_OVERRIDES = (
    ROOT / "evaluation/config/rustsec_heap_primitive_overrides.json"
)
DEFAULT_MECHANISM_AMENDMENTS = (
    ROOT / "evaluation/config/rustsec_heap_mechanism_amendments.json"
)
DEFAULT_OUTPUT = ROOT / "evaluation/config/rustsec_heap_complete_scope.json"
DEFAULT_CATALOGS = (
    ROOT / "evaluation/config/rustsec_heap_harnesses.json",
    ROOT / "evaluation/config/rustsec_heap_expansion_harnesses.json",
)
MECHANISM_ORDER = (
    "type_isolation",
    "reclaim_checks",
    "quarantine",
    "guard_pages",
    "force_initialize",
    "memory_tagging",
    "recovery_layout_validation",
)
OBSERVED_OUTCOMES = frozenset(("detected", "mitigated"))
VALID_OUTCOMES = OBSERVED_OUTCOMES | frozenset(("no_signal", "inconclusive"))
TERMINAL_INTEGRATION = frozenset(("executable", "blocked"))
VALID_OVERRIDE_PRIMITIVES = frozenset(
    (
        "aliasing_violation",
        "double_free",
        "invalid_free",
        "misaligned_allocation",
        "misaligned_reference",
        "out_of_bounds_read",
        "out_of_bounds_write",
        "type_confusion",
        "uninitialized_drop",
        "uninitialized_read",
        "use_after_free",
    )
)


class ScopeError(RuntimeError):
    """Raised when the scope ledger would silently lose or overclaim a case."""


def load_json(path: pathlib.Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ScopeError(f"JSON root must be an object: {path}")
    return value


def strong_candidate_ids(review: dict[str, Any]) -> list[str]:
    strong = review.get("strong_candidates")
    if not isinstance(strong, dict):
        raise ScopeError("review strong_candidates must be an object")
    result: set[str] = set()
    for key in (
        "cross_type_reuse",
        "tracked_reclaim_or_invalid_free",
        "supplemental_rudra_source_evidence",
    ):
        values = strong.get(key, [])
        if not isinstance(values, list) or not all(isinstance(value, str) for value in values):
            raise ScopeError(f"review strong_candidates.{key} must be a string list")
        result.update(values)
    return sorted(result)


def _ordered_mechanisms(values: Iterable[str]) -> list[str]:
    unique = set(values)
    return [value for value in MECHANISM_ORDER if value in unique] + sorted(
        unique - set(MECHANISM_ORDER)
    )


def candidate_mechanisms(advisory_id: str, review: dict[str, Any]) -> list[str]:
    strong = review["strong_candidates"]
    mechanisms: list[str] = []
    if advisory_id in strong.get("cross_type_reuse", []):
        mechanisms.append("type_isolation")
    if advisory_id in strong.get("tracked_reclaim_or_invalid_free", []) or advisory_id in strong.get(
        "supplemental_rudra_source_evidence", []
    ):
        mechanisms.append("reclaim_checks")
    return _ordered_mechanisms(mechanisms)


def primary_primitive(
    inventory_entry: dict[str, Any], corpus_case: dict[str, Any] | None
) -> str:
    if corpus_case is not None:
        taxonomy = corpus_case.get("taxonomy")
        if isinstance(taxonomy, dict) and isinstance(taxonomy.get("primary_primitive"), str):
            return str(taxonomy["primary_primitive"])
    signals = set(inventory_entry.get("classification_signals", []))
    for value in (
        "double_free",
        "use_after_free",
        "invalid_deallocation",
        "out_of_bounds",
        "uninitialized_exposure",
    ):
        if value in signals:
            return value
    return "manual_classification_required"


def _primitive_override_index(
    payloads: Sequence[dict[str, Any]], known_advisory_ids: set[str]
) -> dict[str, dict[str, Any]]:
    """Validate reviewed primitive overrides and index them by advisory."""

    result: dict[str, dict[str, Any]] = {}
    for payload in payloads:
        rows = payload.get("overrides")
        if not isinstance(rows, list):
            raise ScopeError("primitive override payload requires an overrides list")
        for row in rows:
            if not isinstance(row, dict):
                raise ScopeError("primitive override must be an object")
            advisory_id = row.get("advisory_id")
            if not isinstance(advisory_id, str):
                raise ScopeError("primitive override requires advisory_id")
            if advisory_id not in known_advisory_ids:
                raise ScopeError(
                    f"primitive override references unknown advisory: {advisory_id}"
                )
            if advisory_id in result:
                raise ScopeError(f"duplicate primitive override: {advisory_id}")
            primitive = row.get("primary_primitive")
            if primitive not in VALID_OVERRIDE_PRIMITIVES:
                raise ScopeError(
                    f"invalid primitive override for {advisory_id}: {primitive!r}"
                )
            replaces = row.get("replaces")
            if not isinstance(replaces, str) or not replaces:
                raise ScopeError(f"primitive override requires replaces: {advisory_id}")
            reason = row.get("reason")
            if not isinstance(reason, str) or not reason.strip():
                raise ScopeError(f"primitive override requires reason: {advisory_id}")
            evidence_paths = row.get("evidence_paths")
            if (
                not isinstance(evidence_paths, list)
                or not evidence_paths
                or not all(isinstance(path, str) and path for path in evidence_paths)
            ):
                raise ScopeError(
                    f"primitive override requires evidence_paths: {advisory_id}"
                )
            case_id = row.get("case_id")
            if case_id is not None and not isinstance(case_id, str):
                raise ScopeError(
                    f"primitive override case_id must be a string: {advisory_id}"
                )
            result[advisory_id] = {
                "case_id": case_id,
                "primary_primitive": primitive,
                "replaces": replaces,
                "reason": reason.strip(),
                "evidence_paths": list(evidence_paths),
            }
    return result


def _mechanism_amendment_index(
    payloads: Sequence[dict[str, Any]], known_advisory_ids: set[str]
) -> dict[tuple[str, str, str, str], dict[str, Any]]:
    """Validate reviewed post-integration mechanism evaluation amendments."""

    result: dict[tuple[str, str, str, str], dict[str, Any]] = {}
    for payload in payloads:
        rows = payload.get("amendments")
        if not isinstance(rows, list):
            raise ScopeError("mechanism amendment payload requires an amendments list")
        for row in rows:
            if not isinstance(row, dict):
                raise ScopeError("mechanism amendment must be an object")
            advisory_id = row.get("advisory_id")
            case_id = row.get("case_id")
            scenario_id = row.get("scenario_id")
            mechanism = row.get("mechanism")
            if not all(
                isinstance(value, str) and value
                for value in (advisory_id, case_id, scenario_id, mechanism)
            ):
                raise ScopeError(
                    "mechanism amendment requires advisory_id, case_id, scenario_id, "
                    "and mechanism"
                )
            assert isinstance(advisory_id, str)
            assert isinstance(case_id, str)
            assert isinstance(scenario_id, str)
            assert isinstance(mechanism, str)
            if advisory_id not in known_advisory_ids:
                raise ScopeError(
                    f"mechanism amendment references unknown advisory: {advisory_id}"
                )
            allowed_outcomes = row.get("allowed_outcomes")
            if (
                not isinstance(allowed_outcomes, list)
                or not allowed_outcomes
                or any(outcome not in VALID_OUTCOMES for outcome in allowed_outcomes)
                or len(set(allowed_outcomes)) != len(allowed_outcomes)
            ):
                raise ScopeError(
                    f"invalid mechanism amendment outcomes for {advisory_id}"
                )
            reason = row.get("reason")
            evidence_paths = row.get("evidence_paths")
            if not isinstance(reason, str) or not reason.strip():
                raise ScopeError(
                    f"mechanism amendment requires reason: {advisory_id}"
                )
            if (
                not isinstance(evidence_paths, list)
                or not evidence_paths
                or not all(isinstance(path, str) and path for path in evidence_paths)
            ):
                raise ScopeError(
                    f"mechanism amendment requires evidence_paths: {advisory_id}"
                )
            key = (advisory_id, case_id, scenario_id, mechanism)
            if key in result:
                raise ScopeError(f"duplicate mechanism amendment: {key}")
            result[key] = {
                "allowed_outcomes": list(allowed_outcomes),
                "reason": reason.strip(),
                "evidence_paths": list(evidence_paths),
            }
    return result


def _catalog_index(
    catalogs: Sequence[dict[str, Any]], corpus_by_case: dict[str, dict[str, Any]]
) -> dict[str, dict[str, Any]]:
    indexed: dict[str, dict[str, Any]] = {}
    for catalog in catalogs:
        for case in catalog.get("cases", []):
            if not isinstance(case, dict):
                raise ScopeError("catalog case must be an object")
            case_id = case.get("case_id")
            advisory_id = case.get("advisory_id")
            if advisory_id is None and isinstance(case_id, str) and case_id in corpus_by_case:
                advisory_id = corpus_by_case[case_id].get("advisory_id")
            if not isinstance(advisory_id, str):
                continue
            scenarios = case.get("scenarios", [])
            if not isinstance(scenarios, list):
                raise ScopeError(f"catalog scenarios must be a list for {case_id}")
            existing = indexed.get(advisory_id)
            if existing is not None and existing.get("case_id") != case_id:
                raise ScopeError(f"duplicate advisory with different case ids: {advisory_id}")
            scenario_ids = {
                str(scenario["scenario_id"])
                for scenario in scenarios
                if isinstance(scenario, dict)
                and isinstance(scenario.get("scenario_id"), str)
            }
            if existing is not None:
                scenario_ids.update(existing["scenario_ids"])
            indexed[advisory_id] = {
                "case_id": case_id,
                "scenario_ids": sorted(scenario_ids),
            }
    return indexed


def _status_index(statuses: Sequence[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    result: dict[str, dict[str, Any]] = {}
    for row in statuses:
        advisory_id = row.get("advisory_id")
        if not isinstance(advisory_id, str):
            raise ScopeError("integration status requires advisory_id")
        if advisory_id in result:
            raise ScopeError(f"duplicate integration status: {advisory_id}")
        status = row.get("integration_status", row.get("status"))
        if status not in ("executable", "blocked"):
            raise ScopeError(f"invalid integration status for {advisory_id}: {status!r}")
        result[advisory_id] = dict(row, integration_status=status)
    return result


def status_rows(payload: dict[str, Any]) -> list[dict[str, Any]]:
    """Normalize shared and batch-local integration status schemas."""

    direct = payload.get("cases", payload.get("statuses"))
    if direct is not None:
        if not isinstance(direct, list):
            raise ScopeError("status cases must be a list")
        rows: list[dict[str, Any]] = []
        aliases = {
            "executable": "executable",
            "executable_and_validated": "executable",
            "executable_baseline_reproduced": "executable",
            "blocked": "blocked",
        }
        for record in direct:
            if not isinstance(record, dict):
                raise ScopeError("status case must be an object")
            raw_status = (
                record.get("integration_status")
                or record.get("status")
                or record.get("terminal_status")
            )
            normalized = aliases.get(raw_status)
            if normalized is None:
                raise ScopeError(
                    f"invalid integration status for {record.get('advisory_id')}: "
                    f"{raw_status!r}"
                )
            rows.append(dict(record, integration_status=normalized))
        for record in payload.get("blocked_cases", []):
            if not isinstance(record, dict):
                raise ScopeError("blocked case must be an object")
            rows.append(
                {
                    **record,
                    "integration_status": "blocked",
                    "blocker_reason": record.get("reason")
                    or record.get("blocker_reason")
                    or record.get("blocker_code"),
                }
            )
        return rows
    rows: list[dict[str, Any]] = []
    for record in payload.get("baseline_results", []):
        if not isinstance(record, dict):
            raise ScopeError("baseline result must be an object")
        rows.append(
            {
                "advisory_id": record.get("advisory_id"),
                "case_id": record.get("case_id"),
                "integration_status": "executable",
                "scenario_ids": [record["scenario_id"]]
                if isinstance(record.get("scenario_id"), str)
                else [],
                "evidence": [
                    record.get("experiment", {}).get("path"),
                    record.get("experiment", {}).get("sha256"),
                ],
            }
        )
    for record in payload.get("blocked_cases", []):
        if not isinstance(record, dict):
            raise ScopeError("blocked case must be an object")
        evidence = list(record.get("repository_artifacts", {}).keys())
        rows.append(
            {
                "advisory_id": record.get("advisory_id"),
                "case_id": record.get("case_id"),
                "integration_status": "blocked",
                "blocker_reason": record.get("reason") or record.get("blocker_code"),
                "evidence": evidence,
            }
        )
    return rows


def _result_index(payload: dict[str, Any] | None) -> dict[str, list[dict[str, Any]]]:
    result: dict[str, list[dict[str, Any]]] = collections.defaultdict(list)
    if payload is None:
        return result
    rows = payload.get("results", [])
    if not isinstance(rows, list):
        raise ScopeError("mechanism results must be a list")
    for row in rows:
        if not isinstance(row, dict):
            raise ScopeError("mechanism result must be an object")
        advisory_id = row.get("advisory_id")
        case_id = row.get("case_id")
        scenario_id = row.get("scenario_id")
        mechanism = row.get("mechanism")
        outcome = row.get("outcome")
        if not all(
            isinstance(value, str) and value
            for value in (advisory_id, case_id, scenario_id, mechanism)
        ):
            raise ScopeError(
                "mechanism result requires advisory_id, case_id, scenario_id, "
                "and mechanism"
            )
        if outcome not in VALID_OUTCOMES:
            raise ScopeError(f"invalid mechanism outcome for {advisory_id}: {outcome!r}")
        result[advisory_id].append(row)
    return result


def _final_attribution(results: Sequence[dict[str, Any]]) -> str:
    observed = {
        str(row["mechanism"])
        for row in results
        if row.get("outcome") in OBSERVED_OUTCOMES
    }
    type_observed = "type_isolation" in observed
    other_observed = bool(observed - {"type_isolation"})
    if type_observed and other_observed:
        return "multiple_allocator_mechanisms"
    if type_observed:
        return "type_isolation_only"
    if other_observed:
        return "other_allocator_feature_only"
    if results and all(row.get("outcome") == "no_signal" for row in results):
        return "no_allocator_signal_observed"
    return "not_yet_attributed"


def build_scope(
    *,
    inventory: dict[str, Any],
    review: dict[str, Any],
    corpus: dict[str, Any],
    catalogs: Sequence[dict[str, Any]],
    statuses: Sequence[dict[str, Any]] = (),
    mechanism_results: dict[str, Any] | None = None,
    primitive_overrides: Sequence[dict[str, Any]] = (),
    mechanism_amendments: Sequence[dict[str, Any]] = (),
) -> dict[str, Any]:
    inventory_by_id = {
        row["advisory_id"]: row
        for row in inventory.get("candidates", [])
        if isinstance(row, dict) and isinstance(row.get("advisory_id"), str)
    }
    for row in inventory.get("rudra_pocs", []):
        if not isinstance(row, dict) or not isinstance(row.get("rustsec_id"), str):
            continue
        inventory_by_id.setdefault(
            row["rustsec_id"],
            {
                "advisory_id": row["rustsec_id"],
                "package": row.get("target_crate"),
                "title": "Rudra panic-safety supplemental source evidence",
                "classification_signals": ["double_free"],
                "source_path": row.get("source_path"),
                "source_sha256": row.get("source_sha256"),
            },
        )
    corpus_by_id = {
        row["advisory_id"]: row
        for row in corpus.get("cases", [])
        if isinstance(row, dict) and isinstance(row.get("advisory_id"), str)
    }
    corpus_by_case = {
        row["case_id"]: row
        for row in corpus_by_id.values()
        if isinstance(row.get("case_id"), str)
    }
    catalog_by_id = _catalog_index(catalogs, corpus_by_case)
    status_by_id = _status_index(statuses)
    results_by_id = _result_index(mechanism_results)
    strong_ids = strong_candidate_ids(review)
    unknown_result_advisories = sorted(set(results_by_id) - set(strong_ids))
    if unknown_result_advisories:
        raise ScopeError(
            "mechanism results reference non-scope advisories: "
            + ", ".join(unknown_result_advisories)
        )
    primitive_override_by_id = _primitive_override_index(
        primitive_overrides, set(strong_ids)
    )
    mechanism_amendment_by_key = _mechanism_amendment_index(
        mechanism_amendments, set(strong_ids)
    )
    used_mechanism_amendments: set[tuple[str, str, str, str]] = set()
    cases: list[dict[str, Any]] = []
    for advisory_id in strong_ids:
        inventory_entry = inventory_by_id.get(advisory_id)
        if inventory_entry is None:
            raise ScopeError(f"strong candidate missing from inventory: {advisory_id}")
        corpus_case = corpus_by_id.get(advisory_id)
        catalog_entry = catalog_by_id.get(advisory_id)
        status_entry = status_by_id.get(advisory_id)
        case_id = (
            (catalog_entry or {}).get("case_id")
            or (status_entry or {}).get("case_id")
            or (corpus_case or {}).get("case_id")
        )
        base_primitive = primary_primitive(inventory_entry, corpus_case)
        primitive_override = primitive_override_by_id.get(advisory_id)
        if primitive_override is not None:
            if (
                primitive_override["case_id"] is not None
                and case_id is not None
                and primitive_override["case_id"] != case_id
            ):
                raise ScopeError(
                    f"primitive override case_id mismatch for {advisory_id}: "
                    f"{primitive_override['case_id']!r} != {case_id!r}"
                )
            if primitive_override["replaces"] != base_primitive:
                raise ScopeError(
                    f"stale primitive override for {advisory_id}: expected to replace "
                    f"{base_primitive!r}, found {primitive_override['replaces']!r}"
                )
            selected_primitive = primitive_override["primary_primitive"]
            primitive_review: dict[str, Any] = {
                "source": "reviewed_override",
                "replaces": primitive_override["replaces"],
                "reason": primitive_override["reason"],
                "evidence_paths": primitive_override["evidence_paths"],
            }
        else:
            selected_primitive = base_primitive
            primitive_review = {"source": "corpus_or_inventory"}
        if catalog_entry is not None and status_entry is not None:
            if (
                status_entry.get("case_id") is not None
                and catalog_entry.get("case_id") != status_entry.get("case_id")
            ):
                raise ScopeError(
                    f"catalog/status case_id conflict for {advisory_id}: "
                    f"{catalog_entry.get('case_id')!r} != "
                    f"{status_entry.get('case_id')!r}"
                )
            if (
                status_entry["integration_status"] == "blocked"
                and catalog_entry["scenario_ids"]
            ):
                raise ScopeError(
                    f"blocked status conflicts with executable catalog scenarios: "
                    f"{advisory_id}"
                )
        if status_entry is not None:
            integration = {
                "status": status_entry["integration_status"],
                "blocker_reason": status_entry.get("blocker_reason"),
                "evidence": list(status_entry.get("evidence", [])),
                "scenario_ids": (
                    list(catalog_entry["scenario_ids"])
                    if catalog_entry is not None
                    and status_entry["integration_status"] == "executable"
                    else list(status_entry.get("scenario_ids", []))
                ),
            }
        elif catalog_entry is not None:
            integration = {
                "status": "executable",
                "blocker_reason": None,
                "evidence": [],
                "scenario_ids": catalog_entry["scenario_ids"],
            }
        elif corpus_case is not None:
            integration = {
                "status": "source_evidence_only",
                "blocker_reason": "catalog source evidence has no executable repository harness",
                "evidence": [],
                "scenario_ids": [],
            }
        else:
            integration = {
                "status": "pending_integration",
                "blocker_reason": None,
                "evidence": [],
                "scenario_ids": [],
            }
        registered_mechanisms = candidate_mechanisms(advisory_id, review)
        validated_results: list[dict[str, Any]] = []
        seen_result_keys: set[tuple[str, str, str | None]] = set()
        for raw_result in results_by_id.get(advisory_id, []):
            result = dict(raw_result)
            if result["case_id"] != case_id:
                raise ScopeError(
                    f"mechanism result case_id mismatch for {advisory_id}: "
                    f"{result['case_id']!r} != {case_id!r}"
                )
            scenario_id = result["scenario_id"]
            if scenario_id not in integration["scenario_ids"]:
                raise ScopeError(
                    f"mechanism result scenario is outside the integrated case for "
                    f"{advisory_id}: {scenario_id}"
                )
            result_key = (result["case_id"], result["mechanism"], scenario_id)
            if result_key in seen_result_keys:
                raise ScopeError(
                    f"duplicate mechanism result for {advisory_id}: {result_key}"
                )
            seen_result_keys.add(result_key)
            if integration["status"] != "executable":
                raise ScopeError(
                    f"mechanism result attached to non-executable case: {advisory_id}"
                )
            mechanism = result["mechanism"]
            if mechanism in registered_mechanisms:
                result["evaluation_registration"] = {
                    "source": "preregistered_candidate_mechanism"
                }
            else:
                amendment_key = (
                    advisory_id,
                    str(case_id),
                    str(scenario_id),
                    mechanism,
                )
                amendment = mechanism_amendment_by_key.get(amendment_key)
                if amendment is None:
                    raise ScopeError(
                        f"mechanism result lacks candidate registration or reviewed "
                        f"amendment: {amendment_key}"
                    )
                if result["outcome"] not in amendment["allowed_outcomes"]:
                    raise ScopeError(
                        f"mechanism amendment outcome mismatch for {advisory_id}: "
                        f"{result['outcome']!r}"
                    )
                used_mechanism_amendments.add(amendment_key)
                result["evaluation_registration"] = {
                    "source": "reviewed_post_integration_amendment",
                    "reason": amendment["reason"],
                    "evidence_paths": amendment["evidence_paths"],
                }
            validated_results.append(result)
        results = sorted(
            validated_results,
            key=lambda row: (
                MECHANISM_ORDER.index(row["mechanism"])
                if row["mechanism"] in MECHANISM_ORDER
                else len(MECHANISM_ORDER),
                str(row.get("scenario_id", "")),
            ),
        )
        cases.append(
            {
                "case_id": case_id,
                "advisory_id": advisory_id,
                "package": inventory_entry.get("package"),
                "title": inventory_entry.get("title"),
                "primary_primitive": selected_primitive,
                "primary_primitive_review": primitive_review,
                "classification_signals": list(inventory_entry.get("classification_signals", [])),
                "assessment_profile": (corpus_case or {}).get("assessment_profile"),
                "candidate_mechanisms": registered_mechanisms,
                "integration": integration,
                "mechanism_results": results,
                "final_attribution": _final_attribution(results),
                "source": {
                    "path": inventory_entry.get("source_path"),
                    "sha256": inventory_entry.get("source_sha256"),
                },
                "claim_grade": False,
            }
        )

    unused_amendments = sorted(
        set(mechanism_amendment_by_key) - used_mechanism_amendments
    )
    if unused_amendments:
        raise ScopeError(f"unused mechanism amendments: {unused_amendments}")

    reviewed_cases = cases
    excluded_cases: list[dict[str, Any]] = []
    for row in reviewed_cases:
        if row["integration"]["status"] != "blocked":
            continue
        reason = row["integration"].get("blocker_reason")
        evidence = row["integration"].get("evidence")
        if (
            not isinstance(reason, str)
            or not reason.strip()
            or not isinstance(evidence, list)
            or not evidence
        ):
            raise ScopeError(
                "blocked scope exclusion requires a reason and evidence: "
                f"{row['advisory_id']}"
            )
        excluded_cases.append(
            {
                **row,
                "scope_exclusion": {
                    "code": "no_executable_reproduction",
                    "reason": reason,
                    "evidence": list(evidence),
                },
            }
        )
    cases = [
        row
        for row in reviewed_cases
        if row["integration"]["status"] != "blocked"
    ]

    primitive_counts = collections.Counter(row["primary_primitive"] for row in cases)
    integration_counts = collections.Counter(row["integration"]["status"] for row in cases)
    excluded_integration_counts = collections.Counter(
        row["integration"]["status"] for row in excluded_cases
    )
    attribution_counts = collections.Counter(row["final_attribution"] for row in cases)
    type_observed = sum(
        any(
            result["mechanism"] == "type_isolation"
            and result["outcome"] in OBSERVED_OUTCOMES
            for result in row["mechanism_results"]
        )
        for row in cases
    )
    other_observed = sum(
        any(
            result["mechanism"] != "type_isolation"
            and result["outcome"] in OBSERVED_OUTCOMES
            for result in row["mechanism_results"]
        )
        for row in cases
    )
    overlap = sum(
        row["final_attribution"] == "multiple_allocator_mechanisms" for row in cases
    )
    return {
        "schema_version": 1,
        "source": "unialloc-rustsec-complete-security-scope",
        "claim_grade": False,
        "claim_boundary": (
            "The evaluable ledger contains only cases with an executable reproduction. "
            "Strong candidates that remain blocked after bounded integration attempts "
            "are retained in excluded_cases with their reasons and evidence; they are "
            "outside every efficacy denominator and are never counted as no-signal "
            "results. Detection and mitigation counts require explicit per-scenario "
            "mechanism results."
        ),
        "rustsec_commit": review.get("rustsec_commit"),
        "counts": {
            "strong_candidate_count": len(cases),
            "reviewed_strong_candidate_count": len(reviewed_cases),
            "excluded_non_evaluable_count": len(excluded_cases),
            "integration_status": dict(sorted(integration_counts.items())),
            "excluded_integration_status": dict(
                sorted(excluded_integration_counts.items())
            ),
            "primary_primitive": dict(sorted(primitive_counts.items())),
            "final_attribution": dict(sorted(attribution_counts.items())),
            "type_isolation_observed_count": type_observed,
            "other_feature_observed_count": other_observed,
            "mechanism_overlap_observed_count": overlap,
            "conditional_candidate_count": len(review.get("conditional_candidates", [])),
            "excluded_review_unit_count": sum(
                len(rows) for rows in review.get("excluded", {}).values()
            ),
        },
        "cases": cases,
        "excluded_cases": excluded_cases,
    }


def require_terminal_integration(cases: Sequence[dict[str, Any]]) -> None:
    pending = [
        str(row.get("advisory_id"))
        for row in cases
        if row.get("integration", {}).get("status") not in TERMINAL_INTEGRATION
    ]
    if pending:
        raise ScopeError("non-terminal integration remains: " + ", ".join(pending))


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--inventory", type=pathlib.Path, default=DEFAULT_INVENTORY)
    parser.add_argument("--review", type=pathlib.Path, default=DEFAULT_REVIEW)
    parser.add_argument("--corpus", type=pathlib.Path, default=DEFAULT_CORPUS)
    parser.add_argument("--catalog", action="append", type=pathlib.Path)
    parser.add_argument("--status", action="append", type=pathlib.Path, default=[])
    parser.add_argument(
        "--primitive-overrides", action="append", type=pathlib.Path
    )
    parser.add_argument(
        "--mechanism-amendments", action="append", type=pathlib.Path
    )
    parser.add_argument("--mechanism-results", type=pathlib.Path)
    parser.add_argument("--output", type=pathlib.Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--require-terminal-integration", action="store_true")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    catalog_paths = tuple(args.catalog) if args.catalog else DEFAULT_CATALOGS
    primitive_override_paths = (
        tuple(args.primitive_overrides)
        if args.primitive_overrides
        else (DEFAULT_PRIMITIVE_OVERRIDES,)
    )
    mechanism_amendment_paths = (
        tuple(args.mechanism_amendments)
        if args.mechanism_amendments
        else (DEFAULT_MECHANISM_AMENDMENTS,)
    )
    statuses: list[dict[str, Any]] = []
    for path in args.status:
        value = load_json(path)
        statuses.extend(status_rows(value))
    result = build_scope(
        inventory=load_json(args.inventory),
        review=load_json(args.review),
        corpus=load_json(args.corpus),
        catalogs=[load_json(path) for path in catalog_paths],
        statuses=statuses,
        mechanism_results=(
            load_json(args.mechanism_results) if args.mechanism_results else None
        ),
        primitive_overrides=[load_json(path) for path in primitive_override_paths],
        mechanism_amendments=[load_json(path) for path in mechanism_amendment_paths],
    )
    if args.require_terminal_integration:
        require_terminal_integration(result["cases"])
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
