#!/usr/bin/env python3
"""Focused regressions for current-dataset provenance and source binding."""

from __future__ import annotations

import argparse
import contextlib
import importlib.util
import io
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


DATASET_NAME = "default_performance"
DATASET_FILE = "default-perf.dat"
DATASET_TEXT = '# # jemalloc\n1 "Collections" 1.0\n'


def write_json(path: pathlib.Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def source_fingerprint(
    digest: str = "a" * 64,
    *,
    head: str = "1" * 40,
    dirty: bool = False,
) -> dict:
    return {
        "schema_version": evaluate.REPOSITORY_SOURCE_FINGERPRINT_SCHEMA_VERSION,
        "algorithm": "sha256",
        "source_digest": digest,
        "head_commit": head,
        "working_tree_dirty": dirty,
        "working_tree_status_line_count": 3 if dirty else 0,
    }


def dataset_config(paper_dir: pathlib.Path) -> dict:
    return {
        "paper_data": {DATASET_NAME: DATASET_FILE},
        "paper_dir_default": str(paper_dir),
    }


def write_source_fixture(base: pathlib.Path, fingerprint: dict) -> tuple[pathlib.Path, pathlib.Path, pathlib.Path]:
    data_dir = base / "data"
    data_dir.mkdir(parents=True)
    dataset = data_dir / DATASET_FILE
    dataset.write_text(DATASET_TEXT, encoding="utf-8")
    provenance = data_dir / "run-record.jsonl"
    provenance.write_text('{"seconds": 1.0}\n', encoding="utf-8")
    manifest = {
        "schema_version": 1,
        "source": "unit-test-current-datasets",
        "claim_grade": True,
        "complete_for_claim": True,
        "evidence_source_fingerprint": fingerprint,
        "datasets": {
            DATASET_NAME: {
                "path": dataset.name,
                "sha256": evaluate.file_sha256(dataset),
                "claim_grade": True,
                "complete_for_claim": True,
                "evidence": [
                    {
                        "path": dataset.name,
                        "sha256": evaluate.file_sha256(dataset),
                        "kind": "dataset",
                    },
                    {
                        "path": provenance.name,
                        "sha256": evaluate.file_sha256(provenance),
                        "kind": "raw-run-record",
                    },
                ],
            }
        },
    }
    manifest_path = data_dir / "dataset-manifest.json"
    write_json(manifest_path, manifest)
    return data_dir, dataset, provenance


@contextlib.contextmanager
def isolated_dataset_environment(
    root: pathlib.Path,
    *,
    fingerprint: dict,
):
    paper_dir = root / "paper"
    paper_dir.mkdir()
    (paper_dir / DATASET_FILE).write_text(DATASET_TEXT, encoding="utf-8")
    results = root / "results"
    reports = root / "reports"
    results.mkdir()
    reports.mkdir()
    with (
        mock.patch.object(evaluate, "RESULTS", results),
        mock.patch.object(evaluate, "REPORTS", reports),
        mock.patch.object(evaluate, "load_config", return_value=dataset_config(paper_dir)),
        mock.patch.object(
            evaluate,
            "repository_source_fingerprint",
            return_value=fingerprint,
        ),
    ):
        yield paper_dir, results, reports


class DatasetProvenanceGateTests(unittest.TestCase):
    def test_import_demotes_source_bound_dataset_after_raw_evidence_tamper(self) -> None:
        with tempfile.TemporaryDirectory() as raw_tmp:
            root = pathlib.Path(raw_tmp)
            fingerprint = source_fingerprint()
            data_dir, dataset, provenance = write_source_fixture(root / "source", fingerprint)
            args = argparse.Namespace(
                data_dir=str(data_dir),
                manifest=str(data_dir / "dataset-manifest.json"),
                dataset=[DATASET_NAME],
                quiet=True,
            )
            with isolated_dataset_environment(root, fingerprint=fingerprint) as (_, results, _):
                self.assertEqual(evaluate.import_current_datasets(args), 0)
                accepted = evaluate.read_json(results / "current_datasets_summary.json")
                accepted_dataset = accepted["datasets"][DATASET_NAME]
                self.assertTrue(accepted_dataset["claim_grade"])
                self.assertTrue(accepted_dataset["scope"]["complete_for_claim"])
                self.assertTrue(accepted_dataset["scope"]["evidence_verified"])
                self.assertEqual(accepted_dataset["scope"]["mismatched_evidence_sha256"], [])
                self.assertIn(str(dataset.resolve()), accepted_dataset["scope"]["verified_evidence"])
                self.assertIn(str(provenance.resolve()), accepted_dataset["scope"]["verified_evidence"])

                provenance.write_text('{"seconds": 9.9, "tampered": true}\n', encoding="utf-8")
                self.assertEqual(evaluate.import_current_datasets(args), 0)
                rejected = evaluate.read_json(results / "current_datasets_summary.json")
                rejected_dataset = rejected["datasets"][DATASET_NAME]
                self.assertFalse(rejected_dataset["claim_grade"])
                self.assertFalse(rejected_dataset["scope"]["complete_for_claim"])
                self.assertFalse(rejected_dataset["scope"]["evidence_verified"])
                self.assertTrue(
                    any(
                        provenance.name in mismatch
                        for mismatch in rejected_dataset["scope"]["mismatched_evidence_sha256"]
                    )
                )
                self.assertTrue(
                    any(
                        "sha256 mismatch" in blocker and provenance.name in blocker
                        for blocker in rejected_dataset["scope"]["claim_grade_blockers"]
                    )
                )

    def test_cached_dataset_integrity_is_rechecked_without_reimport(self) -> None:
        with tempfile.TemporaryDirectory() as raw_tmp:
            root = pathlib.Path(raw_tmp)
            fingerprint = source_fingerprint()
            data_dir, _dataset, provenance = write_source_fixture(
                root / "source",
                fingerprint,
            )
            args = argparse.Namespace(
                data_dir=str(data_dir),
                manifest=str(data_dir / "dataset-manifest.json"),
                dataset=[DATASET_NAME],
                quiet=True,
            )
            with isolated_dataset_environment(root, fingerprint=fingerprint) as (_, results, _):
                self.assertEqual(evaluate.import_current_datasets(args), 0)
                cached = evaluate.read_json(results / "current_datasets_summary.json")
                self.assertTrue(
                    evaluate.fresh_current_dataset_integrity_status(
                        cached,
                        DATASET_NAME,
                        current=fingerprint,
                    )["ready"]
                )

                provenance.write_text(
                    '{"seconds": 4.2, "tampered_after_import": true}\n',
                    encoding="utf-8",
                )
                fresh = evaluate.fresh_current_dataset_integrity_status(
                    cached,
                    DATASET_NAME,
                    current=fingerprint,
                )

            self.assertFalse(fresh["ready"])
            self.assertIn("sha256 mismatch", " ".join(fresh["blockers"]))
            self.assertIn(provenance.name, " ".join(fresh["blockers"]))

    def test_cached_dataset_metrics_are_recomputed_from_hashed_bytes(self) -> None:
        with tempfile.TemporaryDirectory() as raw_tmp:
            root = pathlib.Path(raw_tmp)
            fingerprint = source_fingerprint()
            data_dir, _dataset, _provenance = write_source_fixture(
                root / "source",
                fingerprint,
            )
            args = argparse.Namespace(
                data_dir=str(data_dir),
                manifest=str(data_dir / "dataset-manifest.json"),
                dataset=[DATASET_NAME],
                quiet=True,
            )
            with isolated_dataset_environment(root, fingerprint=fingerprint) as (_, results, _):
                self.assertEqual(evaluate.import_current_datasets(args), 0)
                cached = evaluate.read_json(results / "current_datasets_summary.json")
                cached["datasets"][DATASET_NAME]["speedup_percent_vs_baselines"] = 9999.0

                fresh = evaluate.fresh_current_dataset_integrity_status(
                    cached,
                    DATASET_NAME,
                    current=fingerprint,
                )

            self.assertFalse(fresh["ready"])
            self.assertEqual(
                fresh["metric_mismatches"],
                ["speedup_percent_vs_baselines"],
            )
            self.assertIn(
                "cached dataset metrics differ from freshly parsed dataset bytes",
                " ".join(fresh["blockers"]),
            )

    def test_cached_dataset_claim_state_is_recomputed_from_manifest_and_bytes(self) -> None:
        with tempfile.TemporaryDirectory() as raw_tmp:
            root = pathlib.Path(raw_tmp)
            fingerprint = source_fingerprint()
            data_dir, _dataset, _provenance = write_source_fixture(
                root / "source",
                fingerprint,
            )
            args = argparse.Namespace(
                data_dir=str(data_dir),
                manifest=str(data_dir / "dataset-manifest.json"),
                dataset=[DATASET_NAME],
                quiet=True,
            )
            with isolated_dataset_environment(root, fingerprint=fingerprint) as (_, results, _):
                self.assertEqual(evaluate.import_current_datasets(args), 0)
                cached = evaluate.read_json(results / "current_datasets_summary.json")
                cached_dataset = cached["datasets"][DATASET_NAME]
                cached_dataset["claim_grade"] = False
                cached_dataset["scope"]["complete_for_claim"] = False

                fresh = evaluate.fresh_current_dataset_integrity_status(
                    cached,
                    DATASET_NAME,
                    current=fingerprint,
                )

            self.assertFalse(fresh["ready"])
            self.assertEqual(
                fresh["claim_state_mismatches"],
                ["claim_grade", "complete_for_claim"],
            )
            self.assertTrue(fresh["fresh_claim_state"]["claim_grade"])

    def test_merge_demotes_dataset_after_dataset_or_provenance_tamper(self) -> None:
        for tamper_target in ("dataset", "provenance"):
            with self.subTest(tamper_target=tamper_target), tempfile.TemporaryDirectory() as raw_tmp:
                root = pathlib.Path(raw_tmp)
                fingerprint = source_fingerprint()
                data_dir, dataset, provenance = write_source_fixture(root / "source", fingerprint)
                if tamper_target == "dataset":
                    dataset.write_text(
                        '# # jemalloc\n1 "Collections" 7.0\n',
                        encoding="utf-8",
                    )
                    expected_tampered_name = dataset.name
                else:
                    provenance.write_text(
                        '{"seconds": 7.0, "tampered": true}\n',
                        encoding="utf-8",
                    )
                    expected_tampered_name = provenance.name

                out_dir = root / "merged"
                args = argparse.Namespace(
                    data_dir=[str(data_dir)],
                    run_id=f"merge-{tamper_target}",
                    output_dir=str(out_dir),
                    dataset=[DATASET_NAME],
                    prefer="last",
                    import_results=False,
                )
                with isolated_dataset_environment(root, fingerprint=fingerprint):
                    with contextlib.redirect_stdout(io.StringIO()):
                        self.assertEqual(evaluate.merge_current_datasets(args), 0)

                merged = evaluate.read_json(out_dir / "dataset-manifest.json")
                merged_dataset = merged["datasets"][DATASET_NAME]
                self.assertFalse(merged_dataset["claim_grade"])
                self.assertFalse(merged_dataset["complete_for_claim"])
                self.assertFalse(merged_dataset["scope"]["merged_source_evidence_verified"])
                self.assertTrue(
                    any(
                        "sha256 mismatch" in blocker and expected_tampered_name in blocker
                        for blocker in merged_dataset["claim_grade_blockers"]
                    )
                )

    def test_shared_source_fingerprint_ignores_head_and_dirty_metadata(self) -> None:
        first = source_fingerprint(head="1" * 40, dirty=False)
        same_source_different_checkout_state = source_fingerprint(
            head="2" * 40,
            dirty=True,
        )

        shared = evaluate.shared_evidence_source_fingerprint(
            [
                {"evidence_source_fingerprint": first},
                {
                    "evidence_source_fingerprint": same_source_different_checkout_state,
                },
            ]
        )

        self.assertIsNotNone(shared)
        assert shared is not None
        self.assertEqual(shared["source_digest"], first["source_digest"])
        self.assertEqual(shared["head_commit"], first["head_commit"])
        self.assertFalse(shared["working_tree_dirty"])

        different_source = source_fingerprint(digest="b" * 64)
        self.assertIsNone(
            evaluate.shared_evidence_source_fingerprint(
                [
                    {"evidence_source_fingerprint": first},
                    {"evidence_source_fingerprint": different_source},
                ]
            )
        )


if __name__ == "__main__":
    unittest.main()
