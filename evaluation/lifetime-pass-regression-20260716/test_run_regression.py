#!/usr/bin/env python3
from __future__ import annotations

import importlib.util
import json
import shutil
import sys
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path


MODULE_PATH = Path(__file__).with_name("run_regression.py")
SPEC = importlib.util.spec_from_file_location("lifetime_pass_regression", MODULE_PATH)
assert SPEC is not None and SPEC.loader is not None
runner = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = runner
SPEC.loader.exec_module(runner)


class LifetimePassRegressionTest(unittest.TestCase):
    def test_run_directory_is_never_reused(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            created = runner.create_run_dir(root, "fresh")
            self.assertTrue(created.is_dir())
            with self.assertRaises(runner.RegressionError):
                runner.create_run_dir(root, "fresh")

    def test_source_closure_hashes_entrypoint_and_shared_modules(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            entry = root / "entry.rs"
            engine = root / "unialloc-rustc-driver-engine.rs"
            lifetime = root / "lifetime_aware.rs"
            entry.write_text("fn main() {}\n")
            engine.write_text("pub fn run() {}\n")
            lifetime.write_text("pub const MODE: u8 = 1;\n")
            first = runner.pass_source_closure(entry)
            self.assertEqual(len(first["files"]), 3)
            lifetime.write_text("pub const MODE: u8 = 2;\n")
            second = runner.pass_source_closure(entry)
            self.assertNotEqual(first["sha256"], second["sha256"])

    @unittest.skipIf(shutil.which("objcopy") is None, "objcopy is unavailable")
    def test_text_hash_does_not_mutate_executable(self) -> None:
        source = Path(shutil.which("true") or "/bin/true")
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            executable = root / "true"
            shutil.copy2(source, executable)
            before = runner.sha256_file(executable)
            record = runner.only_text_sha256(executable, root / "true.text")
            self.assertGreater(record["text_bytes"], 0)
            self.assertEqual(runner.sha256_file(executable), before)

    def test_pre_extraction_reference_rejects_tampered_binary(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            build_dir = root / "builds" / "bedrock"
            binary_dir = root / "bin"
            build_dir.mkdir(parents=True)
            binary_dir.mkdir()
            expected = build_dir / "expected"
            expected.write_bytes(b"measured binary")
            (build_dir / "build.json").write_text(
                json.dumps({"binary_sha256": runner.sha256_file(expected)})
            )
            (binary_dir / "bedrock").write_bytes(b"tampered binary")
            pinned = replace(
                runner.TARGETS["bedrock"],
                pre_extraction_binary_sha256=runner.sha256_file(expected),
            )
            with self.assertRaisesRegex(runner.RegressionError, "checked-in pin"):
                runner.pre_extraction_reference(
                    reference_raw=root, target=pinned
                )

    def test_checked_in_pin_rejects_coordinated_build_and_binary_tamper(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            build_dir = root / "builds" / "bedrock"
            binary_dir = root / "bin"
            build_dir.mkdir(parents=True)
            binary_dir.mkdir()
            binary = binary_dir / "bedrock"
            binary.write_bytes(b"coordinated tamper")
            tampered_sha = runner.sha256_file(binary)
            (build_dir / "build.json").write_text(
                json.dumps({"binary_sha256": tampered_sha})
            )
            pinned = replace(
                runner.TARGETS["bedrock"],
                pre_extraction_binary_sha256="a" * 64,
            )
            with self.assertRaisesRegex(
                runner.RegressionError, "build record disagrees with the checked-in"
            ):
                runner.pre_extraction_reference(reference_raw=root, target=pinned)

    @unittest.skipIf(shutil.which("objcopy") is None, "objcopy is unavailable")
    def test_reference_failure_propagates_to_parity_gate(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            executable = root / "true"
            shutil.copy2(Path(shutil.which("true") or "/bin/true"), executable)
            compat_audit = root / "compat-audit"
            standalone_audit = root / "standalone-audit"
            compat_audit.mkdir()
            standalone_audit.mkdir()
            payload = {
                "source": "test",
                "rewrite_candidates": [
                    {
                        "allocation_site_id": "site:1",
                        "lifetime_hint_basis": "automatic_rust_lifetime_prior_return_long",
                        "lifetime_hint": 2,
                    }
                ],
            }
            (compat_audit / "audit.json").write_text(json.dumps(payload))
            (standalone_audit / "audit.json").write_text(json.dumps(payload))
            canonical = runner.canonical_lifetime_audit(compat_audit)
            record = runner.parity_record(
                target="test",
                compat_binary=executable,
                standalone_binary=executable,
                compat_audit=compat_audit,
                standalone_audit=standalone_audit,
                reference={
                    "source": "test-reference",
                    "build_record": "/test/build.json",
                    "build_record_sha256": "1" * 64,
                    "expected_binary_sha256": "0" * 64,
                    "checked_in_binary_sha256": "0" * 64,
                    "build_record_binary_sha256": "0" * 64,
                    "observed_reference_binary_sha256": "0" * 64,
                    "canonical_audit_sha256": runner.canonical_sha256(canonical),
                    "checked_in_canonical_audit_sha256": runner.canonical_sha256(
                        canonical
                    ),
                    "lifetime_candidate_count": 1,
                },
                scratch_dir=root / "scratch",
            )
            self.assertTrue(record["code_parity"]["passed"])
            self.assertTrue(record["audit_parity"]["passed"])
            self.assertFalse(record["pre_extraction_reference"]["passed"])
            self.assertFalse(record["passed"])

    def test_at_least_three_pairs_are_required(self) -> None:
        with self.assertRaises(runner.RegressionError):
            runner.measure_target(
                target=runner.TARGETS["bedrock"],
                build={"variants": {}},
                run_dir=Path("/unused"),
                pairs=2,
                cpu=None,
                timeout=1,
                sample_interval=0.1,
            )

    def test_canonical_audit_ignores_argv_and_skipped_candidates(self) -> None:
        relevant = {
            "allocation_site_id": "site:1",
            "callsite": 1,
            "type_id": 2,
            "module_id": 3,
            "mir_function": "f",
            "basic_block": "bb0",
            "destination_place": "_1",
            "lowering_kind": "semantic_scope_enter_exit_rewrite",
            "lifetime_hint": 2,
            "lifetime_hint_confidence": 70,
            "lifetime_hint_basis": "automatic_rust_lifetime_prior_return_long",
            "placement_hint": 0,
            "placement_hint_basis": "default",
            "size_operand": None,
            "align_operand": None,
            "rewrite_status": "actual_semantic_scope_generic_type_rewrite_applied",
            "replacement_symbol": "__unialloc_semantic_scope_push_for_rust_type_hints",
            "replacement_resolution_status": "resolved",
            "metadata_pairing_contract": "recovery",
            "semantic_scope_unwind_pop_inserted": False,
            "lifetime_analysis_features": {
                "requested_layout_basis": "runtime_layout",
                "runtime_layout_captured_by_semantic_scope": True,
                "runtime_observation_min_requested_bytes": 4096,
                "runtime_observation_max_requested_bytes": 28672,
                "runtime_join_key": {
                    "requested_size_bytes": None,
                    "requested_align_bytes": None,
                },
            },
        }
        skipped = {
            **relevant,
            "allocation_site_id": "skipped",
            "lifetime_hint_basis": "not_applicable_skipped_candidate",
        }
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            first = root / "first"
            second = root / "second"
            first.mkdir()
            second.mkdir()
            (first / "a.json").write_text(
                json.dumps(
                    {
                        "source": "crate",
                        "rustc_args": ["--out-dir", "/one"],
                        "rewrite_candidates": [skipped, relevant],
                    }
                )
            )
            (second / "b.json").write_text(
                json.dumps(
                    {
                        "source": "standalone-lifetime-pass",
                        "rustc_args": ["--out-dir", "/two"],
                        "rewrite_candidates": [relevant],
                    }
                )
            )
            self.assertEqual(
                runner.canonical_lifetime_audit(first),
                runner.canonical_lifetime_audit(second),
            )

    def test_canonical_audit_detects_hint_change(self) -> None:
        candidate = {
            "allocation_site_id": "site:1",
            "lifetime_hint_basis": "automatic_rust_lifetime_prior_return_long",
            "lifetime_hint": 2,
        }
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            first = root / "first"
            second = root / "second"
            first.mkdir()
            second.mkdir()
            (first / "a.json").write_text(
                json.dumps({"source": "crate", "rewrite_candidates": [candidate]})
            )
            (second / "a.json").write_text(
                json.dumps(
                    {
                        "source": "crate",
                        "rewrite_candidates": [{**candidate, "lifetime_hint": 0}],
                    }
                )
            )
            self.assertNotEqual(
                runner.canonical_lifetime_audit(first),
                runner.canonical_lifetime_audit(second),
            )

    def test_paired_effect_keeps_time_and_rss_separate(self) -> None:
        compat = {
            "operation_seconds": 10.0,
            "operation_procfs": {"peak_rss_kib": 10000},
        }
        standalone = {
            "operation_seconds": 10.1,
            "operation_procfs": {"peak_rss_kib": 10500},
        }
        effect = runner.paired_effect(compat, standalone)
        self.assertAlmostEqual(effect["operation_slowdown_percent"], 1.0)
        self.assertEqual(effect["peak_rss_increase_kib"], 500)
        self.assertAlmostEqual(effect["peak_rss_increase_percent"], 5.0)


if __name__ == "__main__":
    unittest.main()
