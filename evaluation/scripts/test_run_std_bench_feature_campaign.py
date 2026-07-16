#!/usr/bin/env python3
"""Regression tests for the incremental full-std_bench feature campaign."""

from __future__ import annotations

import importlib.util
import json
import os
import sys
import tempfile
import threading
import unittest
from concurrent.futures import Future
from pathlib import Path
from unittest import mock


SCRIPT = Path(__file__).with_name("run_std_bench_feature_campaign.py")
SPEC = importlib.util.spec_from_file_location("run_std_bench_feature_campaign", SCRIPT)
assert SPEC is not None and SPEC.loader is not None
campaign = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = campaign
SPEC.loader.exec_module(campaign)


def raw_record(
    variant_id: str,
    *,
    benchmark: str = "vec::bench_from_elem_1000",
    protocol_sha256: str = "a" * 64,
    ns_per_iter: float = 1234.0,
) -> dict[str, object]:
    """Return one minimal valid absolute-metric process record."""

    return {
        "schema_version": campaign.RAW_RECORD_SCHEMA_VERSION,
        "protocol_sha256": protocol_sha256,
        "cohort_id": "primary",
        "measurement_session_id": "11111111-1111-4111-8111-111111111111",
        "measurement_anchor": variant_id == "unialloc",
        "variant_id": variant_id,
        "allocator": variant_id,
        "benchmark": benchmark,
        "phase": "measured",
        "round": 1,
        "status": "valid",
        "valid": True,
        "timed_out": False,
        "terminal_reason": None,
        "time_parse_status": "complete",
        "timeout_seconds": 30,
        "cpu": 20,
        "numa_node": 0,
        "binary_sha256": "b" * 64,
        "fixed_work_contract": campaign.VARIANT_REGISTRY[
            variant_id
        ].fixed_work_contract,
        "performance_claim_eligible": campaign.VARIANT_REGISTRY[
            variant_id
        ].performance_claim_eligible,
        "peak_rss_claim_eligible": campaign.VARIANT_REGISTRY[
            variant_id
        ].peak_rss_claim_eligible,
        "rss_work_model": campaign.VARIANT_REGISTRY[variant_id].rss_work_model,
        "ns_per_iter": ns_per_iter,
        "peak_rss_kib": 4096,
        "wall_seconds": 0.25,
        "user_seconds": 0.20,
        "system_seconds": 0.01,
    }


def process_arguments(root: Path) -> dict[str, object]:
    binary = root / "std-bench"
    binary.write_bytes(b"binary")
    return {
        "output_dir": root,
        "protocol": {"protocol_sha256": "a" * 64},
        "cohort_id": "primary",
        "variant": campaign.VARIANT_REGISTRY["unialloc"],
        "variant_manifest": {"variant_manifest_sha256": "c" * 64},
        "build": {
            "binary": str(binary),
            "binary_sha256": campaign.sha256_file(binary),
        },
        "benchmark": "vec::bench_from_elem_1000",
        "phase": "measured",
        "round_index": 1,
        "cpu": 20,
        "numa_node": 0,
        "timeout_seconds": 30,
        "measurement_session_id": "11111111-1111-4111-8111-111111111111",
    }


def parsed_time() -> dict[str, object]:
    return {
        "time_exit_status": 0,
        "peak_rss_kib": 4096,
        "wall_seconds": 0.25,
        "user_seconds": 0.20,
        "system_seconds": 0.01,
    }


GNU_TIME_OUTPUT = """\
User time (seconds): 0.20
System time (seconds): 0.01
Percent of CPU this job got: 84%
Elapsed (wall clock) time (h:mm:ss or m:ss): 0:00.25
Maximum resident set size (kbytes): 4096
Major (requiring I/O) page faults: 0
Minor (reclaiming a frame) page faults: 1
Voluntary context switches: 2
Involuntary context switches: 3
Exit status: 0
"""


class FeatureCampaignTest(unittest.TestCase):
    def test_registry_is_complete_and_lifetime_arm_is_diagnostic(self) -> None:
        registry = campaign.validate_variant_registry(campaign.VARIANT_REGISTRY)
        self.assertEqual(
            {
                "unialloc",
                "typed_plain",
                "typeiso_perf",
                "lifetime_transport_all_unknown",
            },
            set(registry),
        )
        self.assertIsNone(registry["unialloc"].mir_policy_flags)
        self.assertEqual(0, registry["typed_plain"].mir_policy_flags)
        self.assertEqual(1, registry["typeiso_perf"].mir_policy_flags)

        for variant_id, variant in registry.items():
            self.assertFalse(variant.fixed_work_contract, variant_id)
            self.assertFalse(variant.peak_rss_claim_eligible, variant_id)
            self.assertEqual(
                "workload_native_adaptive_iterations", variant.rss_work_model
            )
        self.assertTrue(registry["unialloc"].performance_claim_eligible)
        self.assertTrue(registry["typed_plain"].performance_claim_eligible)
        self.assertTrue(registry["typeiso_perf"].performance_claim_eligible)

        lifetime = registry["lifetime_transport_all_unknown"]
        self.assertEqual(0, lifetime.mir_policy_flags)
        self.assertEqual("force-track-all-unknown-diagnostic", lifetime.runtime_arm)
        self.assertEqual("transport_all_unknown_diagnostic", lifetime.claim_scope)
        self.assertFalse(lifetime.fixed_work_contract)
        self.assertFalse(lifetime.performance_claim_eligible)
        self.assertFalse(lifetime.peak_rss_claim_eligible)
        self.assertFalse(lifetime.epoch_contract)
        self.assertNotIn("lifetime-aware", lifetime.label.lower())

    def test_variant_build_environments_use_the_actual_wrapper_with_flags_zero_and_one(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            wrapper = root / "unialloc-rustc-wrapper"
            wrapper.write_text("wrapper", encoding="utf-8")
            sysroot = root / "sysroot"
            (sysroot / "lib").mkdir(parents=True)
            for variant_id, expected_flags in (("typed_plain", "0"), ("typeiso_perf", "1")):
                env = campaign.variant_build_environment(
                    campaign.VARIANT_REGISTRY[variant_id],
                    base_environment={"PATH": "/usr/bin"},
                    wrapper=wrapper,
                    sysroot=sysroot,
                    audit_dir=root / f"audit-{variant_id}",
                    pass_log_dir=root / f"logs-{variant_id}",
                    target_dir=root / f"target-{variant_id}",
                    temporary_dir=root / "tmp",
                )
                self.assertEqual(str(wrapper.resolve()), env["RUSTC_WRAPPER"])
                self.assertEqual(expected_flags, env["UNIALLOC_LOWERING_POLICY_FLAGS"])
                self.assertEqual("1", env["UNIALLOC_ACTUAL_MIR_REWRITE"])
                self.assertEqual("1", env["UNIALLOC_ACTUAL_SEMANTIC_SCOPE_REWRITE"])
                self.assertEqual("std_bench", env["UNIALLOC_RUSTC_TARGET_CRATES"])

    def test_timed_runtime_is_allowlisted_and_uses_host_wide_lock(self) -> None:
        with tempfile.TemporaryDirectory() as directory, mock.patch.dict(
            os.environ,
            {
                "PATH": "/usr/bin",
                "HOME": "/home/test",
                "LD_PRELOAD": "/tmp/wrong.so",
                "MALLOC_CONF": "dirty_decay_ms:0",
                "GLIBC_TUNABLES": "glibc.pthread.rseq=0",
                "RUSTFLAGS": "-Ctarget-cpu=native",
                "UNIALLOC_LOWERING_POLICY_FLAGS": "99",
            },
            clear=True,
        ):
            environment = campaign.variant_runtime_environment(
                campaign.VARIANT_REGISTRY["typeiso_perf"], Path(directory)
            )
        self.assertEqual(
            {
                "PATH",
                "HOME",
                "LANG",
                "LC_ALL",
                "TMPDIR",
                "UNIALLOC_STD_BENCH_DISABLE_TYPE_STATS",
                "UNIALLOC_STD_BENCH_DISABLE_AGGREGATE_STATS",
            },
            set(environment),
        )
        self.assertNotIn("GLIBC_TUNABLES", environment)
        self.assertEqual(
            campaign.suite_contract.HOST_PRIMARY_MEASUREMENT_LOCK,
            campaign.MEASUREMENT_LOCK,
        )

    def test_adding_and_removing_selection_never_rewrites_old_raw_shards(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory)
            record = raw_record("unialloc")
            path = campaign.persist_raw_record(output, record)
            original = path.read_bytes()

            campaign.persist_selection(
                output,
                cohort_id="primary",
                variant_ids=("unialloc", "typed_plain"),
            )
            campaign.persist_selection(
                output,
                cohort_id="primary",
                variant_ids=("unialloc",),
            )
            self.assertEqual(original, path.read_bytes())

            typed_path = campaign.persist_raw_record(output, raw_record("typed_plain"))
            self.assertTrue(typed_path.is_file())
            self.assertEqual(original, path.read_bytes())
            with self.assertRaisesRegex(RuntimeError, "immutable raw record"):
                campaign.persist_raw_record(
                    output, raw_record("unialloc", ns_per_iter=9999.0)
                )

    def test_measurement_session_binds_full_ordered_variant_selection(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory)
            protocol = {"protocol_sha256": "a" * 64}
            selected = ("unialloc", "typed_plain")
            session = campaign.ensure_measurement_session(
                output,
                protocol=protocol,
                cohort_id="primary",
                variant_ids=selected,
            )
            self.assertEqual(
                campaign.MEASUREMENT_SESSION_SCHEMA_VERSION,
                session["schema_version"],
            )
            self.assertEqual(list(selected), session["variant_ids"])
            reused = campaign.ensure_measurement_session(
                output,
                protocol=protocol,
                cohort_id="primary",
                variant_ids=selected,
            )
            self.assertEqual(
                session["measurement_session_id"], reused["measurement_session_id"]
            )

            for changed in (
                ("unialloc", "typed_plain", "typeiso_perf"),
                ("typed_plain", "unialloc"),
                ("unialloc",),
            ):
                with self.subTest(changed=changed), self.assertRaisesRegex(
                    campaign.CampaignError, "variant selection"
                ):
                    campaign.ensure_measurement_session(
                        output,
                        protocol=protocol,
                        cohort_id="primary",
                        variant_ids=changed,
                    )

    def test_selection_drift_rejects_before_any_process_or_view_mutation(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory).resolve()
            protocol = {"protocol_sha256": "a" * 64}
            selected = ("unialloc", "typed_plain")
            campaign.ensure_measurement_session(
                output,
                protocol=protocol,
                cohort_id="primary",
                variant_ids=selected,
            )
            selection_path = campaign.persist_selection(
                output,
                cohort_id="primary",
                variant_ids=selected,
            )
            original_selection = selection_path.read_bytes()
            args = campaign.parse_args(
                [
                    "--source-root",
                    str(campaign.ROOT),
                    "--output-dir",
                    str(output),
                    "--cohort-id",
                    "primary",
                    "--variants",
                    "unialloc,typed_plain,typeiso_perf",
                    "--build-only",
                    "--jobs",
                    "1",
                    "--cpus",
                    "20",
                ]
            )
            with (
                mock.patch.object(campaign, "ensure_host_tools") as host_tools,
                mock.patch.object(campaign.subprocess, "run") as run,
                mock.patch.object(campaign.subprocess, "Popen") as popen,
            ):
                with self.assertRaisesRegex(
                    campaign.CampaignError, "variant selection"
                ):
                    campaign.run_campaign(args)
            host_tools.assert_not_called()
            run.assert_not_called()
            popen.assert_not_called()
            self.assertEqual(original_selection, selection_path.read_bytes())

    def test_protocol_mismatch_fails_closed_without_touching_raw_records(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory)
            protocol = {
                "schema_version": campaign.PROTOCOL_SCHEMA_VERSION,
                "compatibility": {
                    "source_head": "c" * 40,
                    "inventory_sha256": "d" * 64,
                    "measured_rounds": 3,
                    "timeout_seconds": 30,
                    "cpus": [20],
                    "numa_node": 0,
                },
            }
            persisted = campaign.ensure_campaign_protocol(output, protocol)
            record = raw_record(
                "unialloc", protocol_sha256=persisted["protocol_sha256"]
            )
            path = campaign.persist_raw_record(output, record)
            original = path.read_bytes()

            incompatible = json.loads(json.dumps(protocol))
            incompatible["compatibility"]["timeout_seconds"] = 60
            with self.assertRaisesRegex(RuntimeError, "protocol mismatch"):
                campaign.ensure_campaign_protocol(output, incompatible)
            self.assertEqual(original, path.read_bytes())

    def test_selected_variants_require_complete_identical_canonical_inventory(self) -> None:
        canonical = tuple(f"family::bench_{index:03d}" for index in range(468))
        manifests = {
            variant_id: {
                "variant_id": variant_id,
                "success": True,
                "canonical_benchmarks": list(canonical),
            }
            for variant_id in ("unialloc", "typed_plain")
        }
        campaign.validate_selected_variant_completeness(
            ("unialloc", "typed_plain"), manifests, canonical
        )

        del manifests["typed_plain"]
        with self.assertRaisesRegex(RuntimeError, "typed_plain.*missing"):
            campaign.validate_selected_variant_completeness(
                ("unialloc", "typed_plain"), manifests, canonical
            )

        manifests["typed_plain"] = {
            "variant_id": "typed_plain",
            "success": True,
            "canonical_benchmarks": list(canonical[:-1]),
        }
        with self.assertRaisesRegex(RuntimeError, "complete 468-leaf inventory"):
            campaign.validate_selected_variant_completeness(
                ("unialloc", "typed_plain"), manifests, canonical
            )

    def test_raw_records_contain_absolute_metrics_and_reject_derived_ratios(self) -> None:
        record = raw_record("unialloc")
        campaign.validate_raw_record(record)
        derived = dict(record, ratio_vs_unialloc=0.9)
        with self.assertRaisesRegex(RuntimeError, "derived metric"):
            campaign.validate_raw_record(derived)

    def test_raw_record_claim_contract_fails_closed_on_reuse(self) -> None:
        for field, value in (
            ("fixed_work_contract", True),
            ("performance_claim_eligible", False),
            ("peak_rss_claim_eligible", True),
            ("rss_work_model", "fixed_work"),
        ):
            with self.subTest(field=field):
                record = raw_record("unialloc")
                record[field] = value
                with self.assertRaisesRegex(RuntimeError, "claim contract mismatch"):
                    campaign.validate_raw_record(record)

    def test_protocol_plan_and_manifest_persist_rss_claim_boundary(self) -> None:
        source_identity = {
            "source_head": "c" * 40,
            "source_branch": "dev",
            "git_objects": {},
        }
        protocol = campaign.build_protocol(
            source_identity=source_identity,
            canonical_benchmarks=("family::bench",),
            variant_ids=tuple(campaign.VARIANT_REGISTRY),
            toolchain="nightly",
            measured_rounds=3,
            timeout_seconds=30,
            cpus=(20,),
            numa_node=0,
        )
        compatibility = protocol["compatibility"]
        self.assertIn(
            "evaluation/scripts/lifetime_prior_six_program_campaign.py",
            compatibility["transitive_evaluator_sha256"],
        )
        self.assertFalse(compatibility["fixed_work_contract"])
        self.assertEqual(
            ["unialloc", "typed_plain", "typeiso_perf"],
            compatibility["performance_claim_eligible_variants"],
        )
        self.assertFalse(compatibility["peak_rss_claim_eligible"])
        self.assertEqual("diagnostic_only", compatibility["peak_rss_interpretation"])
        self.assertEqual(
            "workload_native_adaptive_iterations",
            compatibility["rss_work_model"],
        )
        self.assertEqual(
            {
                "fixed_work_contract": False,
                "performance_claim_eligible": False,
                "peak_rss_claim_eligible": False,
                "rss_work_model": "workload_native_adaptive_iterations",
            },
            compatibility["variant_claim_contracts"][
                "lifetime_transport_all_unknown"
            ],
        )

        core_protocol = campaign.build_protocol(
            source_identity=source_identity,
            canonical_benchmarks=("family::bench",),
            variant_ids=("unialloc", "typed_plain", "typeiso_perf"),
            toolchain="nightly",
            measured_rounds=3,
            timeout_seconds=30,
            cpus=(20,),
            numa_node=0,
        )
        core_compatibility = core_protocol["compatibility"]
        self.assertNotIn(
            "evaluation/scripts/lifetime_prior_six_program_campaign.py",
            core_compatibility["transitive_evaluator_sha256"],
        )
        self.assertEqual(
            ["unialloc", "typed_plain", "typeiso_perf"],
            core_compatibility["variant_ids"],
        )
        self.assertEqual(
            {"unialloc", "typed_plain", "typeiso_perf"},
            set(core_compatibility["variant_claim_contracts"]),
        )

        args = campaign.parse_args(
            ["--plan-only", "--jobs", "1", "--cpus", "20"]
        )
        plan = campaign.campaign_plan(
            output_dir=Path("raw"),
            cohort_id="primary",
            variant_ids=args.variants,
            canonical_benchmarks=("family::bench",),
            args=args,
        )
        self.assertEqual(
            ["unialloc", "typed_plain", "typeiso_perf"],
            plan["performance_claim_eligible_variants"],
        )
        self.assertFalse(plan["peak_rss_claim_eligible"])

        with tempfile.TemporaryDirectory() as directory:
            manifest = campaign.persist_variant_manifest(
                Path(directory),
                campaign.VARIANT_REGISTRY["typeiso_perf"],
                source_identity=source_identity,
                toolchain="nightly",
            )
        self.assertTrue(manifest["performance_claim_eligible"])
        self.assertFalse(manifest["peak_rss_claim_eligible"])
        self.assertEqual("diagnostic_only", manifest["peak_rss_interpretation"])

    def test_worker_failure_and_keyboard_interrupt_terminate_all_groups(self) -> None:
        for error in (RuntimeError("worker failed"), KeyboardInterrupt("stop")):
            with self.subTest(error=type(error).__name__):
                future: Future[None] = Future()
                future.set_exception(error)
                stop = threading.Event()
                registry = mock.Mock(spec=campaign.ProcessGroupRegistry)
                with self.assertRaises(type(error)):
                    campaign.await_worker_futures(
                        [future], stop=stop, registry=registry
                    )
                self.assertTrue(stop.is_set())
                registry.terminate_all.assert_called_once_with()

    def test_process_cell_reuses_commit_before_popen_and_indexes_record(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            arguments = process_arguments(root)
            calls: list[list[str]] = []

            class FakeProcess:
                returncode = 0
                pid = 10001

                def __init__(self, command: list[str], **_: object) -> None:
                    calls.append(command)
                    Path(command[3]).write_text(GNU_TIME_OUTPUT, encoding="utf-8")

                def poll(self) -> int:
                    return self.returncode

                def wait(self, timeout: int | None = None) -> int:
                    del timeout
                    return self.returncode

                def communicate(self, timeout: int | None = None) -> tuple[bytes, bytes]:
                    del timeout
                    benchmark = arguments["benchmark"]
                    return (
                        f"test {benchmark} ... bench: 1,234 ns/iter\n".encode(),
                        b"",
                    )

            with mock.patch.object(campaign.subprocess, "Popen", FakeProcess):
                first = campaign.execute_benchmark_process(**arguments)
                second = campaign.execute_benchmark_process(**arguments)
            self.assertEqual(1, len(calls))
            self.assertEqual(first["record_path"], second["record_path"])
            self.assertEqual(first["record_sha256"], second["record_sha256"])
            self.assertIn("attempts/record/", first["stdout_path"])
            campaign.validate_process_artifacts(root, first)

            index_path = campaign.rebuild_absolute_index(
                root,
                cohort_id="primary",
                variant_ids=("unialloc",),
                canonical_benchmarks=(str(arguments["benchmark"]),),
                protocol=arguments["protocol"],
                builds={"unialloc": arguments["build"]},
                variant_manifests={
                    "unialloc": arguments["variant_manifest"]
                },
                lane_cpus={str(arguments["benchmark"]): 20},
                timeout_seconds=30,
                numa_node=0,
                measurement_session_id=str(
                    arguments["measurement_session_id"]
                ),
            )
            rows = [
                json.loads(line)
                for line in index_path.read_text(encoding="utf-8").splitlines()
            ]
            self.assertEqual(1, len(rows))
            self.assertEqual(first["record_path"], rows[0]["record_path"])
            self.assertEqual(first["record_sha256"], rows[0]["record_sha256"])

    def test_failed_process_attempt_does_not_block_retry(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            arguments = process_arguments(root)
            calls = 0

            class FakeProcess:
                returncode = 0
                pid = 10002

                def __init__(self, command: list[str], **_: object) -> None:
                    nonlocal calls
                    calls += 1
                    Path(command[3]).write_text(GNU_TIME_OUTPUT, encoding="utf-8")

                def poll(self) -> int:
                    return self.returncode

                def wait(self, timeout: int | None = None) -> int:
                    del timeout
                    return self.returncode

                def communicate(self, timeout: int | None = None) -> tuple[bytes, bytes]:
                    del timeout
                    name = (
                        "wrong::benchmark"
                        if calls == 1
                        else str(arguments["benchmark"])
                    )
                    return f"test {name} ... bench: 123 ns/iter\n".encode(), b""

            with mock.patch.object(campaign.subprocess, "Popen", FakeProcess):
                with self.assertRaises(campaign.CampaignError):
                    campaign.execute_benchmark_process(**arguments)
                record = campaign.execute_benchmark_process(**arguments)
            self.assertEqual(2, calls)
            self.assertTrue((root / record["record_path"]).is_file())
            attempts = list(
                (root / "raw/primary/unialloc").glob(
                    "*/measured/round-01/attempts/record/*"
                )
            )
            self.assertEqual(2, len(attempts))

    def test_timeout_with_incomplete_gnu_time_is_committed_and_reused(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            arguments = process_arguments(root)
            calls = 0

            class FakeProcess:
                pid = 10004

                def __init__(self, _command: list[str], **_: object) -> None:
                    nonlocal calls
                    calls += 1
                    self.returncode: int | None = None
                    self.communications = 0

                def poll(self) -> int | None:
                    return self.returncode

                def wait(self, timeout: int | None = None) -> int:
                    del timeout
                    assert self.returncode is not None
                    return self.returncode

                def communicate(
                    self, timeout: int | None = None
                ) -> tuple[bytes, bytes]:
                    self.communications += 1
                    if timeout is not None:
                        raise campaign.subprocess.TimeoutExpired("std-bench", timeout)
                    return b"", b""

            def terminate(process: FakeProcess) -> None:
                process.returncode = -15

            with (
                mock.patch.object(campaign.subprocess, "Popen", FakeProcess),
                mock.patch.object(
                    campaign,
                    "terminate_and_wait_process_group",
                    side_effect=terminate,
                ),
            ):
                first = campaign.execute_benchmark_process(**arguments)
                second = campaign.execute_benchmark_process(**arguments)

            self.assertEqual(1, calls)
            self.assertEqual("timeout_censored", first["status"])
            self.assertTrue(first["timed_out"])
            self.assertFalse(first["valid"])
            self.assertEqual("process_timeout", first["terminal_reason"])
            self.assertEqual("incomplete_after_timeout", first["time_parse_status"])
            self.assertNotIn("peak_rss_kib", first)
            self.assertEqual(first["record_sha256"], second["record_sha256"])
            self.assertEqual(b"", (root / str(first["time_path"])).read_bytes())

    def test_terminal_accounting_uses_only_the_common_complete_leaf_set(self) -> None:
        variants = ("unialloc", "typed_plain", "typeiso_perf")
        benchmarks = tuple(f"family::bench_{index:03d}" for index in range(468))
        records: list[dict[str, object]] = []

        def append_record(
            variant_id: str,
            benchmark: str,
            phase: str,
            round_index: int,
            *,
            timed_out: bool = False,
        ) -> None:
            records.append(
                {
                    "variant_id": variant_id,
                    "allocator": variant_id,
                    "benchmark": benchmark,
                    "phase": phase,
                    "round": round_index,
                    "status": "timeout_censored" if timed_out else "valid",
                    "valid": not timed_out,
                    "timed_out": timed_out,
                    "record_path": (
                        f"raw/{variant_id}/{benchmark}/{phase}/{round_index}.json"
                    ),
                    "record_sha256": "d" * 64,
                }
            )

        for benchmark in benchmarks[:-1]:
            for variant_id in variants:
                append_record(variant_id, benchmark, "warmup", 0)
                for round_index in range(1, 4):
                    append_record(variant_id, benchmark, "measured", round_index)
        append_record("unialloc", benchmarks[-1], "warmup", 0, timed_out=True)

        accounting = campaign.build_terminal_accounting(
            records=records,
            cohort_id="primary",
            measurement_session_id="11111111-1111-4111-8111-111111111111",
            protocol_sha256="a" * 64,
            variant_ids=variants,
            canonical_benchmarks=benchmarks,
            measured_rounds=3,
        )

        self.assertTrue(accounting["terminal_accounting_complete"])
        self.assertEqual(468, accounting["terminal_benchmark_count"])
        self.assertEqual(467, accounting["common_complete_benchmark_count"])
        self.assertEqual(1, accounting["excluded_benchmark_count"])
        self.assertEqual([benchmarks[-1]], accounting["excluded_benchmarks"])
        self.assertEqual(
            {
                "complete": 467 * len(variants),
                "timeout_censored": 1,
                "peer_timeout_blocked": 2,
            },
            accounting["cell_status_counts"],
        )
        excluded = accounting["benchmarks"][-1]
        self.assertEqual("excluded_timeout_censored", excluded["status"])
        self.assertEqual(["unialloc"], excluded["timeout_variants"])
        self.assertEqual(
            ["timeout_censored", "peer_timeout_blocked", "peer_timeout_blocked"],
            [cell["status"] for cell in excluded["cells"]],
        )

    def test_terminal_accounting_rejects_unexplained_pending_cells(self) -> None:
        benchmarks = tuple(f"family::bench_{index:03d}" for index in range(468))
        with self.assertRaisesRegex(
            campaign.CampaignError, "incomplete leaf without a timeout"
        ):
            campaign.build_terminal_accounting(
                records=(),
                cohort_id="primary",
                measurement_session_id="11111111-1111-4111-8111-111111111111",
                protocol_sha256="a" * 64,
                variant_ids=("unialloc", "typed_plain", "typeiso_perf"),
                canonical_benchmarks=benchmarks,
                measured_rounds=3,
            )

    def test_committed_process_reuse_rejects_artifact_mutation_before_popen(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            arguments = process_arguments(root)
            calls = 0

            class FakeProcess:
                returncode = 0
                pid = 10003

                def __init__(self, command: list[str], **_: object) -> None:
                    nonlocal calls
                    calls += 1
                    Path(command[3]).write_text(GNU_TIME_OUTPUT, encoding="utf-8")

                def poll(self) -> int:
                    return self.returncode

                def wait(self, timeout: int | None = None) -> int:
                    del timeout
                    return self.returncode

                def communicate(self, timeout: int | None = None) -> tuple[bytes, bytes]:
                    del timeout
                    return (
                        f"test {arguments['benchmark']} ... bench: 123 ns/iter\n".encode(),
                        b"",
                    )

            with mock.patch.object(campaign.subprocess, "Popen", FakeProcess):
                record = campaign.execute_benchmark_process(**arguments)
                (root / record["stdout_path"]).write_bytes(b"corrupt")
                with self.assertRaises(
                    campaign.immutable_evidence.ImmutableEvidenceError
                ):
                    campaign.execute_benchmark_process(**arguments)
            self.assertEqual(1, calls)


if __name__ == "__main__":
    unittest.main()
