#!/usr/bin/env python3
"""Regressions for the full RustSec heap-candidate inventory."""

from __future__ import annotations

import importlib.util
import json
import pathlib
import tempfile
import textwrap
import unittest
from unittest import mock


ROOT = pathlib.Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "evaluation" / "scripts" / "inventory_rustsec_heap_candidates.py"
CORPUS = ROOT / "evaluation" / "config" / "rustsec_heap_security_corpus.json"
INVENTORY = ROOT / "evaluation" / "config" / "rustsec_heap_candidate_inventory.json"
REVIEW = ROOT / "evaluation" / "config" / "rustsec_temporal_reclaim_review.json"

spec = importlib.util.spec_from_file_location("rustsec_heap_candidate_inventory", SCRIPT)
assert spec is not None and spec.loader is not None
inventory = importlib.util.module_from_spec(spec)
spec.loader.exec_module(inventory)


def advisory_document(
    advisory_id: str,
    package: str,
    title: str,
    *,
    categories: list[str] | None = None,
    keywords: list[str] | None = None,
) -> str:
    category_line = f"categories = {json.dumps(categories)}\n" if categories else ""
    keyword_line = f"keywords = {json.dumps(keywords)}\n" if keywords else ""
    return textwrap.dedent(
        f"""\
        ```toml
        [advisory]
        id = "{advisory_id}"
        package = "{package}"
        date = "2026-01-01"
        {category_line}{keyword_line}
        [versions]
        patched = [">= 1.0.1"]
        ```

        # {title}

        Synthetic test advisory.
        """
    )


class RustSecHeapCandidateInventoryTests(unittest.TestCase):
    def test_repository_inventory_records_the_complete_pinned_screen(self) -> None:
        artifact = json.loads(INVENTORY.read_text(encoding="utf-8"))
        summary = artifact["summary"]

        self.assertFalse(artifact["claim_grade"])
        self.assertEqual(summary["parsed_advisory_count"], 1140)
        self.assertEqual(summary["active_advisory_count"], 875)
        self.assertEqual(summary["memory_corruption_category_count"], 253)
        self.assertEqual(summary["manifest_candidate_screen_count"], 276)
        self.assertEqual(summary["active_manifest_candidate_screen_count"], 275)
        self.assertEqual(summary["expanded_candidate_screen_count"], 348)
        self.assertEqual(summary["active_expanded_candidate_screen_count"], 347)
        self.assertEqual(summary["active_high_recall_review_count"], 439)
        self.assertEqual(summary["temporal_reclaim_title_keyword_candidates"], 84)
        self.assertEqual(summary["temporal_reclaim_selected_in_current_corpus"], 22)
        self.assertEqual(summary["temporal_reclaim_unselected"], 62)
        self.assertEqual(len(artifact["candidates"]), 348)
        self.assertEqual(
            artifact["rudra_poc_summary"],
            {
                "panic_safety_selected_unique_rustsec_id_count": 6,
                "panic_safety_unique_rustsec_id_count": 24,
                "panic_safety_unselected_unique_rustsec_id_count": 18,
                "parseable_poc_count": 165,
                "rustsec_mapped_poc_count": 129,
                "selected_unique_rustsec_id_count": 19,
                "unique_rustsec_id_count": 129,
                "unselected_unique_rustsec_id_count": 110,
            },
        )

        by_id = {entry["advisory_id"]: entry for entry in artifact["candidates"]}
        rkyv = by_id["RUSTSEC-2026-0122"]
        self.assertIn("use_after_free", rkyv["classification_signals"])
        self.assertIn("double_free", rkyv["classification_signals"])
        self.assertIn("tracked_reclaim_review", rkyv["mechanism_review_queues"])

        scc = by_id["RUSTSEC-2026-0205"]
        self.assertFalse(scc["manifest_candidate_screen"])
        self.assertIn("double_free", scc["classification_signals"])
        self.assertIsNone(scc["current_corpus_case_id"])

    def test_manual_temporal_reclaim_review_is_count_consistent(self) -> None:
        artifact = json.loads(INVENTORY.read_text(encoding="utf-8"))
        review = json.loads(REVIEW.read_text(encoding="utf-8"))
        current = {
            case["advisory_id"]
            for case in json.loads(CORPUS.read_text(encoding="utf-8"))["cases"]
        }
        reuse = set(review["strong_candidates"]["cross_type_reuse"])
        reclaim = set(
            review["strong_candidates"]["tracked_reclaim_or_invalid_free"]
        )
        overlap = set(review["strong_candidates"]["overlap"])
        supplemental = set(
            review["strong_candidates"]["supplemental_rudra_source_evidence"]
        )
        strict_strong = reuse | reclaim
        all_strong = strict_strong | supplemental
        excluded = {
            advisory_id
            for values in review["excluded"].values()
            for advisory_id in values
        }
        conditional = {
            entry["advisory_id"] for entry in review["conditional_candidates"]
        }

        self.assertEqual(overlap, reuse & reclaim)
        self.assertEqual(len(reuse), 24)
        self.assertEqual(len(reclaim), 33)
        self.assertEqual(len(strict_strong), 50)
        self.assertEqual(len(supplemental), 3)
        self.assertTrue(strict_strong.isdisjoint(supplemental))
        self.assertEqual(len(all_strong), 53)
        self.assertEqual(len(all_strong & current), 17)
        self.assertEqual(len(all_strong - current), 36)
        self.assertEqual(len(conditional), 7)
        self.assertEqual(len(excluded), 26)
        self.assertTrue(all_strong.isdisjoint(conditional))
        self.assertTrue(all_strong.isdisjoint(excluded))
        self.assertTrue(conditional.isdisjoint(excluded))
        self.assertEqual(len(strict_strong) + len(conditional) + len(excluded), 83)

        inventory_ids = {entry["advisory_id"] for entry in artifact["candidates"]}
        rudra_ids = {
            entry["rustsec_id"]
            for entry in artifact["rudra_pocs"]
            if isinstance(entry["rustsec_id"], str)
        }
        reviewed_ids = all_strong | conditional | excluded
        self.assertTrue(reviewed_ids <= inventory_ids | rudra_ids)
        self.assertEqual(len(review["first_expansion_wave"]["advisory_ids"]), 19)

    def test_title_signals_expand_beyond_exact_manifest_keywords(self) -> None:
        with tempfile.TemporaryDirectory() as raw_tmp:
            root = pathlib.Path(raw_tmp)
            db = root / "db"
            cases = [
                (
                    "RUSTSEC-2026-0001",
                    "category-only",
                    "Memory corruption with a neutral title",
                    ["memory-corruption"],
                    None,
                ),
                (
                    "RUSTSEC-2026-0002",
                    "spaced-keyword",
                    "Use after free in a safe API",
                    None,
                    None,
                ),
                (
                    "RUSTSEC-2026-0003",
                    "panic-drop",
                    "Elements may double drop during unwind",
                    None,
                    None,
                ),
                (
                    "RUSTSEC-2026-0004",
                    "integer-only",
                    "Integer overflow causes a clean error",
                    None,
                    None,
                ),
            ]
            for advisory_id, package, title, categories, keywords in cases:
                crate = db / "crates" / package
                crate.mkdir(parents=True)
                (crate / f"{advisory_id}.md").write_text(
                    advisory_document(
                        advisory_id,
                        package,
                        title,
                        categories=categories,
                        keywords=keywords,
                    ),
                    encoding="utf-8",
                )

            corpus_value = {
                "source_snapshots": {
                    "rustsec_advisory_db": {
                        "commit": "a" * 40,
                        "commit_time": "2026-01-01T00:00:00Z",
                        "url": "https://example.invalid/rustsec",
                        "memory_corruption_category_count": 1,
                        "candidate_screen_query": {
                            "category_any": ["memory-corruption"],
                            "keyword_any": ["use-after-free", "double-free"],
                        },
                    }
                },
                "cases": [
                    {
                        "advisory_id": "RUSTSEC-2026-0001",
                        "case_id": "RSH-001",
                    }
                ],
            }
            corpus_path = root / "corpus.json"
            corpus_path.write_text(json.dumps(corpus_value), encoding="utf-8")

            with mock.patch.object(inventory, "git_head", return_value="a" * 40):
                result = inventory.build_inventory(db, corpus_path)

        self.assertEqual(result["summary"]["manifest_candidate_screen_count"], 1)
        self.assertEqual(result["summary"]["expanded_candidate_screen_count"], 3)
        by_id = {entry["advisory_id"]: entry for entry in result["candidates"]}
        self.assertIn("use_after_free", by_id["RUSTSEC-2026-0002"]["classification_signals"])
        self.assertIn("double_free", by_id["RUSTSEC-2026-0003"]["classification_signals"])
        self.assertNotIn("RUSTSEC-2026-0004", by_id)

    def test_commit_mismatch_fails_closed(self) -> None:
        corpus_value = json.loads(CORPUS.read_text(encoding="utf-8"))
        with tempfile.TemporaryDirectory() as raw_tmp:
            root = pathlib.Path(raw_tmp)
            corpus_path = root / "corpus.json"
            corpus_path.write_text(json.dumps(corpus_value), encoding="utf-8")
            with mock.patch.object(inventory, "git_head", return_value="b" * 40):
                with self.assertRaisesRegex(inventory.InventoryError, "commit mismatch"):
                    inventory.build_inventory(root, corpus_path)

    def test_generic_integer_overflow_is_not_a_spatial_signal(self) -> None:
        self.assertEqual(
            inventory.classification_signals(
                "Integer overflow causes a denial of service", []
            ),
            [],
        )


if __name__ == "__main__":
    unittest.main()
