#!/usr/bin/env python3
"""Focused fail-closed tests for the Google TCMalloc Bazel helper."""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
import google_tcmalloc_bazel_probe as helper


class GoogleTcmallocBazelProbeTests(unittest.TestCase):
    def test_contract_pins_google_revision_and_official_malloc_target(self) -> None:
        provenance = helper.base_provenance()
        self.assertEqual(provenance["commit"], helper.GOOGLE_TCMALLOC_COMMIT)
        self.assertEqual(
            provenance["pinned_module_release"],
            "0.0.0-20250927-12f2552",
        )
        self.assertEqual(provenance["rules_cc_module_version"], "0.1.5")
        self.assertEqual(provenance["required_bazel_version"], "8.4.2")
        self.assertEqual(provenance["revision_role"], "compatibility-pin-not-latest")
        self.assertIn("compatibility pin", provenance["pin_policy"])
        self.assertEqual(provenance["repository"], "https://github.com/google/tcmalloc.git")
        self.assertEqual(
            provenance["malloc_target"],
            "@com_google_tcmalloc//tcmalloc:tcmalloc",
        )
        self.assertEqual(provenance["bazel_target"], helper.BAZEL_TARGET)
        self.assertEqual(provenance["bazel_control_target"], helper.BAZEL_CONTROL_TARGET)
        self.assertEqual(provenance["build_options"], list(helper.BUILD_OPTIONS))
        self.assertEqual(provenance["probe_copts"], list(helper.PROBE_COPTS))
        self.assertIn("Temeraire", provenance["hugepage_allocator_identity"])
        self.assertFalse(provenance["fallback_allowed"])
        self.assertFalse(provenance["gperftools_legacy_eligible_as_google_tcmalloc"])

    def test_missing_bazel_writes_blocked_provenance_without_fallback(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            provenance_path = root / "provenance.json"
            with mock.patch.object(helper.shutil, "which", return_value=None):
                returncode = helper.main(
                    [
                        "--build-root",
                        str(root / "build"),
                        "--provenance",
                        str(provenance_path),
                        "--bazel",
                        "missing-bazel",
                    ]
                )
            self.assertEqual(returncode, 2)
            provenance = json.loads(provenance_path.read_text(encoding="utf-8"))
            self.assertEqual(provenance["status"], "BLOCKED")
            self.assertFalse(provenance["claim_eligible"])
            self.assertFalse(provenance["fallback_allowed"])
            self.assertIn("Bazel executable unavailable", provenance["blocked_reasons"][0])

    def test_bazelisk_is_pinned_to_the_repository_compatibility_version(self) -> None:
        completed = mock.Mock(stdout="bazel 8.4.2\n")
        with (
            mock.patch.object(helper.shutil, "which", return_value="/tmp/bazelisk"),
            mock.patch.object(helper, "run", return_value=completed) as run,
        ):
            resolved, version, environment = helper.require_bazel("bazelisk")

        self.assertEqual(resolved, "/tmp/bazelisk")
        self.assertEqual(version, "bazel 8.4.2")
        self.assertEqual(environment["USE_BAZEL_VERSION"], "8.4.2")
        run.assert_called_once_with(
            ["/tmp/bazelisk", "--version"], env=environment
        )

    def test_wrong_bazel_version_is_blocked(self) -> None:
        completed = mock.Mock(stdout="bazel 9.2.0\n")
        with (
            mock.patch.object(helper.shutil, "which", return_value="/tmp/bazelisk"),
            mock.patch.object(helper, "run", return_value=completed),
            self.assertRaisesRegex(helper.BuildBlocked, "unexpected Bazel identity"),
        ):
            helper.require_bazel("bazelisk")

    def test_missing_probe_source_still_writes_blocked_provenance(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            provenance_path = root / "provenance.json"
            with mock.patch.object(
                helper, "SOURCE", helper.ROOT / "evaluation/probes/missing.c"
            ):
                returncode = helper.main(
                    [
                        "--build-root",
                        str(root / "build"),
                        "--provenance",
                        str(provenance_path),
                    ]
                )
            self.assertEqual(returncode, 2)
            provenance = json.loads(provenance_path.read_text(encoding="utf-8"))
            self.assertEqual(provenance["status"], "BLOCKED")
            self.assertIsNone(provenance["source_sha256"])
            self.assertTrue(provenance["blocked_reasons"])

    def test_workspace_uses_link_time_malloc_attribute(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            checkout = root / "checkout"
            checkout.mkdir()
            workspace = root / "workspace"
            helper.write_workspace(workspace, checkout)
            build = (workspace / "BUILD.bazel").read_text(encoding="utf-8")
            module = (workspace / "MODULE.bazel").read_text(encoding="utf-8")
            self.assertIn(
                'malloc = "@com_google_tcmalloc//tcmalloc:tcmalloc"', build
            )
            self.assertIn('load("@rules_cc//cc:cc_binary.bzl", "cc_binary")', build)
            self.assertIn('"-fno-builtin-malloc"', build)
            self.assertIn('name = "cross_allocator_large_page_workload_system"', build)
            self.assertIn("local_path_override", module)
            self.assertIn('bazel_dep(name = "rules_cc", version = "0.1.5")', module)
            self.assertNotIn("LD_PRELOAD", build)
            self.assertNotIn("gperftools", build.lower())

    def test_artifact_refresh_replaces_a_read_only_prior_copy(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "source"
            destination = root / "bin" / "probe"
            source.write_bytes(b"fresh")
            source.chmod(0o555)
            destination.parent.mkdir()
            destination.write_bytes(b"stale")
            destination.chmod(0o555)

            helper.replace_executable_artifact(source, destination)

            self.assertEqual(destination.read_bytes(), b"fresh")
            self.assertTrue(destination.stat().st_mode & 0o111)


if __name__ == "__main__":
    unittest.main()
