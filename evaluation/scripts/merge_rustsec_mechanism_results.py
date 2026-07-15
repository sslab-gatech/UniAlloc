#!/usr/bin/env python3
"""Merge strict RustSec mechanism-result ledgers without dropping duplicates."""

from __future__ import annotations

import argparse
import collections
import hashlib
import json
import pathlib
import re
import sys
from typing import Any, Iterable


ROOT = pathlib.Path(__file__).resolve().parents[2]


class MergeError(RuntimeError):
    """Raised when mechanism results conflict or are malformed."""


POSITIVE_OUTCOMES = frozenset({"detected", "mitigated"})
VALID_OUTCOMES = frozenset({*POSITIVE_OUTCOMES, "no_signal", "inconclusive"})
CASE_RE = re.compile(r"RSH-\d{3}")
ADVISORY_RE = re.compile(r"RUSTSEC-(?:\d{4}|TEST)-\d{4}")
POSITIVE_CONTRACTS = {
    (
        "reclaim_checks",
        "detected",
        "exact_diagnostic_true_positive",
        "duplicate_reclaim_event_in_integrated_witness",
    ),
    (
        "recovery_layout_validation",
        "detected",
        "exact_diagnostic_true_positive",
        "allocation_deallocation_layout_mismatch_edge",
    ),
    (
        "type_isolation",
        "mitigated",
        "causal_mitigation_true_positive",
        "exploit_enabling_cross_identity_reuse_edge",
    ),
}
NO_SIGNAL_CONTRACT = (
    "reclaim_checks",
    "no_signal",
    "matched_negative_without_exact_reclaim_diagnostic",
)


def load(path: pathlib.Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise MergeError(f"cannot read {path}: {error}") from error
    if not isinstance(value, dict) or not isinstance(value.get("results"), list):
        raise MergeError(f"mechanism result input is malformed: {path}")
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


def result_key(row: dict[str, Any]) -> tuple[str, str, str]:
    values = (
        row.get("advisory_id"),
        row.get("mechanism"),
        row.get("scenario_id", row.get("case_id")),
    )
    if not all(isinstance(value, str) and value for value in values):
        raise MergeError("result requires advisory, mechanism, and scenario/case ids")
    return values  # type: ignore[return-value]


def validate_true_positive_contract(row: dict[str, Any]) -> None:
    advisory_id = row.get("advisory_id")
    case_id = row.get("case_id")
    scenario_id = row.get("scenario_id")
    if not isinstance(advisory_id, str) or not ADVISORY_RE.fullmatch(advisory_id):
        raise MergeError(f"invalid advisory_id: {advisory_id!r}")
    if not isinstance(case_id, str) or not CASE_RE.fullmatch(case_id):
        raise MergeError(f"invalid case_id: {case_id!r}")
    if (
        not isinstance(scenario_id, str)
        or not scenario_id.startswith(f"{case_id}-")
    ):
        raise MergeError(
            f"scenario_id must be explicitly bound to {case_id}: {scenario_id!r}"
        )
    outcome = row.get("outcome")
    if outcome not in VALID_OUTCOMES:
        raise MergeError(f"invalid mechanism outcome: {outcome!r}")
    expected_positive = outcome in POSITIVE_OUTCOMES
    if row.get("true_positive") is not expected_positive:
        raise MergeError(
            f"true_positive must be {expected_positive} for outcome {outcome!r}"
        )
    semantics = row.get("result_semantics")
    if not isinstance(semantics, str) or not semantics:
        raise MergeError("result_semantics must be a non-empty string")
    positive_scope = row.get("positive_scope")
    if expected_positive:
        if not isinstance(positive_scope, str) or not positive_scope:
            raise MergeError("positive true_positive result requires positive_scope")
        contract = (row.get("mechanism"), outcome, semantics, positive_scope)
        if contract not in POSITIVE_CONTRACTS:
            raise MergeError(f"unsupported positive result contract: {contract!r}")
        if row.get("negative_boundary") is not None:
            raise MergeError("positive result must not define negative_boundary")
    elif positive_scope is not None:
        raise MergeError("non-positive result must not define positive_scope")
    elif outcome == "no_signal":
        contract = (row.get("mechanism"), outcome, semantics)
        if contract != NO_SIGNAL_CONTRACT:
            raise MergeError(f"unsupported no-signal result contract: {contract!r}")
        negative_boundary = row.get("negative_boundary")
        if not isinstance(negative_boundary, str) or not negative_boundary:
            raise MergeError("no-signal result requires negative_boundary")
    elif outcome == "inconclusive":
        if semantics != "evidence_gap":
            raise MergeError("inconclusive result requires evidence_gap semantics")
        if row.get("mechanism") not in {
            "reclaim_checks",
            "recovery_layout_validation",
            "type_isolation",
        }:
            raise MergeError("inconclusive result uses an unsupported mechanism")


def merge(inputs: Iterable[tuple[dict[str, Any], dict[str, Any]]]) -> dict[str, Any]:
    indexed: dict[tuple[str, str, str], dict[str, Any]] = {}
    artifacts: list[dict[str, Any]] = []
    boundaries: list[str] = []
    for payload, input_artifact in inputs:
        artifacts.append(input_artifact)
        boundary = payload.get("boundary")
        if isinstance(boundary, str) and boundary not in boundaries:
            boundaries.append(boundary)
        for row in payload["results"]:
            if not isinstance(row, dict):
                raise MergeError("mechanism result must be an object")
            validate_true_positive_contract(row)
            key = result_key(row)
            previous = indexed.get(key)
            if previous is not None and previous != row:
                raise MergeError(f"conflicting duplicate mechanism result: {key}")
            indexed[key] = row
    results = [indexed[key] for key in sorted(indexed)]
    counts = collections.Counter(str(row.get("outcome")) for row in results)
    return {
        "schema_version": 1,
        "source": "unialloc-rustsec-merged-mechanism-results",
        "claim_grade": False,
        "boundary": " ".join(boundaries),
        "inputs": artifacts,
        "counts": dict(sorted(counts.items())),
        "results": results,
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", action="append", type=pathlib.Path, required=True)
    parser.add_argument("--output", type=pathlib.Path)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        inputs = []
        for path in args.input:
            resolved = path.expanduser().resolve()
            inputs.append((load(resolved), artifact(resolved)))
        result = merge(inputs)
        encoded = json.dumps(result, indent=2, sort_keys=True) + "\n"
        if args.output is not None:
            args.output.parent.mkdir(parents=True, exist_ok=True)
            args.output.write_text(encoded, encoding="utf-8")
        print(encoded, end="")
        return 0
    except (OSError, MergeError) as error:
        print(f"error: {error}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
