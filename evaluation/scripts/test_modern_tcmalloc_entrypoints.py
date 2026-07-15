#!/usr/bin/env python3
"""Fail-closed tests for modern google/tcmalloc in top-level eval entrypoints."""

from __future__ import annotations

import argparse
import importlib.util
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock


SCRIPT_DIR = Path(__file__).resolve().parent


def load_script(name: str):
    path = SCRIPT_DIR / f"{name}.py"
    spec = importlib.util.spec_from_file_location(f"test_{name}_module", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


driver = load_script("paper_workload_driver")
evaluate = load_script("evaluate")


def driver_args(**overrides: object) -> argparse.Namespace:
    values = {
        "google_tcmalloc_prefix": None,
        "tcmalloc_lib_dir": None,
    }
    values.update(overrides)
    return argparse.Namespace(**values)


class ModernTcmallocEntrypointTest(unittest.TestCase):
    def test_driver_rejects_arbitrary_gperftools_named_library(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            prefix = Path(directory)
            lib_dir = prefix / "lib"
            lib_dir.mkdir()
            (lib_dir / "libtcmalloc.so.4").write_bytes(b"gperftools")

            probe = driver.tcmalloc_library_probe(
                driver_args(google_tcmalloc_prefix=str(prefix))
            )

        self.assertFalse(probe["ok"])
        self.assertIsNone(probe["configured_library_dir"])
        self.assertNotIn("ctypes_find_library_tcmalloc", probe)
        self.assertTrue(probe["validation_errors"])

    def test_deprecated_driver_lib_dir_still_requires_modern_authentication(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            lib_dir = Path(directory) / "lib"
            lib_dir.mkdir()
            identity = {"allocator_family": "google/tcmalloc", "sha256": "a" * 64}
            with mock.patch.object(
                driver.GOOGLE_TCMALLOC,
                "validate_library_dir",
                return_value=identity,
            ) as validate:
                probe = driver.tcmalloc_library_probe(
                    driver_args(tcmalloc_lib_dir=str(lib_dir))
                )

        self.assertTrue(probe["ok"])
        self.assertTrue(probe["deprecated_input"])
        validate.assert_called_once_with(lib_dir.resolve())

    def test_evaluate_uses_canonical_prefix_and_exports_link_paths(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            prefix = Path(directory)
            lib_dir = prefix / "lib"
            lib_dir.mkdir()
            identity = {"allocator_family": "google/tcmalloc", "sha256": "b" * 64}
            with mock.patch.dict(
                os.environ,
                {evaluate.GOOGLE_TCMALLOC_PREFIX_ENV: str(prefix)},
            ), mock.patch.object(
                evaluate.GOOGLE_TCMALLOC,
                "validate_library_dir",
                return_value=identity,
            ):
                probe = evaluate.tcmalloc_library_probe()
                env = evaluate.local_collections_dependency_env(
                    {"allocator": "tcmalloc"}
                )

        self.assertTrue(probe["ok"])
        self.assertEqual(probe["configured_prefix"], str(prefix.resolve()))
        self.assertEqual(env[evaluate.GOOGLE_TCMALLOC_PREFIX_ENV], str(prefix.resolve()))
        self.assertIn(str(lib_dir.resolve()), env["LIBRARY_PATH"])
        self.assertIn(str(lib_dir.resolve()), env["LD_LIBRARY_PATH"])
        self.assertNotIn("UNIALLOC_TCMALLOC_LIB_DIR", env)

    def test_system_library_lookup_cannot_rescue_failed_modern_validation(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            prefix = Path(directory)
            (prefix / "lib").mkdir()
            with mock.patch.dict(
                os.environ,
                {evaluate.GOOGLE_TCMALLOC_PREFIX_ENV: str(prefix)},
            ), mock.patch.object(
                evaluate.GOOGLE_TCMALLOC,
                "validate_library_dir",
                side_effect=RuntimeError("invalid provenance"),
            ), mock.patch.object(
                evaluate.ctypes.util,
                "find_library",
                return_value="libtcmalloc.so.4",
            ) as find_library:
                probe = evaluate.tcmalloc_library_probe()
                available = evaluate.tcmalloc_library_available()

        self.assertFalse(probe["ok"])
        self.assertFalse(available)
        find_library.assert_not_called()

    def test_gperftools_remains_explicitly_legacy(self) -> None:
        self.assertEqual(
            driver.ALLOCATOR_FEATURES["gperftools_legacy"],
            "bench_gperftools_legacy",
        )
        self.assertEqual(
            evaluate.FEATURE_TO_ALLOCATOR["bench_gperftools_legacy"],
            "gperftools_legacy",
        )

    def test_generated_compiler_bench_uses_modern_adapter(self) -> None:
        source = evaluate.compiler_proto_root_source(
            "generated_modules",
            evaluate.COMPILER_PROTO_TYPE_ID_BASIS,
        )
        self.assertIn('feature = "bench_tcmalloc"', source)
        self.assertIn("mod google_tcmalloc;", source)
        self.assertIn("use google_tcmalloc::GoogleTcmalloc;", source)
        self.assertIn('feature = "bench_gperftools_legacy"', source)
        self.assertIn("use gperftools_tcmalloc::TCMalloc;", source)
        self.assertNotIn("use tcmalloc::TCMalloc;", source)


if __name__ == "__main__":
    unittest.main()
