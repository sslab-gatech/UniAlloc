#!/usr/bin/env python3
"""Focused tests for the modern google/tcmalloc build and identity contract."""

from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
ROOT = SCRIPT_DIR.parents[1]
SUPPORT_SPEC = importlib.util.spec_from_file_location(
    "google_tcmalloc_support", SCRIPT_DIR / "google_tcmalloc_support.py"
)
assert SUPPORT_SPEC is not None and SUPPORT_SPEC.loader is not None
support = importlib.util.module_from_spec(SUPPORT_SPEC)
SUPPORT_SPEC.loader.exec_module(support)
sys.modules["google_tcmalloc_support"] = support

BUILD_SPEC = importlib.util.spec_from_file_location(
    "build_google_tcmalloc", SCRIPT_DIR / "build_google_tcmalloc.py"
)
assert BUILD_SPEC is not None and BUILD_SPEC.loader is not None
builder = importlib.util.module_from_spec(BUILD_SPEC)
BUILD_SPEC.loader.exec_module(builder)


class GoogleTcmallocSupportTest(unittest.TestCase):
    def write_fixture(self, root: Path) -> Path:
        lib_dir = root / "lib"
        lib_dir.mkdir()
        library = lib_dir / support.LIBRARY_NAME
        library.write_bytes(b"modern-google-tcmalloc-fixture")
        manifest = {
            "allocator_family": "google/tcmalloc",
            "variant": "modern-hpaa-adaptive-subrelease",
            "upstream_revision": support.UPSTREAM_REVISION,
            "bazel_version": support.BAZEL_VERSION,
            "library_sha256": support.sha256_file(library),
            "hpaa_probe_output": list(support.REQUIRED_PROBE_LINES),
        }
        (root / support.PROVENANCE_NAME).write_text(json.dumps(manifest))
        return lib_dir

    def test_manifest_binds_revision_hpaa_and_library_hash(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            lib_dir = self.write_fixture(root)
            identity = support.validate_library_dir(lib_dir, inspect_symbols=False)
            self.assertEqual(identity["upstream_revision"], support.UPSTREAM_REVISION)
            self.assertEqual(identity["variant"], "modern-hpaa-adaptive-subrelease")
            (lib_dir / support.LIBRARY_NAME).write_bytes(b"tampered")
            with self.assertRaisesRegex(RuntimeError, "provenance or HPAA"):
                support.validate_library_dir(lib_dir, inspect_symbols=False)

    def test_gperftools_filename_cannot_satisfy_modern_baseline(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "lib").mkdir()
            (root / "lib/libtcmalloc.so.4").write_bytes(b"gperftools")
            with self.assertRaisesRegex(RuntimeError, "modern google/tcmalloc artifact"):
                support.validate_library_dir(root / "lib", inspect_symbols=False)

    def test_generated_bazel_target_uses_default_hpaa_with_adaptive_subrelease(self) -> None:
        files = builder.integration_files()
        build = files["BUILD.bazel"]
        probe = files["hpaa_probe.cc"]
        self.assertIn('"//tcmalloc"', build)
        self.assertNotIn("want_hpaa", build)
        self.assertIn("HugePageAware:", probe)
        self.assertIn("PARAMETER hpaa_subrelease 1", probe)
        self.assertIn(support.UPSTREAM_REVISION, files["shim.cc"])
        self.assertIn('dlsym(RTLD_DEFAULT, "malloc")', files["shim.cc"])
        self.assertIn('dlsym(RTLD_DEFAULT, "free")', files["shim.cc"])
        self.assertIn("MallocExtension::GetOwnership", files["shim.cc"])
        self.assertIn("constructor(65535)", files["shim.cc"])
        self.assertIn(support.RUNTIME_IDENTITY_MARKER.rstrip(), files["shim.cc"])

    def test_bazel_version_comparison_is_exact(self) -> None:
        self.assertEqual(builder.parse_bazel_version("bazel 8.4.2"), "8.4.2")
        self.assertNotEqual(builder.parse_bazel_version("bazel 8.4.20"), "8.4.2")
        with self.assertRaisesRegex(RuntimeError, "unrecognized Bazel"):
            builder.parse_bazel_version("bazelisk version: 8.4.2")

    def test_dynamic_symbol_parser_rejects_undefined_local_and_substrings(self) -> None:
        table = """
  1: 000000 10 FUNC GLOBAL DEFAULT 12 malloc
  2: 000000  0 FUNC GLOBAL DEFAULT UND free
  3: 000000 10 FUNC LOCAL DEFAULT 12 unialloc_google_tcmalloc_alloc
  4: 000000 10 FUNC WEAK DEFAULT 12 free@@TCMALLOC_1
  5: 000000 10 FUNC GLOBAL HIDDEN 12 unialloc_google_tcmalloc_revision
  6: 000000 10 FUNC GLOBAL DEFAULT 12 malloc_confusing_suffix
"""
        self.assertEqual(
            support.parse_exported_dynamic_symbols(table),
            {"malloc", "free", "malloc_confusing_suffix"},
        )

    def test_lockfile_and_rust_adapter_share_exact_pin(self) -> None:
        self.assertEqual(builder.sha256_file(builder.LOCKFILE), builder.LOCKFILE_SHA256)
        adapter = (ROOT / "unialloc/benches/google_tcmalloc.rs").read_text()
        self.assertIn(support.UPSTREAM_REVISION, adapter)
        self.assertIn(support.REVISION_SYMBOL, adapter)

    def test_modern_tcmalloc_rejects_mixed_allocator_features(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            result = subprocess.run(
                [
                    "cargo",
                    "bench",
                    "-p",
                    "unialloc",
                    "--bench",
                    "vec_deque_append_bench",
                    "--features",
                    "bench_tcmalloc,bench_ptmalloc",
                    "--no-run",
                    "--offline",
                ],
                cwd=ROOT,
                env={**os.environ, "CARGO_TARGET_DIR": directory},
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
            )
        self.assertNotEqual(result.returncode, 0, result.stdout)
        self.assertIn(
            "bench_tcmalloc must be the only enabled benchmark allocator feature",
            result.stdout,
        )


if __name__ == "__main__":
    unittest.main()
