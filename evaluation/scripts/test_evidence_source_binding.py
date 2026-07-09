#!/usr/bin/env python3
"""Regression tests for repository-source binding across long local runs."""

from __future__ import annotations

import importlib.util
import json
import pathlib
import tempfile
import types
import unittest
from typing import Optional
from unittest import mock


ROOT = pathlib.Path(__file__).resolve().parents[2]
EVALUATE_PATH = ROOT / "evaluation" / "scripts" / "evaluate.py"

spec = importlib.util.spec_from_file_location("unialloc_evaluate_source_binding", EVALUATE_PATH)
assert spec is not None and spec.loader is not None
evaluate = importlib.util.module_from_spec(spec)
spec.loader.exec_module(evaluate)


def fingerprint(digest: str, *, head: str = "head") -> dict:
    return {
        "schema_version": evaluate.REPOSITORY_SOURCE_FINGERPRINT_SCHEMA_VERSION,
        "algorithm": "sha256",
        "source_digest": digest,
        "head_commit": head,
        "working_tree_dirty": True,
    }


class EvidenceSourceBindingTests(unittest.TestCase):
    def test_run_local_binds_each_run_and_leaves_mixed_summary_unbound(self) -> None:
        first = fingerprint("1" * 64, head="before")
        changed = fingerprint("2" * 64, head="after")
        args = types.SimpleNamespace(
            run_id="source-change-during-local-run",
            features=["bench_ourself"],
            runs=2,
            profile="release",
            bench_filter=None,
            timeout=1,
            keep_going=True,
        )

        def fake_run_one(
            command: list[str],
            out_dir: pathlib.Path,
            label: str,
            feature: str,
            run_index: int,
            timeout: int,
            bench_filter: Optional[str],
        ) -> dict:
            return {
                "schema_version": 1,
                "label": label,
                "feature": feature,
                "run_index": run_index,
                "bench_filter": bench_filter,
                "command": command,
                "exit_code": 0,
                "wall_seconds": 0.01,
                "bench_results": [],
            }

        with tempfile.TemporaryDirectory() as tmpdir:
            raw = pathlib.Path(tmpdir)
            with (
                mock.patch.object(evaluate, "RAW", raw),
                mock.patch.object(evaluate.shutil, "which", return_value="/usr/bin/cargo"),
                mock.patch.object(evaluate, "run_one", side_effect=fake_run_one),
                mock.patch.object(
                    evaluate,
                    "repository_source_fingerprint",
                    side_effect=[first, first, first, changed],
                ),
            ):
                self.assertEqual(evaluate.run_local(args), 0)

            run_dir = raw / args.run_id
            records = [
                json.loads(line)
                for line in (run_dir / "runs.jsonl").read_text(encoding="utf-8").splitlines()
                if line.strip()
            ]
            summary = json.loads((run_dir / "run_summary.json").read_text(encoding="utf-8"))

        self.assertEqual(records[0]["evidence_source_fingerprint"]["source_digest"], "1" * 64)
        self.assertNotIn("evidence_source_fingerprint", records[1])
        self.assertIn("repository source changed", " ".join(records[1]["evidence_source_binding_blockers"]))
        self.assertNotIn("evidence_source_fingerprint", summary)
        self.assertIn("do not share one stable", " ".join(summary["evidence_source_binding_blockers"]))

    def test_claim_check_demotes_pass_when_source_changes_during_evaluation(self) -> None:
        first = fingerprint("1" * 64, head="before")
        changed = fingerprint("2" * 64, head="after")
        claims = [
            {"id": "C-test", "metric": "test", "required_for_overclaim": True},
        ]
        passing = {
            "claim_id": "C-test",
            "metric": "test",
            "required_for_overclaim": True,
            "status": "pass",
            "evidence": [],
            "repository_source_binding": {"ready": True, "blockers": []},
        }

        with (
            mock.patch.object(evaluate, "load_config", return_value={"claims": claims}),
            mock.patch.object(evaluate, "load_current_summary", return_value={"datasets": {}}),
            mock.patch.object(evaluate, "evaluate_claim", return_value=passing),
            mock.patch.object(
                evaluate,
                "apply_claim_repository_source_binding",
                side_effect=lambda result, _summary, current: dict(result),
            ),
            mock.patch.object(
                evaluate,
                "repository_source_fingerprint",
                side_effect=[first, changed],
            ),
        ):
            report = evaluate.build_claim_check_report("current")

        self.assertFalse(report["source_stable_during_claim_check"])
        self.assertEqual(report["started_repository_source_fingerprint"], first)
        self.assertEqual(report["current_repository_source_fingerprint"], changed)
        self.assertEqual(report["results"][0]["status"], "missing")
        self.assertEqual(report["summary"]["passing_count"], 0)
        self.assertEqual(report["summary"]["missing_count"], 1)
        self.assertIn("repository source changed", " ".join(report["claim_check_blockers"]))


if __name__ == "__main__":
    unittest.main()
