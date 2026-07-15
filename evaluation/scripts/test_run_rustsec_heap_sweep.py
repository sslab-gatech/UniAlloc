#!/usr/bin/env python3
"""Unit tests for the RustSec heap sweep orchestrator."""

from __future__ import annotations

import contextlib
import importlib.util
import io
import json
import pathlib
import tempfile
import unittest
from unittest import mock


ROOT = pathlib.Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "evaluation" / "scripts" / "run_rustsec_heap_sweep.py"
spec = importlib.util.spec_from_file_location("rustsec_heap_sweep", SCRIPT)
assert spec is not None and spec.loader is not None
sweep = importlib.util.module_from_spec(spec)
spec.loader.exec_module(sweep)


class Completed:
    def __init__(self, returncode: int, stdout: str = "", stderr: str = "") -> None:
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


class RustSecHeapSweepTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = pathlib.Path(self.temporary.name)
        self.catalog = self.root / "catalog.json"
        self.catalog.write_text('{"catalog":"fake"}\n', encoding="utf-8")
        self.cache = self.root / "cache"
        self.output = self.root / "out"
        self.current_impl = "ab" * 32
        self.scenarios = [
            {
                "scenario_id": "RSH-001-upstream",
                "case_id": "RSH-001",
                "crate": "a",
                "tool": "asan",
            },
            {
                "scenario_id": "RSH-002-derived",
                "case_id": "RSH-002",
                "crate": "b",
                "tool": "miri",
            },
        ]

    def invoke(self, argv: list[str], fake_run) -> tuple[int, str, str]:
        stdout = io.StringIO()
        stderr = io.StringIO()
        with mock.patch.object(sweep.subprocess, "run", side_effect=fake_run):
            with mock.patch.object(
                sweep,
                "current_unialloc_implementation_digest",
                return_value=self.current_impl,
                create=True,
            ):
                with (
                    contextlib.redirect_stdout(stdout),
                    contextlib.redirect_stderr(stderr),
                ):
                    code = sweep.main(argv)
        return code, stdout.getvalue(), stderr.getvalue()

    def base_argv(self, *extra: str) -> list[str]:
        return [
            "--catalog",
            str(self.catalog),
            "--cache",
            str(self.cache),
            "--output-dir",
            str(self.output),
            *extra,
        ]

    def fake_list_payload(self, scenarios=None) -> str:
        return json.dumps(
            {"scenarios": self.scenarios if scenarios is None else scenarios}
        )

    def expected_preflight_arms(
        self, *, unsupported_pair: tuple[str, str] | None = None
    ) -> list[dict[str, object]]:
        return [
            {
                "allocator_variant": allocator,
                "archive_variant": archive,
                "supported": (archive, allocator) != unsupported_pair,
            }
            for archive in ("vulnerable", "patched")
            for allocator in ("system", "unialloc", "typed_plain", "typeiso")
        ]

    def preflight_payload(
        self,
        scenario_id: str,
        *,
        repetitions: int = 1,
        arms: list[dict[str, object]] | None = None,
        unsupported_pair: tuple[str, str] | None = None,
        catalog_sha256: str | None = None,
    ) -> dict[str, object]:
        return {
            "scenario_id": scenario_id,
            "claim_grade": False,
            "catalog_sha256": catalog_sha256 or sweep.sha256_file(self.catalog),
            "repetitions_requested": repetitions,
            "unsafe_execution_requested": False,
            "planned_arms": (
                arms
                if arms is not None
                else self.expected_preflight_arms(unsupported_pair=unsupported_pair)
            ),
        }

    def run_payload(
        self,
        scenario_id: str,
        *,
        repetitions: int = 1,
        unsupported_pair: tuple[str, str] | None = None,
        compile_rejection_pair: tuple[str, str] | None = None,
        unexpected: int = 0,
        duplicate_last_pair: bool = False,
        catalog_sha256: str | None = None,
    ) -> tuple[dict[str, object], dict[str, object]]:
        catalog_sha = catalog_sha256 or sweep.sha256_file(self.catalog)
        preflight = self.preflight_payload(
            scenario_id,
            repetitions=repetitions,
            unsupported_pair=unsupported_pair,
            catalog_sha256=catalog_sha,
        )
        arms: list[dict[str, object]] = []
        for archive in ("vulnerable", "patched"):
            for allocator in ("system", "unialloc", "typed_plain", "typeiso"):
                pair = (archive, allocator)
                if pair == unsupported_pair:
                    arms.append(
                        {
                            "scenario_id": scenario_id,
                            "archive_variant": archive,
                            "allocator_variant": allocator,
                            "repetitions_requested": repetitions,
                            "execution_status": "unsupported_allocator_topology",
                            "oracle_validation": {
                                "expected_contract": {"kind": "asan_finding"},
                                "status": "unsupported",
                            },
                        }
                    )
                    continue
                compile_rejection = pair == compile_rejection_pair
                fingerprint_payload = {
                    "catalog_sha256": catalog_sha,
                    "scenario_id": scenario_id,
                    "archive_variant": archive,
                    "allocator_variant": allocator,
                    "orchestrator_sha256": sweep.sha256_file(sweep.EXPERIMENT_RUNNER),
                    "unialloc_implementation_sha256": self.current_impl,
                }
                executed = 0 if compile_rejection else repetitions
                arms.append(
                    {
                        "scenario_id": scenario_id,
                        "archive_variant": archive,
                        "allocator_variant": allocator,
                        "repetitions_requested": repetitions,
                        "execution_status": "completed",
                        "fingerprint_payload": fingerprint_payload,
                        "fingerprint": sweep.canonical_sha256(fingerprint_payload),
                        "repetition_summary": {
                            "requested": repetitions,
                            "executed": executed,
                        },
                        "oracle_validation": {
                            "expected_contract": {
                                "kind": (
                                    "compile_rejection"
                                    if compile_rejection
                                    else "asan_finding"
                                )
                            },
                            "status": (
                                "patched_control_compile_rejection_observed"
                                if compile_rejection
                                else "tool_finding_observed"
                            ),
                        },
                    }
                )
        if duplicate_last_pair:
            arms[-1] = dict(arms[0])
        experiment = {
            "scenario_id": scenario_id,
            "claim_grade": False,
            "unsafe_execution_requested": True,
            "variants": ["system", "unialloc", "typed_plain", "typeiso"],
            "archive_variants": ["vulnerable", "patched"],
            "repetitions_requested": repetitions,
            "orchestration_success": unexpected == 0,
            "unexpected_arm_count": unexpected,
            "arms": arms,
        }
        return preflight, experiment

    def write_preflight(
        self, out_dir: pathlib.Path, scenario_id: str, **kwargs
    ) -> dict[str, object]:
        payload = self.preflight_payload(scenario_id, **kwargs)
        sweep.write_json(out_dir / "preflight.json", payload)
        return payload

    def write_experiment(
        self, out_dir: pathlib.Path, scenario_id: str, **kwargs
    ) -> dict[str, object]:
        preflight, payload = self.run_payload(scenario_id, **kwargs)
        sweep.write_json(out_dir / "preflight.json", preflight)
        sweep.write_json(out_dir / "experiment.json", payload)
        return payload

    def test_preflight_sweep_continues_after_failure_and_aggregates_summary(
        self,
    ) -> None:
        def fake_run(command, **kwargs):
            if "list" in command:
                return Completed(0, self.fake_list_payload())
            scenario_id = command[command.index("--scenario") + 1]
            out_dir = pathlib.Path(command[command.index("--output-dir") + 1])
            if scenario_id == "RSH-001-upstream":
                payload = self.write_preflight(
                    out_dir,
                    scenario_id,
                    unsupported_pair=("patched", "typeiso"),
                )
                return Completed(0, json.dumps(payload), "")
            return Completed(2, "", "boom")

        code, stdout, stderr = self.invoke(
            self.base_argv("--action", "preflight"), fake_run
        )

        self.assertEqual(code, 1, stderr)
        summary = json.loads(stdout)
        self.assertEqual(summary["totals"]["scenario_count"], 2)
        self.assertEqual(summary["totals"]["success_count"], 1)
        self.assertEqual(summary["totals"]["failed_count"], 1)
        self.assertEqual(summary["totals"]["arm_count"], 8)
        self.assertEqual(summary["totals"]["unsupported_count"], 1)
        self.assertEqual(summary["totals"]["planned_repetition_slots"], 7)
        self.assertEqual(summary["claim_grade"], False)
        self.assertTrue(
            (
                self.output / "RSH-002-derived" / "logs" / "preflight.stderr.log"
            ).is_file()
        )

    def test_resume_successful_requires_and_uses_current_binding(self) -> None:
        calls = []

        def initial_run(command, **kwargs):
            calls.append(command)
            if "list" in command:
                return Completed(0, json.dumps({"scenarios": [self.scenarios[0]]}))
            scenario_id = command[command.index("--scenario") + 1]
            out_dir = pathlib.Path(command[command.index("--output-dir") + 1])
            payload = self.write_preflight(out_dir, scenario_id)
            return Completed(0, json.dumps(payload), "")

        first_code, _stdout, first_stderr = self.invoke(
            self.base_argv("--action", "preflight"), initial_run
        )
        self.assertEqual(first_code, 0, first_stderr)
        stage_call_count = len(calls) - 1
        self.assertEqual(stage_call_count, 1)

        def resume_run(command, **kwargs):
            calls.append(command)
            if "list" in command:
                return Completed(0, json.dumps({"scenarios": [self.scenarios[0]]}))
            raise AssertionError("stage command should be resumed")

        code, stdout, stderr = self.invoke(
            self.base_argv("--action", "preflight", "--resume-successful"), resume_run
        )

        self.assertEqual(code, 0, stderr)
        summary = json.loads(stdout)
        self.assertEqual(summary["records"][0]["status"], "resumed")
        self.assertEqual(
            summary["records"][0]["result_sha256"],
            sweep.sha256_file(self.output / "RSH-001-upstream" / "preflight.json"),
        )
        self.assertEqual(len(list(self.output.rglob("*.sweep-binding.json"))), 1)

    def test_resume_rejects_runner_hash_binding_mismatch_and_reruns(self) -> None:
        def first_run(command, **kwargs):
            if "list" in command:
                return Completed(0, json.dumps({"scenarios": [self.scenarios[0]]}))
            scenario_id = command[command.index("--scenario") + 1]
            out_dir = pathlib.Path(command[command.index("--output-dir") + 1])
            payload = self.write_preflight(out_dir, scenario_id)
            return Completed(0, json.dumps(payload), "")

        code, _stdout, stderr = self.invoke(
            self.base_argv("--action", "preflight"), first_run
        )
        self.assertEqual(code, 0, stderr)
        binding_path = next(self.output.rglob("*.sweep-binding.json"))
        binding = sweep.load_json(binding_path)
        binding["runner_sha256"] = "00" * 32
        sweep.write_json(binding_path, binding)
        stage_calls = 0

        def rerun(command, **kwargs):
            nonlocal stage_calls
            if "list" in command:
                return Completed(0, json.dumps({"scenarios": [self.scenarios[0]]}))
            stage_calls += 1
            scenario_id = command[command.index("--scenario") + 1]
            out_dir = pathlib.Path(command[command.index("--output-dir") + 1])
            payload = self.write_preflight(out_dir, scenario_id)
            return Completed(0, json.dumps(payload), "")

        code, stdout, stderr = self.invoke(
            self.base_argv("--action", "preflight", "--resume-successful"), rerun
        )
        self.assertEqual(code, 0, stderr)
        self.assertEqual(stage_calls, 1)
        self.assertEqual(json.loads(stdout)["records"][0]["status"], "success")

    def test_non_resume_removes_stale_result_and_requires_fresh_valid_json(
        self,
    ) -> None:
        result_path = self.output / "RSH-001-upstream" / "preflight.json"
        sweep.write_json(result_path, self.preflight_payload("RSH-001-upstream"))

        def fake_run(command, **kwargs):
            if "list" in command:
                return Completed(0, json.dumps({"scenarios": [self.scenarios[0]]}))
            return Completed(0, "claimed success", "")

        code, stdout, stderr = self.invoke(
            self.base_argv("--action", "preflight"), fake_run
        )

        self.assertEqual(code, 1, stderr)
        record = json.loads(stdout)["records"][0]
        self.assertEqual(record["status"], "failed")
        self.assertEqual(record["arm_count"], 0)
        self.assertIn("fresh valid result", record["failure_reason"])
        self.assertFalse(result_path.exists())

    def test_invalid_fresh_result_never_contributes_counts(self) -> None:
        def fake_run(command, **kwargs):
            if "list" in command:
                return Completed(0, json.dumps({"scenarios": [self.scenarios[0]]}))
            out_dir = pathlib.Path(command[command.index("--output-dir") + 1])
            out_dir.mkdir(parents=True, exist_ok=True)
            (out_dir / "preflight.json").write_text("{bad json", encoding="utf-8")
            return Completed(0, "", "")

        code, stdout, stderr = self.invoke(
            self.base_argv("--action", "preflight"), fake_run
        )
        self.assertEqual(code, 1, stderr)
        record = json.loads(stdout)["records"][0]
        self.assertEqual(record["arm_count"], 0)
        self.assertIsNone(record["result_sha256"])

    def test_preflight_rejects_duplicate_matrix_pairs(self) -> None:
        arms = self.expected_preflight_arms()
        arms[-1] = dict(arms[0])

        def fake_run(command, **kwargs):
            if "list" in command:
                return Completed(0, json.dumps({"scenarios": [self.scenarios[0]]}))
            out_dir = pathlib.Path(command[command.index("--output-dir") + 1])
            payload = self.write_preflight(out_dir, "RSH-001-upstream", arms=arms)
            return Completed(0, json.dumps(payload), "")

        code, stdout, stderr = self.invoke(
            self.base_argv("--action", "preflight"), fake_run
        )
        self.assertEqual(code, 1, stderr)
        self.assertIn(
            "duplicate matrix pair", json.loads(stdout)["records"][0]["failure_reason"]
        )

    def test_run_rejects_duplicate_matrix_pairs_and_invalid_fingerprints(self) -> None:
        def fake_run(command, **kwargs):
            if "list" in command:
                return Completed(0, json.dumps({"scenarios": [self.scenarios[0]]}))
            out_dir = pathlib.Path(command[command.index("--output-dir") + 1])
            payload = self.write_experiment(
                out_dir, "RSH-001-upstream", duplicate_last_pair=True
            )
            return Completed(0, json.dumps(payload), "")

        code, stdout, stderr = self.invoke(
            self.base_argv("--action", "run", "--execute-unsafe"), fake_run
        )
        self.assertEqual(code, 1, stderr)
        self.assertIn(
            "duplicate matrix pair", json.loads(stdout)["records"][0]["failure_reason"]
        )

    def test_run_requires_unsafe_gate_before_list(self) -> None:
        with mock.patch.object(sweep.subprocess, "run") as run:
            code, stdout, stderr = self.invoke(self.base_argv("--action", "run"), run)

        self.assertEqual(code, 2)
        self.assertIn("execute-unsafe", stderr)
        run.assert_not_called()
        self.assertEqual(stdout, "")

    def test_run_summary_counts_terminal_and_compile_rejection_slots(self) -> None:
        def fake_run(command, **kwargs):
            if "list" in command:
                return Completed(0, json.dumps({"scenarios": [self.scenarios[0]]}))
            out_dir = pathlib.Path(command[command.index("--output-dir") + 1])
            payload = self.write_experiment(
                out_dir,
                "RSH-001-upstream",
                repetitions=2,
                compile_rejection_pair=("patched", "system"),
            )
            return Completed(0, json.dumps(payload), "")

        code, stdout, stderr = self.invoke(
            self.base_argv("--action", "run", "--execute-unsafe", "--repetitions", "2"),
            fake_run,
        )

        self.assertEqual(code, 0, stderr)
        summary = json.loads(stdout)
        self.assertEqual(summary["totals"]["arm_count"], 8)
        self.assertEqual(summary["totals"]["terminal_arm_count"], 8)
        self.assertEqual(summary["totals"]["expected_compile_rejection_count"], 1)
        self.assertEqual(summary["totals"]["planned_repetition_slots"], 14)
        self.assertEqual(summary["totals"]["executed_repetitions"], 14)
        self.assertEqual(summary["totals"]["run_count"], 14)
        self.assertTrue(summary["valid"])

    def test_worker_exception_becomes_scenario_failure_record(self) -> None:
        def fake_run(command, **kwargs):
            if "list" in command:
                return Completed(0, json.dumps({"scenarios": [self.scenarios[0]]}))
            raise RuntimeError("worker exploded")

        code, stdout, stderr = self.invoke(
            self.base_argv("--action", "preflight"), fake_run
        )
        self.assertEqual(code, 1, stderr)
        record = json.loads(stdout)["records"][0]
        self.assertEqual(record["status"], "failed")
        self.assertIn("worker exploded", record["failure_reason"])

    def test_hash_drift_invalidates_summary(self) -> None:
        original_catalog_sha = sweep.sha256_file(self.catalog)

        def fake_run(command, **kwargs):
            if "list" in command:
                return Completed(0, json.dumps({"scenarios": [self.scenarios[0]]}))
            out_dir = pathlib.Path(command[command.index("--output-dir") + 1])
            payload = self.write_preflight(
                out_dir,
                "RSH-001-upstream",
                catalog_sha256=original_catalog_sha,
            )
            self.catalog.write_text('{"catalog":"drifted"}\n', encoding="utf-8")
            return Completed(0, json.dumps(payload), "")

        code, stdout, stderr = self.invoke(
            self.base_argv("--action", "preflight"), fake_run
        )
        self.assertEqual(code, 1, stderr)
        summary = json.loads(stdout)
        self.assertFalse(summary["valid"])
        self.assertIn("catalog", summary["input_drift"])

    def test_rejects_unsafe_scenario_path_segments(self) -> None:
        for bad in ("../x", "x/y", "/x", ""):
            with self.subTest(bad=bad):
                with self.assertRaises(sweep.SweepError):
                    sweep.validate_scenario_id(bad)

    def test_rejects_filesystem_root_and_output_ancestor_of_inputs(self) -> None:
        for output in (pathlib.Path("/"), self.root):
            with self.subTest(output=output):
                argv = self.base_argv()
                argv[argv.index(str(self.output))] = str(output)
                with mock.patch.object(sweep.subprocess, "run") as run:
                    code, _stdout, stderr = self.invoke(argv, run)
                self.assertEqual(code, 2)
                self.assertIn("dedicated sweep directory", stderr)
                run.assert_not_called()

    def test_allow_download_rejects_parallel_scenarios(self) -> None:
        with mock.patch.object(sweep.subprocess, "run") as run:
            code, _stdout, stderr = self.invoke(
                self.base_argv("--allow-download", "--scenario-jobs", "2"), run
            )
        self.assertEqual(code, 2)
        self.assertIn("allow-download", stderr)
        run.assert_not_called()

    def test_unknown_scenario_filter_fails(self) -> None:
        def fake_run(command, **kwargs):
            return Completed(0, self.fake_list_payload())

        code, _stdout, stderr = self.invoke(
            self.base_argv("--scenario", "RSH-999-missing"), fake_run
        )

        self.assertEqual(code, 2)
        self.assertIn("unknown scenario", stderr)


if __name__ == "__main__":
    unittest.main()
