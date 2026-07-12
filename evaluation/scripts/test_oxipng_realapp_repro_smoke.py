#!/usr/bin/env python3
"""Unit tests for the Oxipng real-app UniAlloc repro smoke."""

from __future__ import annotations

import importlib.util
import json
import pathlib
import subprocess
import sys
import tempfile
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "evaluation" / "scripts" / "oxipng_realapp_repro_smoke.py"

spec = importlib.util.spec_from_file_location("oxipng_realapp_repro_smoke", SCRIPT)
assert spec is not None and spec.loader is not None
smoke = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = smoke
spec.loader.exec_module(smoke)


class OxipngRealappReproSmokeTests(unittest.TestCase):
    def make_minimal_oxipng_checkout(self, root: pathlib.Path) -> pathlib.Path:
        oxipng = root / "oxipng"
        (oxipng / "src").mkdir(parents=True)
        (oxipng / "Cargo.toml").write_text(
            "[package]\nname = \"oxipng\"\nversion = \"0.0.0\"\n\n[dependencies]\nbit-vec = \"0.6\"\n",
            encoding="utf-8",
        )
        (oxipng / "src" / "lib.rs").write_text(
            "#![deny(warnings)]\n#[cfg(feature = \"parallel\")]\nextern crate rayon;\n",
            encoding="utf-8",
        )
        (oxipng / "src" / "main.rs").write_text(
            "use std::time::Duration;\nfn main() {\n    let success = true;\n    if !success {\n        exit(1);\n    }\n}\n",
            encoding="utf-8",
        )
        return oxipng

    def test_apply_instrumentation_uses_relative_unialloc_path(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            tmp = pathlib.Path(td)
            oxipng = self.make_minimal_oxipng_checkout(tmp)
            unialloc = tmp / "repo" / "unialloc"
            unialloc.mkdir(parents=True)

            smoke.apply_instrumentation(oxipng, unialloc)

            cargo = (oxipng / "Cargo.toml").read_text(encoding="utf-8")
            self.assertIn('unialloc = { path = "../repo/unialloc"', cargo)
            self.assertNotIn(str(unialloc), cargo)
            self.assertIn("[workspace]", cargo)
            self.assertIn("#[global_allocator]", (oxipng / "src" / "main.rs").read_text(encoding="utf-8"))
            self.assertIn("extern crate unialloc;", (oxipng / "src" / "lib.rs").read_text(encoding="utf-8"))

    def test_parse_stats_requires_exactly_one_stats_line(self) -> None:
        stats = smoke.parse_stats('noise\nUNIALLOC_STATS_JSON={"typed_allocations":1,"fallback_allocations":0}\n')
        self.assertEqual(stats["typed_allocations"], 1)
        with self.assertRaises(smoke.SmokeError):
            smoke.parse_stats("missing\n")
        with self.assertRaises(smoke.SmokeError):
            smoke.parse_stats("UNIALLOC_STATS_JSON={}\nUNIALLOC_STATS_JSON={}\n")

    def test_collect_audits_sums_only_target_crate(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            audit_dir = pathlib.Path(td)
            (audit_dir / "oxipng.json").write_text(
                json.dumps(
                    {
                        "compiler_pass": {
                            "crate_name": "oxipng",
                            "actual_allocator_call_replacement": True,
                            "actual_semantic_scope_rewrite": True,
                            "body_clone_returned_to_rustc": True,
                            "direct_local_metadata_abi": True,
                            "direct_local_size_align_with_semantic_drop": True,
                            "rewrite_applied_count": 2,
                            "semantic_scope_rewrite_applied_count": 3,
                            "semantic_scope_drop_rewrite_applied_count": 4,
                            "semantic_scope_unsolved_candidate_count": 5,
                            "semantic_scope_drop_unsolved_candidate_count": 6,
                        }
                    }
                ),
                encoding="utf-8",
            )
            (audit_dir / "dep.json").write_text(json.dumps({"compiler_pass": {"crate_name": "dep"}}), encoding="utf-8")

            audits, totals = smoke.collect_audits(audit_dir, "oxipng")

            self.assertEqual(len(audits), 1)
            self.assertEqual(totals["direct_rewrite_applied_count"], 2)
            self.assertEqual(totals["semantic_scope_rewrite_applied_count"], 3)
            self.assertEqual(totals["semantic_scope_drop_rewrite_applied_count"], 4)
            self.assertEqual(totals["semantic_scope_unsolved_candidate_count"], 5)
            self.assertEqual(totals["semantic_scope_drop_unsolved_candidate_count"], 6)
            coverage = smoke.compiler_coverage_summary(totals)
            self.assertFalse(coverage["complete"])
            self.assertTrue(coverage["partial_coverage"])
            self.assertEqual(coverage["unsolved_candidate_count"], 11)

    def test_contract_fails_closed_on_missing_direct_rewrite(self) -> None:
        cmd = smoke.CommandResult(["cmd"], 0, "", "")
        with self.assertRaisesRegex(smoke.SmokeError, "direct MIR"):
            smoke.assert_contract(
                build=cmd,
                run=cmd,
                stats={"typed_allocations": 1, "fallback_allocations": 0, "type_isolation_corrupt_slots": 0},
                output_sha256="a",
                expected_output_sha256="a",
                audits=[{"file": "audit.json", **{key: True for key in smoke.REQUIRED_AUDIT_FLAGS}}],
                audit_totals={
                    "direct_rewrite_applied_count": 0,
                    "semantic_scope_rewrite_applied_count": 1,
                    "semantic_scope_drop_rewrite_applied_count": 1,
                    "semantic_scope_unsolved_candidate_count": 0,
                    "semantic_scope_drop_unsolved_candidate_count": 0,
                },
                fallback_note="reported fallback_allocations=0",
            )


    def test_contract_fails_closed_on_false_audit_flags(self) -> None:
        cmd = smoke.CommandResult(["cmd"], 0, "", "")
        totals = {
            "direct_rewrite_applied_count": 1,
            "semantic_scope_rewrite_applied_count": 1,
            "semantic_scope_drop_rewrite_applied_count": 1,
            "semantic_scope_unsolved_candidate_count": 0,
            "semantic_scope_drop_unsolved_candidate_count": 0,
        }
        audit = {key: True for key in smoke.REQUIRED_AUDIT_FLAGS}
        audit.update({
            "file": "audit.json",
            "body_clone_returned_to_rustc": False,
        })
        with self.assertRaisesRegex(smoke.SmokeError, "body_clone_returned_to_rustc"):
            smoke.assert_contract(
                build=cmd,
                run=cmd,
                stats={"typed_allocations": 1, "fallback_allocations": 0, "type_isolation_corrupt_slots": 0},
                output_sha256="a",
                expected_output_sha256="a",
                audits=[audit],
                audit_totals=totals,
                fallback_note="reported fallback_allocations=0",
            )


    def test_contract_accepts_mixed_no_candidate_and_applied_audits(self) -> None:
        cmd = smoke.CommandResult(["cmd"], 0, "", "")
        no_candidates = {
            "file": "no-candidates.json",
            "replacement_resolution_status": smoke.NO_SUPPORTED_DIRECT_REWRITE_STATUS,
            "actual_semantic_scope_rewrite": True,
            "body_clone_returned_to_rustc": True,
            **{key: False for key in smoke.DIRECT_REWRITE_AUDIT_FLAGS},
        }
        applied = {
            "file": "applied.json",
            "replacement_resolution_status": "resolved_unialloc_allocator_metadata_abi",
            **{key: True for key in smoke.REQUIRED_AUDIT_FLAGS},
        }

        smoke.assert_contract(
            build=cmd,
            run=cmd,
            stats={"typed_allocations": 1, "fallback_allocations": 0, "type_isolation_corrupt_slots": 0},
            output_sha256="a",
            expected_output_sha256="a",
            audits=[no_candidates, applied],
            audit_totals={
                "direct_rewrite_applied_count": 1,
                "semantic_scope_rewrite_applied_count": 2,
                "semantic_scope_drop_rewrite_applied_count": 1,
                "semantic_scope_unsolved_candidate_count": 0,
                "semantic_scope_drop_unsolved_candidate_count": 0,
            },
            fallback_note="reported fallback_allocations=0",
        )


    def test_fresh_temp_dir_rejects_existing_files_without_deleting(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            temp_dir = pathlib.Path(td) / "temp"
            temp_dir.mkdir()
            stale = temp_dir / "stale-target"
            stale.write_text("do not delete\n", encoding="utf-8")

            with self.assertRaisesRegex(smoke.SmokeError, "fresh/empty"):
                smoke.require_fresh_temp_dir(temp_dir)

            self.assertEqual(stale.read_text(encoding="utf-8"), "do not delete\n")


    def test_input_must_be_contained_inside_detached_checkout(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = pathlib.Path(td)
            checkout = root / "checkout"
            checkout.mkdir()
            inside = checkout / "tests" / "in.png"
            inside.parent.mkdir()
            inside.write_bytes(b"png")
            outside = root / "outside.png"
            outside.write_bytes(b"png")

            smoke.require_contained_path(inside, checkout, label="input PNG")
            with self.assertRaisesRegex(smoke.SmokeError, "detached checkout"):
                smoke.require_contained_path(outside, checkout, label="input PNG")


    def test_fresh_output_dir_rejects_existing_files_without_deleting(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            output_dir = pathlib.Path(td) / "out"
            output_dir.mkdir()
            stale = output_dir / "stale-audit.json"
            stale.write_text("do not delete\n", encoding="utf-8")

            with self.assertRaisesRegex(smoke.SmokeError, "fresh/empty"):
                smoke.require_fresh_output_dir(output_dir)

            self.assertEqual(stale.read_text(encoding="utf-8"), "do not delete\n")

    def test_posix_only_guard_rejects_windows(self) -> None:
        smoke.require_posix_host("posix")
        with self.assertRaisesRegex(smoke.SmokeError, "POSIX-only"):
            smoke.require_posix_host("nt")

    def test_protected_path_overlap_rejects_repo_and_pinned_containment(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            tmp = pathlib.Path(td)
            pinned = tmp / "pinned" / "checkout"
            pinned.mkdir(parents=True)

            with self.assertRaisesRegex(smoke.SmokeError, "repo root"):
                smoke.reject_protected_path_overlap(ROOT / "evaluation" / "raw" / "stale", pinned=pinned, label="output")

            with self.assertRaisesRegex(smoke.SmokeError, "pinned checkout"):
                smoke.reject_protected_path_overlap(pinned / "nested-output", pinned=pinned, label="output")

            with self.assertRaisesRegex(smoke.SmokeError, "pinned checkout"):
                smoke.reject_protected_path_overlap(pinned.parent, pinned=pinned, label="temporary parent")

    def test_source_binding_records_head_status_pass_source_and_toolchain(self) -> None:
        binding = smoke.source_binding(
            "nightly-test",
            pathlib.Path("/tmp/example-sysroot"),
            "rustc 1.2.3\nhost: test",
        )

        self.assertRegex(binding["repo_head"], r"^[0-9a-f]{40}$")
        self.assertEqual(binding["scoped_status_paths"], [str(path) for path in smoke.SCOPED_STATUS_PATHS])
        self.assertIn("Cargo.toml", binding["scoped_file_hashes"])
        self.assertIn("unialloc/build.rs", binding["scoped_file_hashes"])
        self.assertIn("alloc_macros/Cargo.toml", binding["scoped_file_hashes"])
        self.assertIn("evaluation/scripts/oxipng_realapp_repro_smoke.py", binding["scoped_file_hashes"])
        self.assertIn("evaluation/scripts/test_oxipng_realapp_repro_smoke.py", binding["scoped_file_hashes"])
        self.assertIn("pass_source_sha256", binding)
        self.assertRegex(binding["pass_source_sha256"], r"^[0-9a-f]{64}$")
        self.assertIn("repo_cargo_lock_sha256", binding)
        self.assertRegex(binding["scoped_fingerprint_sha256"], r"^[0-9a-f]{64}$")
        self.assertGreater(binding["scoped_file_count"], 0)
        self.assertEqual(binding["build_toolchain"], "nightly-test")
        self.assertEqual(binding["rustc_sysroot"], "/tmp/example-sysroot")
        self.assertEqual(binding["rustc_verbose_version"], "rustc 1.2.3\nhost: test")

    def test_source_drift_rejection_checks_scoped_fingerprint(self) -> None:
        start = {
            "repo_head": "a",
            "scoped_status": "",
            "scoped_fingerprint_sha256": "one",
            "pass_source_sha256": "p",
            "repo_cargo_lock_sha256": "c",
            "rustc_sysroot": "/tmp/sysroot",
            "rustc_verbose_version": "rustc",
        }
        smoke.reject_source_drift(start, dict(start))
        end = dict(start)
        end["scoped_fingerprint_sha256"] = "two"
        with self.assertRaisesRegex(smoke.SmokeError, "drifted"):
            smoke.reject_source_drift(start, end)

    def test_pass_binary_binding_records_hash_and_boundary(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            binary = pathlib.Path(td) / "wrapper"
            binary.write_bytes(b"fake-wrapper\n")

            digest = smoke.sha256_file(binary)
            with self.assertRaisesRegex(smoke.SmokeError, "expected-pass-binary-sha256"):
                smoke.pass_binary_binding(binary, built_by_script=False)
            with self.assertRaisesRegex(smoke.SmokeError, "pass-binary-provenance"):
                smoke.pass_binary_binding(binary, built_by_script=False, expected_sha256=digest)
            with self.assertRaisesRegex(smoke.SmokeError, "hash mismatch"):
                smoke.pass_binary_binding(
                    binary, built_by_script=False, expected_sha256="0" * 64, provenance="unit-test wrapper"
                )

            provided = smoke.pass_binary_binding(
                binary, built_by_script=False, expected_sha256=digest, provenance="unit-test wrapper"
            )
            built = smoke.pass_binary_binding(binary, built_by_script=True)

            self.assertRegex(provided["sha256"], r"^[0-9a-f]{64}$")
            self.assertEqual(provided["expected_sha256"], digest)
            self.assertEqual(provided["provenance"], "unit-test wrapper")
            self.assertFalse(provided["built_by_script"])
            self.assertIn("not binary provenance", provided["source_boundary"])
            self.assertTrue(built["built_by_script"])
            self.assertIn("current run", built["source_boundary"])

    def test_verify_pinned_checkout_rejects_dirty_checkout(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            repo = pathlib.Path(td) / "repo"
            repo.mkdir()
            subprocess.run(["git", "init"], cwd=repo, check=True, stdout=subprocess.PIPE)
            (repo / "file.txt").write_text("clean\n", encoding="utf-8")
            subprocess.run(["git", "add", "file.txt"], cwd=repo, check=True)
            subprocess.run(
                ["git", "-c", "user.name=Unit Test", "-c", "user.email=test@example.invalid", "commit", "-m", "init"],
                cwd=repo,
                check=True,
                stdout=subprocess.PIPE,
            )
            clean = smoke.verify_pinned_checkout(repo, None)
            self.assertEqual(clean["status"], "")
            (repo / "file.txt").write_text("dirty\n", encoding="utf-8")
            with self.assertRaisesRegex(smoke.SmokeError, "must be clean"):
                smoke.verify_pinned_checkout(repo, None)


if __name__ == "__main__":
    unittest.main()
