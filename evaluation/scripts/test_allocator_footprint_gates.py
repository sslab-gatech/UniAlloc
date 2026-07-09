#!/usr/bin/env python3
"""Regression tests for the fast allocator-footprint gate."""

from __future__ import annotations

import argparse
import importlib.util
import json
import pathlib
import tempfile
import unittest
from unittest import mock


ROOT = pathlib.Path(__file__).resolve().parents[2]
EVALUATE_PATH = ROOT / "evaluation" / "scripts" / "evaluate.py"


spec = importlib.util.spec_from_file_location("unialloc_evaluate", EVALUATE_PATH)
assert spec is not None and spec.loader is not None
evaluate = importlib.util.module_from_spec(spec)
spec.loader.exec_module(evaluate)


class AllocatorFootprintGateTests(unittest.TestCase):
    def test_command_builder_keeps_toolchain_override_explicit(self) -> None:
        selected, unknown = evaluate.allocator_footprint_regression_specs(
            ["freelist-aligned-stale-cycle"]
        )
        self.assertEqual(unknown, [])

        command = evaluate.allocator_footprint_regression_command(
            selected[0],
            cargo="/usr/bin/cargo",
            toolchain="nightly",
        )

        self.assertEqual(command[:6], ["/usr/bin/cargo", "+nightly", "test", "-p", "unialloc", "--lib"])
        self.assertIn("freelist_aligned_run_candidate_scan_bounds_stale_skip_cycle", command)
        self.assertIn("--no-default-features", command)
        self.assertEqual(command[-2:], ["--format", "terse"])

    def test_fragmentation_regressions_are_in_fast_gate(self) -> None:
        selected, unknown = evaluate.allocator_footprint_regression_specs(
            [
                "freelist-aligned-same-size-fragment-min",
                "freelist-aligned-lookahead-fragment-min",
                "hosted-large-over-page-alignment-real",
                "fixed-heap-large-over-page-alignment-real",
                "separate-sc-single-strict-align-cursor",
            ]
        )

        self.assertEqual(unknown, [])
        self.assertEqual(
            [spec["test"] for spec in selected],
            [
                "freelist_aligned_run_fast_path_prefers_less_fragmenting_same_size_run",
                "freelist_aligned_run_fast_path_looks_ahead_for_less_fragmenting_larger_run",
                "rust_allocator_allocates_large_over_page_alignment_without_freelist_churn",
                "rust_allocator_allocates_page512_alignment_after_fixed_heap_slack_chunk_split",
                "allocate_rotates_partial_start_to_non_full_strict_alignment_match",
            ],
        )
        self.assertTrue(all(spec["no_default_features"] for spec in selected))
        self.assertEqual(selected[-1]["features"], "separate_sc_backend")

    def test_page_strict_alignment_caps_are_in_fast_gate(self) -> None:
        selected, unknown = evaluate.allocator_footprint_regression_specs(
            [
                "efficient-page-strict-align-cap-empty",
                "efficient-page-strict-align-cap-adaptive",
                "efficient-page-strict-align-cap-multipage",
                "efficient-page-strict-align-cap-partial",
                "separate-page-strict-align-cap",
            ]
        )

        self.assertEqual(unknown, [])
        self.assertEqual(
            [spec["test"] for spec in selected],
            [
                "efficient_object_page_strict_alignment_caps_empty_page_batch",
                "efficient_object_page_strict_alignment_cap_is_page_payload_adaptive",
                "efficient_object_page_strict_alignment_multi_page_batch_uses_adaptive_cap",
                "efficient_object_page_strict_alignment_caps_partial_freelist_batch",
                "allocate_batch_strict_alignment_returns_only_capped_matches",
            ],
        )
        self.assertTrue(all(spec["no_default_features"] for spec in selected))

    def test_metadata_segregated_hot_path_is_in_fast_gate(self) -> None:
        selected, unknown = evaluate.allocator_footprint_regression_specs(
            ["metadata-segregated-aggregate-scan-reuse"]
        )

        self.assertEqual(unknown, [])
        self.assertEqual(
            [spec["test"] for spec in selected],
            ["metadata_segregated_bucket_push_reuses_single_aggregate_scan"],
        )
        self.assertEqual(selected[0]["category"], "type-cache-hot-path-footprint-accounting")
        self.assertTrue(selected[0]["no_default_features"])

    def test_allocator_footprint_gate_writes_passing_audit(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = pathlib.Path(tmp)
            calls = []

            def fake_run(out_dir, label, command, timeout, *, extra_env=None):
                calls.append((label, command, timeout, extra_env))
                stdout = pathlib.Path(out_dir) / f"{label}.stdout.txt"
                stderr = pathlib.Path(out_dir) / f"{label}.stderr.txt"
                stdout.write_text("running 1 test\n.\ntest result: ok\n", encoding="utf-8")
                stderr.write_text("", encoding="utf-8")
                return {
                    "label": label,
                    "command": command,
                    "exit_code": 0,
                    "passed": True,
                    "wall_seconds": 0.01,
                    "stdout": str(stdout),
                    "stderr": str(stderr),
                }

            args = argparse.Namespace(
                run_id="unit-pass",
                output_dir=str(tmp_path),
                toolchain="",
                timeout=5,
                only=["zone-retained-empty-bitmap-default"],
                no_update_results=True,
            )
            with mock.patch.object(evaluate.shutil, "which", return_value="/usr/bin/cargo"), mock.patch.object(
                evaluate, "run_platform_command", side_effect=fake_run
            ):
                rc = evaluate.audit_allocator_footprint_regressions(args)

            self.assertEqual(rc, 0)
            self.assertEqual(len(calls), 1)
            self.assertEqual(calls[0][3], {"CARGO_INCREMENTAL": "0"})
            audit = json.loads((tmp_path / "allocator-footprint-regression-gate-audit.json").read_text())
            self.assertTrue(audit["passed"], audit)
            self.assertEqual(audit["summary"]["selected_count"], 1)
            self.assertEqual(audit["summary"]["passed_count"], 1)
            self.assertFalse(audit["summary"]["slow_benchmark_matrix_run"])
            self.assertFalse(audit["summary"]["extra_cargo_target_dir_created"])

    def test_allocator_footprint_gate_rejects_unknown_selector_without_running_cargo(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            args = argparse.Namespace(
                run_id="unit-unknown",
                output_dir=tmp,
                toolchain="",
                timeout=5,
                only=["does-not-exist"],
                no_update_results=True,
            )
            with mock.patch.object(evaluate.shutil, "which", return_value="/usr/bin/cargo"), mock.patch.object(
                evaluate, "run_platform_command"
            ) as run_mock:
                rc = evaluate.audit_allocator_footprint_regressions(args)

            self.assertEqual(rc, 1)
            run_mock.assert_not_called()
            audit = json.loads(
                (pathlib.Path(tmp) / "allocator-footprint-regression-gate-audit.json").read_text()
            )
            self.assertIn("unknown allocator footprint regression selector", audit["blockers"][0])

    def test_allocator_footprint_gate_preserves_failure_stderr_tail(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = pathlib.Path(tmp)

            def fake_run(out_dir, label, command, timeout, *, extra_env=None):
                stdout = pathlib.Path(out_dir) / f"{label}.stdout.txt"
                stderr = pathlib.Path(out_dir) / f"{label}.stderr.txt"
                stdout.write_text("running 1 test\n", encoding="utf-8")
                stderr.write_text("allocator regression failed with retained slab leak\n", encoding="utf-8")
                return {
                    "label": label,
                    "command": command,
                    "exit_code": 101,
                    "passed": False,
                    "wall_seconds": 0.02,
                    "stdout": str(stdout),
                    "stderr": str(stderr),
                }

            args = argparse.Namespace(
                run_id="unit-fail",
                output_dir=str(tmp_path),
                toolchain="",
                timeout=5,
                only=["zone-retained-empty-recycle-error"],
                no_update_results=True,
            )
            with mock.patch.object(evaluate.shutil, "which", return_value="/usr/bin/cargo"), mock.patch.object(
                evaluate, "run_platform_command", side_effect=fake_run
            ):
                rc = evaluate.audit_allocator_footprint_regressions(args)

            self.assertEqual(rc, 1)
            audit = json.loads((tmp_path / "allocator-footprint-regression-gate-audit.json").read_text())
            self.assertFalse(audit["passed"])
            self.assertIn("exit_code=101", audit["blockers"][0])
            self.assertIn("retained slab leak", audit["blockers"][0])

    def test_worklist_summary_exposes_latest_allocator_footprint_gate(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = pathlib.Path(tmp)
            old_results = evaluate.RESULTS
            evaluate.RESULTS = tmp_path
            try:
                (tmp_path / "allocator_footprint_regression_gate_audit.json").write_text(
                    json.dumps(
                        {
                            "source": "allocator-footprint-regression-gate",
                            "generated_at": "2026-07-09T00:00:00Z",
                            "passed": True,
                            "summary": {"selected_count": 8, "passed_count": 8},
                            "blockers": [],
                        },
                        sort_keys=True,
                    )
                    + "\n",
                    encoding="utf-8",
                )

                summary = evaluate.allocator_footprint_worklist_summary()
            finally:
                evaluate.RESULTS = old_results

        self.assertTrue(summary["present"])
        self.assertTrue(summary["passed"])
        self.assertEqual(summary["summary"]["selected_count"], 8)
        self.assertEqual(summary["blocker_count"], 0)


if __name__ == "__main__":
    unittest.main()
