#!/usr/bin/env python3
"""Unit tests for the complete std_bench allocator campaign."""

from __future__ import annotations

import importlib.util
import json
import sys
import tempfile
import unittest
from pathlib import Path


SCRIPT = Path(__file__).with_name("run_std_bench_allocator_full.py")
SPEC = importlib.util.spec_from_file_location("run_std_bench_allocator_full", SCRIPT)
assert SPEC is not None and SPEC.loader is not None
campaign = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = campaign
SPEC.loader.exec_module(campaign)


def process_record(
    *,
    benchmark: str,
    allocator: str,
    phase: str,
    round_index: int,
    ns_per_iter: float | None = None,
    timed_out: bool = False,
) -> dict[str, object]:
    variant = next(
        item for item in campaign.VARIANTS if item.allocator == allocator
    )
    valid = not timed_out
    record: dict[str, object] = {
        "schema_version": 2,
        "phase": phase,
        "round": round_index,
        "allocator": allocator,
        "feature": variant.feature,
        "benchmark": benchmark,
        "valid": valid,
        "timed_out": timed_out,
        "status": "valid" if valid else "timeout_censored",
        "timeout_seconds": 30,
        "cpu": 20,
        "numa_node": 0,
        "stdout_path": f"runs/{phase}/{round_index}/{allocator}/{benchmark}.stdout",
    }
    if ns_per_iter is not None:
        record["ns_per_iter"] = ns_per_iter
    return record


class FullCampaignTest(unittest.TestCase):
    def test_inventory_requires_the_complete_canonical_surface(self) -> None:
        inventory = [f"family::bench_{index:03d}" for index in range(468)]
        self.assertEqual(
            tuple(inventory), campaign.validate_canonical_inventory(inventory)
        )
        with self.assertRaisesRegex(RuntimeError, "468"):
            campaign.validate_canonical_inventory(inventory[:-1])
        with self.assertRaisesRegex(RuntimeError, "duplicate"):
            campaign.validate_canonical_inventory(inventory[:-1] + [inventory[0]])

    def test_lane_assignment_is_total_fixed_and_uses_explicit_cpus(self) -> None:
        benchmarks = tuple(f"family::bench_{index}" for index in range(13))
        lanes = campaign.assign_benchmark_lanes(benchmarks, (20, 22, 24))
        self.assertEqual([20, 22, 24], [lane.cpu for lane in lanes])
        assigned = [benchmark for lane in lanes for benchmark in lane.benchmarks]
        self.assertCountEqual(benchmarks, assigned)
        self.assertEqual(len(benchmarks), len(set(assigned)))
        for index, benchmark in enumerate(benchmarks):
            lane = lanes[index % len(lanes)]
            self.assertIn(benchmark, lane.benchmarks)

        self.assertEqual((20, 22, 24), campaign.parse_cpu_list("20,22,24"))
        with self.assertRaisesRegex(ValueError, "unique"):
            campaign.parse_cpu_list("20,20")

    def test_cell_state_accepts_only_a_valid_prefix_or_terminal_timeout(self) -> None:
        benchmark = "vec::bench"
        allocator = "unialloc"
        complete = [
            process_record(
                benchmark=benchmark,
                allocator=allocator,
                phase="warmup",
                round_index=0,
                ns_per_iter=100,
            ),
            *[
                process_record(
                    benchmark=benchmark,
                    allocator=allocator,
                    phase="measured",
                    round_index=round_index,
                    ns_per_iter=100 + round_index,
                )
                for round_index in range(1, 4)
            ],
        ]
        state = campaign.classify_cell_history(complete, measured_rounds=3)
        self.assertEqual("complete", state.status)
        self.assertEqual((1, 2, 3), state.valid_measured_rounds)

        warmup_timeout = [
            process_record(
                benchmark=benchmark,
                allocator=allocator,
                phase="warmup",
                round_index=0,
                timed_out=True,
            )
        ]
        state = campaign.classify_cell_history(warmup_timeout, measured_rounds=3)
        self.assertEqual("censored", state.status)
        self.assertEqual("warmup", state.timeout_phase)

        measured_timeout = complete[:2] + [
            process_record(
                benchmark=benchmark,
                allocator=allocator,
                phase="measured",
                round_index=2,
                timed_out=True,
            )
        ]
        state = campaign.classify_cell_history(measured_timeout, measured_rounds=3)
        self.assertEqual("censored", state.status)
        self.assertEqual(2, state.timeout_round)
        self.assertEqual((1,), state.valid_measured_rounds)

        with self.assertRaisesRegex(RuntimeError, "after a timeout"):
            campaign.classify_cell_history(
                measured_timeout + [complete[-1]], measured_rounds=3
            )

    def test_summary_uses_complete_cells_and_filters_tiny_ratios(self) -> None:
        benchmarks = (
            "vec::stable",
            "slice::tiny",
            "btree::timeout",
        )
        factors = {
            variant.allocator: 0.8 + index * 0.1
            for index, variant in enumerate(campaign.VARIANTS)
        }
        factors["unialloc"] = 1.0
        records: list[dict[str, object]] = []
        for benchmark in benchmarks:
            for variant in campaign.VARIANTS:
                if benchmark == "btree::timeout" and variant.allocator == "scudo":
                    records.append(
                        process_record(
                            benchmark=benchmark,
                            allocator=variant.allocator,
                            phase="warmup",
                            round_index=0,
                            timed_out=True,
                        )
                    )
                    continue
                base = 1000.0 if benchmark != "slice::tiny" else 50.0
                value = base * factors[variant.allocator]
                records.append(
                    process_record(
                        benchmark=benchmark,
                        allocator=variant.allocator,
                        phase="warmup",
                        round_index=0,
                        ns_per_iter=value,
                    )
                )
                for round_index, jitter in enumerate((-1.0, 0.0, 2.0), start=1):
                    records.append(
                        process_record(
                            benchmark=benchmark,
                            allocator=variant.allocator,
                            phase="measured",
                            round_index=round_index,
                            ns_per_iter=value + jitter,
                        )
                    )

        with tempfile.TemporaryDirectory() as directory:
            summary = campaign.summarize(
                records,
                benchmarks=benchmarks,
                measured_rounds=3,
                output_dir=Path(directory),
                require_terminal=True,
            )

        self.assertEqual(
            ["vec::stable", "slice::tiny"],
            summary["comparison_selection"]["comparable_benchmarks"],
        )
        self.assertEqual(
            ["vec::stable"],
            summary["robustness_selection"]["selected_benchmarks"],
        )
        self.assertEqual(1, len(summary["censored_cells"]))
        self.assertEqual("btree::timeout", summary["censored_cells"][0]["benchmark"])
        self.assertEqual("scudo", summary["censored_cells"][0]["allocator"])

        ptmalloc = next(
            row for row in summary["aggregates"] if row["allocator"] == "ptmalloc"
        )
        self.assertEqual(2, ptmalloc["raw"]["benchmark_count"])
        self.assertEqual(1, ptmalloc["robust"]["benchmark_count"])
        self.assertEqual("vec::stable", ptmalloc["robust"]["minimum"]["benchmark"])
        self.assertEqual("vec::stable", ptmalloc["robust"]["maximum"]["benchmark"])

    def test_quantiles_and_extrema_ties_are_deterministic(self) -> None:
        self.assertEqual(
            {"p05": 1.2, "p25": 2.0, "p50": 3.0, "p75": 4.0, "p95": 4.8},
            campaign.ratio_quantiles([1, 2, 3, 4, 5]),
        )
        rows = [
            ("z::first", 1.0, 100.0, 100.0),
            ("a::second", 1.0, 100.0, 100.0),
        ]
        stats = campaign.ratio_distribution(rows)
        self.assertEqual("a::second", stats["minimum"]["benchmark"])
        self.assertEqual("a::second", stats["maximum"]["benchmark"])
        self.assertEqual(2, stats["minimum"]["tie_count"])
        self.assertEqual(2, stats["maximum"]["tie_count"])

    def test_resume_accepts_timeout_terminal_and_rejects_bad_records(self) -> None:
        valid = process_record(
            benchmark="vec::ok",
            allocator="unialloc",
            phase="warmup",
            round_index=0,
            ns_per_iter=100,
        )
        timeout = process_record(
            benchmark="vec::slow",
            allocator="unialloc",
            phase="warmup",
            round_index=0,
            timed_out=True,
        )
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory)
            records_path = output / "records.jsonl"
            records_path.write_text(
                json.dumps(valid) + "\n" + json.dumps(timeout) + "\n"
            )
            completed = campaign.load_completed_records(output)
            self.assertEqual(2, len(completed))

            records_path.write_text((json.dumps(valid) + "\n") * 2)
            with self.assertRaisesRegex(RuntimeError, "duplicate"):
                campaign.load_completed_records(output)

            bad = dict(valid)
            bad["valid"] = False
            bad["status"] = "invalid"
            records_path.write_text(json.dumps(bad) + "\n")
            with self.assertRaisesRegex(RuntimeError, "invalid"):
                campaign.load_completed_records(output)


if __name__ == "__main__":
    unittest.main()
