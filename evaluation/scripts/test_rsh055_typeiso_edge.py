#!/usr/bin/env python3
"""Focused source-contract regressions for the derived RSH-055 edge."""

from __future__ import annotations

import hashlib
import importlib.util
import pathlib
import unittest


ROOT = pathlib.Path(__file__).resolve().parents[2]
HARNESS = ROOT / "evaluation" / "harnesses" / "rustsec_heap_expansion" / "RSH-055"
VULNERABLE = HARNESS / "derived_reuse.rs"
PATCHED = HARNESS / "derived_reuse_patched.rs"
CATALOG = ROOT / "evaluation/config/rustsec_heap_strong_batch_b_harnesses.json"
RUNNER = ROOT / "evaluation/scripts/run_rustsec_heap_harness.py"

spec = importlib.util.spec_from_file_location("rustsec_heap_harness", RUNNER)
assert spec is not None and spec.loader is not None
runner = importlib.util.module_from_spec(spec)
spec.loader.exec_module(runner)


def sha256(path: pathlib.Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


class Rsh055TypeIsolationEdgeTests(unittest.TestCase):
    def setUp(self) -> None:
        catalog = runner.load_catalog(CATALOG)
        _, scenarios = runner.index_catalog(catalog)
        self.scenario = scenarios["RSH-055-derived-reuse"][1]

    def test_sources_are_hash_pinned_for_catalog_registration(self) -> None:
        self.assertEqual(
            sha256(VULNERABLE),
            "480e5eeb5ead5bd3a0231269c00c005f25a5fed10bc41a5ff132ec0f30553f7a",
        )
        self.assertEqual(
            sha256(PATCHED),
            "b770ca00c0746a4a386649898d6fce4515612a533c06bb162344432fa3673bb8",
        )
        self.assertEqual(
            self.scenario["source_path"], VULNERABLE.relative_to(ROOT).as_posix()
        )
        self.assertEqual(self.scenario["source_sha256"], sha256(VULNERABLE))
        patched = self.scenario["patched_source"]
        self.assertEqual(
            patched["source_path"], PATCHED.relative_to(ROOT).as_posix()
        )
        self.assertEqual(patched["source_sha256"], sha256(PATCHED))

    def test_vulnerable_adapter_preserves_consumed_slot_stale_clone(self) -> None:
        source = VULNERABLE.read_text(encoding="utf-8")

        self.assertIn("struct DropDetector", source)
        self.assertIn("let value = black_box(self.payload[0]);", source)
        self.assertIn("seed.to_vec()", source)
        self.assertIn("black_box(materialize)", source)
        self.assertIn("let consumed = iter.next()", source)
        self.assertIn("fn reclaim_payload<T>(value: T)", source)
        self.assertIn("|| black_box(reclaim)(consumed)", source)
        self.assertIn("struct Replacement(u64);", source)
        self.assertIn("Box::new(Replacement", source)
        self.assertIn("if original_address == replacement_address", source)
        self.assertIn("let cloned = iter.clone();", source)
        self.assertLess(
            source.index("if original_address == replacement_address"),
            source.index("let cloned = iter.clone();"),
        )
        self.assertIn("report_vulnerability_edge_reuse_denial", source)
        self.assertEqual(source.count("0x5253_4837_0000_0001"), 1)
        self.assertEqual(source.count("0x5253_4837_0000_0002"), 1)

    def test_patched_control_uses_one_identity_and_no_foreign_object(self) -> None:
        source = PATCHED.read_text(encoding="utf-8")

        self.assertNotIn("struct Replacement", source)
        self.assertNotIn("Box::new", source)
        self.assertIn("seed.to_vec()", source)
        self.assertNotIn("if original_address == replacement_address", source)
        self.assertIn("let cloned = iter.clone();", source)
        self.assertIn("fn reclaim_payload<T>(value: T)", source)
        self.assertIn("|| black_box(reclaim)(consumed)", source)
        self.assertIn("REPLACEMENT_ALLOC_CALLSITE", source)
        self.assertIn("REPLACEMENT_RECLAIM_CALLSITE", source)
        self.assertGreaterEqual(
            source.count("VICTIM_TYPE_ID,\n        VICTIM_MODULE_ID"), 4
        )
        self.assertIn("report_vulnerability_edge_reuse_denial", source)

    def test_registered_annotation_is_bounded_to_eight_byte_edge(self) -> None:
        expected_annotation = {
            "kind": "manual_exact_vulnerability_edge_identity",
            "expected_layout": {"size": 8, "align": 8},
            "victim_type_id": 0x5253_4837_0000_0001,
            "victim_module_id": 0x5253_4837_0000_0002,
            "replacement_semantic_type_fragment": "Box<witness::Replacement",
            "replacement_source_file_fragment": "src/witness.rs:",
        }
        annotation = self.scenario["type_isolation_edge_annotation"]
        for key, expected in expected_annotation.items():
            self.assertEqual(annotation[key], expected)
        self.assertIn("collision-gated", annotation["claim_scope"])
        exclusion = self.scenario["compiler_target_exclusion"]
        self.assertFalse(exclusion["compiler_automatic_victim_coverage"])
        self.assertIn("automatic mode disables the manual scope", exclusion["reason"])
        self.assertIn("automatic-edge-identity probe", exclusion["claim_boundary"])
        contract = annotation["automatic_compiler_coverage_contract"]
        self.assertEqual(contract["coverage_scope"], "source_shaped_derived")
        self.assertIn("Vec<u64", contract["victim"]["semantic_type_fragment"])
        self.assertIn("Replacement", contract["replacement"]["semantic_type_fragment"])
        self.assertIn("only that equality gates", self.scenario["oracle"]["vulnerable"])
        self.assertIn("the claim stops at this reuse edge", VULNERABLE.read_text())


if __name__ == "__main__":
    unittest.main()
