#!/usr/bin/env python3
"""Aggregate conservative automatic lifetime-classifier rustc audits.

The denominator is unique semantic allocation owner sites. Allocation and Drop
rows are paired separately so compiler metadata plumbing cannot inflate static
coverage or hide a mismatched nonzero lifetime identity, including profile and
manual decisions outside the automatic-classifier coverage denominator.
"""

from __future__ import annotations

import argparse
from collections import Counter, defaultdict
import json
from pathlib import Path
import sys
from typing import Iterable


PASS_SOURCE = "unialloc-rustc-driver-mir-rewrite-dry-run"
ALLOCATION_KIND = "semantic_scope_enter_exit_rewrite"
DROP_KIND = "semantic_scope_drop_rewrite"
EPHEMERAL_BASIS = "automatic_exact_local_drop_before_phase_boundary"
LONG_LIVED_BASIS = "automatic_exact_local_drop_after_phase_boundary"
UNSUPPORTED_BASIS = "automatic_unsupported_site_unknown"


class SummaryError(RuntimeError):
    """Raised when an input cannot support a fail-closed classifier summary."""


def automatic_unknown_basis(basis: str) -> bool:
    return basis == "automatic_below_confidence_threshold" or (
        basis.startswith("automatic_") and basis.endswith("_unknown")
    )


def exact_site_key(row: dict[str, object]) -> tuple[int, int, int]:
    try:
        return (
            int(row["callsite"]),
            int(row["type_id"]),
            int(row["module_id"]),
        )
    except (KeyError, TypeError, ValueError) as error:
        raise SummaryError(f"candidate has an invalid exact identity: {row}") from error


def owner_pair_key(row: dict[str, object]) -> tuple[int, str, str, str]:
    try:
        return (
            int(row["module_id"]),
            str(row["mir_function"]),
            str(row["semantic_object_type"]),
            str(row["destination_place"]),
        )
    except (KeyError, TypeError, ValueError) as error:
        raise SummaryError(f"candidate has an invalid owner-pair identity: {row}") from error


def load_audit(path: Path) -> dict[str, object]:
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise SummaryError(f"cannot read rustc audit {path}: {error}") from error
    if not isinstance(document, dict):
        raise SummaryError(f"rustc audit {path} is not a JSON object")
    if document.get("schema_version") != 1 or document.get("source") != PASS_SOURCE:
        raise SummaryError(f"JSON file is not a supported UniAlloc rustc audit: {path}")
    if not isinstance(document.get("compiler_pass"), dict):
        raise SummaryError(f"rustc audit has no compiler_pass object: {path}")
    if not isinstance(document.get("rewrite_candidates"), list):
        raise SummaryError(f"rustc audit has no rewrite_candidates array: {path}")
    return document


def insert_unique_row(
    rows: dict[tuple[int, int, int], dict[str, object]],
    row: dict[str, object],
    *,
    path: Path,
) -> None:
    key = exact_site_key(row)
    previous = rows.get(key)
    if previous is None:
        rows[key] = row
        return
    comparable = ("lifetime_hint", "lifetime_hint_confidence", "lifetime_hint_basis")
    if any(previous.get(field) != row.get(field) for field in comparable):
        raise SummaryError(
            f"conflicting duplicate exact site {key} while reading {path}: "
            f"{[(field, previous.get(field), row.get(field)) for field in comparable]}"
        )


def _ratio(numerator: int, denominator: int) -> float:
    return numerator / denominator if denominator else 0.0


def lifetime_selection_present(row: dict[str, object]) -> bool:
    """Return whether a row carries an allocator-visible lifetime selection."""

    return int(row.get("lifetime_hint") or 0) != 0 or str(
        row.get("lifetime_hint_basis") or ""
    ) == "profile_exact_match"


def summarize_audit_root(label: str, root: Path) -> dict[str, object]:
    if not root.is_dir():
        raise SummaryError(f"audit root is not a directory: {root}")
    paths = sorted(root.rglob("*.json"))
    if not paths:
        raise SummaryError(f"audit root contains no JSON files: {root}")

    allocations: dict[tuple[int, int, int], dict[str, object]] = {}
    drops: dict[tuple[int, int, int], dict[str, object]] = {}
    enabled_files = 0
    for path in paths:
        audit = load_audit(path)
        compiler_pass = audit["compiler_pass"]
        assert isinstance(compiler_pass, dict)
        if compiler_pass.get("automatic_lifetime_classifier_enabled") is True:
            enabled_files += 1
        candidates = audit["rewrite_candidates"]
        assert isinstance(candidates, list)
        for candidate in candidates:
            if not isinstance(candidate, dict):
                raise SummaryError(f"non-object rewrite candidate in {path}")
            kind = candidate.get("lowering_kind")
            if kind == ALLOCATION_KIND:
                insert_unique_row(allocations, candidate, path=path)
            elif kind == DROP_KIND:
                insert_unique_row(drops, candidate, path=path)

    ephemeral = 0
    long_lived = 0
    abstained = 0
    unsupported = 0
    nonautomatic = 0
    unknown_reasons: Counter[str] = Counter()
    for row in allocations.values():
        basis = str(row.get("lifetime_hint_basis") or "")
        hint = int(row.get("lifetime_hint") or 0)
        if basis == EPHEMERAL_BASIS and hint == 1:
            ephemeral += 1
        elif basis == LONG_LIVED_BASIS and hint == 2:
            long_lived += 1
        elif basis == UNSUPPORTED_BASIS and hint == 0:
            unsupported += 1
            unknown_reasons[basis] += 1
        elif automatic_unknown_basis(basis) and hint == 0:
            abstained += 1
            unknown_reasons[basis] += 1
        else:
            nonautomatic += 1

    drops_by_owner: dict[tuple[int, str, str, str], list[dict[str, object]]] = defaultdict(list)
    for row in drops.values():
        drops_by_owner[owner_pair_key(row)].append(row)

    pairing_checked = 0
    pairing_missing = 0
    pairing_mismatch = 0
    for allocation in allocations.values():
        basis = str(allocation.get("lifetime_hint_basis") or "")
        matching_drops = drops_by_owner.get(owner_pair_key(allocation), [])
        if not lifetime_selection_present(allocation) and not any(
            lifetime_selection_present(drop) for drop in matching_drops
        ):
            continue
        pairing_checked += 1
        if not matching_drops:
            pairing_missing += 1
            continue
        expected = (
            int(allocation.get("lifetime_hint") or 0),
            int(allocation.get("lifetime_hint_confidence") or 0),
            basis,
        )
        if any(
            (
                int(drop.get("lifetime_hint") or 0),
                int(drop.get("lifetime_hint_confidence") or 0),
                str(drop.get("lifetime_hint_basis") or ""),
            )
            != expected
            for drop in matching_drops
        ):
            pairing_mismatch += 1

    candidates = len(allocations)
    classified = ephemeral + long_lived
    eligible = classified + abstained
    unknown = abstained + unsupported
    return {
        "schema_version": 1,
        "source": "unialloc-automatic-lifetime-classifier-audit-summary",
        "label": label,
        "audit_root": str(root.resolve()),
        "audit_file_count": len(paths),
        "classifier_enabled_audit_file_count": enabled_files,
        "semantic_candidate_allocation_site_count": candidates,
        "automatic_eligible_allocation_site_count": eligible,
        "classified_allocation_site_count": classified,
        "ephemeral_allocation_site_count": ephemeral,
        "long_lived_allocation_site_count": long_lived,
        "abstained_allocation_site_count": abstained,
        "unsupported_allocation_site_count": unsupported,
        "unknown_allocation_site_count": unknown,
        "nonautomatic_allocation_site_count": nonautomatic,
        "classification_coverage": _ratio(classified, eligible),
        "end_to_end_classification_coverage": _ratio(classified, candidates),
        "unknown_reason_counts": dict(sorted(unknown_reasons.items())),
        "classified_pairing_checked_count": pairing_checked,
        "classified_pairing_missing_drop_count": pairing_missing,
        "classified_pairing_mismatch_count": pairing_mismatch,
        "pairing_consistent": pairing_missing == 0 and pairing_mismatch == 0,
        "denominator_contract": "classification_coverage uses automatic-eligible direct destination-owner sites; end_to_end_classification_coverage uses every unique semantic allocation candidate; exact (callsite,type_id,module_id) deduplication; Drop rows excluded",
    }


def combine_summaries(summaries: Iterable[dict[str, object]]) -> dict[str, object]:
    items = list(summaries)
    additive = (
        "audit_file_count",
        "classifier_enabled_audit_file_count",
        "semantic_candidate_allocation_site_count",
        "automatic_eligible_allocation_site_count",
        "classified_allocation_site_count",
        "ephemeral_allocation_site_count",
        "long_lived_allocation_site_count",
        "abstained_allocation_site_count",
        "unsupported_allocation_site_count",
        "unknown_allocation_site_count",
        "nonautomatic_allocation_site_count",
        "classified_pairing_checked_count",
        "classified_pairing_missing_drop_count",
        "classified_pairing_mismatch_count",
    )
    totals = {field: sum(int(item[field]) for item in items) for field in additive}
    reasons: Counter[str] = Counter()
    for item in items:
        raw_reasons = item.get("unknown_reason_counts", {})
        if isinstance(raw_reasons, dict):
            reasons.update({str(key): int(value) for key, value in raw_reasons.items()})
    candidates = totals["semantic_candidate_allocation_site_count"]
    eligible = totals["automatic_eligible_allocation_site_count"]
    classified = totals["classified_allocation_site_count"]
    return {
        **totals,
        "classification_coverage": _ratio(classified, eligible),
        "end_to_end_classification_coverage": _ratio(classified, candidates),
        "unknown_reason_counts": dict(sorted(reasons.items())),
        "pairing_consistent": (
            totals["classified_pairing_missing_drop_count"] == 0
            and totals["classified_pairing_mismatch_count"] == 0
        ),
    }


def parse_labeled_root(value: str) -> tuple[str, Path]:
    label, separator, raw_path = value.partition("=")
    if not separator or not label or not raw_path:
        raise argparse.ArgumentTypeError("expected LABEL=/absolute/audit/root")
    return label, Path(raw_path)


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--audit-root",
        action="append",
        required=True,
        type=parse_labeled_root,
        metavar="LABEL=PATH",
        help="recursively aggregate one rustc audit directory; repeat per application",
    )
    parser.add_argument("--out", type=Path, help="optional JSON output path")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(sys.argv[1:] if argv is None else argv)
    try:
        per_application = [
            summarize_audit_root(label, root) for label, root in args.audit_root
        ]
    except SummaryError as error:
        print(f"error: {error}", file=sys.stderr)
        return 2
    document = {
        "schema_version": 1,
        "source": "unialloc-automatic-lifetime-classifier-evaluation",
        "applications": per_application,
        "total": combine_summaries(per_application),
    }
    encoded = json.dumps(document, indent=2, sort_keys=True) + "\n"
    if args.out is not None:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(encoded, encoding="utf-8")
    sys.stdout.write(encoded)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
