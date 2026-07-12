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

    def test_contract_fails_closed_on_missing_direct_rewrite(self) -> None:
        cmd = smoke.CommandResult(["cmd"], 0, "", "")
        with self.assertRaisesRegex(smoke.SmokeError, "direct MIR"):
            smoke.assert_contract(
                build=cmd,
                run=cmd,
                stats={"typed_allocations": 1, "fallback_allocations": 0, "type_isolation_corrupt_slots": 0},
                output_sha256="a",
                expected_output_sha256="a",
                audits=[{"file": "audit.json"}],
                audit_totals={
                    "direct_rewrite_applied_count": 0,
                    "semantic_scope_rewrite_applied_count": 1,
                    "semantic_scope_drop_rewrite_applied_count": 1,
                    "semantic_scope_unsolved_candidate_count": 0,
                    "semantic_scope_drop_unsolved_candidate_count": 0,
                },
                fallback_note="reported fallback_allocations=0",
            )


    def test_fresh_output_dir_rejects_existing_files_without_deleting(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            output_dir = pathlib.Path(td) / "out"
            output_dir.mkdir()
            stale = output_dir / "stale-audit.json"
            stale.write_text("do not delete\n", encoding="utf-8")

            with self.assertRaisesRegex(smoke.SmokeError, "fresh/empty"):
                smoke.require_fresh_output_dir(output_dir)

            self.assertEqual(stale.read_text(encoding="utf-8"), "do not delete\n")

    def test_source_binding_records_head_status_pass_source_and_toolchain(self) -> None:
        binding = smoke.source_binding("nightly-test", pathlib.Path("/tmp/example-sysroot"))

        self.assertRegex(binding["repo_head"], r"^[0-9a-f]{40}$")
        self.assertEqual(binding["scoped_status_paths"], [
            "unialloc/src",
            "unialloc/Cargo.toml",
            "tools/unialloc-rustc-pass/unialloc-rustc-mir-rewrite-dry-run.rs",
        ])
        self.assertIn("pass_source_sha256", binding)
        self.assertRegex(binding["pass_source_sha256"], r"^[0-9a-f]{64}$")
        self.assertEqual(binding["build_toolchain"], "nightly-test")
        self.assertEqual(binding["rustc_sysroot"], "/tmp/example-sysroot")

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
