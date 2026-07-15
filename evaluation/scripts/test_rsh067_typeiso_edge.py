#!/usr/bin/env python3
"""Focused contracts for the derived RSH-067 Type Isolation edge."""

from __future__ import annotations

import importlib.util
import pathlib
import unittest


ROOT = pathlib.Path(__file__).resolve().parents[2]
CATALOG = (
    ROOT / "evaluation" / "config" / "rustsec_heap_strong_batch_c_harnesses.json"
)
RUNNER = ROOT / "evaluation" / "scripts" / "run_rustsec_heap_harness.py"
VULNERABLE = (
    ROOT
    / "evaluation"
    / "harnesses"
    / "rustsec_heap_expansion"
    / "RSH-067"
    / "derived_reuse.rs"
)
PATCHED = VULNERABLE.with_name("derived_reuse_patched.rs")
VULNERABLE_SHA256 = "5bc90e106316eeb2cd67876dea882360400d49f0b0915bdf45c4cdba7068ae4e"
PATCHED_SHA256 = "1ddbec25a9cdb9885ab04a67a91a177d31f31840885e61ed4412665d93f1a09d"

spec = importlib.util.spec_from_file_location("rustsec_heap_harness", RUNNER)
assert spec is not None and spec.loader is not None
runner = importlib.util.module_from_spec(spec)
spec.loader.exec_module(runner)


class Rsh067TypeIsolationEdgeTests(unittest.TestCase):
    def test_sources_are_hash_pinned_and_use_generic_victim_helpers(self) -> None:
        self.assertEqual(runner.sha256_file(VULNERABLE), VULNERABLE_SHA256)
        self.assertEqual(runner.sha256_file(PATCHED), PATCHED_SHA256)
        for path in (VULNERABLE, PATCHED):
            source = path.read_text(encoding="utf-8")
            self.assertIn("fn materialize_server<T: Clone", source)
            self.assertIn("seed.to_vec()", source)
            self.assertIn("fn reclaim_server<T>", source)
            self.assertIn("0x5253_4843_0000_0001", source)
            self.assertIn("0x5253_4843_0000_0002", source)
            self.assertIn("#![forbid(unsafe_code)]", source)
            self.assertNotIn("unsafe {", source)

    def test_vulnerable_source_preserves_free_then_stale_access_edge(self) -> None:
        source = VULNERABLE.read_text(encoding="utf-8")
        select = source.index("ssl::select_next_proto(&server")
        reclaim = source.index("black_box(reclaim)(server)")
        replacement = source.index("Box::new(Replacement")
        stale_read = source.index("black_box(selected[0])")

        self.assertLess(select, reclaim)
        self.assertLess(reclaim, replacement)
        self.assertLess(replacement, stale_read)
        self.assertIn("if replacement_address == original_address", source)
        self.assertIn("struct Replacement([u8; PAYLOAD_SIZE]);", source)
        self.assertIn("report_vulnerability_edge_reuse_denial", source)

    def test_patched_source_is_safe_same_identity_vec_reuse(self) -> None:
        source = PATCHED.read_text(encoding="utf-8")
        selected_read = source.index("black_box(selected[0])")
        victim_reclaim = source.index("black_box(reclaim)(victim)")
        replacement = source.index("let replacement = crate::with_vulnerability_edge_identity")

        self.assertLess(selected_read, victim_reclaim)
        self.assertLess(victim_reclaim, replacement)
        self.assertNotIn("struct Replacement", source)
        self.assertNotIn("selected[0]", source[victim_reclaim:])
        self.assertGreaterEqual(
            source.count("VICTIM_TYPE_ID,\n        VICTIM_MODULE_ID"), 4
        )

    def test_registered_catalog_metadata_matches_the_bounded_contract(self) -> None:
        catalog = runner.load_catalog(CATALOG)
        _cases, scenarios = runner.index_catalog(catalog)
        registered = scenarios.get("RSH-067-derived-reuse")
        if registered is None:
            return
        _case, scenario = registered

        self.assertEqual(scenario["compiler_target_crates"], ["rsh-067-harness"])
        exclusion = scenario["compiler_target_exclusion"]
        self.assertEqual(exclusion["subject_crate"], "openssl")
        self.assertFalse(exclusion["compiler_automatic_victim_coverage"])
        self.assertIn("manual", exclusion["reason"])
        self.assertIn("allocator policy", exclusion["claim_boundary"])
        annotation = scenario["type_isolation_edge_annotation"]
        self.assertEqual(
            annotation["kind"], "manual_exact_vulnerability_edge_identity"
        )
        self.assertEqual(annotation["expected_layout"], {"size": 64, "align": 1})
        self.assertEqual(annotation["victim_type_id"], 0x5253_4843_0000_0001)
        self.assertEqual(annotation["victim_module_id"], 0x5253_4843_0000_0002)
        runner.validate_repo_file(scenario["source_path"], VULNERABLE_SHA256)
        runner.validate_repo_file(
            scenario["patched_source"]["source_path"], PATCHED_SHA256
        )


if __name__ == "__main__":
    unittest.main()
