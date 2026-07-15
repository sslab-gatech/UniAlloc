#!/usr/bin/env python3
"""Focused contracts for bounded RSH-065 and RSH-069 Type Isolation edges."""

from __future__ import annotations

import importlib.util
import pathlib
import unittest


ROOT = pathlib.Path(__file__).resolve().parents[2]
RUNNER = ROOT / "evaluation/scripts/run_rustsec_heap_harness.py"
CATALOGS = {
    "RSH-065": ROOT / "evaluation/config/rustsec_heap_strong_batch_c_harnesses.json",
    "RSH-069": ROOT / "evaluation/config/rustsec_heap_strong_batch_f_harnesses.json",
}

spec = importlib.util.spec_from_file_location("rustsec_heap_harness", RUNNER)
assert spec is not None and spec.loader is not None
runner = importlib.util.module_from_spec(spec)
spec.loader.exec_module(runner)


class MediumDerivedTypeIsolationEdgeTests(unittest.TestCase):
    def scenario(self, case_id: str) -> dict[str, object]:
        catalog = runner.load_catalog(CATALOGS[case_id])
        _, scenarios = runner.index_catalog(catalog)
        return scenarios[f"{case_id}-derived-reuse"][1]

    def test_manual_edges_have_structured_compiler_exclusions(self) -> None:
        expected = {
            "RSH-065": (
                "mail-internals",
                {"size": 4, "align": 1},
                0x5253_4865_0000_0001,
                0x5253_4865_0000_0002,
                "8123e1c70af07184d3896e1220b1de242319f98845a13a2f0c27098a4dcb1a53",
                "2cbc7f945b9961f6d022897ac54191aacfc239c3dea77969f252ba326270070f",
            ),
            "RSH-069": (
                "openssl",
                {"size": 4096, "align": 1},
                0x5253_4869_0000_0001,
                0x5253_4869_0000_0002,
                "59156d8f175c07124388b9dbf4befd91e6a66af79ccd5318137fdef92a7129a5",
                "447cf102407be6753715fa2f7cabce26b82dc8631d099072709e0d44f17c1f01",
            ),
        }
        for case_id, (
            subject,
            layout,
            type_id,
            module_id,
            vulnerable_hash,
            patched_hash,
        ) in expected.items():
            with self.subTest(case_id=case_id):
                scenario = self.scenario(case_id)
                self.assertEqual(
                    scenario["compiler_target_crates"],
                    [f"{case_id.lower()}-harness"],
                )
                exclusion = scenario["compiler_target_exclusion"]
                self.assertEqual(exclusion["subject_crate"], subject)
                self.assertFalse(exclusion["compiler_automatic_victim_coverage"])
                self.assertIn("manual", exclusion["reason"])
                self.assertIn("Indirect generic", exclusion["reason"])
                self.assertIn(
                    "automatic compiler victim coverage", exclusion["claim_boundary"]
                )
                annotation = scenario["type_isolation_edge_annotation"]
                self.assertEqual(annotation["expected_layout"], layout)
                self.assertEqual(
                    annotation["kind"],
                    "manual_exact_vulnerability_edge_identity",
                )
                self.assertEqual(annotation["victim_type_id"], type_id)
                self.assertEqual(annotation["victim_module_id"], module_id)
                self.assertEqual(scenario["source_sha256"], vulnerable_hash)
                self.assertEqual(
                    scenario["patched_source"]["source_sha256"], patched_hash
                )
                self.assertIn(
                    "cross-identity reuse edge only", annotation["claim_scope"]
                )
                self.assertIn(
                    "only that equality gates", scenario["oracle"]["vulnerable"]
                )

    def test_rsh065_preserves_reserve_to_stale_copy_and_safe_control(self) -> None:
        scenario = self.scenario("RSH-065")
        vulnerable_path = runner.validate_repo_file(
            scenario["source_path"], scenario["source_sha256"]
        )
        patched = scenario["patched_source"]
        patched_path = runner.validate_repo_file(
            patched["source_path"], patched["source_sha256"]
        )
        vulnerable = vulnerable_path.read_text(encoding="utf-8")
        safe = patched_path.read_text(encoding="utf-8")

        self.assertIn("seed.to_vec()", vulnerable)
        self.assertIn("black_box(materialize)", vulnerable)
        self.assertIn("fn reclaim_target<T>(value: T)", vulnerable)
        self.assertNotIn("reserve_exact", vulnerable)
        self.assertLess(
            vulnerable.index("let stale"),
            vulnerable.index("let mut grown ="),
        )
        self.assertLess(
            vulnerable.index("black_box(reclaim)(target)"),
            vulnerable.index("let replacement ="),
        )
        self.assertIn("ptr::copy(", vulnerable)
        self.assertLess(
            vulnerable.index("if original_address == replacement_address"),
            vulnerable.index("ptr::copy("),
        )
        self.assertNotIn("splice", safe)
        self.assertIn('black_box(*b"abXcd")', safe)
        self.assertIn("seed.to_vec()", safe)
        self.assertIn("black_box(materialize)", safe)
        self.assertNotIn("ptr::copy(", safe)
        self.assertGreaterEqual(safe.count("VICTIM_TYPE_ID,"), 4)

    def test_rsh069_preserves_consume_free_to_ffi_read_and_safe_control(self) -> None:
        scenario = self.scenario("RSH-069")
        vulnerable_path = runner.validate_repo_file(
            scenario["source_path"], scenario["source_sha256"]
        )
        patched = scenario["patched_source"]
        patched_path = runner.validate_repo_file(
            patched["source_path"], patched["source_sha256"]
        )
        vulnerable = vulnerable_path.read_text(encoding="utf-8")
        safe = patched_path.read_text(encoding="utf-8")

        self.assertIn("owner.map_or", vulnerable)
        self.assertIn("seed.to_vec()", vulnerable)
        self.assertIn("fn consume_owner<T, R: Copy>", vulnerable)
        self.assertLess(
            vulnerable.index("black_box(consume)"),
            vulnerable.index("let mut replacement ="),
        )
        self.assertIn("CStr::from_ptr(stale)", vulnerable)
        self.assertLess(
            vulnerable.index("if original_address == replacement_address"),
            vulnerable.index("CStr::from_ptr(stale)"),
        )
        self.assertNotIn("consume_owner", safe)
        self.assertNotIn("fn cstring_from_seed", safe)
        self.assertNotIn("Box::new", safe)
        self.assertIn(
            "let victim_bytes = crate::with_vulnerability_edge_identity", safe
        )
        self.assertIn(
            "let replacement_bytes = crate::with_vulnerability_edge_identity", safe
        )
        self.assertIn("black_box(materialize)", safe)
        self.assertIn("assert_eq!(original_address, victim_backing_address)", safe)
        self.assertIn(
            "assert_eq!(replacement.as_ptr() as usize, replacement_backing_address)",
            safe,
        )
        self.assertLess(
            safe.index("CStr::from_ptr(victim.as_ptr())"),
            safe.index("let reclaim: fn(CString)"),
        )
        self.assertGreaterEqual(safe.count("VICTIM_TYPE_ID,"), 4)


if __name__ == "__main__":
    unittest.main()
