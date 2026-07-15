#!/usr/bin/env python3
"""Render a dependency-free SVG/CSV figure from the complete RustSec ledger."""

from __future__ import annotations

import argparse
import collections
import csv
import html
import json
import pathlib
from typing import Any, Sequence


ROOT = pathlib.Path(__file__).resolve().parents[2]
DEFAULT_SCOPE = ROOT / "evaluation/config/rustsec_heap_complete_scope.json"
DEFAULT_OUTPUT = ROOT / "docs/figures/rustsec-security-scope-20260714"
MECHANISM_COVERAGE_LABELS = (
    ("type_isolation_edge_covered", "Type Isolation edge covered", "#2A6FDB"),
    (
        "reclaim_checks_exact_detection",
        "reclaim_checks exact detection",
        "#E28E2C",
    ),
    (
        "recovery_layout_validation_exact_detection",
        "Recovery-layout validation",
        "#8E5EA2",
    ),
)
CASE_ATTRIBUTION_LABELS = (
    ("other_allocator_feature_only", "Other allocator feature only", "#E28E2C"),
    ("type_isolation_only", "Type Isolation only", "#2A6FDB"),
    (
        "multiple_allocator_mechanisms",
        "Type Isolation + other allocator feature",
        "#7A5195",
    ),
    (
        "no_allocator_signal_observed",
        "No exact allocator signal observed",
        "#C84A4A",
    ),
    ("not_yet_attributed", "Unresolved mechanism attribution", "#E2B84B"),
)
INTEGRATION_LABELS = (
    ("executable", "Executable", "#3A9D5D"),
    ("source_evidence_only", "Source evidence only", "#E2B84B"),
    ("pending_integration", "Pending integration", "#9AA0A6"),
    ("excluded_before_evaluation", "Excluded before evaluation", "#6B7280"),
)
VALID_INTEGRATION_STATUSES = frozenset(
    ("executable", "source_evidence_only", "pending_integration")
)
VALID_MECHANISM_OUTCOMES = frozenset(
    ("detected", "mitigated", "no_signal", "inconclusive")
)
OBSERVED_MECHANISM_OUTCOMES = frozenset(("detected", "mitigated"))
TYPE_ISOLATION_POSITIVE_SCOPE = "exploit_enabling_cross_identity_reuse_edge"
TYPE_ISOLATION_AUTOMATIC_SOURCE_FIELDS = (
    "automatic_source_coverage",
    "compiler_automatic_victim_coverage",
)
TYPE_ISOLATION_SOURCE_TRUE_POSITIVE_FIELDS = (
    "source_level_true_positive",
    "source_vulnerability_detection_validated",
)


def _case_identifier(row: dict[str, Any], index: int) -> str:
    # The figure's machine-readable lists are named ``*_case_ids``. Prefer the
    # stable RSH case identifier and retain advisory_id only as a compatibility
    # fallback for small synthetic ledgers and older scope snapshots.
    for key in ("case_id", "advisory_id"):
        value = row.get(key)
        if isinstance(value, str) and value:
            return value
    return f"case[{index}]"


def _mechanism_results(row: dict[str, Any], identifier: str) -> list[dict[str, Any]]:
    raw_results = row.get("mechanism_results", [])
    if not isinstance(raw_results, list):
        raise ValueError(f"mechanism_results must be a list for {identifier}")
    results: list[dict[str, Any]] = []
    for result in raw_results:
        if not isinstance(result, dict):
            raise ValueError(f"mechanism result must be an object for {identifier}")
        mechanism = result.get("mechanism")
        outcome = result.get("outcome")
        if not isinstance(mechanism, str) or not mechanism:
            raise ValueError(f"mechanism result requires a mechanism for {identifier}")
        if outcome not in VALID_MECHANISM_OUTCOMES:
            raise ValueError(
                f"unrecognized mechanism outcome for {identifier}: {outcome!r}"
            )
        results.append(result)
    return results


def _type_isolation_edge_covered(
    result: dict[str, Any], identifier: str
) -> bool:
    """Validate and classify one observed Type Isolation result.

    Type Isolation receives figure credit only for the preregistered manual
    derived-edge contract.  Raising on an observed result with weaker metadata
    prevents a future ``detected``/``mitigated`` label from silently inflating
    the figure.
    """

    if result.get("mechanism") != "type_isolation":
        return False
    if result.get("outcome") not in OBSERVED_MECHANISM_OUTCOMES:
        return False

    violations: list[str] = []
    if result.get("outcome") != "mitigated":
        violations.append("outcome=mitigated")
    if result.get("true_positive") is not True:
        violations.append("true_positive=true")
    if result.get("positive_scope") != TYPE_ISOLATION_POSITIVE_SCOPE:
        violations.append(f"positive_scope={TYPE_ISOLATION_POSITIVE_SCOPE}")

    automatic_source_values = [
        result[field]
        for field in TYPE_ISOLATION_AUTOMATIC_SOURCE_FIELDS
        if field in result
    ]
    if not automatic_source_values or any(
        value is not False for value in automatic_source_values
    ):
        violations.append("automatic_source_coverage=false")

    source_true_positive_values = [
        result[field]
        for field in TYPE_ISOLATION_SOURCE_TRUE_POSITIVE_FIELDS
        if field in result
    ]
    if not source_true_positive_values or any(
        value is not False for value in source_true_positive_values
    ):
        violations.append("source_level_true_positive=false")

    if violations:
        raise ValueError(
            "observed Type Isolation result violates the manual derived-edge "
            f"claim contract for {identifier}: {', '.join(violations)}"
        )
    return True


def figure_summary(ledger: dict[str, Any]) -> dict[str, Any]:
    cases = ledger.get("cases", [])
    if not isinstance(cases, list):
        raise ValueError("scope cases must be a list")
    excluded_cases = ledger.get("excluded_cases", [])
    if not isinstance(excluded_cases, list):
        raise ValueError("excluded_cases must be a list")
    primitive: collections.Counter[str] = collections.Counter()
    integration: collections.Counter[str] = collections.Counter()
    coverage_case_ids = {
        key: [] for key, _label, _color in MECHANISM_COVERAGE_LABELS
    }
    attribution_case_ids = {
        key: [] for key, _label, _color in CASE_ATTRIBUTION_LABELS
    }
    synthetic_typeiso_case_ids: list[str] = []
    unresolved_integration_case_ids: list[str] = []

    for index, raw_row in enumerate(cases):
        if not isinstance(raw_row, dict):
            raise ValueError(f"scope case at index {index} must be an object")
        identifier = _case_identifier(raw_row, index)
        primary_primitive = raw_row.get("primary_primitive")
        if not isinstance(primary_primitive, str) or not primary_primitive:
            raise ValueError(f"primary_primitive must be a string for {identifier}")
        primitive[primary_primitive] += 1

        integration_record = raw_row.get("integration", {})
        if not isinstance(integration_record, dict):
            raise ValueError(f"integration must be an object for {identifier}")
        integration_status = integration_record.get("status")
        if integration_status not in VALID_INTEGRATION_STATUSES:
            raise ValueError(
                f"unrecognized integration status for {identifier}: "
                f"{integration_status!r}"
            )
        integration[str(integration_status)] += 1
        results = _mechanism_results(raw_row, identifier)

        observed = [
            result
            for result in results
            if result["outcome"] in OBSERVED_MECHANISM_OUTCOMES
        ]
        unsupported_observed = sorted(
            {
                str(result["mechanism"])
                for result in observed
                if result["mechanism"]
                not in {
                    "type_isolation",
                    "reclaim_checks",
                    "recovery_layout_validation",
                }
            }
        )
        if unsupported_observed:
            raise ValueError(
                "observed allocator mechanisms require separate plot categories for "
                f"{identifier}: {unsupported_observed}"
            )
        if any(
            result["mechanism"] == "reclaim_checks"
            and result["outcome"] == "mitigated"
            for result in results
        ):
            raise ValueError(
                "reclaim_checks mitigation cannot be labeled as exact detection for "
                f"{identifier}"
            )

        # Evaluate every Type Isolation result before the integration-status
        # branch.  A list avoids ``any`` short-circuiting past a second,
        # malformed observed result after one valid positive.
        covered_typeiso_results = [
            result
            for result in results
            if result["mechanism"] == "type_isolation"
            and _type_isolation_edge_covered(result, identifier)
        ]
        type_edge_covered = bool(covered_typeiso_results)

        if integration_status != "executable":
            unresolved_integration_case_ids.append(identifier)
            continue

        reclaim_detected = any(
            result["mechanism"] == "reclaim_checks"
            and result["outcome"] == "detected"
            for result in results
        )
        layout_validation_detected = any(
            result["mechanism"] == "recovery_layout_validation"
            and result["outcome"] == "detected"
            for result in results
        )
        other_feature_detected = reclaim_detected or layout_validation_detected
        if type_edge_covered:
            coverage_case_ids["type_isolation_edge_covered"].append(identifier)
            if any(
                result.get("synthetic_reduction") is True
                for result in covered_typeiso_results
            ):
                synthetic_typeiso_case_ids.append(identifier)
        if reclaim_detected:
            coverage_case_ids["reclaim_checks_exact_detection"].append(identifier)
        if layout_validation_detected:
            coverage_case_ids[
                "recovery_layout_validation_exact_detection"
            ].append(identifier)

        if type_edge_covered and other_feature_detected:
            attribution_case_ids["multiple_allocator_mechanisms"].append(identifier)
        elif type_edge_covered:
            attribution_case_ids["type_isolation_only"].append(identifier)
        elif other_feature_detected:
            attribution_case_ids["other_allocator_feature_only"].append(
                identifier
            )
        elif results and all(result["outcome"] == "no_signal" for result in results):
            attribution_case_ids["no_allocator_signal_observed"].append(identifier)
        else:
            # Empty result sets, inconclusive results, and mixed
            # no-signal/inconclusive result sets retain an explicit evidence gap.
            attribution_case_ids["not_yet_attributed"].append(identifier)

    excluded_case_ids: list[str] = []
    for index, raw_row in enumerate(excluded_cases):
        if not isinstance(raw_row, dict):
            raise ValueError(f"excluded case at index {index} must be an object")
        identifier = _case_identifier(raw_row, index)
        integration_record = raw_row.get("integration", {})
        exclusion = raw_row.get("scope_exclusion", {})
        if not isinstance(integration_record, dict) or not isinstance(exclusion, dict):
            raise ValueError(f"excluded case metadata must be objects for {identifier}")
        if integration_record.get("status") != "blocked":
            raise ValueError(f"excluded case must retain blocked status for {identifier}")
        reason = exclusion.get("reason")
        evidence = exclusion.get("evidence")
        if not isinstance(reason, str) or not reason.strip():
            raise ValueError(f"excluded case lacks a reason for {identifier}")
        if not isinstance(evidence, list) or not evidence:
            raise ValueError(f"excluded case lacks evidence for {identifier}")
        excluded_case_ids.append(identifier)

    integration["excluded_before_evaluation"] = len(excluded_case_ids)

    coverage_counts = {
        key: len(coverage_case_ids[key])
        for key, _label, _color in MECHANISM_COVERAGE_LABELS
    }
    attribution_counts = {
        key: len(attribution_case_ids[key])
        for key, _label, _color in CASE_ATTRIBUTION_LABELS
    }
    accounted_case_ids = set().union(
        *(set(values) for values in attribution_case_ids.values())
    )
    return {
        "scope_count": len(cases),
        "reviewed_candidate_count": len(cases) + len(excluded_case_ids),
        "excluded_before_evaluation_count": len(excluded_case_ids),
        "excluded_before_evaluation_case_ids": excluded_case_ids,
        "rustsec_commit": ledger.get("rustsec_commit"),
        "primitive_counts": dict(
            sorted(primitive.items(), key=lambda item: (-item[1], item[0]))
        ),
        "integration_status": dict(sorted(integration.items())),
        "mechanism_coverage_counts": coverage_counts,
        "mechanism_coverage_case_ids": coverage_case_ids,
        "mechanism_categories_are_nonexclusive": True,
        "type_isolation_claim_boundary": {
            "manual_derived_reuse_edge_case_count": coverage_counts[
                "type_isolation_edge_covered"
            ],
            "synthetic_reduction_case_count": len(synthetic_typeiso_case_ids),
            "synthetic_reduction_case_ids": synthetic_typeiso_case_ids,
            "automatic_source_true_positive_case_count": 0,
            "compiler_automatic_victim_coverage": False,
            "source_vulnerability_detection_validated": False,
            "claim_grade": ledger.get("claim_grade") is True,
        },
        "case_attribution_counts": attribution_counts,
        "case_attribution_case_ids": attribution_case_ids,
        "case_attribution_categories_are_exclusive": True,
        "accounted_case_count": len(accounted_case_ids),
        "unresolved_integration_count": len(unresolved_integration_case_ids),
        "unresolved_integration_case_ids": unresolved_integration_case_ids,
    }


def _text(value: object) -> str:
    return html.escape(str(value), quote=True)


def _bar_rows(
    rows: list[str],
    *,
    values: list[tuple[str, int, str]],
    x: int,
    y: int,
    width: int,
    max_value: int,
    row_height: int = 31,
    label_width: int = 190,
) -> None:
    for index, (label, value, color) in enumerate(values):
        row_y = y + index * row_height
        bar_width = 0 if max_value == 0 else round(width * value / max_value)
        rows.append(
            f'<text x="{x}" y="{row_y + 17}" class="label">{_text(label)}</text>'
        )
        rows.append(
            f'<rect x="{x + label_width}" y="{row_y + 3}" width="{bar_width}" height="20" '
            f'rx="3" fill="{color}"/>'
        )
        rows.append(
            f'<text x="{x + label_width + 10 + bar_width}" '
            f'y="{row_y + 18}" class="count">{value}</text>'
        )


def render_svg(summary: dict[str, Any]) -> str:
    attribution_panel_y = max(430, 170 + 31 * len(summary["primitive_counts"]))
    coverage_panel_y = attribution_panel_y + 220
    note_y = coverage_panel_y + 165
    width, height = 1240, note_y + 55
    rows = [
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" '
        f'height="{height}" viewBox="0 0 {width} {height}">',
        "<style>",
        ".title{font-family:sans-serif;font-size:25px;font-weight:700;fill:#17202A}"
        ".subtitle{font-family:sans-serif;font-size:14px;fill:#566573}",
        ".panel{font-family:sans-serif;font-size:17px;font-weight:700;fill:#17202A}"
        ".label{font-family:sans-serif;font-size:13px;fill:#273746}",
        ".count{font-family:sans-serif;font-size:13px;font-weight:700;fill:#17202A}"
        ".note{font-family:sans-serif;font-size:12px;fill:#566573}",
        "</style>",
        '<rect width="100%" height="100%" fill="#FFFFFF"/>',
        '<text x="45" y="45" class="title">RUSTSEC scope: integration and '
        "allocator-mechanism coverage</text>",
        f'<text x="45" y="70" class="subtitle">Evaluable scope: '
        f'{summary["scope_count"]} of {summary["reviewed_candidate_count"]} reviewed '
        f'advisories · {summary["excluded_before_evaluation_count"]} excluded before '
        f'evaluation · RustSec '
        f'{_text(summary.get("rustsec_commit"))}</text>',
        '<text x="45" y="115" class="panel">A. Vulnerability primitives in the 49-case efficacy scope</text>',
    ]
    primitive_values = [
        (label.replace("_", " ").title(), value, "#4C78A8")
        for label, value in summary["primitive_counts"].items()
    ]
    _bar_rows(
        rows,
        values=primitive_values,
        x=45,
        y=135,
        width=320,
        max_value=max((value for _label, value, _color in primitive_values), default=1),
    )
    rows.append('<text x="650" y="115" class="panel">B. Integration readiness</text>')
    integration_values = [
        (label, int(summary["integration_status"].get(key, 0)), color)
        for key, label, color in INTEGRATION_LABELS
    ]
    _bar_rows(
        rows,
        values=integration_values,
        x=650,
        y=135,
        width=270,
        max_value=max(summary["scope_count"], 1),
    )
    rows.append(
        f'<text x="45" y="{coverage_panel_y}" class="panel">'
        "D. Nonexclusive mechanism-specific evidence</text>"
    )
    attribution_values = [
        (label, int(summary["case_attribution_counts"].get(key, 0)), color)
        for key, label, color in CASE_ATTRIBUTION_LABELS
    ]
    rows.append(
        f'<text x="45" y="{attribution_panel_y}" class="panel">'
        "C. Exclusive case attribution</text>"
    )
    _bar_rows(
        rows,
        values=attribution_values,
        x=45,
        y=attribution_panel_y + 20,
        width=560,
        max_value=max(summary["scope_count"], 1),
        row_height=35,
        label_width=390,
    )
    coverage_values = [
        (label, int(summary["mechanism_coverage_counts"].get(key, 0)), color)
        for key, label, color in MECHANISM_COVERAGE_LABELS
    ]
    _bar_rows(
        rows,
        values=coverage_values,
        x=45,
        y=coverage_panel_y + 20,
        width=560,
        max_value=max(summary["scope_count"], 1),
        row_height=35,
        label_width=390,
    )
    rows.extend(
        [
            f'<text x="45" y="{note_y}" class="note">Case-attribution bars '
            "partition the 49-case efficacy denominator. Mechanism bars are "
            "nonexclusive; RSH-002 contributes to both Type Isolation and "
            "reclaim_checks.</text>",
            f'<text x="45" y="{note_y + 20}" class="note">No-signal requires a '
            "completed matched trial; unrun and inconclusive integrated cases remain "
            "in their own row.</text>",
            f'<text x="650" y="{note_y + 20}" class="note">All Type Isolation '
            "positives are manual derived reuse edges, including "
            f'{summary["type_isolation_claim_boundary"]["synthetic_reduction_case_count"]} '
            "synthetic reductions; automatic source-level true positives: 0.</text>",
            f'<text x="650" y="{note_y + 40}" class="note">Excluded candidates remain '
            "in the audit ledger and outside the efficacy denominator.</text>",
            "</svg>",
        ]
    )
    return "\n".join(rows) + "\n"


def write_figure_bundle(ledger: dict[str, Any], output: pathlib.Path) -> dict[str, Any]:
    summary = figure_summary(ledger)
    output.mkdir(parents=True, exist_ok=True)
    (output / "rustsec-security-scope.svg").write_text(
        render_svg(summary), encoding="utf-8"
    )
    with (output / "rustsec-security-scope.csv").open(
        "w", encoding="utf-8", newline=""
    ) as handle:
        writer = csv.writer(handle)
        writer.writerow(("section", "key", "label", "count"))
        for key, count in summary["primitive_counts"].items():
            writer.writerow(("primitive", key, key.replace("_", " ").title(), count))
        integration_labels = {
            key: label for key, label, _color in INTEGRATION_LABELS
        }
        for key, count in summary["integration_status"].items():
            writer.writerow(("integration", key, integration_labels[key], count))
        for key, label, _color in CASE_ATTRIBUTION_LABELS:
            writer.writerow(
                (
                    "case_attribution",
                    key,
                    label,
                    summary["case_attribution_counts"][key],
                )
            )
        for key, label, _color in MECHANISM_COVERAGE_LABELS:
            writer.writerow(
                (
                    "mechanism_coverage",
                    key,
                    label,
                    summary["mechanism_coverage_counts"][key],
                )
            )
    (output / "summary.json").write_text(
        json.dumps(summary, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    return summary


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--scope", type=pathlib.Path, default=DEFAULT_SCOPE)
    parser.add_argument("--output", type=pathlib.Path, default=DEFAULT_OUTPUT)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    ledger = json.loads(args.scope.read_text(encoding="utf-8"))
    write_figure_bundle(ledger, args.output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
