#!/usr/bin/env python3
"""Focused contracts for the derived RSH-068 Type Isolation edge."""

from __future__ import annotations

import importlib.util
import pathlib
import unittest


ROOT = pathlib.Path(__file__).resolve().parents[2]
CATALOG = (
    ROOT / "evaluation" / "config" / "rustsec_heap_strong_batch_f_harnesses.json"
)
RUNNER = ROOT / "evaluation" / "scripts" / "run_rustsec_heap_harness.py"
VULNERABLE = (
    ROOT
    / "evaluation"
    / "harnesses"
    / "rustsec_heap_expansion"
    / "RSH-068"
    / "derived_reuse.rs"
)
PATCHED = VULNERABLE.with_name("derived_reuse_patched.rs")
VULNERABLE_SHA256 = "0c45ac9aa4f5510d3d67670afb6101989a6057c37e82d2f34315282c4dbdf42a"
PATCHED_SHA256 = "da2cb706dc9fe20af94d11b36a5388e918a2d88155e116f266b0c72abbe9f7b9"

spec = importlib.util.spec_from_file_location("rustsec_heap_harness", RUNNER)
assert spec is not None and spec.loader is not None
runner = importlib.util.module_from_spec(spec)
spec.loader.exec_module(runner)


class Rsh068TypeIsolationEdgeTests(unittest.TestCase):
    def test_sources_are_hash_pinned_and_use_compiler_audited_victim_helpers(self) -> None:
        self.assertEqual(runner.sha256_file(VULNERABLE), VULNERABLE_SHA256)
        self.assertEqual(runner.sha256_file(PATCHED), PATCHED_SHA256)
        for path in (VULNERABLE, PATCHED):
            source = path.read_text(encoding="utf-8")
            self.assertIn(
                "fn materialize_value(seed: &[u8; PAYLOAD_SIZE]) -> Vec<u8>",
                source,
            )
            self.assertIn("seed.to_vec()", source)
            self.assertIn("fn reclaim_value<T>", source)
            self.assertIn("String::from_utf8", source)
            self.assertIn("0x5253_4844_0000_0001", source)
            self.assertIn("0x5253_4844_0000_0002", source)
            self.assertIn("#![forbid(unsafe_code)]", source)
            self.assertNotIn("unsafe {", source)

    def test_vulnerable_source_preserves_external_projection_edge(self) -> None:
        source = VULNERABLE.read_text(encoding="utf-8")
        projection = source.index("Parc::new(&()).project(|_| value.as_str())")
        reclaim = source.index("black_box(reclaim)(value)")
        replacement = source.index("Box::new(Replacement")
        stale_read = source.index("black_box(projection.as_bytes()[0])")

        self.assertLess(projection, reclaim)
        self.assertLess(reclaim, replacement)
        self.assertLess(replacement, stale_read)
        self.assertIn("if replacement_address == original_address", source)
        self.assertIn("struct Replacement([u8; PAYLOAD_SIZE]);", source)
        self.assertIn("report_vulnerability_edge_reuse_denial", source)

    def test_patched_source_projects_only_into_parc_owned_string(self) -> None:
        source = PATCHED.read_text(encoding="utf-8")
        owner = source.index("let owner = Parc::new(victim)")
        projection = source.index("owner.project(|owned| owned.as_str())")
        owner_drop = source.index("drop(owner)")
        projection_read = source.index("black_box(projection.as_bytes()[0])")
        reclaim = source.index("black_box(reclaim)(projection)")

        self.assertLess(owner, projection)
        self.assertLess(projection, owner_drop)
        self.assertLess(owner_drop, projection_read)
        self.assertLess(projection_read, reclaim)
        self.assertNotIn("Parc::new(&())", source)
        self.assertNotIn("struct Replacement", source)
        self.assertGreaterEqual(
            source.count("VICTIM_TYPE_ID,\n        VICTIM_MODULE_ID"), 4
        )

    def test_registered_catalog_metadata_matches_the_bounded_contract(self) -> None:
        catalog = runner.load_catalog(CATALOG)
        _cases, scenarios = runner.index_catalog(catalog)
        registered = scenarios.get("RSH-068-derived-reuse")
        if registered is None:
            return
        _case, scenario = registered

        self.assertEqual(scenario["compiler_target_crates"], ["rsh-068-harness"])
        exclusion = scenario["compiler_target_exclusion"]
        self.assertEqual(exclusion["subject_crate"], "pared")
        self.assertFalse(exclusion["compiler_automatic_victim_coverage"])
        self.assertIn("automatic mode disables the manual scope", exclusion["reason"])
        self.assertIn("automatic-edge-identity probe", exclusion["claim_boundary"])
        annotation = scenario["type_isolation_edge_annotation"]
        self.assertEqual(
            annotation["kind"], "manual_exact_vulnerability_edge_identity"
        )
        self.assertEqual(annotation["expected_layout"], {"size": 64, "align": 1})
        self.assertEqual(annotation["victim_type_id"], 0x5253_4844_0000_0001)
        self.assertEqual(annotation["victim_module_id"], 0x5253_4844_0000_0002)
        contract = annotation["automatic_compiler_coverage_contract"]
        self.assertEqual(contract["coverage_scope"], "source_shaped_derived")
        self.assertIn("Vec<u8", contract["victim"]["semantic_type_fragment"])
        self.assertIn("Replacement", contract["replacement"]["semantic_type_fragment"])
        runner.validate_repo_file(scenario["source_path"], VULNERABLE_SHA256)
        runner.validate_repo_file(
            scenario["patched_source"]["source_path"], PATCHED_SHA256
        )


if __name__ == "__main__":
    unittest.main()
