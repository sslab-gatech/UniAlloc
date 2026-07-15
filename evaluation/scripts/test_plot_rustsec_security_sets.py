#!/usr/bin/env python3
"""Regression tests for the PPT-ready RustSec set diagrams."""

from __future__ import annotations

import importlib.util
import json
import pathlib
import tempfile
import unittest
import xml.etree.ElementTree as ET


ROOT = pathlib.Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "evaluation/scripts/plot_rustsec_security_sets.py"
SCOPE = ROOT / "evaluation/config/rustsec_heap_complete_scope.json"
CORRECTIONS = (
    ROOT / "evaluation/config/rustsec_heap_posthoc_scope_corrections.json"
)
CHECKED_IN = ROOT / "docs/figures/rustsec-security-sets-20260715"


def load_module():
    spec = importlib.util.spec_from_file_location("plot_rustsec_security_sets", SCRIPT)
    if spec is None or spec.loader is None:
        raise RuntimeError("unable to load plot_rustsec_security_sets")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class RustSecSecuritySetFigureTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.plot = load_module()
        cls.scope = json.loads(SCOPE.read_text(encoding="utf-8"))
        cls.corrections = json.loads(CORRECTIONS.read_text(encoding="utf-8"))
        cls.summary = cls.plot.derive_summary(cls.scope, cls.corrections)

    def test_post_source_audit_scope_removes_stack_only_rsh006(self) -> None:
        self.assertEqual(self.summary["screened_candidate_count"], 53)
        self.assertEqual(self.summary["retained_heap_candidate_count"], 52)
        self.assertEqual(self.summary["executable_heap_case_count"], 48)
        self.assertEqual(
            self.summary["post_source_audit_exclusions"]["case_ids"],
            ["RSH-006"],
        )
        self.assertNotIn(
            "RSH-006", self.summary["full_scope"]["retained_case_ids"]
        )
        rsh006 = next(
            row
            for row in self.summary["membership_rows"]
            if row["case_id"] == "RSH-006"
        )
        self.assertEqual(rsh006["source_ledger_primitive"], "use_after_free")
        self.assertEqual(
            rsh006["presentation_primitive"],
            "stack_lifetime_dangling_target",
        )

    def test_full_scope_sets_preserve_positive_overlap_and_partition(self) -> None:
        full = self.summary["full_scope"]
        self.assertEqual(full["retained_case_count"], 52)
        self.assertEqual(full["executable_case_count"], 48)
        self.assertEqual(full["audit_only_count"], 4)
        self.assertEqual(full["positive_union_count"], 43)
        self.assertEqual(full["type_isolation"]["count"], 12)
        self.assertEqual(full["reclaim_checks"]["count"], 31)
        self.assertEqual(full["recovery_layout_validation"]["count"], 1)
        self.assertEqual(full["type_isolation_only"]["count"], 11)
        self.assertEqual(full["reclaim_checks_only"]["count"], 30)
        self.assertEqual(full["type_isolation_and_reclaim_checks"]["case_ids"], ["RSH-002"])
        self.assertEqual(full["no_allocator_signal"]["count"], 3)
        self.assertEqual(
            full["mechanism_boundary"]["case_ids"], ["RSH-003", "RSH-019"]
        )
        self.assertEqual(
            full["audit_only"]["case_ids"],
            ["RSH-054", "RSH-056", "RSH-059", "RSH-073"],
        )

    def test_uaf_scope_uses_corrected_seventeen_case_denominator(self) -> None:
        uaf = self.summary["uaf_scope"]
        self.assertEqual(uaf["retained_case_count"], 18)
        self.assertEqual(uaf["executable_case_count"], 17)
        self.assertEqual(uaf["audit_only"]["case_ids"], ["RSH-059"])
        self.assertEqual(uaf["positive_union_count"], 14)
        self.assertEqual(uaf["type_isolation_only"]["count"], 11)
        self.assertEqual(uaf["reclaim_checks_only"]["case_ids"], ["RSH-053", "RSH-057"])
        self.assertEqual(uaf["type_isolation_and_reclaim_checks"]["case_ids"], ["RSH-002"])
        self.assertEqual(uaf["no_allocator_signal"]["case_ids"], ["RSH-075"])
        self.assertEqual(
            uaf["mechanism_boundary"]["case_ids"], ["RSH-003", "RSH-019"]
        )

    def test_correction_must_bind_the_frozen_scope_hash(self) -> None:
        mutated = json.loads(json.dumps(self.corrections))
        mutated["base_scope"]["sha256"] = "0" * 64
        with self.assertRaisesRegex(ValueError, "base-scope hash mismatch"):
            self.plot.validate_corrections(
                self.scope,
                mutated,
                scope_path=SCOPE,
            )

    def test_nonpositive_executable_cases_require_explicit_boundaries(self) -> None:
        mutated = json.loads(json.dumps(self.corrections))
        mutated["mechanism_boundaries"] = mutated["mechanism_boundaries"][:1]
        correction_rows = self.plot.validate_corrections(
            self.scope, mutated, scope_path=SCOPE
        )
        boundary_rows = self.plot.validate_mechanism_boundaries(
            self.scope,
            mutated,
            excluded_case_ids={row["case_id"] for row in correction_rows},
        )
        excluded = {row["case_id"] for row in correction_rows}
        executable = [
            row for row in self.scope["cases"] if row["case_id"] not in excluded
        ]
        with self.assertRaisesRegex(
            ValueError, "explicit nonpositive classification.*RSH-019"
        ):
            self.plot._scope_summary(
                executable,
                self.scope["excluded_cases"],
                mechanism_boundary_case_ids={
                    row["case_id"] for row in boundary_rows
                },
            )

    def test_summary_rejects_in_memory_inputs_that_do_not_match_paths(self) -> None:
        mutated = json.loads(json.dumps(self.scope))
        mutated["cases"][0]["package"] = "mutated"
        with self.assertRaisesRegex(ValueError, "scope object does not match"):
            self.plot.derive_summary(mutated, self.corrections)

    def test_bundle_exports_valid_svg_and_machine_readable_membership(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            output = pathlib.Path(directory)
            self.plot.write_bundle(self.summary, output)
            for name in (
                "rustsec-security-sets-overview.svg",
                "rustsec-security-sets-full.svg",
                "rustsec-security-sets-uaf.svg",
            ):
                ET.parse(output / name)
            exported = json.loads(
                (output / "rustsec-security-sets-data.json").read_text(
                    encoding="utf-8"
                )
            )
            self.assertEqual(exported, self.summary)
            manifest = json.loads(
                (output / "artifact-manifest.json").read_text(encoding="utf-8")
            )
            self.assertEqual(len(manifest["artifacts"]), 6)
            for artifact in manifest["artifacts"]:
                path = output / artifact["path"]
                self.assertEqual(path.stat().st_size, artifact["bytes"])
                self.assertEqual(self.plot.sha256_file(path), artifact["sha256"])
            csv_text = (output / "rustsec-security-set-membership.csv").read_text(
                encoding="utf-8"
            )
            self.assertIn("RSH-006", csv_text)
            self.assertIn("post_source_audit_exclusion", csv_text)
            self.assertIn("RSH-002", csv_text)

    def test_checked_in_bundle_matches_deterministic_sources(self) -> None:
        deterministic_names = (
            "README.md",
            "rustsec-security-set-membership.csv",
            "rustsec-security-sets-data.json",
            "rustsec-security-sets-full.svg",
            "rustsec-security-sets-overview.svg",
            "rustsec-security-sets-uaf.svg",
        )
        with tempfile.TemporaryDirectory() as directory:
            output = pathlib.Path(directory)
            self.plot.write_bundle(self.summary, output)
            for name in deterministic_names:
                self.assertEqual(
                    (output / name).read_bytes(),
                    (CHECKED_IN / name).read_bytes(),
                    name,
                )

        overview = (CHECKED_IN / "rustsec-security-sets-overview.svg").read_text(
            encoding="utf-8"
        )
        full = (CHECKED_IN / "rustsec-security-sets-full.svg").read_text(
            encoding="utf-8"
        )
        uaf = (CHECKED_IN / "rustsec-security-sets-uaf.svg").read_text(
            encoding="utf-8"
        )
        self.assertIn("52 reviewed · 48 executable · 4 audit-only", overview)
        self.assertIn("18 reviewed · 17 executable · 1 audit-only", overview)
        self.assertIn("Type Isolation · 12", full)
        self.assertIn("Reclaim checks · 31", full)
        self.assertIn("14/17 covered", uaf)

        manifest = json.loads(
            (CHECKED_IN / "artifact-manifest.json").read_text(encoding="utf-8")
        )
        self.assertEqual(len(manifest["artifacts"]), 12)
        for artifact in manifest["artifacts"]:
            path = CHECKED_IN / artifact["path"]
            self.assertEqual(path.stat().st_size, artifact["bytes"])
            self.assertEqual(self.plot.sha256_file(path), artifact["sha256"])

    def test_nonraster_bundle_removes_stale_raster_outputs(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            output = pathlib.Path(directory)
            stale = output / "rustsec-security-sets-overview.png"
            stale.write_bytes(b"stale")

            self.plot.write_bundle(self.summary, output)

            self.assertFalse(stale.exists())
            manifest = json.loads(
                (output / "artifact-manifest.json").read_text(encoding="utf-8")
            )
            self.assertNotIn(
                stale.name,
                {artifact["path"] for artifact in manifest["artifacts"]},
            )

    def test_pdf_timestamp_normalization_is_deterministic(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = pathlib.Path(directory) / "figure.pdf"
            path.write_bytes(
                b"prefix /CreationDate (D:20260715170000) "
                b"/ModDate (D:20260715170001) suffix"
            )

            self.plot.normalize_pdf_timestamps(path)

            self.assertEqual(
                path.read_bytes(),
                b"prefix /CreationDate (D:20260715000000) "
                b"/ModDate (D:20260715000000) suffix",
            )


if __name__ == "__main__":
    unittest.main()
