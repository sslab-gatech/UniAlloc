#!/usr/bin/env python3
"""Regression tests for the dedicated RSH-064 Neon Node/V8 runner."""

from __future__ import annotations

import contextlib
import importlib.util
import io
import pathlib
import tempfile
import unittest
from unittest import mock


ROOT = pathlib.Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "evaluation" / "scripts" / "run_rsh064_neon_witness.py"
CATALOG = (
    ROOT / "evaluation" / "config" / "rustsec_heap_neon_node_harnesses.json"
)
CASE_DIR = ROOT / "evaluation" / "harnesses" / "rustsec_heap_expansion" / "RSH-064"

spec = importlib.util.spec_from_file_location("rsh064_neon_runner", SCRIPT)
assert spec is not None and spec.loader is not None
runner = importlib.util.module_from_spec(spec)
spec.loader.exec_module(runner)


class Rsh064NeonRunnerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.catalog = runner.load_catalog(CATALOG)

    def test_catalog_pins_exact_case_sources_archives_and_locks(self) -> None:
        case, scenario = runner.selected_case(self.catalog)
        self.assertEqual(case["case_id"], "RSH-064")
        self.assertEqual(case["advisory_id"], "RUSTSEC-2022-0028")
        self.assertEqual(case["primary_primitive"], "use_after_free")
        self.assertEqual(scenario["host_kind"], "node_napi_addon")
        self.assertEqual(scenario["expected_payload"], [0, 1, 2, 3])
        self.assertEqual(
            scenario["supported_allocator_variants"], list(runner.VARIANTS)
        )
        self.assertEqual(
            case["vulnerable_dependency"]["sha256"],
            "544694d02bbff81f78dba5ef7d29341cc6d256edcae4fbb2d684491d5755c748",
        )
        self.assertEqual(
            case["patched_dependency"]["sha256"],
            "28e15415261d880aed48122e917a45e87bb82cf0260bb6db48bbab44b7464373",
        )
        self.assertEqual(
            case["reclaim_spin_dependency"]["sha256"],
            "b87bbf98cb81332a56c1ee8929845836f85e8ddd693157c30d76660196014478",
        )
        expected_locks = {
            "vulnerable": "c6f58fb877d2646feb7cc694d08041f82363b91bce767a6a44d97cc2f4219032",
            "patched": "6f9609ddfbe9c596b0466303a358be5c927883e7773934e8ce8b4bb994104f49",
            "reclaim": "7f5e873ef95f07890863ef1e5a72b9976176298e274ca23a0429216042208a75",
            "legacy_vulnerable": "015cbde1abe37ab4f69e3d1ec9fff559104e7550a63f3e787605851c141a459c",
            "legacy_patched": "bcee7db236b011d959630789b7550c7c1951c5260b3ca9a77d8ab4ac7811c95d",
        }
        self.assertEqual(
            {name: value["sha256"] for name, value in case["lockfiles"].items()},
            expected_locks,
        )

    def test_catalog_keeps_published_node_witness_and_separate_derived_edge(self) -> None:
        _cases, scenarios = runner.harness.index_catalog(self.catalog)
        self.assertEqual(
            set(scenarios),
            {"RSH-064-node-addon", "RSH-064-derived-reuse"},
        )
        _case, derived = scenarios["RSH-064-derived-reuse"]
        self.assertEqual(derived["adapter_kind"], "derived_adapter")
        self.assertEqual(
            derived["classification_role"], "derived_reuse_experiment"
        )
        self.assertEqual(derived["oracle"]["tool"], "native_address_trace")
        annotation = derived["type_isolation_edge_annotation"]
        self.assertEqual(
            annotation["kind"], "manual_exact_vulnerability_edge_identity"
        )
        self.assertEqual(annotation["expected_layout"], {"size": 4, "align": 1})
        self.assertIn("Vec<u8>", annotation["claim_scope"])

        vulnerable = (ROOT / derived["source_path"]).read_text(encoding="utf-8")
        patched_meta = derived["patched_source"]
        patched = (ROOT / patched_meta["source_path"]).read_text(encoding="utf-8")
        self.assertIn("with_vulnerability_edge_identity", vulnerable)
        self.assertIn("report_vulnerability_edge_reuse_denial", vulnerable)
        self.assertIn("unsafe { external.read_after_owner_release() }", vulnerable)
        self.assertIn("Box::new(Replacement", vulnerable)
        self.assertIn("black_box(materialize)", vulnerable)
        self.assertIn("black_box(reclaim)", vulnerable)
        self.assertIn("with_vulnerability_edge_identity", patched)
        self.assertIn("report_vulnerability_edge_reuse_denial", patched)
        self.assertIn("black_box(materialize)", patched)
        self.assertIn("black_box(reclaim)", patched)
        self.assertNotIn("ExternalView", patched)
        self.assertNotIn("unsafe", patched)

    def test_addon_and_node_host_match_integrated_live_proof(self) -> None:
        addon = (CASE_DIR / "addon.rs").read_text(encoding="utf-8")
        driver = (CASE_DIR / "invoke.js").read_text(encoding="utf-8")
        published = (CASE_DIR / "published_witness.rs").read_text(encoding="utf-8")
        self.assertIn(
            "let buf = JsArrayBuffer::external(&mut cx, data.as_mut_slice());",
            addon,
        )
        self.assertIn("drop(data);", addon)
        self.assertIn('#[neon::main]', addon)
        self.assertIn('cx.export_function("soundnessHole", soundness_hole)?;', addon)
        self.assertIn("JsArrayBuffer::external(&mut cx, data.as_mut_slice())", published)
        self.assertIn("const addon = require(process.argv[2]);", driver)
        self.assertIn("Buffer.alloc(4096, i & 255)", driver)

    def test_plan_uses_one_patched_control_and_all_vulnerable_variants(self) -> None:
        arms = runner.planned_arms(runner.VARIANTS)
        self.assertEqual(
            arms[0],
            {
                "archive_variant": "patched",
                "allocator_variant": "system",
                "oracle": "expected_compile_rejection",
            },
        )
        self.assertEqual(
            [arm["allocator_variant"] for arm in arms[1:]],
            list(runner.VARIANTS),
        )

    def test_manifest_and_source_transformations_are_mechanism_specific(self) -> None:
        case, scenario = runner.selected_case(self.catalog)
        base = (ROOT / scenario["source"]["path"]).read_text(encoding="utf-8")
        subject = pathlib.Path("/tmp/subject/neon-0.10.0")
        system_manifest = runner.manifest_text(
            dependency=case["vulnerable_dependency"],
            subject=subject,
            allocator_variant="system",
        )
        reclaim_manifest = runner.manifest_text(
            dependency=case["vulnerable_dependency"],
            subject=subject,
            allocator_variant="reclaim_checks",
            spin_subject=pathlib.Path("/tmp/spin/spin-0.9.0"),
        )
        self.assertIn('crate-type = ["cdylib"]', system_manifest)
        self.assertIn('features = ["napi-6"]', system_manifest)
        self.assertNotIn("unialloc =", system_manifest)
        self.assertIn('features = ["stats", "reclaim_checks"]', reclaim_manifest)
        self.assertIn('[patch.crates-io]', reclaim_manifest)
        self.assertEqual(runner.allocator_source(base, "typed_plain"), base)
        self.assertEqual(runner.allocator_source(base, "typeiso"), base)
        transformed = runner.allocator_source(base, "reclaim_checks")
        self.assertTrue(transformed.startswith(base.rstrip()))
        self.assertIn("#[global_allocator]", transformed)
        self.assertIn("static ALLOCATOR: UniAlloc = UniAlloc;", transformed)

    def test_corrupt_byte_oracle_binds_the_initialized_payload(self) -> None:
        corrupt = runner.parse_node_output(
            "byteLength=4 bytes=178,125,131,73\nafterChurn=110,111,100,101\n",
            [0, 1, 2, 3],
        )
        clean = runner.parse_node_output(
            "byteLength=4 bytes=0,1,2,3\nafterChurn=0,1,2,3\n",
            [0, 1, 2, 3],
        )
        malformed = runner.parse_node_output("no addon record\n", [0, 1, 2, 3])
        self.assertEqual(corrupt["status"], "corrupt_bytes_observed")
        self.assertTrue(corrupt["observed"])
        self.assertEqual(clean["status"], "expected_payload_preserved")
        self.assertFalse(clean["observed"])
        self.assertEqual(malformed["status"], "malformed_node_output")

    def test_patched_control_requires_both_borrow_errors_and_static_bound(self) -> None:
        stderr = (
            "error[E0597]: data does not live long enough\n"
            "error[E0505]: cannot move out of data\n"
            "T: Send + 'static\n"
        )
        expected = runner.classify_compile_rejection(101, stderr)
        incomplete = runner.classify_compile_rejection(101, "error[E0597]\n")
        self.assertEqual(
            expected["status"], "patched_control_compile_rejection_observed"
        )
        self.assertTrue(expected["observed"])
        self.assertEqual(
            incomplete["status"], "expected_compile_rejection_not_observed"
        )
        self.assertFalse(incomplete["observed"])

    def test_result_classifies_reclaim_no_signal_and_typeiso_coverage_inconclusive(self) -> None:
        def fake_arm(*_args, arm, **_kwargs):
            archive = arm["archive_variant"]
            allocator = arm["allocator_variant"]
            if archive == "patched":
                return {
                    "archive_variant": archive,
                    "allocator_variant": allocator,
                    "oracle_validation": {"observed": True},
                }
            result = {
                "archive_variant": archive,
                "allocator_variant": allocator,
                "oracle_validation": {"observed": True},
                "repetition_summary": {
                    "exact_reclaim_signal_observed": 0,
                    "reuse_denial_signal_observed": 0,
                },
            }
            if allocator == "typeiso":
                result["compiler_evidence"] = {
                    "harness_summary": {
                        "complete_for_claim": False,
                        "rewrite_applied_count": 0,
                        "semantic_scope_unsolved_candidate_count": 3,
                    }
                }
            return result

        with tempfile.TemporaryDirectory() as temporary:
            root = pathlib.Path(temporary)
            with (
                mock.patch.object(runner, "run_arm", side_effect=fake_arm),
                mock.patch.object(
                    runner.rustsec_experiment,
                    "prepare_typeiso_tools",
                    return_value={"tools": "prepared"},
                ),
                mock.patch.object(
                    runner,
                    "input_fingerprint",
                    return_value={
                        "catalog_sha256": "a" * 64,
                        "runner_sha256": "b" * 64,
                        "unialloc_implementation_sha256": "c" * 64,
                    },
                ),
            ):
                result = runner.run_experiment(
                    self.catalog,
                    catalog_path=CATALOG,
                    variants=runner.VARIANTS,
                    archive_cache=root / "cache",
                    allow_download=False,
                    output_dir=root,
                    node="node",
                    toolchain="nightly-2026-06-11",
                    repetitions=1,
                    build_timeout=10,
                    run_timeout=10,
                    offline=True,
                )
        self.assertTrue(result["orchestration_success"])
        self.assertEqual(result["mechanism_evaluation"]["outcome"], "no_signal")
        self.assertEqual(
            result["type_isolation_evaluation"]["outcome"], "inconclusive"
        )
        self.assertEqual(
            result["type_isolation_evaluation"]["reason"],
            "compiler_critical_site_coverage_not_validated",
        )

    def test_run_rejects_missing_unsafe_opt_in_before_preflight(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            stderr = io.StringIO()
            with (
                contextlib.redirect_stderr(stderr),
                mock.patch.object(runner, "preflight_payload") as preflight,
            ):
                code = runner.main(
                    [
                        "--action",
                        "run",
                        "--output-dir",
                        temporary,
                    ]
                )
        self.assertEqual(code, 2)
        self.assertIn("--execute-unsafe", stderr.getvalue())
        preflight.assert_not_called()


if __name__ == "__main__":
    unittest.main()
