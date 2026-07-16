#!/usr/bin/env python3
"""Materialize one macro baseline selection from immutable planned cells."""

from __future__ import annotations

import argparse
import json
import pathlib
import sys
from collections.abc import Mapping, Sequence
from typing import Any

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[2]))

from evaluation.scripts import immutable_evidence  # noqa: E402
from evaluation.scripts import (  # noqa: E402
    run_primary_macro_allocator_baselines as campaign,
)

SCHEMA_VERSION = 1


def load_object(path: pathlib.Path, context: str) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise campaign.CampaignError(f"{context} is invalid: {path}") from error
    if not isinstance(value, dict):
        raise campaign.CampaignError(f"{context} must be a JSON object: {path}")
    return value


def reconstruct_plan(value: Mapping[str, Any]) -> campaign.CampaignPlan:
    cells: list[campaign.PlannedCell] = []
    raw_cells = value.get("cells")
    raw_cohorts = value.get("cohorts")
    if not isinstance(raw_cells, list) or not isinstance(raw_cohorts, list):
        raise campaign.CampaignError("selection plan cells or cohorts are missing")
    for raw in raw_cells:
        if not isinstance(raw, dict):
            raise campaign.CampaignError("selection plan cell is invalid")
        identity = campaign.CellIdentity(
            str(raw.get("cohort_id")),
            str(raw.get("target_id")),
            str(raw.get("harness_id")),
            str(raw.get("variant")),
            str(raw.get("phase")),
            int(raw.get("round")),
        )
        cells.append(
            campaign.PlannedCell(
                identity=identity,
                relative_path=str(raw.get("relative_path")),
                target_fingerprint=str(raw.get("target_fingerprint")),
                variant_fingerprint=str(raw.get("variant_fingerprint")),
                cell_fingerprint=str(raw.get("cell_fingerprint")),
            )
        )
    cohorts: list[campaign.CohortPlan] = []
    for raw in raw_cohorts:
        if not isinstance(raw, dict) or not isinstance(
            raw.get("variant_fingerprints"), dict
        ):
            raise campaign.CampaignError("selection plan cohort is invalid")
        cohorts.append(
            campaign.CohortPlan(
                target_id=str(raw.get("target_id")),
                subject_variant=str(raw.get("subject_variant")),
                variants=tuple(str(item) for item in raw.get("variants", ())),
                cohort_id=str(raw.get("cohort_id")),
                target_fingerprint=str(raw.get("target_fingerprint")),
                variant_fingerprints={
                    str(key): str(digest)
                    for key, digest in raw["variant_fingerprints"].items()
                },
            )
        )
    plan = campaign.CampaignPlan(
        protocol_fingerprint=str(value.get("protocol_fingerprint")),
        requested_targets=tuple(
            str(item) for item in value.get("requested_targets", ())
        ),
        requested_variants=tuple(
            str(item) for item in value.get("requested_variants", ())
        ),
        execution_variants=tuple(
            str(item) for item in value.get("execution_variants", ())
        ),
        warmup_rounds=int(value.get("warmup_rounds")),
        measured_rounds=int(value.get("measured_rounds")),
        cohorts=tuple(cohorts),
        cells=tuple(cells),
    )
    selection = campaign._selection_fingerprint(plan)
    if (
        value.get("schema_version") != 1
        or value.get("protocol_id") != campaign.PROTOCOL_ID
        or value.get("selection_fingerprint") != selection
        or value.get("cell_count") != len(cells)
        or len({cell.relative_path for cell in cells}) != len(cells)
    ):
        raise campaign.CampaignError("selection plan identity is invalid")
    return plan


def collect_selected_cells(
    raw_dir: pathlib.Path, plan: campaign.CampaignPlan
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    index_rows: list[dict[str, Any]] = []
    records: list[dict[str, Any]] = []
    for cell in sorted(plan.cells, key=lambda item: item.relative_path):
        path = raw_dir / cell.relative_path
        row = load_object(path, "planned measurement cell")
        identity = cell.identity.as_dict()
        if (
            any(row.get(key) != expected for key, expected in identity.items())
            or row.get("protocol_id") != campaign.PROTOCOL_ID
            or row.get("protocol_fingerprint") != plan.protocol_fingerprint
            or row.get("target_fingerprint") != cell.target_fingerprint
            or row.get("variant_fingerprint") != cell.variant_fingerprint
            or row.get("cell_fingerprint") != cell.cell_fingerprint
            or row.get("success") is not True
            or not isinstance(row.get("correctness"), dict)
            or row["correctness"].get("passed") is not True
        ):
            raise campaign.CampaignError(
                f"planned measurement cell identity mismatch: {path}"
            )
        index_rows.append(
            {
                "path": cell.relative_path,
                "sha256": campaign.sha256_file(path),
                **identity,
                "protocol_fingerprint": plan.protocol_fingerprint,
            }
        )
        records.append(row)
    if len(records) != len(plan.cells):
        raise campaign.CampaignError("planned measurement cell set is incomplete")
    return index_rows, records


def materialize(
    *, suite_path: pathlib.Path, raw_dir: pathlib.Path, selection: str
) -> pathlib.Path:
    raw_dir = raw_dir.resolve()
    plan_path = raw_dir / "selections" / selection / "plan.json"
    plan_value = load_object(plan_path, "selection plan")
    plan = reconstruct_plan(plan_value)
    if campaign._selection_fingerprint(plan) != selection:
        raise campaign.CampaignError("requested selection differs from its plan")
    contract = campaign.load_campaign_contract(suite_path.resolve())
    if plan.requested_targets != tuple(contract.targets):
        raise campaign.CampaignError("selection targets differ from the suite")
    index_rows, cells = collect_selected_cells(raw_dir, plan)
    index_path = raw_dir / "cells.jsonl"
    immutable_evidence.atomic_write_bytes(
        index_path,
        "".join(json.dumps(row, sort_keys=True) + "\n" for row in index_rows).encode(
            "utf-8"
        ),
    )
    summary_path = campaign.persist_summary_view(
        raw_dir=raw_dir,
        plan=plan,
        contract=contract,
        cells=cells,
    )
    script_path = pathlib.Path(__file__).resolve()
    manifest_path = summary_path.parent / "view-materialization.json"
    campaign.write_json(
        manifest_path,
        {
            "schema_version": SCHEMA_VERSION,
            "selection_fingerprint": selection,
            "materializer": {
                "path": str(script_path),
                "sha256": campaign.sha256_file(script_path),
            },
            "plan": {
                "path": str(plan_path.resolve()),
                "sha256": campaign.sha256_file(plan_path),
            },
            "cell_index": {
                "path": str(index_path.resolve()),
                "sha256": campaign.sha256_file(index_path),
                "cell_count": len(index_rows),
            },
            "summary": {
                "path": str(summary_path.resolve()),
                "sha256": campaign.sha256_file(summary_path),
            },
            "raw_cell_policy": (
                "read exactly the immutable relative paths declared by the selected "
                "plan; ignore nested adapter and Criterion JSON artifacts"
            ),
            "raw_cells_mutated": False,
        },
    )
    return manifest_path


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--suite", type=pathlib.Path, required=True)
    parser.add_argument("--raw-dir", type=pathlib.Path, required=True)
    parser.add_argument("--selection", required=True)
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    print(
        materialize(
            suite_path=args.suite,
            raw_dir=args.raw_dir,
            selection=args.selection,
        )
    )
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except campaign.CampaignError as error:
        print(f"error: {error}")
        raise SystemExit(2) from error
