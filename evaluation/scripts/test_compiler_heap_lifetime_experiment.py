#!/usr/bin/env python3
"""Unit tests for the compiler heap-lifetime A/B experiment."""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path
import sys
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "evaluation" / "scripts" / "compiler_heap_lifetime_experiment.py"
spec = importlib.util.spec_from_file_location("compiler_heap_lifetime_experiment", SCRIPT)
assert spec is not None and spec.loader is not None
experiment = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = experiment
spec.loader.exec_module(experiment)


def candidate(
    index: int,
    basis: str,
    hint: int,
    confidence: int,
    *,
    exact_drop_path: bool = False,
) -> dict[str, object]:
    row: dict[str, object] = {
        "lowering_kind": experiment.ALLOCATION_LOWERING_KIND,
        "callsite": 1000 + index,
        "type_id": 2000 + index,
        "module_id": 3000,
        "mir_function": f"fixture::{index}",
        "destination_place": f"_{index}",
        "lifetime_hint": hint,
        "lifetime_hint_confidence": confidence,
        "lifetime_hint_basis": basis,
    }
    if exact_drop_path:
        row["lifetime_analysis_features"] = {"exact_drop_path": True}
    return row


def write_audit(root: Path, rows: list[dict[str, object]], *, enabled: bool) -> None:
    root.mkdir(parents=True, exist_ok=True)
    (root / "fixture.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "source": "unialloc-rustc-driver-mir-rewrite-dry-run",
                "compiler_pass": {
                    "automatic_heap_lifetime_inference_enabled": enabled,
                },
                "rewrite_candidates": rows,
            }
        ),
        encoding="utf-8",
    )


def result(arm: str, *, checksum: int = 77) -> dict[str, object]:
    policy = {
        "default": 0,
        "runtime-adaptive": 5,
        "hints-ordinary": 7,
        "hints-thp": 6,
    }[arm]
    record: dict[str, object] = {
        "passed": True,
        "arm": arm,
        "policy": policy,
        "checksum": checksum,
        "allocation_ns": 400_000_000,
        "touch_ns": 500_000_000,
        "workload_ns": 1_000_000_000,
        "mapping_failures": 0,
        "smaps_rss_kib": 100,
        "smaps_anon_hugepages_kib": 2048 if arm == "hints-thp" else 0,
    }
    for field in experiment.RUNTIME_COUNTER_FIELDS:
        record.setdefault(field, 0)
    if arm in {"hints-ordinary", "hints-thp"}:
        record["compiler_inferred_direct_long_routes"] = 2
        record["compiler_inferred_direct_long_routes_without_trailer"] = 2
        record["compiler_inferred_direct_long_requested_bytes"] = 8192
        record["compiler_inferred_direct_long_slot_bytes"] = 8192
    if arm == "hints-thp":
        record["compiler_inferred_density_promotion_successes"] = 1
    return record


def sample(
    arm: str,
    block: int,
    *,
    wall: float,
    rss: int,
    checksum: int = 77,
    warmup: bool = False,
) -> dict[str, object]:
    workload = result(arm, checksum=checksum)
    workload["allocation_ns"] = int(wall * 0.4 * 1_000_000_000)
    workload["touch_ns"] = int(wall * 0.5 * 1_000_000_000)
    workload["workload_ns"] = int(wall * 1_000_000_000)
    return {
        "arm": arm,
        "block": block,
        "warmup": warmup,
        "outer_wall_seconds": wall,
        "gnu_time": {"elapsed_seconds": wall, "max_rss_kib": rss},
        "result": workload,
    }


class CompilerHeapLifetimeExperimentTests(unittest.TestCase):
    def test_prefixed_json_parser_uses_last_valid_record(self) -> None:
        text = "\n".join(
            (
                experiment.RESULT_PREFIX + json.dumps({"value": 1}),
                experiment.RESULT_PREFIX + "broken",
                experiment.RESULT_PREFIX + json.dumps({"value": 2}),
            )
        )
        self.assertEqual(experiment.parse_prefixed_json(text), {"value": 2})
        self.assertIsNone(experiment.parse_prefixed_json("ordinary output"))

    def test_gnu_time_parser_requires_complete_numeric_row(self) -> None:
        row = experiment.GNU_TIME_PREFIX + "\t1.25\t0.8\t0.2\t4096\t2\t30\t0\n"
        parsed = experiment.parse_gnu_time(row)
        self.assertEqual(parsed["elapsed_seconds"], 1.25)
        self.assertEqual(parsed["max_rss_kib"], 4096)
        self.assertEqual(parsed["exit_status"], 0)
        with self.assertRaises(experiment.ExperimentError):
            experiment.parse_gnu_time(experiment.GNU_TIME_PREFIX + "\tbroken")

    def test_compiler_summary_counts_exact_patterns_and_unknown(self) -> None:
        exact_rows = [
            candidate(index, basis, hint, 100)
            for index, (basis, hint) in enumerate(experiment.PROVEN_BASIS_HINTS.items())
        ]
        exact_rows.extend(
            candidate(
                10 + index,
                "automatic_heap_cleanup_or_unwind_unknown",
                0,
                0,
                exact_drop_path=True,
            )
            for index in range(2)
        )
        exact_rows.append(
            candidate(20, "automatic_heap_nonlinear_control_flow_unknown", 0, 0)
        )
        baseline_rows = [candidate(30, "default_unknown", 0, 0)]
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            write_audit(root / "baseline", baseline_rows, enabled=False)
            write_audit(root / "inferred", exact_rows, enabled=True)
            baseline = experiment.summarize_compiler_audits(root / "baseline")
            inferred = experiment.summarize_compiler_audits(root / "inferred")
            experiment.validate_compiler_contract(baseline, inferred)

        self.assertEqual(inferred["bounded_mechanism_site_count"], 2)
        self.assertEqual(inferred["proven_scoped_site_count"], 0)
        self.assertEqual(inferred["eventual_release_fact_site_count"], 2)
        self.assertEqual(inferred["bounded_process_long_oracle_site_count"], 2)
        self.assertEqual(inferred["heap_unknown_site_count"], 3)
        self.assertAlmostEqual(inferred["bounded_mechanism_coverage"], 0.4)

    def test_compiler_summary_rejects_a_proven_basis_with_wrong_hint(self) -> None:
        rows = [
            candidate(
                0,
                "automatic_heap_exact_box_leak",
                experiment.PROVEN_SCOPED_HINT,
                100,
            )
        ]
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            write_audit(root, rows, enabled=True)
            with self.assertRaises(experiment.ExperimentError):
                experiment.summarize_compiler_audits(root)

    def test_compiler_summary_rejects_an_unrecognized_proven_basis(self) -> None:
        rows = [
            candidate(
                0,
                "automatic_heap_future_proof",
                experiment.BOUNDED_PROCESS_LONG_ORACLE_HINT,
                100,
            )
        ]
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            write_audit(root, rows, enabled=True)
            with self.assertRaises(experiment.ExperimentError):
                experiment.summarize_compiler_audits(root)

    def test_paired_bootstrap_preserves_a_constant_effect(self) -> None:
        effect = experiment.paired_lower_is_better_effect(
            [(10.0, 8.0), (20.0, 16.0), (5.0, 4.0)],
            resamples=200,
            seed=1,
        )
        self.assertAlmostEqual(effect["median_percent"], 25.0)
        self.assertAlmostEqual(effect["bootstrap_95_low_percent"], 25.0)
        self.assertAlmostEqual(effect["bootstrap_95_high_percent"], 25.0)

    def test_summary_excludes_warmups_and_builds_paired_comparisons(self) -> None:
        rows = [
            sample("default", 99, wall=99.0, rss=999, warmup=True),
            sample("runtime-adaptive", 99, wall=99.0, rss=999, warmup=True),
            sample("hints-ordinary", 99, wall=99.0, rss=999, warmup=True),
            sample("hints-thp", 99, wall=99.0, rss=999, warmup=True),
        ]
        for block in range(2):
            rows.extend(
                (
                    sample("default", block, wall=10.0, rss=100),
                    sample("runtime-adaptive", block, wall=9.0, rss=90),
                    sample("hints-ordinary", block, wall=8.0, rss=80),
                    sample("hints-thp", block, wall=5.0, rss=90),
                )
            )
        summary = experiment.summarize_samples(rows, bootstrap_resamples=200, seed=7)

        self.assertEqual(summary["correctness_checksum"], "77")
        self.assertEqual(summary["per_arm"]["default"]["measured_runs"], 2)
        self.assertAlmostEqual(
            summary["per_arm"]["default"]["median_allocation_seconds"], 4.0
        )
        layout = summary["comparisons"]["compiler_layout_vs_default"]
        self.assertAlmostEqual(layout["metrics"]["outer_wall_seconds"]["median_percent"], 25.0)
        self.assertAlmostEqual(layout["metrics"]["allocation_seconds"]["median_percent"], 25.0)
        self.assertAlmostEqual(layout["metrics"]["touch_seconds"]["median_percent"], 25.0)
        self.assertTrue(summary["thp_gate"]["claim_ready"])
        self.assertFalse(
            summary["external_baseline_gate"][
                "gperftools_legacy_eligible_as_google_tcmalloc"
            ]
        )

    def test_thp_gate_requires_every_measured_pair_to_have_actual_backing(self) -> None:
        rows = []
        for block in range(2):
            rows.extend(
                (
                    sample("default", block, wall=10.0, rss=100),
                    sample("runtime-adaptive", block, wall=9.0, rss=90),
                    sample("hints-ordinary", block, wall=8.0, rss=80),
                    sample("hints-thp", block, wall=5.0, rss=90),
                )
            )
        rows[-1]["result"]["smaps_anon_hugepages_kib"] = 0

        summary = experiment.summarize_samples(rows, bootstrap_resamples=20, seed=1)

        self.assertFalse(summary["thp_gate"]["claim_ready"])
        self.assertEqual(summary["thp_gate"]["included_blocks"], [0])
        self.assertEqual(summary["thp_gate"]["excluded_blocks"], [1])
        thp_effect = summary["comparisons"]["thp_increment_vs_same_hints"]
        self.assertEqual(thp_effect["included_blocks"], [0])
        self.assertEqual(thp_effect["metrics"]["touch_seconds"]["paired_samples"], 1)

    def test_correctness_mismatch_fails_closed(self) -> None:
        rows = [
            sample("default", 0, wall=10.0, rss=100, checksum=1),
            sample("runtime-adaptive", 0, wall=9.0, rss=90, checksum=1),
            sample("hints-ordinary", 0, wall=8.0, rss=80, checksum=2),
            sample("hints-thp", 0, wall=5.0, rss=90, checksum=1),
        ]
        with self.assertRaises(experiment.ExperimentError):
            experiment.summarize_samples(rows, bootstrap_resamples=20, seed=1)

    def test_ordinary_control_rejects_nohugepage_advice_failure(self) -> None:
        rows = [
            sample("default", 0, wall=10.0, rss=100),
            sample("runtime-adaptive", 0, wall=9.0, rss=90),
            sample("hints-ordinary", 0, wall=8.0, rss=80),
            sample("hints-thp", 0, wall=5.0, rss=90),
        ]
        rows[2]["result"]["nohugepage_advice_failures"] = 1
        with self.assertRaises(experiment.ExperimentError):
            experiment.summarize_samples(rows, bootstrap_resamples=20, seed=1)

    def test_inferred_arm_rejects_a_bounded_long_oracle_deallocation(self) -> None:
        rows = [
            sample("default", 0, wall=10.0, rss=100),
            sample("runtime-adaptive", 0, wall=9.0, rss=90),
            sample("hints-ordinary", 0, wall=8.0, rss=80),
            sample("hints-thp", 0, wall=5.0, rss=90),
        ]
        rows[3]["result"]["compiler_inferred_direct_long_deallocations"] = 1
        with self.assertRaises(experiment.ExperimentError):
            experiment.summarize_samples(rows, bootstrap_resamples=20, seed=1)

    def test_compiler_environment_selects_marker_free_inference_explicitly(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            env = experiment.compiler_environment(
                wrapper=root / "wrapper",
                sysroot=root / "sysroot",
                audit_dir=root / "audit",
                log_dir=root / "logs",
                inference=True,
                target_dir=root / "target",
            )
        self.assertEqual(env["UNIALLOC_AUTO_HEAP_LIFETIME_INFERENCE"], "1")
        self.assertEqual(env["UNIALLOC_ACTUAL_SEMANTIC_SCOPE_REWRITE"], "1")
        self.assertEqual(env["UNIALLOC_RUSTC_TARGET_CRATES"], experiment.TARGET_CRATE)

    def test_fixture_crate_inherits_the_repository_lockfile(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            crate = experiment.create_fixture_crate(Path(temporary))
            self.assertEqual(
                (crate / "Cargo.lock").read_bytes(),
                (experiment.ROOT / "Cargo.lock").read_bytes(),
            )

    def test_fixture_contains_all_required_patterns_and_matched_policies(self) -> None:
        source = experiment.FIXTURE_SOURCE.read_text(encoding="utf-8")
        for marker in (
            "fn exact_local_drop",
            "fn exact_move_chain_drop",
            "fn exact_mem_forget",
            "fn exact_box_leak",
            "fn ambiguous_fallback",
            "LifetimeHugepagePolicy::AdaptiveRuntimeHugepage",
            "LifetimeHugepagePolicy::CompilerInferredOrdinary",
            "LifetimeHugepagePolicy::CompilerInferredHugepage",
        ):
            self.assertIn(marker, source)


if __name__ == "__main__":
    unittest.main()
