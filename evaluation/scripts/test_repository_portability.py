#!/usr/bin/env python3
"""Regression tests for portable tracked templates and generated sources."""

from __future__ import annotations

import importlib.util
import json
import pathlib
import tempfile
import types
import unittest
from unittest import mock


ROOT = pathlib.Path(__file__).resolve().parents[2]
EVALUATE_PATH = ROOT / "evaluation" / "scripts" / "evaluate.py"

spec = importlib.util.spec_from_file_location("unialloc_evaluate_portability", EVALUATE_PATH)
assert spec is not None and spec.loader is not None
evaluate = importlib.util.module_from_spec(spec)
spec.loader.exec_module(evaluate)


class RepositoryPortabilityTests(unittest.TestCase):
    def test_load_config_resolves_relative_paper_checkout_from_repo_root(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            repo = pathlib.Path(tmpdir) / "UniAlloc"
            config = repo / "evaluation" / "config" / "paper_claims.json"
            config.parent.mkdir(parents=True)
            config.write_text('{"paper_dir_default": "../rust-alloc-paper"}\n', encoding="utf-8")
            with mock.patch.object(evaluate, "ROOT", repo):
                loaded = evaluate.load_config(config)
            self.assertEqual(
                pathlib.Path(loaded["paper_dir_default"]),
                (repo.parent / "rust-alloc-paper").resolve(),
            )

    def test_portable_tracked_template_strips_repo_paper_and_timestamp(self) -> None:
        paper = ROOT.parent / "rust-alloc-paper"
        payload = {
            "generated_at": "2026-07-09T00:00:00Z",
            "repo_file": str(ROOT / "evaluation" / "config.json"),
            "paper_file": str(paper / "data" / "default-perf.dat"),
            "nested": {"generated_at": "later"},
        }
        portable = evaluate.portable_tracked_template(payload, paper_dir=paper)
        self.assertIsNone(portable["generated_at"])
        self.assertIsNone(portable["nested"]["generated_at"])
        self.assertEqual(portable["repo_file"], "evaluation/config.json")
        self.assertEqual(portable["paper_file"], "../rust-alloc-paper/data/default-perf.dat")

    def test_portable_rust_source_span_normalizes_sysroot_and_repo(self) -> None:
        sysroot_span = (
            "/Users/example/.rustup/toolchains/nightly/lib/rustlib/src/rust/"
            "library/alloc/src/macros.rs:52:13: 52:47"
        )
        self.assertEqual(
            evaluate.portable_rust_source_span(sysroot_span),
            "$RUST_SRC/library/alloc/src/macros.rs:52:13: 52:47",
        )
        repo_span = f"{ROOT}/unialloc/src/lib.rs:1:1: 1:2"
        self.assertEqual(
            evaluate.portable_rust_source_span(repo_span),
            "unialloc/src/lib.rs:1:1: 1:2",
        )

    def test_direct_abi_probe_generator_emits_portable_source_spans(self) -> None:
        rows = [
            {
                "type_id": 1,
                "module_id": 2,
                "policy_flags": 3,
                "callsite": 4,
                "source_span": (
                    "/Users/example/.rustup/toolchains/nightly/lib/rustlib/src/rust/"
                    "library/alloc/src/macros.rs:52:13: 52:47"
                ),
                "mir_function": "alloc::vec::Vec<T>::with_capacity",
                "direct_allocator_abi_plan": {
                    "size_operand": "_1",
                    "align_operand": "const 8_usize",
                },
            }
        ]
        with tempfile.TemporaryDirectory() as tmpdir:
            output = pathlib.Path(tmpdir) / "probe.rs"
            evaluate.write_rustc_driver_direct_allocator_abi_probe_source(rows, output)
            source = output.read_text(encoding="utf-8")
        self.assertIn("$RUST_SRC/library/alloc/src/macros.rs", source)
        self.assertNotIn("/Users/example", source)

    def test_collectors_restore_generated_sources_on_success_and_error(self) -> None:
        collectors = (
            (
                "collect_rustc_driver_lowering_fixture",
                "_collect_rustc_driver_lowering_fixture_impl",
                "RUSTC_DRIVER_LOWERING_EXAMPLE_PATH",
            ),
            (
                "collect_rustc_driver_direct_allocator_abi_probe",
                "_collect_rustc_driver_direct_allocator_abi_probe_impl",
                "RUSTC_DRIVER_DIRECT_ALLOCATOR_ABI_PROBE_PATH",
            ),
        )
        for public_name, impl_name, path_name in collectors:
            for raises in (False, True):
                with self.subTest(collector=public_name, raises=raises):
                    with tempfile.TemporaryDirectory() as tmpdir:
                        source_path = pathlib.Path(tmpdir) / "generated.rs"
                        source_path.write_bytes(b"canonical source\n")

                        def mutate_source(_args: object) -> int:
                            source_path.write_bytes(b"temporary generated source\n")
                            if raises:
                                raise RuntimeError("collector failed")
                            return 17

                        with (
                            mock.patch.object(evaluate, path_name, source_path),
                            mock.patch.object(evaluate, impl_name, side_effect=mutate_source),
                        ):
                            collector = getattr(evaluate, public_name)
                            if raises:
                                with self.assertRaisesRegex(RuntimeError, "collector failed"):
                                    collector(types.SimpleNamespace())
                            else:
                                self.assertEqual(collector(types.SimpleNamespace()), 17)
                        self.assertEqual(source_path.read_bytes(), b"canonical source\n")

    def test_restore_helper_removes_new_generated_file_after_error(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            source_path = pathlib.Path(tmpdir) / "new-generated.rs"

            def fail() -> None:
                source_path.write_text("temporary\n", encoding="utf-8")
                raise RuntimeError("failed")

            with self.assertRaisesRegex(RuntimeError, "failed"):
                evaluate.run_with_restored_file(source_path, fail)
            self.assertFalse(source_path.exists())

    def test_checked_in_templates_do_not_capture_developer_paths_or_time(self) -> None:
        templates = (
            ROOT / "evaluation" / "config" / "paper_external_workloads.template.json",
            ROOT / "evaluation" / "config" / "paper_workload_wrappers.template.json",
        )
        for path in templates:
            with self.subTest(path=path):
                data = json.loads(path.read_text(encoding="utf-8"))
                self.assertIsNone(data["generated_at"])
                self.assertNotIn("/Users/hqzhao", json.dumps(data, sort_keys=True))
        claims = json.loads(
            (ROOT / "evaluation" / "config" / "paper_claims.json").read_text(encoding="utf-8")
        )
        self.assertEqual(claims["paper_dir_default"], "../rust-alloc-paper")

        source_paths = (
            EVALUATE_PATH,
            ROOT / "evaluation" / "README.md",
            ROOT / "docs" / "evaluation-gap-analysis.md",
            ROOT / "evaluation" / "external" / "faf" / "workload_config.template.json",
            ROOT / "unialloc" / "examples" / "rustc_driver_direct_allocator_abi_probe_generated.rs",
        )
        for path in source_paths:
            with self.subTest(path=path):
                self.assertNotIn("/Users/hqzhao", path.read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
