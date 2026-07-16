#!/usr/bin/env python3
"""Unit tests for the complete std_bench allocator campaign."""

from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock


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
    variant = campaign.VARIANT_BY_ID[allocator]
    thp_state = campaign.base.mimalloc_thp_expected_state(variant)
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
        "stderr_path": f"runs/{phase}/{round_index}/{allocator}/{benchmark}.stderr",
        "binary_sha256": f"{allocator}-binary-sha256",
        "glibc_tunables_present": False,
        "scudo_identity_marker_count": 1 if allocator == "scudo" else 0,
        "google_tcmalloc_identity_marker_count": (
            1 if campaign.base.is_google_tcmalloc_variant(variant) else 0
        ),
        "scudo_runtime_library": "/tmp/libscudo.so" if allocator == "scudo" else None,
        "mimalloc_thp_runtime": {
            "applicable": thp_state is not None,
            "required": thp_state is not None,
            "verified": True,
            "initial_verified": True,
            "initial_marker_count": 1 if thp_state is not None else 0,
            "initial_pr_get_thp_disable": thp_state,
            "marker_count": 1 if thp_state is not None else 0,
            "pr_get_thp_disable": thp_state,
        },
        "mimalloc_thp_runtime_sha256": (
            "runtime-sha256" if thp_state is not None else None
        ),
        "reported_benchmark": benchmark if valid else None,
        "exit_code": 0 if valid else -15,
        "time_exit_status": 0 if valid else None,
    }
    if ns_per_iter is not None:
        record["ns_per_iter"] = ns_per_iter
    return record


class FullCampaignTest(unittest.TestCase):
    def test_publication_variant_set_is_explicit_and_stable(self) -> None:
        self.assertEqual(
            (
                "unialloc",
                "jemalloc",
                "mimalloc",
                "mimalloc_no_thp",
                "google_tcmalloc",
            ),
            campaign.PUBLICATION_VARIANT_IDS,
        )
        selected = campaign.selected_variants(campaign.PUBLICATION_VARIANT_IDS)
        self.assertEqual(
            campaign.PUBLICATION_VARIANT_IDS,
            tuple(variant.allocator for variant in selected),
        )
        self.assertEqual("bench_mimalloc", selected[3].feature)
        self.assertEqual("bench_tcmalloc", selected[4].feature)
        self.assertEqual(
            campaign.PUBLICATION_VARIANT_IDS,
            campaign.parse_variant_ids(",".join(campaign.PUBLICATION_VARIANT_IDS)),
        )
        with self.assertRaisesRegex(ValueError, "unialloc"):
            campaign.parse_variant_ids("jemalloc,mimalloc")
        with self.assertRaisesRegex(ValueError, "aliases"):
            campaign.parse_variant_ids("unialloc,tcmalloc,google_tcmalloc")

    def test_variant_selection_mismatch_starts_no_external_process(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            output = root / "raw"
            publication = campaign.selected_variants(
                campaign.PUBLICATION_VARIANT_IDS
            )
            campaign.bind_campaign_selection(output, publication)
            args = campaign.parse_args(
                [
                    "--source-root",
                    str(root),
                    "--output-dir",
                    str(output),
                    "--jobs",
                    "1",
                    "--cpus",
                    "0",
                    "--variants",
                    "unialloc,jemalloc",
                ]
            )
            with mock.patch.object(
                campaign.base, "ensure_host_tools"
            ) as ensure_tools, mock.patch.object(
                campaign.base, "validate_clean_source"
            ) as validate_source, mock.patch.object(
                campaign.subprocess, "Popen"
            ) as popen, mock.patch.object(
                campaign.subprocess, "run"
            ) as run:
                with self.assertRaisesRegex(RuntimeError, "selection differs"):
                    campaign.run_campaign(args)
            ensure_tools.assert_not_called()
            validate_source.assert_not_called()
            popen.assert_not_called()
            run.assert_not_called()

    def test_legacy_variant_row_drift_starts_no_external_process(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            output = root / "raw"
            output.mkdir()
            publication = campaign.selected_variants(
                campaign.PUBLICATION_VARIANT_IDS
            )
            legacy_rows = [dict(variant.__dict__) for variant in publication]
            legacy_rows[2]["feature"] = "bench_ourself"
            (output / "campaign-config.json").write_text(
                json.dumps(
                    {
                        "variant_ids": list(campaign.PUBLICATION_VARIANT_IDS),
                        "variants": legacy_rows,
                    }
                )
                + "\n",
                encoding="utf-8",
            )
            args = campaign.parse_args(
                [
                    "--source-root",
                    str(root),
                    "--output-dir",
                    str(output),
                    "--jobs",
                    "1",
                    "--cpus",
                    "0",
                    "--variants",
                    ",".join(campaign.PUBLICATION_VARIANT_IDS),
                ]
            )
            with mock.patch.object(
                campaign.base, "ensure_host_tools"
            ) as ensure_tools, mock.patch.object(
                campaign.base, "validate_clean_source"
            ) as validate_source, mock.patch.object(
                campaign.subprocess, "Popen"
            ) as popen, mock.patch.object(
                campaign.subprocess, "run"
            ) as run:
                with self.assertRaisesRegex(RuntimeError, "selection differs"):
                    campaign.run_campaign(args)
            ensure_tools.assert_not_called()
            validate_source.assert_not_called()
            popen.assert_not_called()
            run.assert_not_called()

    def test_mimalloc_thp_runtime_sets_and_proves_process_policy(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory)
            record = campaign.ensure_mimalloc_thp_runtime(output)
            runtime = Path(record["binary"])
            for expected in (0, 1):
                proc = subprocess.run(
                    ["/bin/true"],
                    check=True,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    env={
                        "PATH": "/usr/bin:/bin",
                        "LD_PRELOAD": str(runtime),
                        "UNIALLOC_MIMALLOC_THP_EXPECTED": str(expected),
                    },
                )
                self.assertEqual(
                    (
                        f"UNIALLOC_MIMALLOC_PR_GET_THP_DISABLE_START={expected}\n"
                        f"UNIALLOC_MIMALLOC_PR_GET_THP_DISABLE={expected}\n"
                    ).encode(),
                    proc.stderr,
                )
            self.assertEqual(
                record,
                campaign.ensure_mimalloc_thp_runtime(output),
            )

            record_path = output / "helpers/mimalloc-thp-runtime/build.json"
            original_record = record_path.read_bytes()
            for field in (
                "source",
                "binary",
                "compiler_version",
                "build_stdout",
                "build_stderr",
            ):
                with self.subTest(record_field=field):
                    mutated = json.loads(original_record)
                    mutated[field] = str(output / f"wrong-{field}")
                    record_path.write_text(json.dumps(mutated) + "\n")
                    with self.assertRaisesRegex(RuntimeError, "identity validation"):
                        campaign.ensure_mimalloc_thp_runtime(output)
                    record_path.write_bytes(original_record)

            for field in (
                "source_sha256",
                "binary_sha256",
                "compiler_version_sha256",
                "build_stdout_sha256",
                "build_stderr_sha256",
            ):
                with self.subTest(record_digest=field):
                    mutated = json.loads(original_record)
                    mutated[field] = "0" * 64
                    record_path.write_text(json.dumps(mutated) + "\n")
                    with self.assertRaisesRegex(RuntimeError, "identity validation"):
                        campaign.ensure_mimalloc_thp_runtime(output)
                    record_path.write_bytes(original_record)

            for field in (
                "source",
                "binary",
                "compiler_version",
                "build_stdout",
                "build_stderr",
            ):
                with self.subTest(artifact=field):
                    artifact = Path(record[field])
                    original = artifact.read_bytes()
                    artifact.write_bytes(original + b"tampered\n")
                    with self.assertRaisesRegex(RuntimeError, "identity validation"):
                        campaign.ensure_mimalloc_thp_runtime(output)
                    artifact.write_bytes(original)

    def test_publication_matrix_has_468_by_5_complete_cells(self) -> None:
        benchmarks = tuple(
            f"family_{index % 8}::bench_{index:03d}" for index in range(468)
        )
        selected = campaign.selected_variants(campaign.PUBLICATION_VARIANT_IDS)
        slots = campaign.expected_process_slots(
            benchmarks, selected, measured_rounds=campaign.MEASURED_ROUNDS
        )
        self.assertEqual(468 * 5 * 4, len(slots))
        self.assertEqual(
            468 * 5,
            sum(phase == "warmup" for phase, _, _, _ in slots),
        )
        self.assertEqual(
            468 * 5 * 3,
            sum(phase == "measured" for phase, _, _, _ in slots),
        )

    def test_mimalloc_variants_share_one_feature_build(self) -> None:
        selected = campaign.selected_variants(campaign.PUBLICATION_VARIANT_IDS)
        builds = {}

        def fake_build(
            source_root: Path,
            output_dir: Path,
            variant: object,
            tcmalloc_lib_dir: Path | None,
        ) -> dict[str, object]:
            del source_root, output_dir, tcmalloc_lib_dir
            binary = Path("/tmp") / f"{variant.feature}-std-bench"
            build = {
                "allocator": variant.allocator,
                "feature": variant.feature,
                "label": variant.label,
                "binary": str(binary),
                "binary_sha256": f"{variant.feature}-sha256",
            }
            builds[variant.allocator] = build
            return build

        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "raw"
            args = SimpleNamespace(
                tcmalloc_lib_dir=Path("/tmp/tcmalloc/lib"),
                scudo_runtime_library=None,
            )
            helper = {
                "binary": "/tmp/libunialloc_mimalloc_thp_runtime.so",
                "binary_sha256": "mimalloc-runtime-sha256",
            }
            with mock.patch.object(
                campaign.base, "validate_clean_source", return_value="deadbeef"
            ), mock.patch.object(
                campaign.base,
                "validate_tcmalloc_dir",
                return_value=Path("/tmp/tcmalloc/lib"),
            ), mock.patch.object(
                campaign.base,
                "tcmalloc_runtime_identity",
                return_value={"sha256": "tcmalloc-sha256"},
            ), mock.patch.object(
                campaign, "ensure_mimalloc_thp_runtime", return_value=helper
            ), mock.patch.object(
                campaign.base, "build_variant", side_effect=fake_build
            ) as build_variant, mock.patch.object(
                campaign.base,
                "inventory_variant",
                side_effect=RuntimeError("stop after builds"),
            ):
                with self.assertRaisesRegex(RuntimeError, "stop after builds"):
                    campaign._run_campaign_locked(
                        args,
                        Path(directory),
                        output,
                        variants=selected,
                        selection=campaign.selection_payload(selected),
                    )

            self.assertEqual(4, build_variant.call_count)
            self.assertEqual(
                [
                    "bench_ourself",
                    "bench_jemalloc",
                    "bench_mimalloc",
                    "bench_tcmalloc",
                ],
                [call.args[2].feature for call in build_variant.call_args_list],
            )
            alias = json.loads(
                (output / "builds/mimalloc_no_thp/base-build-alias.json").read_text()
            )
            self.assertTrue(alias["binary_reused"])
            self.assertEqual("mimalloc", alias["base_build_allocator"])
            self.assertEqual(
                builds["mimalloc"]["binary_sha256"], alias["binary_sha256"]
            )

    def test_campaign_output_directory_has_an_exclusive_process_lock(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory)
            with campaign.exclusive_campaign_lock(output):
                with self.assertRaisesRegex(RuntimeError, "already active"):
                    with campaign.exclusive_campaign_lock(output):
                        self.fail("a second campaign acquired the same output lock")

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

    def test_resumed_records_revalidate_runtime_and_binary_identity(self) -> None:
        record = process_record(
            benchmark="vec::ok",
            allocator="scudo",
            phase="measured",
            round_index=1,
            ns_per_iter=123.0,
        )
        scudo = next(item for item in campaign.VARIANTS if item.allocator == "scudo")
        campaign.validate_process_record_identity(
            record,
            variant=scudo,
            binary_sha256="scudo-binary-sha256",
            cpu=20,
            numa_node=0,
            timeout_seconds=30,
            scudo_runtime=Path("/tmp/libscudo.so"),
        )

        bad_marker = dict(record)
        bad_marker["scudo_identity_marker_count"] = 0
        with self.assertRaisesRegex(RuntimeError, "Scudo marker"):
            campaign.validate_process_record_identity(
                bad_marker,
                variant=scudo,
                binary_sha256="scudo-binary-sha256",
                cpu=20,
                numa_node=0,
                timeout_seconds=30,
                scudo_runtime=Path("/tmp/libscudo.so"),
            )

        timeout = process_record(
            benchmark="vec::slow",
            allocator="scudo",
            phase="warmup",
            round_index=0,
            timed_out=True,
        )
        timeout["timeout_seconds"] = 10
        with self.assertRaisesRegex(RuntimeError, "timeout bound"):
            campaign.validate_process_record_identity(
                timeout,
                variant=scudo,
                binary_sha256="scudo-binary-sha256",
                cpu=20,
                numa_node=0,
                timeout_seconds=30,
                scudo_runtime=Path("/tmp/libscudo.so"),
            )

    def test_resumed_mimalloc_records_require_exact_thp_proof(self) -> None:
        variant = campaign.VARIANT_BY_ID["mimalloc_no_thp"]
        record = process_record(
            benchmark="vec::ok",
            allocator="mimalloc_no_thp",
            phase="measured",
            round_index=1,
            ns_per_iter=123.0,
        )
        campaign.validate_process_record_identity(
            record,
            variant=variant,
            binary_sha256="mimalloc_no_thp-binary-sha256",
            cpu=20,
            numa_node=0,
            timeout_seconds=30,
            scudo_runtime=None,
            mimalloc_thp_runtime_sha256="runtime-sha256",
        )

        wrong_state = json.loads(json.dumps(record))
        wrong_state["mimalloc_thp_runtime"]["pr_get_thp_disable"] = 0
        with self.assertRaisesRegex(RuntimeError, "THP runtime proof"):
            campaign.validate_process_record_identity(
                wrong_state,
                variant=variant,
                binary_sha256="mimalloc_no_thp-binary-sha256",
                cpu=20,
                numa_node=0,
                timeout_seconds=30,
                scudo_runtime=None,
                mimalloc_thp_runtime_sha256="runtime-sha256",
            )

        with self.assertRaisesRegex(RuntimeError, "THP runtime"):
            campaign.validate_process_record_identity(
                record,
                variant=variant,
                binary_sha256="mimalloc_no_thp-binary-sha256",
                cpu=20,
                numa_node=0,
                timeout_seconds=30,
                scudo_runtime=None,
                mimalloc_thp_runtime_sha256="different-runtime",
            )


if __name__ == "__main__":
    unittest.main()
