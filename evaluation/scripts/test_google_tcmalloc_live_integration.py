#!/usr/bin/env python3
"""Live integration gate for the pinned modern Google TCMalloc artifact."""

from __future__ import annotations

import importlib.util
import os
import shutil
import subprocess
import sys
import unittest
from pathlib import Path
from unittest import mock


SCRIPT_DIR = Path(__file__).resolve().parent
ROOT = SCRIPT_DIR.parents[1]


def load_script(name: str):
    spec = importlib.util.spec_from_file_location(name, SCRIPT_DIR / f"{name}.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


support = load_script("google_tcmalloc_support")
matrix = load_script("realworld_type_isolation_matrix")

PREFIX_ENV = "UNIALLOC_GOOGLE_TCMALLOC_PREFIX"
REQUIRE_ENV = "UNIALLOC_REQUIRE_LIVE_GOOGLE_TCMALLOC"


def require_live_artifact() -> bool:
    value = os.environ.get(REQUIRE_ENV)
    if value is None:
        return False
    if value != "1":
        raise AssertionError(f"{REQUIRE_ENV} must be exactly 1 when set")
    return True


def configured_prefix() -> Path:
    if PREFIX_ENV in os.environ:
        value = os.environ[PREFIX_ENV]
        if not value.strip():
            raise AssertionError(f"{PREFIX_ENV} must be a non-empty path")
        return Path(value).expanduser().resolve()
    return ROOT / "evaluation" / "deps" / "tcmalloc"


class GoogleTcmallocLiveIntegrationTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        explicitly_configured = PREFIX_ENV in os.environ
        required = require_live_artifact()
        cls.prefix = configured_prefix()
        cls.lib_dir = cls.prefix / "lib"
        cls.library = cls.lib_dir / support.LIBRARY_NAME

        missing = []
        if not cls.library.is_file():
            missing.append(str(cls.library))
        if not (cls.prefix / support.PROVENANCE_NAME).is_file():
            missing.append(str(cls.prefix / support.PROVENANCE_NAME))
        if missing:
            reason = "live Google TCMalloc prerequisites missing: " + ", ".join(missing)
            if not explicitly_configured and not required and len(missing) == 2:
                raise unittest.SkipTest(reason)
            raise AssertionError(reason)

    def test_live_artifact_passes_full_authentication(self) -> None:
        identity = support.validate_library_dir(self.lib_dir)
        self.assertEqual(identity["path"], str(self.library.resolve()))
        self.assertEqual(identity["upstream_revision"], support.UPSTREAM_REVISION)
        self.assertEqual(identity["variant"], "modern-hpaa-adaptive-subrelease")

    def test_preloaded_process_proves_runtime_identity(self) -> None:
        library_path = str(self.library.resolve())
        if os.pathsep in library_path or any(char.isspace() for char in library_path):
            self.fail(
                "live Google TCMalloc prefix cannot contain LD_PRELOAD separators"
            )
        cc = shutil.which("cc")
        true_path = shutil.which("true")
        if cc is None or true_path is None:
            self.fail("live Google TCMalloc runtime probe requires cc and true")

        runtime = matrix.tcmalloc_runtime_evidence(prefix=self.prefix)
        proof = matrix.prove_tcmalloc_preload(
            Path(true_path), runtime, timeout=30
        )
        self.assertTrue(proof["success"])
        self.assertEqual(proof["resolved_library"], library_path)
        self.assertEqual(
            proof["runtime_identity"],
            {
                "revision": support.UPSTREAM_REVISION,
                "hpaa_active": 1,
                "malloc_provider_is_self": 1,
                "exact_library_mapped": True,
            },
        )
        self.assertIsNotNone(proof["runtime_probe_compile"])

        env = os.environ.copy()
        env["LD_PRELOAD"] = library_path
        marker_probe = subprocess.run(
            [true_path],
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=30,
        )
        self.assertEqual(
            marker_probe.returncode, 0, marker_probe.stdout + marker_probe.stderr
        )
        marker_lines = marker_probe.stderr.splitlines(keepends=True)
        self.assertEqual(marker_lines.count(support.RUNTIME_IDENTITY_MARKER), 1)
        self.assertNotIn(support.RUNTIME_FAILURE_MARKER, marker_lines)


class GoogleTcmallocLiveContractTest(unittest.TestCase):
    def test_present_artifact_cannot_skip_missing_runtime_tools(self) -> None:
        case = GoogleTcmallocLiveIntegrationTest(
            "test_preloaded_process_proves_runtime_identity"
        )
        case.prefix = Path("/tmp/google-tcmalloc")
        case.library = case.prefix / "lib" / support.LIBRARY_NAME
        with mock.patch.object(shutil, "which", return_value=None):
            with self.assertRaisesRegex(AssertionError, "requires cc and true"):
                case.test_preloaded_process_proves_runtime_identity()


if __name__ == "__main__":
    unittest.main()
