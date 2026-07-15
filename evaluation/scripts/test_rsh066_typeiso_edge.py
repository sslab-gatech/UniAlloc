#!/usr/bin/env python3
"""Focused contracts for the derived RSH-066 Type Isolation edge."""

from __future__ import annotations

import importlib.util
import pathlib
import unittest


ROOT = pathlib.Path(__file__).resolve().parents[2]
CATALOG = (
    ROOT / "evaluation" / "config" / "rustsec_heap_strong_batch_c_harnesses.json"
)
RUNNER = ROOT / "evaluation" / "scripts" / "run_rustsec_heap_harness.py"
HARNESS = ROOT / "evaluation" / "harnesses" / "rustsec_heap_expansion" / "RSH-066"
VULNERABLE = HARNESS / "derived_reuse.rs"
PATCHED = HARNESS / "derived_reuse_patched.rs"
VULNERABLE_SHA256 = "149082fe2e1c5d51e51bdd23a0d603895a63a1268fb922235b7acf0af664b71f"
PATCHED_SHA256 = "aaab7486d537889e1df49db364ff0d0f9a358339fcb1ca7cbb50d77d77f74151"

spec = importlib.util.spec_from_file_location("rustsec_heap_harness", RUNNER)
assert spec is not None and spec.loader is not None
runner = importlib.util.module_from_spec(spec)
spec.loader.exec_module(runner)


class Rsh066TypeIsolationEdgeTests(unittest.TestCase):
    def setUp(self) -> None:
        catalog = runner.load_catalog(CATALOG)
        _cases, scenarios = runner.index_catalog(catalog)
        self.scenario = scenarios["RSH-066-derived-reuse"][1]

    def test_sources_are_hash_pinned_for_catalog_registration(self) -> None:
        self.assertEqual(runner.sha256_file(VULNERABLE), VULNERABLE_SHA256)
        self.assertEqual(runner.sha256_file(PATCHED), PATCHED_SHA256)
        runner.validate_repo_file(self.scenario["source_path"], VULNERABLE_SHA256)
        runner.validate_repo_file(
            self.scenario["patched_source"]["source_path"], PATCHED_SHA256
        )

    def test_vulnerable_adapter_preserves_old_owner_stale_read(self) -> None:
        source = VULNERABLE.read_text(encoding="utf-8")

        self.assertIn("let stale = current.as_str();", source)
        self.assertIn("let old_owner = current.replace", source)
        self.assertIn("black_box(reclaim)(old_owner)", source)
        self.assertIn("struct Replacement([u8; PAYLOAD_SIZE]);", source)
        self.assertIn("Box::new(Replacement", source)
        self.assertIn("if original_address == replacement_address", source)
        self.assertIn("stale.as_bytes()[0]", source)
        self.assertIn("report_vulnerability_edge_reuse_denial", source)
        self.assertEqual(source.count("0x5253_4842_0000_0001"), 1)
        self.assertEqual(source.count("0x5253_4842_0000_0002"), 1)

    def test_victim_materialization_avoids_harness_identity_shadowing(self) -> None:
        vulnerable = VULNERABLE.read_text(encoding="utf-8")
        patched = PATCHED.read_text(encoding="utf-8")
        self.assertIn("fn materialize_string(byte: u8) -> String", vulnerable)
        self.assertIn("fn materialize_atomic(value: String) -> AtomicStr", vulnerable)
        self.assertIn("let victim_seed = materialize_string(b'A');", vulnerable)
        self.assertIn("fn materialize_atomic(value: &str) -> AtomicStr", patched)
        for source in (vulnerable, patched):
            self.assertIn("fn reclaim_value<T>(value: T)", source)
            self.assertIn("black_box(materialize)", source)
            self.assertIn("black_box(reclaim)", source)
            self.assertIn("#![forbid(unsafe_code)]", source)
            self.assertNotIn("unsafe {", source)
        self.assertIn("let reclaim: fn(Arc<String>) = reclaim_value;", vulnerable)
        self.assertNotIn("|| drop(old_owner)", vulnerable)

    def test_patched_control_releases_guard_before_same_identity_reuse(self) -> None:
        source = PATCHED.read_text(encoding="utf-8")

        self.assertNotIn("struct Replacement", source)
        self.assertNotIn("stale", source)
        self.assertIn("let guard = victim.as_str();", source)
        self.assertLess(
            source.index("drop(guard);"),
            source.index("black_box(reclaim)(victim)"),
        )
        self.assertLess(
            source.index('let replacement_seed = "B".repeat(PAYLOAD_SIZE);'),
            source.index("black_box(reclaim)(victim)"),
        )
        self.assertIn("REPLACEMENT_ALLOC_CALLSITE", source)
        self.assertIn("REPLACEMENT_RECLAIM_CALLSITE", source)
        self.assertGreaterEqual(
            source.count("VICTIM_TYPE_ID,\n        VICTIM_MODULE_ID"), 4
        )
        self.assertIn("report_vulnerability_edge_reuse_denial", source)

    def test_registered_annotation_is_bounded_to_string_buffer_edge(self) -> None:
        self.assertEqual(self.scenario["compiler_target_crates"], ["rsh-066-harness"])
        exclusion = self.scenario["compiler_target_exclusion"]
        self.assertEqual(exclusion["subject_crate"], "rust-i18n-support")
        self.assertFalse(exclusion["compiler_automatic_victim_coverage"])
        self.assertIn("automatic mode disables the manual scope", exclusion["reason"])
        self.assertIn("automatic-edge-identity probe", exclusion["claim_boundary"])

        annotation = self.scenario["type_isolation_edge_annotation"]
        self.assertEqual(
            annotation["kind"], "manual_exact_vulnerability_edge_identity"
        )
        self.assertEqual(annotation["expected_layout"], {"size": 4096, "align": 1})
        self.assertEqual(annotation["victim_type_id"], 0x5253_4842_0000_0001)
        self.assertEqual(annotation["victim_module_id"], 0x5253_4842_0000_0002)
        self.assertEqual(
            annotation["replacement_semantic_type_fragment"],
            "Box<witness::Replacement",
        )
        contract = annotation["automatic_compiler_coverage_contract"]
        self.assertEqual(contract["coverage_scope"], "source_shaped_derived")
        self.assertIn("String", contract["victim"]["semantic_type_fragment"])
        self.assertIn("Replacement", contract["replacement"]["semantic_type_fragment"])
        self.assertIn("the claim stops at this reuse edge", VULNERABLE.read_text())


if __name__ == "__main__":
    unittest.main()
