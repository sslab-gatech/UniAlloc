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
        "causal_compiler_bound_reuse_edge_mitigation",
        "exploit_enabling_cross_identity_reuse_edge",
    ),
}
NO_SIGNAL_CONTRACT = (
    "reclaim_checks",
    "no_signal",
    "matched_negative_without_exact_reclaim_diagnostic",
)
LEGACY_TYPEISO_MITIGATION_CONTRACT = (
    "type_isolation",
    "mitigated",
    "causal_mitigation_true_positive",
    "exploit_enabling_cross_identity_reuse_edge",
)
LEGACY_NORMALIZATION_BOUNDARY = (
    "Legacy mixed inputs contribute detector results and non-positive Type "
    "Isolation source-witness evidence only. Their superseded Type Isolation "
    "mitigation rows receive no credit; current automatic evidence supplies all "
    "validated Type Isolation mitigations."
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


def validate_mechanism_result_contract(row: dict[str, Any]) -> None:
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
    mechanism = row.get("mechanism")
    if mechanism == "type_isolation":
        if "true_positive" in row:
            raise MergeError(
                "Type Isolation result must use validated_mitigation exclusively"
            )
        if row.get("validated_mitigation") is not expected_positive:
            raise MergeError(
                "validated_mitigation must be "
                f"{expected_positive} for outcome {outcome!r}"
            )
    else:
        if "validated_mitigation" in row:
            raise MergeError(
                "detector result must use true_positive exclusively"
            )
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
            raise MergeError("validated positive result requires positive_scope")
        contract = (mechanism, outcome, semantics, positive_scope)
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
        if mechanism not in {
            "reclaim_checks",
            "recovery_layout_validation",
            "type_isolation",
        }:
            raise MergeError("inconclusive result uses an unsupported mechanism")

    if mechanism == "type_isolation" and expected_positive:
        compiler_automatic = row.get("compiler_automatic_victim_coverage")
        source_validated = row.get("source_vulnerability_detection_validated")
        if not isinstance(compiler_automatic, bool):
            raise MergeError(
                "Type Isolation positive requires explicit compiler coverage"
            )
        if source_validated is not False:
            raise MergeError(
                "derived Type Isolation positive requires an explicit source boundary"
            )
        if row.get("vulnerability_specific_detection_signal") not in (None, False):
            raise MergeError(
                "derived Type Isolation mitigation cannot claim vulnerability-specific detection"
            )
        if row.get("full_source_vulnerability_detection") not in (None, False):
            raise MergeError(
                "derived Type Isolation mitigation cannot claim full-source detection"
            )
        automatic_probe = row.get("automatic_edge_identity_probe", False)
        if not isinstance(automatic_probe, bool):
            raise MergeError(
                "Type Isolation automatic probe marker must be boolean"
            )
        manual_annotation = row.get("manual_victim_identity_annotation")
        if automatic_probe:
            if compiler_automatic is not True or manual_annotation is not False:
                raise MergeError(
                    "automatic Type Isolation provenance is internally inconsistent"
                )
        elif compiler_automatic is not False or manual_annotation not in (None, True):
            raise MergeError(
                "manual Type Isolation provenance is internally inconsistent"
            )
        if "source_level_true_positive" in row:
            raise MergeError(
                "derived Type Isolation result uses legacy true-positive terminology"
            )
        for field in ("automatic_source_coverage",):
            if field in row and row[field] is not False:
                raise MergeError(
                    f"derived Type Isolation positive requires {field}=false"
                )
        synthetic = row.get("synthetic_reduction") is True
        expected_fidelity = (
            "compiler_automatic_synthetic_reduction"
            if automatic_probe and synthetic
            else "compiler_automatic_derived_reduction"
            if automatic_probe
            else "synthetic_manual_reduction"
            if synthetic
            else "manual_derived_reduction"
        )
        fidelity = row.get("reduction_fidelity")
        if fidelity is not None and fidelity != expected_fidelity:
            raise MergeError(
                "Type Isolation reduction fidelity contradicts identity provenance"
            )


def normalize_legacy_payload(
    payload: dict[str, Any],
    *,
    superseding_typeiso_keys: frozenset[tuple[str, str, str]],
) -> dict[str, Any]:
    """Remove superseded mitigations and neutralize retained source evidence."""

    normalized_results: list[dict[str, Any]] = []
    superseded_count = 0
    normalized_inconclusive_count = 0
    for raw_row in payload["results"]:
        if not isinstance(raw_row, dict):
            raise MergeError("legacy mechanism result must be an object")
        row = dict(raw_row)
        if row.get("mechanism") != "type_isolation":
            normalized_results.append(row)
            continue

        outcome = row.get("outcome")
        if outcome == "mitigated":
            contract = (
                row.get("mechanism"),
                outcome,
                row.get("result_semantics"),
                row.get("positive_scope"),
            )
            key = result_key(row)
            if (
                contract != LEGACY_TYPEISO_MITIGATION_CONTRACT
                or row.get("true_positive") is not True
                or "validated_mitigation" in row
            ):
                raise MergeError(
                    f"unsupported legacy Type Isolation mitigation row: {key}"
                )
            if key not in superseding_typeiso_keys:
                raise MergeError(
                    f"legacy Type Isolation mitigation lacks current replacement: {key}"
                )
            superseded_count += 1
            continue

        key = result_key(row)
        if (
            outcome != "inconclusive"
            or row.get("true_positive") is not False
            or "validated_mitigation" in row
            or row.get("result_semantics") != "evidence_gap"
            or row.get("positive_scope") is not None
        ):
            raise MergeError(
                f"unsupported legacy Type Isolation source result: {key}"
            )
        row.pop("true_positive")
        row.pop("source_level_true_positive", None)
        row.update(
            {
                "validated_mitigation": False,
                "source_vulnerability_detection_validated": False,
                "vulnerability_specific_detection_signal": False,
                "full_source_vulnerability_detection": False,
                "schema_normalization": (
                    "legacy_nonpositive_typeiso_to_validated_mitigation"
                ),
            }
        )
        normalized_results.append(row)
        normalized_inconclusive_count += 1

    return {
        **payload,
        "boundary": LEGACY_NORMALIZATION_BOUNDARY,
        "legacy_normalization": {
            "normalized_typeiso_inconclusive_count": (
                normalized_inconclusive_count
            ),
            "superseded_typeiso_mitigation_count": superseded_count,
        },
        "results": normalized_results,
    }


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
            validate_mechanism_result_contract(row)
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
    parser.add_argument(
        "--legacy-input",
        action="append",
        type=pathlib.Path,
        default=[],
        help=(
            "legacy mixed ledger fragment; detector rows are retained, source "
            "inconclusives are neutralized, and mitigations require a strict "
            "replacement from --input"
        ),
    )
    parser.add_argument(
        "--normalized-legacy-output",
        type=pathlib.Path,
        help=(
            "required with --legacy-input; writes the strict detector/source "
            "fragment that becomes the terminal ledger input"
        ),
    )
    parser.add_argument("--output", type=pathlib.Path)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        if bool(args.legacy_input) != bool(args.normalized_legacy_output):
            raise MergeError(
                "--legacy-input and --normalized-legacy-output must be used together"
            )
        strict_inputs = []
        for path in args.input:
            resolved = path.expanduser().resolve()
            strict_inputs.append((load(resolved), artifact(resolved)))
        strict_preview = merge(strict_inputs)
        superseding_typeiso_keys = frozenset(
            result_key(row)
            for row in strict_preview["results"]
            if row.get("mechanism") == "type_isolation"
            and row.get("outcome") == "mitigated"
            and row.get("validated_mitigation") is True
        )
        legacy_inputs = []
        for path in args.legacy_input:
            resolved = path.expanduser().resolve()
            legacy_inputs.append(
                (
                    normalize_legacy_payload(
                        load(resolved),
                        superseding_typeiso_keys=superseding_typeiso_keys,
                    ),
                    artifact(resolved),
                )
            )
        if legacy_inputs:
            normalized_legacy = merge(legacy_inputs)
            normalized_path = args.normalized_legacy_output.expanduser().resolve()
            normalized_path.parent.mkdir(parents=True, exist_ok=True)
            normalized_path.write_text(
                json.dumps(normalized_legacy, indent=2, sort_keys=True) + "\n",
                encoding="utf-8",
            )
            inputs = [
                (normalized_legacy, artifact(normalized_path)),
                *strict_inputs,
            ]
        else:
            inputs = strict_inputs
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
