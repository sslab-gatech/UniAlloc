#!/usr/bin/env python3
"""Regression tests for the pinned RSH-064 derived reuse entry point."""

from __future__ import annotations

import contextlib
import importlib.util
import io
import pathlib
import unittest
from unittest import mock


ROOT = pathlib.Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "evaluation" / "scripts" / "run_rsh064_derived_reuse.py"

spec = importlib.util.spec_from_file_location("rsh064_derived_runner", SCRIPT)
assert spec is not None and spec.loader is not None
runner = importlib.util.module_from_spec(spec)
spec.loader.exec_module(runner)


class Rsh064DerivedReuseRunnerTests(unittest.TestCase):
    def test_entry_point_pins_catalog_and_scenario(self) -> None:
        forwarded = runner.forwarded_args(
            ["--action", "preflight", "--output-dir", "/tmp/rsh064"]
        )
        self.assertEqual(
            forwarded[:4],
            [
                "--catalog",
                str(runner.CATALOG),
                "--scenario",
                "RSH-064-derived-reuse",
            ],
        )
        self.assertEqual(
            forwarded[4:],
            ["--action", "preflight", "--output-dir", "/tmp/rsh064"],
        )

    def test_entry_point_rejects_catalog_or_scenario_override(self) -> None:
        for override in (
            ["--catalog", "/tmp/other.json"],
            ["--catalog=/tmp/other.json"],
            ["--scenario", "RSH-999-other"],
            ["--scenario=RSH-999-other"],
        ):
            with self.subTest(override=override):
                with self.assertRaises(ValueError):
                    runner.forwarded_args(override)

    def test_main_validates_dedicated_catalog_before_shared_runner(self) -> None:
        with (
            mock.patch.object(runner.neon_runner, "load_catalog") as load,
            mock.patch.object(runner.experiment, "main", return_value=0) as shared,
        ):
            code = runner.main(["--action", "list"])
        self.assertEqual(code, 0)
        load.assert_called_once_with(runner.CATALOG)
        shared.assert_called_once_with(
            [
                "--catalog",
                str(runner.CATALOG),
                "--scenario",
                runner.SCENARIO,
                "--action",
                "list",
            ]
        )

    def test_catalog_validation_error_is_reported_without_running(self) -> None:
        stderr = io.StringIO()
        with (
            contextlib.redirect_stderr(stderr),
            mock.patch.object(
                runner.neon_runner,
                "load_catalog",
                side_effect=runner.neon_runner.harness.HarnessError("hash mismatch"),
            ),
            mock.patch.object(runner.experiment, "main") as shared,
        ):
            code = runner.main(["--action", "list"])
        self.assertEqual(code, 2)
        self.assertIn("hash mismatch", stderr.getvalue())
        shared.assert_not_called()


if __name__ == "__main__":
    unittest.main()
