#!/usr/bin/env python3
"""Render PPT-ready set diagrams from the current RustSec security scope.

The SVG output is editable in current PowerPoint releases.  An optional
ImageMagick export produces high-resolution PNG and PDF fallbacks without
adding a project dependency.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import html
import json
import pathlib
import re
import shutil
import subprocess
from typing import Any, Iterable, Sequence


ROOT = pathlib.Path(__file__).resolve().parents[2]
DEFAULT_SCOPE = ROOT / "evaluation/config/rustsec_heap_complete_scope.json"
DEFAULT_CORRECTIONS = (
    ROOT / "evaluation/config/rustsec_heap_posthoc_scope_corrections.json"
)
DEFAULT_OUTPUT = ROOT / "docs/figures/rustsec-security-sets-20260715"

TYPEISO_SCOPE = "exploit_enabling_cross_identity_reuse_edge"
POSITIVE_OUTCOMES = frozenset(("detected", "mitigated"))
MECHANISMS = (
    "type_isolation",
    "reclaim_checks",
    "recovery_layout_validation",
)
PDF_TIMESTAMP_PATTERN = re.compile(
    rb"/(CreationDate|ModDate) \(D:\d{14}\)"
)
PDF_TIMESTAMP = b"20260715000000"

COLORS = {
    "ink": "#172033",
    "muted": "#58667A",
    "quiet": "#7C8798",
    "panel": "#F8FAFD",
    "line": "#C9D2DF",
    "blue": "#2F6FDB",
    "blue_fill": "#CFE0FF",
    "orange": "#C96A16",
    "orange_fill": "#F9D4AE",
    "purple": "#7150B9",
    "purple_fill": "#E3D8FA",
    "gray": "#667085",
    "gray_fill": "#E9EDF2",
    "amber": "#9A6B00",
    "amber_fill": "#F8E9B5",
    "audit": "#9B4B46",
    "audit_fill": "#F4D5D1",
    "white": "#FFFFFF",
}


def sha256_file(path: pathlib.Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _case_sort_key(case_id: str) -> tuple[int, str]:
    try:
        return int(case_id.split("-", 1)[1]), case_id
    except (IndexError, ValueError):
        return 10**9, case_id


def _sorted_ids(values: Iterable[str]) -> list[str]:
    return sorted(set(values), key=_case_sort_key)


def _load_object(path: pathlib.Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"JSON root must be an object: {path}")
    return value


def validate_corrections(
    scope: dict[str, Any],
    corrections: dict[str, Any],
    *,
    scope_path: pathlib.Path = DEFAULT_SCOPE,
) -> list[dict[str, Any]]:
    if corrections.get("schema_version") != 1:
        raise ValueError("unsupported scope-correction schema")
    base = corrections.get("base_scope")
    if not isinstance(base, dict):
        raise ValueError("scope corrections require base_scope")
    expected_path = pathlib.PurePosixPath(str(base.get("path", "")))
    try:
        actual_relative = scope_path.resolve().relative_to(ROOT).as_posix()
    except ValueError as error:
        raise ValueError(f"base scope is outside the repository: {scope_path}") from error
    if expected_path.as_posix() != actual_relative:
        raise ValueError(
            f"base-scope path mismatch: {expected_path.as_posix()} != {actual_relative}"
        )
    actual_sha256 = sha256_file(scope_path)
    if base.get("sha256") != actual_sha256:
        raise ValueError(
            "base-scope hash mismatch: "
            f"{base.get('sha256')!r} != {actual_sha256!r}"
        )

    raw_exclusions = corrections.get("exclusions")
    if not isinstance(raw_exclusions, list) or not raw_exclusions:
        raise ValueError("scope corrections require a nonempty exclusions list")
    scope_rows = {
        row.get("case_id"): row
        for row in [*scope.get("cases", []), *scope.get("excluded_cases", [])]
        if isinstance(row, dict) and isinstance(row.get("case_id"), str)
    }
    seen: set[str] = set()
    validated: list[dict[str, Any]] = []
    for raw in raw_exclusions:
        if not isinstance(raw, dict):
            raise ValueError("scope correction must be an object")
        case_id = raw.get("case_id")
        if not isinstance(case_id, str) or not case_id:
            raise ValueError("scope correction requires case_id")
        if case_id in seen:
            raise ValueError(f"duplicate scope correction: {case_id}")
        seen.add(case_id)
        row = scope_rows.get(case_id)
        if row is None:
            raise ValueError(f"scope correction references unknown case: {case_id}")
        for field in ("advisory_id", "package"):
            if raw.get(field) != row.get(field):
                raise ValueError(f"scope correction {field} mismatch for {case_id}")
        if raw.get("action") != "exclude_from_heap_allocator_efficacy_denominator":
            raise ValueError(f"unsupported scope-correction action for {case_id}")
        if raw.get("reason_code") != "stack_only_dangling_target":
            raise ValueError(f"unsupported scope-correction reason for {case_id}")
        if raw.get("original_primitive") != row.get("primary_primitive"):
            raise ValueError(f"scope-correction original primitive mismatch for {case_id}")
        if raw.get("corrected_primitive") != "stack_lifetime_dangling_target":
            raise ValueError(f"unsupported corrected primitive for {case_id}")
        reason = raw.get("reason")
        evidence_paths = raw.get("evidence_paths")
        if not isinstance(reason, str) or not reason.strip():
            raise ValueError(f"scope correction lacks reason for {case_id}")
        if (
            not isinstance(evidence_paths, list)
            or not evidence_paths
            or not all(isinstance(path, str) and path for path in evidence_paths)
        ):
            raise ValueError(f"scope correction lacks evidence for {case_id}")
        missing = [path for path in evidence_paths if not (ROOT / path).is_file()]
        if missing:
            raise ValueError(
                f"scope correction evidence is missing for {case_id}: {missing}"
            )
        positives = [
            result
            for result in row.get("mechanism_results", [])
            if isinstance(result, dict)
            and result.get("outcome") in POSITIVE_OUTCOMES
        ]
        if positives:
            raise ValueError(
                f"post-source-audit exclusion would discard positive evidence: {case_id}"
            )
        validated.append(dict(raw))
    return sorted(validated, key=lambda row: _case_sort_key(str(row["case_id"])))


def validate_mechanism_boundaries(
    scope: dict[str, Any],
    corrections: dict[str, Any],
    *,
    excluded_case_ids: set[str],
) -> list[dict[str, Any]]:
    raw_boundaries = corrections.get("mechanism_boundaries")
    if not isinstance(raw_boundaries, list) or not raw_boundaries:
        raise ValueError("scope corrections require mechanism_boundaries")
    scope_rows = {
        str(row["case_id"]): row
        for row in scope.get("cases", [])
        if isinstance(row, dict) and isinstance(row.get("case_id"), str)
    }
    seen: set[str] = set()
    validated: list[dict[str, Any]] = []
    for raw in raw_boundaries:
        if not isinstance(raw, dict):
            raise ValueError("mechanism-boundary record must be an object")
        case_id = raw.get("case_id")
        if not isinstance(case_id, str) or not case_id:
            raise ValueError("mechanism-boundary record requires case_id")
        if case_id in seen:
            raise ValueError(f"duplicate mechanism-boundary record: {case_id}")
        seen.add(case_id)
        if case_id in excluded_case_ids:
            raise ValueError(f"excluded case cannot be a mechanism boundary: {case_id}")
        row = scope_rows.get(case_id)
        if row is None:
            raise ValueError(f"mechanism boundary references unknown case: {case_id}")
        for field in ("advisory_id", "package"):
            if raw.get(field) != row.get(field):
                raise ValueError(f"mechanism-boundary {field} mismatch for {case_id}")
        if raw.get("classification") != "same_object_or_pre_reuse_concurrency":
            raise ValueError(f"unsupported mechanism-boundary class for {case_id}")
        reason = raw.get("reason")
        evidence_paths = raw.get("evidence_paths")
        if not isinstance(reason, str) or not reason.strip():
            raise ValueError(f"mechanism boundary lacks reason for {case_id}")
        if (
            not isinstance(evidence_paths, list)
            or not evidence_paths
            or not all(isinstance(path, str) and path for path in evidence_paths)
        ):
            raise ValueError(f"mechanism boundary lacks evidence for {case_id}")
        missing = [path for path in evidence_paths if not (ROOT / path).is_file()]
        if missing:
            raise ValueError(
                f"mechanism-boundary evidence is missing for {case_id}: {missing}"
            )
        if row.get("final_attribution") != "not_yet_attributed":
            raise ValueError(
                f"mechanism boundary conflicts with final attribution for {case_id}"
            )
        positives = [
            result
            for result in row.get("mechanism_results", [])
            if isinstance(result, dict)
            and result.get("outcome") in POSITIVE_OUTCOMES
        ]
        if positives:
            raise ValueError(f"mechanism boundary has positive evidence: {case_id}")
        validated.append(dict(raw))
    return sorted(validated, key=lambda row: _case_sort_key(str(row["case_id"])))


def _positive_sets(cases: Sequence[dict[str, Any]]) -> dict[str, set[str]]:
    result = {mechanism: set() for mechanism in MECHANISMS}
    for case in cases:
        case_id = str(case["case_id"])
        for raw in case.get("mechanism_results", []):
            if not isinstance(raw, dict) or raw.get("outcome") not in POSITIVE_OUTCOMES:
                continue
            mechanism = raw.get("mechanism")
            if mechanism not in result:
                raise ValueError(
                    f"positive mechanism needs a diagram category for {case_id}: {mechanism!r}"
                )
            if mechanism == "type_isolation":
                required = (
                    raw.get("outcome") == "mitigated"
                    and raw.get("validated_mitigation") is True
                    and raw.get("positive_scope") == TYPEISO_SCOPE
                    and raw.get("compiler_automatic_victim_coverage") is True
                    and raw.get("manual_victim_identity_annotation") is False
                )
                if not required:
                    raise ValueError(f"weak Type Isolation positive for {case_id}")
            else:
                if raw.get("outcome") != "detected" or raw.get("true_positive") is not True:
                    raise ValueError(f"weak exact-detection positive for {case_id}")
            result[str(mechanism)].add(case_id)
    return result


def _set_record(values: Iterable[str]) -> dict[str, Any]:
    case_ids = _sorted_ids(values)
    return {"count": len(case_ids), "case_ids": case_ids}


def _scope_summary(
    executable: Sequence[dict[str, Any]],
    audit_only: Sequence[dict[str, Any]],
    *,
    mechanism_boundary_case_ids: set[str],
) -> dict[str, Any]:
    executable_ids = {str(case["case_id"]) for case in executable}
    audit_ids = {str(case["case_id"]) for case in audit_only}
    positives = _positive_sets(executable)
    typeiso = positives["type_isolation"]
    reclaim = positives["reclaim_checks"]
    recovery = positives["recovery_layout_validation"]
    if (typeiso & recovery) or (reclaim & recovery):
        raise ValueError("recovery-layout overlap requires an explicit diagram region")
    no_signal = {
        str(case["case_id"])
        for case in executable
        if case.get("final_attribution") == "no_allocator_signal_observed"
    }
    positive_union = typeiso | reclaim | recovery
    boundary = set(mechanism_boundary_case_ids)
    if not boundary <= executable_ids:
        raise ValueError(
            "mechanism-boundary records fall outside this executable denominator: "
            f"{_sorted_ids(boundary - executable_ids)}"
        )
    if positive_union & no_signal or positive_union & boundary or no_signal & boundary:
        raise ValueError("executable outcome groups must be disjoint")
    unclassified = executable_ids - positive_union - no_signal - boundary
    if unclassified:
        raise ValueError(
            "executable cases require an explicit nonpositive classification: "
            f"{_sorted_ids(unclassified)}"
        )
    retained_ids = executable_ids | audit_ids
    return {
        "retained_case_count": len(retained_ids),
        "retained_case_ids": _sorted_ids(retained_ids),
        "executable_case_count": len(executable_ids),
        "executable_case_ids": _sorted_ids(executable_ids),
        "audit_only_count": len(audit_ids),
        "positive_union_count": len(positive_union),
        "positive_union_case_ids": _sorted_ids(positive_union),
        "coverage_percent_of_executable": round(
            100.0 * len(positive_union) / len(executable_ids), 1
        ),
        "type_isolation": _set_record(typeiso),
        "reclaim_checks": _set_record(reclaim),
        "recovery_layout_validation": _set_record(recovery),
        "type_isolation_only": _set_record(typeiso - reclaim - recovery),
        "reclaim_checks_only": _set_record(reclaim - typeiso - recovery),
        "type_isolation_and_reclaim_checks": _set_record(typeiso & reclaim),
        "recovery_layout_validation_only": _set_record(recovery - typeiso - reclaim),
        "no_allocator_signal": _set_record(no_signal),
        "mechanism_boundary": _set_record(boundary),
        "audit_only": _set_record(audit_ids),
    }


def derive_summary(
    scope: dict[str, Any],
    corrections: dict[str, Any],
    *,
    scope_path: pathlib.Path = DEFAULT_SCOPE,
    corrections_path: pathlib.Path = DEFAULT_CORRECTIONS,
) -> dict[str, Any]:
    if scope != _load_object(scope_path):
        raise ValueError("scope object does not match the hash-bound scope path")
    if corrections != _load_object(corrections_path):
        raise ValueError(
            "corrections object does not match the recorded corrections path"
        )
    correction_rows = validate_corrections(
        scope, corrections, scope_path=scope_path
    )
    correction_ids = {str(row["case_id"]) for row in correction_rows}
    boundary_rows = validate_mechanism_boundaries(
        scope,
        corrections,
        excluded_case_ids=correction_ids,
    )
    boundary_ids = {str(row["case_id"]) for row in boundary_rows}
    raw_cases = scope.get("cases")
    raw_excluded = scope.get("excluded_cases")
    if not isinstance(raw_cases, list) or not isinstance(raw_excluded, list):
        raise ValueError("scope cases and excluded_cases must be lists")
    if not all(isinstance(row, dict) for row in [*raw_cases, *raw_excluded]):
        raise ValueError("scope case rows must be objects")
    screened_ids = {
        str(row["case_id"])
        for row in [*raw_cases, *raw_excluded]
        if isinstance(row.get("case_id"), str)
    }
    if len(screened_ids) != len(raw_cases) + len(raw_excluded):
        raise ValueError("screened scope contains missing or duplicate case IDs")

    executable = [
        row for row in raw_cases if str(row["case_id"]) not in correction_ids
    ]
    audit_only = [
        row for row in raw_excluded if str(row["case_id"]) not in correction_ids
    ]
    uaf_executable = [
        row for row in executable if row.get("primary_primitive") == "use_after_free"
    ]
    uaf_audit = [
        row for row in audit_only if row.get("primary_primitive") == "use_after_free"
    ]
    full = _scope_summary(
        executable,
        audit_only,
        mechanism_boundary_case_ids=boundary_ids,
    )
    uaf_executable_ids = {str(row["case_id"]) for row in uaf_executable}
    uaf = _scope_summary(
        uaf_executable,
        uaf_audit,
        mechanism_boundary_case_ids=boundary_ids & uaf_executable_ids,
    )

    membership_rows: list[dict[str, Any]] = []
    full_type = set(full["type_isolation"]["case_ids"])
    full_reclaim = set(full["reclaim_checks"]["case_ids"])
    full_recovery = set(full["recovery_layout_validation"]["case_ids"])
    full_no_signal = set(full["no_allocator_signal"]["case_ids"])
    full_boundary = set(full["mechanism_boundary"]["case_ids"])
    full_audit = set(full["audit_only"]["case_ids"])
    correction_by_id = {str(row["case_id"]): row for row in correction_rows}
    for row in sorted(
        [*raw_cases, *raw_excluded],
        key=lambda value: _case_sort_key(str(value["case_id"])),
    ):
        case_id = str(row["case_id"])
        if case_id in correction_by_id:
            status = "post_source_audit_exclusion"
            outcome = "stack_only_out_of_heap_scope"
        elif case_id in full_audit:
            status = "audit_only"
            outcome = "no_executable_witness"
        else:
            status = "executable"
            active = [
                name
                for name, members in (
                    ("type_isolation", full_type),
                    ("reclaim_checks", full_reclaim),
                    ("recovery_layout_validation", full_recovery),
                )
                if case_id in members
            ]
            if active:
                outcome = "+".join(active)
            elif case_id in full_no_signal:
                outcome = "no_allocator_signal"
            elif case_id in full_boundary:
                outcome = "mechanism_boundary"
            else:
                raise ValueError(f"case lacks a presentation outcome: {case_id}")
        membership_rows.append(
            {
                "case_id": case_id,
                "advisory_id": row.get("advisory_id"),
                "package": row.get("package"),
                "source_ledger_primitive": row.get("primary_primitive"),
                "presentation_primitive": correction_by_id.get(
                    case_id, {}
                ).get("corrected_primitive", row.get("primary_primitive")),
                "presentation_status": status,
                "presentation_outcome": outcome,
                "type_isolation": case_id in full_type,
                "reclaim_checks": case_id in full_reclaim,
                "recovery_layout_validation": case_id in full_recovery,
            }
        )

    return {
        "schema_version": 1,
        "source": "unialloc-rustsec-security-set-diagrams",
        "claim_grade": False,
        "source_scope": {
            "path": scope_path.resolve().relative_to(ROOT).as_posix(),
            "sha256": sha256_file(scope_path),
            "rustsec_commit": scope.get("rustsec_commit"),
        },
        "scope_corrections": {
            "path": corrections_path.resolve().relative_to(ROOT).as_posix(),
            "sha256": sha256_file(corrections_path),
        },
        "screened_candidate_count": len(screened_ids),
        "retained_heap_candidate_count": full["retained_case_count"],
        "executable_heap_case_count": full["executable_case_count"],
        "post_source_audit_exclusions": {
            "count": len(correction_rows),
            "case_ids": _sorted_ids(correction_ids),
            "records": correction_rows,
        },
        "mechanism_boundary_classifications": {
            "count": len(boundary_rows),
            "case_ids": _sorted_ids(boundary_ids),
            "records": boundary_rows,
        },
        "full_scope": full,
        "uaf_scope": uaf,
        "membership_rows": membership_rows,
        "claim_boundary": (
            "Type Isolation counts causal mitigation of compiler-bound measured "
            "cross-identity reuse edges, not full source-vulnerability detection. "
            "Reclaim and recovery-layout counts require exact diagnostics in matched "
            "vulnerable arms with valid controls. Audit-only and post-source-audit "
            "exclusions remain outside efficacy denominators."
        ),
    }


def _escape(value: object) -> str:
    return html.escape(str(value), quote=True)


def _text(
    x: float,
    y: float,
    value: object,
    *,
    size: int = 22,
    weight: int = 400,
    color: str = "ink",
    anchor: str = "middle",
) -> str:
    fill = COLORS.get(color, color)
    return (
        f'<text x="{x:g}" y="{y:g}" text-anchor="{anchor}" '
        'font-family="Arial, Helvetica, sans-serif" '
        f'font-size="{size}" font-weight="{weight}" fill="{fill}">'
        f"{_escape(value)}</text>"
    )


def _multiline(
    x: float,
    y: float,
    lines: Sequence[str],
    *,
    size: int = 18,
    weight: int = 400,
    color: str = "muted",
    anchor: str = "middle",
    gap: int | None = None,
) -> str:
    fill = COLORS.get(color, color)
    line_gap = gap or round(size * 1.28)
    tspans = "".join(
        f'<tspan x="{x:g}" dy="{0 if index == 0 else line_gap}">{_escape(line)}</tspan>'
        for index, line in enumerate(lines)
    )
    return (
        f'<text x="{x:g}" y="{y:g}" text-anchor="{anchor}" '
        'font-family="Arial, Helvetica, sans-serif" '
        f'font-size="{size}" font-weight="{weight}" fill="{fill}">{tspans}</text>'
    )


def _rect(
    x: float,
    y: float,
    width: float,
    height: float,
    *,
    fill: str = "white",
    stroke: str = "line",
    stroke_width: float = 2,
    radius: float = 22,
    dash: str | None = None,
) -> str:
    dash_attr = f' stroke-dasharray="{dash}"' if dash else ""
    return (
        f'<rect x="{x:g}" y="{y:g}" width="{width:g}" height="{height:g}" '
        f'rx="{radius:g}" fill="{COLORS.get(fill, fill)}" '
        f'stroke="{COLORS.get(stroke, stroke)}" stroke-width="{stroke_width:g}"'
        f"{dash_attr}/>"
    )


def _circle(
    cx: float,
    cy: float,
    radius: float,
    *,
    fill: str,
    stroke: str,
    opacity: float = 0.72,
    dash: str | None = None,
) -> str:
    dash_attr = f' stroke-dasharray="{dash}"' if dash else ""
    return (
        f'<circle cx="{cx:g}" cy="{cy:g}" r="{radius:g}" '
        f'fill="{COLORS.get(fill, fill)}" fill-opacity="{opacity:g}" '
        f'stroke="{COLORS.get(stroke, stroke)}" stroke-width="4"{dash_attr}/>'
    )


def _badge(
    x: float,
    y: float,
    width: float,
    height: float,
    *,
    title: str,
    count: int,
    subtitle: Sequence[str],
    fill: str,
    stroke: str,
) -> list[str]:
    return [
        _rect(x, y, width, height, fill=fill, stroke=stroke, radius=20),
        _text(x + width / 2, y + 34, title, size=19, weight=700),
        _text(x + width / 2, y + 83, count, size=43, weight=800),
        _multiline(
            x + width / 2,
            y + 110,
            list(subtitle),
            size=14,
            color="muted",
            gap=18,
        ),
    ]


def _svg_document(body: Sequence[str], *, width: int = 1600, height: int = 900) -> str:
    return "\n".join(
        [
            f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" '
            f'height="{height}" viewBox="0 0 {width} {height}">',
            '<rect width="100%" height="100%" fill="#FFFFFF"/>',
            *body,
            "</svg>",
            "",
        ]
    )


def render_full_svg(summary: dict[str, Any]) -> str:
    data = summary["full_scope"]
    screened = summary["screened_candidate_count"]
    removed = summary["post_source_audit_exclusions"]["count"]
    retained = summary["retained_heap_candidate_count"]
    elements = [
        _text(60, 56, "RustSec heap security coverage sets", size=34, weight=800, anchor="start"),
        _text(
            60,
            89,
            f"{screened} screened · {removed} stack-only case removed after source audit · {retained} retained heap cases",
            size=18,
            color="muted",
            anchor="start",
        ),
        _rect(50, 120, 1500, 690, fill="white", stroke="ink", stroke_width=3, radius=28),
        _text(82, 159, f"Retained heap-relevant cases · {retained}", size=22, weight=700, anchor="start"),
        _rect(82, 182, 1135, 570, fill="panel", stroke="line", stroke_width=2, radius=24),
        _text(110, 219, f'Executable reproductions · {data["executable_case_count"]}', size=21, weight=700, anchor="start"),
        _rect(1238, 182, 280, 570, fill="panel", stroke="line", stroke_width=2, radius=24),
        _text(1378, 223, "Audit-only", size=22, weight=700),
        _multiline(1378, 253, ["No valid executable", "witness"], size=16, color="muted"),
        _text(1378, 400, data["audit_only_count"], size=72, weight=800, color="audit"),
        _text(1378, 438, "outside efficacy denominator", size=15, color="muted"),
        _multiline(1378, 630, data["audit_only"]["case_ids"], size=14, color="muted", gap=20),
        _circle(382, 481, 214, fill="blue_fill", stroke="blue", opacity=0.78),
        _circle(668, 481, 252, fill="orange_fill", stroke="orange", opacity=0.72),
        _text(300, 310, f'Type Isolation · {data["type_isolation"]["count"]}', size=22, weight=700, color="blue"),
        _text(300, 336, "measured reuse-edge mitigation", size=14, color="muted"),
        _text(290, 494, data["type_isolation_only"]["count"], size=61, weight=800, color="blue"),
        _text(290, 525, "TypeIso only", size=17, weight=700, color="blue"),
        _text(522, 459, data["type_isolation_and_reclaim_checks"]["count"], size=52, weight=800),
        _text(522, 490, "both", size=16, weight=700),
        _text(754, 310, f'Reclaim checks · {data["reclaim_checks"]["count"]}', size=22, weight=700, color="orange"),
        _text(754, 336, "exact duplicate-reclaim detection", size=14, color="muted"),
        _text(760, 494, data["reclaim_checks_only"]["count"], size=61, weight=800, color="orange"),
        _text(760, 525, "reclaim only", size=17, weight=700, color="orange"),
        _circle(1009, 342, 69, fill="purple_fill", stroke="purple", opacity=0.9),
        _text(1009, 327, "Layout", size=15, weight=700, color="purple"),
        _text(1009, 372, data["recovery_layout_validation"]["count"], size=34, weight=800, color="purple"),
        _rect(928, 448, 244, 117, fill="gray_fill", stroke="gray", radius=18),
        _text(1050, 480, "No allocator signal", size=18, weight=700),
        _text(1050, 530, data["no_allocator_signal"]["count"], size=42, weight=800, color="gray"),
        _rect(928, 590, 244, 117, fill="amber_fill", stroke="amber", radius=18, dash="8 6"),
        _text(1050, 622, "Mechanism boundary", size=18, weight=700),
        _text(1050, 672, data["mechanism_boundary"]["count"], size=42, weight=800, color="amber"),
        _rect(100, 665, 242, 62, fill="ink", stroke="ink", radius=15),
        _text(221, 696, f'{data["positive_union_count"]}/{data["executable_case_count"]} covered', size=23, weight=800, color="white"),
        _text(221, 718, "by measured allocator mechanisms", size=12, color="#E8EDF5"),
        _text(
            60,
            850,
            "Overlap is case-level: RSH-002 has both a Type Isolation reuse-edge mitigation and an exact reclaim-check detection.",
            size=16,
            color="muted",
            anchor="start",
        ),
        _text(
            1540,
            878,
            "TypeIso credit is bounded to compiler-bound cross-identity reuse edges; full source detections remain 0.",
            size=13,
            color="quiet",
            anchor="end",
        ),
    ]
    return _svg_document(elements)


def render_uaf_svg(summary: dict[str, Any]) -> str:
    data = summary["uaf_scope"]
    retained = data["retained_case_count"]
    executable = data["executable_case_count"]
    audit_only = data["audit_only_count"]
    elements = [
        _text(60, 56, "Use-after-free coverage sets", size=34, weight=800, anchor="start"),
        _text(
            60,
            89,
            f"{retained} retained heap-relevant UAF cases · {executable} executable + {audit_only} audit-only · RSH-006 stack-only correction excluded",
            size=18,
            color="muted",
            anchor="start",
        ),
        _rect(50, 120, 1500, 690, fill="white", stroke="ink", stroke_width=3, radius=28),
        _text(82, 159, f"Retained heap-relevant UAF cases · {retained}", size=22, weight=700, anchor="start"),
        _rect(82, 182, 1160, 570, fill="panel", stroke="line", stroke_width=2, radius=24),
        _text(110, 219, f"Executable UAF reproductions · {executable}", size=21, weight=700, anchor="start"),
        _rect(1263, 182, 255, 570, fill="panel", stroke="line", stroke_width=2, radius=24),
        _text(1390, 223, "Audit-only", size=22, weight=700),
        _multiline(1390, 253, ["No valid executable", "witness"], size=16, color="muted"),
        _text(1390, 400, data["audit_only_count"], size=72, weight=800, color="audit"),
        _text(1390, 438, "outside efficacy denominator", size=15, color="muted"),
        _multiline(1390, 650, data["audit_only"]["case_ids"], size=16, color="muted"),
        _circle(445, 481, 238, fill="blue_fill", stroke="blue", opacity=0.78),
        _circle(759, 481, 176, fill="orange_fill", stroke="orange", opacity=0.72),
        _text(350, 300, f'Type Isolation · {data["type_isolation"]["count"]}', size=22, weight=700, color="blue"),
        _text(350, 327, "cross-identity reuse edges", size=15, color="muted"),
        _text(370, 500, data["type_isolation_only"]["count"], size=64, weight=800, color="blue"),
        _text(370, 534, "TypeIso only", size=17, weight=700, color="blue"),
        _text(619, 462, data["type_isolation_and_reclaim_checks"]["count"], size=52, weight=800),
        _text(619, 494, "both", size=16, weight=700),
        _text(803, 354, f'Reclaim · {data["reclaim_checks"]["count"]}', size=21, weight=700, color="orange"),
        _text(803, 381, "duplicate reclaim", size=14, color="muted"),
        _text(810, 500, data["reclaim_checks_only"]["count"], size=58, weight=800, color="orange"),
        _text(810, 534, "reclaim only", size=17, weight=700, color="orange"),
        _rect(966, 338, 230, 126, fill="gray_fill", stroke="gray", radius=18),
        _text(1081, 372, "Foreign allocator", size=18, weight=700),
        _text(1081, 420, data["no_allocator_signal"]["count"], size=42, weight=800, color="gray"),
        _text(1081, 446, "completed no-signal", size=14, color="muted"),
        _rect(966, 500, 230, 138, fill="amber_fill", stroke="amber", radius=18, dash="8 6"),
        _text(1081, 535, "Pre-reuse race", size=18, weight=700),
        _text(1081, 584, data["mechanism_boundary"]["count"], size=42, weight=800, color="amber"),
        _text(1081, 614, "needs temporal/concurrency", size=14, color="muted"),
        _rect(115, 665, 255, 62, fill="ink", stroke="ink", radius=15),
        _text(242, 696, f'{data["positive_union_count"]}/{data["executable_case_count"]} covered', size=24, weight=800, color="white"),
        _text(242, 718, "by TypeIso or reclaim checks", size=12, color="#E8EDF5"),
        _text(
            60,
            850,
            "Uncovered executable UAF controls: RSH-003 and RSH-019 are same-object/pre-reuse races; RSH-075 uses SQLite's C allocator.",
            size=16,
            color="muted",
            anchor="start",
        ),
        _text(
            1540,
            878,
            "RSH-006 is excluded because the dangling target is stack storage, outside the heap allocator scope.",
            size=13,
            color="quiet",
            anchor="end",
        ),
    ]
    return _svg_document(elements)


def _mini_panel(
    *,
    x: int,
    title: str,
    subtitle: str,
    data: dict[str, Any],
    show_recovery: bool,
) -> list[str]:
    elements = [
        _rect(x, 150, 720, 630, fill="white", stroke="ink", stroke_width=2.5, radius=26),
        _text(x + 36, 196, title, size=27, weight=800, anchor="start"),
        _text(x + 36, 226, subtitle, size=16, color="muted", anchor="start"),
        _rect(x + 35, 260, 650, 365, fill="panel", stroke="line", radius=20),
        _text(x + 60, 294, f'Executable · {data["executable_case_count"]}', size=19, weight=700, anchor="start"),
        _circle(x + 190, 454, 130, fill="blue_fill", stroke="blue", opacity=0.78),
        _circle(x + 342, 454, 145 if show_recovery else 105, fill="orange_fill", stroke="orange", opacity=0.72),
        _text(x + 125, 444, data["type_isolation_only"]["count"], size=48, weight=800, color="blue"),
        _text(x + 125, 473, "TypeIso only", size=14, weight=700, color="blue"),
        _text(x + 266, 440, data["type_isolation_and_reclaim_checks"]["count"], size=40, weight=800),
        _text(x + 266, 469, "both", size=13, weight=700),
        _text(x + 390, 444, data["reclaim_checks_only"]["count"], size=48, weight=800, color="orange"),
        _text(x + 390, 473, "reclaim only", size=14, weight=700, color="orange"),
        _rect(x + 520, 300, 140, 105, fill="gray_fill", stroke="gray", radius=16),
        _text(x + 590, 330, "No signal", size=16, weight=700),
        _text(x + 590, 375, data["no_allocator_signal"]["count"], size=38, weight=800, color="gray"),
        _rect(x + 520, 450, 140, 105, fill="amber_fill", stroke="amber", radius=16, dash="7 5"),
        _text(x + 590, 480, "Boundary", size=16, weight=700),
        _text(x + 590, 525, data["mechanism_boundary"]["count"], size=38, weight=800, color="amber"),
        _rect(x + 510, 654, 160, 96, fill="audit_fill", stroke="audit", radius=16),
        _text(x + 590, 682, "Audit-only", size=16, weight=700),
        _text(x + 590, 728, data["audit_only_count"], size=38, weight=800, color="audit"),
        _rect(x + 45, 676, 295, 54, fill="ink", stroke="ink", radius=13),
        _text(x + 192, 710, f'{data["positive_union_count"]}/{data["executable_case_count"]} executable covered', size=19, weight=800, color="white"),
    ]
    if show_recovery:
        elements.extend(
            [
                _circle(x + 495, 600, 24, fill="purple_fill", stroke="purple", opacity=0.92),
                _text(x + 495, 597, "layout", size=9, weight=700, color="purple"),
                _text(x + 495, 615, data["recovery_layout_validation"]["count"], size=16, weight=800, color="purple"),
            ]
        )
    return elements


def render_overview_svg(summary: dict[str, Any]) -> str:
    full = summary["full_scope"]
    uaf = summary["uaf_scope"]
    elements = [
        _text(60, 58, "RustSec heap security evaluation: mechanism sets", size=34, weight=800, anchor="start"),
        _text(
            60,
            91,
            f'Post-source-audit view: {summary["screened_candidate_count"]} screened, RSH-006 removed as stack-only, {summary["retained_heap_candidate_count"]} heap-relevant cases retained.',
            size=18,
            color="muted",
            anchor="start",
        ),
        *_mini_panel(
            x=55,
            title="Full retained heap scope",
            subtitle=f'{full["retained_case_count"]} reviewed · {full["executable_case_count"]} executable · {full["audit_only_count"]} audit-only',
            data=full,
            show_recovery=True,
        ),
        *_mini_panel(
            x=825,
            title="Use-after-free subset",
            subtitle=f'{uaf["retained_case_count"]} reviewed · {uaf["executable_case_count"]} executable · {uaf["audit_only_count"]} audit-only',
            data=uaf,
            show_recovery=False,
        ),
        _text(
            60,
            840,
            "Circle overlap = the same case has qualified evidence from both mechanisms (RSH-002).",
            size=15,
            color="muted",
            anchor="start",
        ),
        _text(
            1540,
            870,
            "Editable SVG for PowerPoint · counts derived from the pinned complete-scope ledger and post-source-audit correction.",
            size=13,
            color="quiet",
            anchor="end",
        ),
    ]
    return _svg_document(elements)


def _case_list(record: dict[str, Any]) -> str:
    return ", ".join(record["case_ids"]) or "None"


def render_readme(summary: dict[str, Any]) -> str:
    full = summary["full_scope"]
    uaf = summary["uaf_scope"]
    return f"""# RustSec security mechanism set diagrams

These slide-ready diagrams use the post-source-audit heap scope. The frozen
ledger screened **{summary['screened_candidate_count']}** cases. RSH-006 is
removed from the heap allocator denominator because its dangling target is a
moved stack-local object. The retained scope contains
**{summary['retained_heap_candidate_count']}** cases: **{full['executable_case_count']}**
executable cases and **{full['audit_only_count']}** audit-only cases.

## Recommended slide assets

- `rustsec-security-sets-overview.svg`: one-slide overview with the full scope
  and UAF inset.
- `rustsec-security-sets-full.svg`: full-scope explanation slide.
- `rustsec-security-sets-uaf.svg`: UAF explanation slide.
- Matching PNG files are high-resolution fallbacks. SVG is the editable
  PowerPoint source.

## Full retained heap scope

| Set or region | Count | Cases |
| --- | ---: | --- |
| Type Isolation only | {full['type_isolation_only']['count']} | {_case_list(full['type_isolation_only'])} |
| Type Isolation and reclaim checks | {full['type_isolation_and_reclaim_checks']['count']} | {_case_list(full['type_isolation_and_reclaim_checks'])} |
| Reclaim checks only | {full['reclaim_checks_only']['count']} | {_case_list(full['reclaim_checks_only'])} |
| Recovery-layout validation | {full['recovery_layout_validation']['count']} | {_case_list(full['recovery_layout_validation'])} |
| Completed no allocator signal | {full['no_allocator_signal']['count']} | {_case_list(full['no_allocator_signal'])} |
| Allocator-mechanism boundary | {full['mechanism_boundary']['count']} | {_case_list(full['mechanism_boundary'])} |
| Audit-only | {full['audit_only']['count']} | {_case_list(full['audit_only'])} |
| Removed after storage-domain audit | 1 | RSH-006 |

The positive mechanism union covers **{full['positive_union_count']}/{full['executable_case_count']}**
executable cases. Type Isolation contributes 12 measured reuse-edge
mitigations, reclaim checks contribute 31 exact duplicate-reclaim detections,
and recovery-layout validation contributes one exact layout diagnostic.
RSH-002 is the only positive overlap.

## Executable UAF subset

| Set or region | Count | Cases |
| --- | ---: | --- |
| Type Isolation only | {uaf['type_isolation_only']['count']} | {_case_list(uaf['type_isolation_only'])} |
| Type Isolation and reclaim checks | {uaf['type_isolation_and_reclaim_checks']['count']} | {_case_list(uaf['type_isolation_and_reclaim_checks'])} |
| Reclaim checks only | {uaf['reclaim_checks_only']['count']} | {_case_list(uaf['reclaim_checks_only'])} |
| Foreign-allocator no-signal control | {uaf['no_allocator_signal']['count']} | {_case_list(uaf['no_allocator_signal'])} |
| Same-object/pre-reuse concurrency boundary | {uaf['mechanism_boundary']['count']} | {_case_list(uaf['mechanism_boundary'])} |
| Audit-only UAF | {uaf['audit_only']['count']} | {_case_list(uaf['audit_only'])} |

The positive union covers **{uaf['positive_union_count']}/{uaf['executable_case_count']}**
executable heap UAF cases. RSH-003 and RSH-019 require temporal-access or
concurrency mechanisms. RSH-075 uses SQLite's C allocator. RSH-006 is outside
the heap denominator.

## Speaker-note boundaries

- **Type Isolation:** causal mitigation of a compiler-bound, measured
  cross-identity reuse edge. This column does not claim full source-level UAF
  detection.
- **Reclaim checks:** exact duplicate-reclaim diagnostic in the vulnerable
  treatment with matched plain and patched controls.
- **Recovery-layout validation:** exact allocation/deallocation layout-mismatch
  diagnostic with Miri-backed ground truth.
- **No allocator signal:** a completed matched experiment with no qualified
  current UniAlloc signal.
- **Mechanism boundary:** the vulnerability requires synchronization or
  ordinary-access temporal validation before allocator reuse.
- **Audit-only:** no valid executable vulnerable/patched witness; these cases
  remain outside every efficacy numerator and denominator.

## Reproduction

```bash
uv run python evaluation/scripts/plot_rustsec_security_sets.py --rasterize
```

Inputs are hash-bound in `rustsec-security-sets-data.json`. All counts remain
exploratory (`claim_grade=false`).
"""


def _write_membership_csv(rows: Sequence[dict[str, Any]], path: pathlib.Path) -> None:
    fields = (
        "case_id",
        "advisory_id",
        "package",
        "source_ledger_primitive",
        "presentation_primitive",
        "presentation_status",
        "presentation_outcome",
        "type_isolation",
        "reclaim_checks",
        "recovery_layout_validation",
    )
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)


def _write_manifest(output: pathlib.Path) -> None:
    manifest_path = output / "artifact-manifest.json"
    artifacts = []
    for path in sorted(output.iterdir(), key=lambda value: value.name):
        if not path.is_file() or path == manifest_path:
            continue
        artifacts.append(
            {
                "path": path.name,
                "bytes": path.stat().st_size,
                "sha256": sha256_file(path),
            }
        )
    manifest_path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "source": "unialloc-rustsec-security-set-diagrams",
                "artifacts": artifacts,
            },
            indent=2,
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )


def write_bundle(summary: dict[str, Any], output: pathlib.Path) -> None:
    output.mkdir(parents=True, exist_ok=True)
    for name in ("overview", "full", "uaf"):
        for suffix in (".png", ".pdf"):
            (output / f"rustsec-security-sets-{name}{suffix}").unlink(
                missing_ok=True
            )
    (output / "rustsec-security-sets-overview.svg").write_text(
        render_overview_svg(summary), encoding="utf-8"
    )
    (output / "rustsec-security-sets-full.svg").write_text(
        render_full_svg(summary), encoding="utf-8"
    )
    (output / "rustsec-security-sets-uaf.svg").write_text(
        render_uaf_svg(summary), encoding="utf-8"
    )
    (output / "rustsec-security-sets-data.json").write_text(
        json.dumps(summary, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    _write_membership_csv(
        summary["membership_rows"],
        output / "rustsec-security-set-membership.csv",
    )
    (output / "README.md").write_text(render_readme(summary), encoding="utf-8")
    _write_manifest(output)


def rasterize_bundle(output: pathlib.Path) -> None:
    convert = shutil.which("convert")
    if convert is None:
        raise RuntimeError("ImageMagick 'convert' is required for --rasterize")
    for svg in sorted(output.glob("rustsec-security-sets-*.svg")):
        png = svg.with_suffix(".png")
        pdf = svg.with_suffix(".pdf")
        subprocess.run(
            [
                convert,
                "-density",
                "192",
                "-background",
                "white",
                str(svg),
                str(png),
            ],
            check=True,
        )
        subprocess.run(
            [
                convert,
                "-density",
                "120",
                "-background",
                "white",
                "-compress",
                "jpeg",
                "-quality",
                "92",
                str(svg),
                str(pdf),
            ],
            check=True,
        )
        normalize_pdf_timestamps(pdf)
    _write_manifest(output)


def normalize_pdf_timestamps(path: pathlib.Path) -> None:
    payload = path.read_bytes()
    normalized, replacements = PDF_TIMESTAMP_PATTERN.subn(
        lambda match: b"/"
        + match.group(1)
        + b" (D:"
        + PDF_TIMESTAMP
        + b")",
        payload,
    )
    if replacements != 2:
        raise RuntimeError(
            f"expected ImageMagick PDF creation and modification timestamps: {path}"
        )
    path.write_bytes(normalized)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--scope", type=pathlib.Path, default=DEFAULT_SCOPE)
    parser.add_argument(
        "--corrections", type=pathlib.Path, default=DEFAULT_CORRECTIONS
    )
    parser.add_argument("--output", type=pathlib.Path, default=DEFAULT_OUTPUT)
    parser.add_argument(
        "--rasterize",
        action="store_true",
        help="also export high-resolution PNG and PDF fallbacks with ImageMagick",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    scope_path = args.scope.expanduser().resolve()
    corrections_path = args.corrections.expanduser().resolve()
    output = args.output.expanduser().resolve()
    scope = _load_object(scope_path)
    corrections = _load_object(corrections_path)
    summary = derive_summary(
        scope,
        corrections,
        scope_path=scope_path,
        corrections_path=corrections_path,
    )
    write_bundle(summary, output)
    if args.rasterize:
        rasterize_bundle(output)
    print(
        json.dumps(
            {
                "output": output.as_posix(),
                "retained_heap_candidates": summary["retained_heap_candidate_count"],
                "executable_heap_cases": summary["executable_heap_case_count"],
                "covered_executable_cases": summary["full_scope"]["positive_union_count"],
                "executable_uaf_cases": summary["uaf_scope"]["executable_case_count"],
                "covered_executable_uaf_cases": summary["uaf_scope"]["positive_union_count"],
            },
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
