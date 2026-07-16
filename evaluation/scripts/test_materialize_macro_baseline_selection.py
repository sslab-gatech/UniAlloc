#!/usr/bin/env python3
"""Tests for exact-plan macro baseline view materialization."""

from __future__ import annotations

import json
import pathlib
import tempfile
import unittest

from evaluation.scripts import materialize_macro_baseline_selection as materializer
from evaluation.scripts import run_primary_macro_allocator_baselines as campaign
from evaluation.scripts import type_isolation_suite_contract


class MacroBaselineSelectionMaterializerTests(unittest.TestCase):
    def setUp(self) -> None:
        contract = campaign.load_campaign_contract(
            type_isolation_suite_contract.CURRENT_SUITE_PATH
        )
        protocol = campaign.build_protocol(contract, toolchain="nightly-2026-06-11")
        full_plan = campaign.build_plan(
            protocol,
            target_ids=("redb",),
            variant_ids=("mimalloc",),
        )
        self.plan = campaign.CampaignPlan(
            protocol_fingerprint=full_plan.protocol_fingerprint,
            requested_targets=full_plan.requested_targets,
            requested_variants=full_plan.requested_variants,
            execution_variants=full_plan.execution_variants,
            warmup_rounds=full_plan.warmup_rounds,
            measured_rounds=full_plan.measured_rounds,
            cohorts=full_plan.cohorts,
            cells=(full_plan.cells[0],),
        )

    def write_cell(self, raw: pathlib.Path, *, fingerprint: str | None = None) -> None:
        cell = self.plan.cells[0]
        path = raw / cell.relative_path
        path.parent.mkdir(parents=True)
        path.write_text(
            json.dumps(
                {
                    **cell.identity.as_dict(),
                    "protocol_id": campaign.PROTOCOL_ID,
                    "protocol_fingerprint": self.plan.protocol_fingerprint,
                    "target_fingerprint": cell.target_fingerprint,
                    "variant_fingerprint": cell.variant_fingerprint,
                    "cell_fingerprint": fingerprint or cell.cell_fingerprint,
                    "success": True,
                    "correctness": {"passed": True},
                }
            ),
            encoding="utf-8",
        )

    def test_collection_reads_only_exact_planned_paths(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            raw = pathlib.Path(directory)
            self.write_cell(raw)
            nested = (
                raw / self.plan.cells[0].relative_path
            ).parent / "attempts/measurement-01/work/target/criterion/tukey.json"
            nested.parent.mkdir(parents=True)
            nested.write_text("[]\n", encoding="utf-8")
            index, records = materializer.collect_selected_cells(raw, self.plan)
            self.assertEqual(1, len(index))
            self.assertEqual(1, len(records))
            self.assertEqual(self.plan.cells[0].relative_path, index[0]["path"])

    def test_collection_rejects_cell_identity_drift(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            raw = pathlib.Path(directory)
            self.write_cell(raw, fingerprint="0" * 64)
            with self.assertRaisesRegex(
                campaign.CampaignError, "cell identity mismatch"
            ):
                materializer.collect_selected_cells(raw, self.plan)


if __name__ == "__main__":
    unittest.main()
