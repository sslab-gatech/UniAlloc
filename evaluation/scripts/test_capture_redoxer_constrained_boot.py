#!/usr/bin/env python3
"""Regression tests for bounded Docker/redoxer capture."""

from __future__ import annotations

import importlib.util
import pathlib
import subprocess
import sys
import time
import unittest


ROOT = pathlib.Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "evaluation" / "scripts" / "capture_redoxer_constrained_boot.py"
SPEC = importlib.util.spec_from_file_location("capture_redoxer_constrained_boot", SCRIPT)
assert SPEC is not None and SPEC.loader is not None
capture = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(capture)


class StreamProcessLinesTests(unittest.TestCase):
    def spawn(self, source: str) -> "subprocess.Popen[str]":
        return subprocess.Popen(
            [sys.executable, "-c", source],
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            bufsize=1,
        )

    def test_streams_complete_output_without_timeout(self) -> None:
        proc = self.spawn(
            "import time; print('redox-start', flush=True); "
            "time.sleep(0.05); print('redox-done', flush=True)"
        )
        lines = []
        timeout_calls = []
        returncode, timed_out = capture.stream_process_lines(
            proc,
            timeout_seconds=2,
            on_line=lines.append,
            on_timeout=lambda: timeout_calls.append(True),
        )
        self.assertEqual(returncode, 0)
        self.assertFalse(timed_out)
        self.assertEqual(lines, ["redox-start\n", "redox-done\n"])
        self.assertEqual(timeout_calls, [])

    def test_silent_child_is_terminated_at_wall_clock_deadline(self) -> None:
        proc = self.spawn("import time; time.sleep(30)")
        timeout_calls = []

        def terminate() -> None:
            timeout_calls.append(True)
            proc.terminate()

        started = time.monotonic()
        returncode, timed_out = capture.stream_process_lines(
            proc,
            timeout_seconds=0.2,
            on_line=lambda _line: None,
            on_timeout=terminate,
        )
        elapsed = time.monotonic() - started
        self.assertTrue(timed_out)
        self.assertNotEqual(returncode, 0)
        self.assertEqual(timeout_calls, [True])
        self.assertLess(elapsed, 2.0)


if __name__ == "__main__":
    unittest.main()
