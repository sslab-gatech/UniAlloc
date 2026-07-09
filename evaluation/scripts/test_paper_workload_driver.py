#!/usr/bin/env python3
"""Regression tests for the local paper workload driver.

These tests protect evidence fidelity rather than benchmark speed: semantic
policy timing must actually run the std_bench metadata sentinels, and those
sentinel rows must not be counted as workload timing.
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import pathlib
import tempfile
import unittest
from unittest import mock


ROOT = pathlib.Path(__file__).resolve().parents[2]
DRIVER_PATH = ROOT / "evaluation" / "scripts" / "paper_workload_driver.py"


spec = importlib.util.spec_from_file_location("paper_workload_driver", DRIVER_PATH)
assert spec is not None and spec.loader is not None
driver = importlib.util.module_from_spec(spec)
spec.loader.exec_module(driver)


def args(**overrides: object) -> argparse.Namespace:
    values = {
        "dataset": "pac_authentication",
        "benchmark": "Collections",
        "allocator": "unialloc",
        "variant_feature": None,
        "bench_filter": "vec::bench_with_capacity_1000",
        "extra_feature": None,
        "timeout": 180,
        "cargo": "cargo",
        "rust_toolchain": None,
        "dry_run": True,
        "allow_host_allocator_mismatch": False,
        "tcmalloc_lib_dir": None,
        "cmake_bin": None,
        "scudo_mode": "auto",
        "scudo_runtime_library": None,
        "compiler_site_replay_type_mapping": None,
        "compiler_site_replay_limit": 4096,
        "compiler_site_id_mode": "cyclic-replay",
        "compiler_site_recovery_scope": "thread-local",
    }
    values.update(overrides)
    return argparse.Namespace(**values)


class PaperWorkloadDriverSemanticHarnessTests(unittest.TestCase):
    def test_semantic_variants_do_not_force_stats_into_timing(self) -> None:
        features = driver.resolve_features(args())
        self.assertEqual(features, ["bench_ourself", "pac"])

    def test_semantic_validation_can_request_stats_feature_explicitly(self) -> None:
        features = driver.resolve_features(args(extra_feature=["stats"]))
        self.assertEqual(features, ["bench_ourself", "pac", "stats"])

    def test_stats_validation_does_not_disable_semantic_counters(self) -> None:
        env = driver.cargo_subprocess_env(args(extra_feature=["stats"]))
        self.assertNotIn("UNIALLOC_STD_BENCH_DISABLE_TYPE_STATS", env)
        self.assertNotIn("UNIALLOC_STD_BENCH_DISABLE_AGGREGATE_STATS", env)

    def test_timing_without_stats_disables_semantic_counters(self) -> None:
        env = driver.cargo_subprocess_env(args())
        self.assertEqual(env["UNIALLOC_STD_BENCH_DISABLE_TYPE_STATS"], "1")
        self.assertEqual(env["UNIALLOC_STD_BENCH_DISABLE_AGGREGATE_STATS"], "1")

    def test_compiler_site_replay_mapping_sets_bench_env(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            mapping = pathlib.Path(tmp) / "type-mapping.json"
            mapping.write_text(
                json.dumps(
                    {
                        "type_mappings": [
                            {"type_id": "0x2a"},
                            {"compiler_type_id": 99},
                            {"type_id": 42},
                            {"type_id": "0"},
                        ]
                    }
                ),
                encoding="utf-8",
            )
            ns = args(
                compiler_site_replay_type_mapping=str(mapping),
                compiler_site_id_mode="consuming-stream",
                compiler_site_replay_limit=10,
            )
            with mock.patch.dict(driver.os.environ, {}, clear=True):
                env = driver.cargo_subprocess_env(ns)

        self.assertEqual(env["UNIALLOC_COMPILER_SITE_TYPE_IDS"], "42,99")
        self.assertEqual(env["UNIALLOC_COMPILER_SITE_TYPE_ID_MODE"], "consuming-stream")
        self.assertEqual(env["UNIALLOC_COMPILER_SITE_RECOVERY_SCOPE"], "thread-local")
        config = driver.compiler_site_replay_config(ns)
        self.assertTrue(config["enabled"], config)
        self.assertEqual(config["type_id_count"], 2)
        self.assertEqual(config["type_id_sample"], [42, 99])
        self.assertEqual(config["recovery_scope"], "thread-local")

    def test_env_delta_summarizes_compiler_site_ids_without_full_stream(self) -> None:
        long_stream = ",".join(str(value) for value in range(1, 20))
        env = {
            "UNIALLOC_COMPILER_SITE_TYPE_IDS": long_stream,
            "UNIALLOC_COMPILER_SITE_TYPE_ID_MODE": "cyclic-replay",
            "UNIALLOC_COMPILER_SITE_RECOVERY_SCOPE": "thread-local",
        }
        delta = driver.env_delta_for_record(env)
        self.assertNotIn("UNIALLOC_COMPILER_SITE_TYPE_IDS", delta)
        self.assertEqual(delta["UNIALLOC_COMPILER_SITE_TYPE_IDS_COUNT"], "19")
        self.assertEqual(delta["UNIALLOC_COMPILER_SITE_TYPE_IDS_SAMPLE"], "1,2,3,4,5,6,7,8")
        self.assertRegex(delta["UNIALLOC_COMPILER_SITE_TYPE_IDS_SHA256"], r"^[0-9a-f]{64}$")
        self.assertEqual(delta["UNIALLOC_COMPILER_SITE_TYPE_ID_MODE"], "cyclic-replay")
        self.assertEqual(delta["UNIALLOC_COMPILER_SITE_RECOVERY_SCOPE"], "thread-local")

    def test_default_dataset_does_not_force_stats(self) -> None:
        features = driver.resolve_features(
            args(dataset="default_performance", bench_filter="vec::bench_with_capacity_1000")
        )
        self.assertEqual(features, ["bench_ourself"])

    def test_semantic_filtered_dry_run_validates_and_derives_sentinel_skips(self) -> None:
        ns = args(dry_run=True)
        features = driver.resolve_features(ns)
        with mock.patch.object(
            driver,
            "collect_bench_list",
            return_value=[
                "aaa_semantic_auto_metadata_enable",
                "vec::bench_with_capacity_1000",
                "vec::bench_new",
                "zzz_semantic_auto_metadata_report",
            ],
        ):
            selection = driver.semantic_harness_selection(ns, features)
        self.assertTrue(selection["required"])
        self.assertFalse(selection["deferred_for_dry_run"])
        self.assertEqual(selection["strategy"], "sentinel-plus-target-skip-selection")
        self.assertEqual(selection["selected_target_benches"], ["vec::bench_with_capacity_1000"])
        self.assertEqual(selection["bench_list_count"], 4)
        self.assertEqual(selection["derived_skips"], ["vec::bench_new"])

    def test_plain_filtered_dry_run_validates_target_exists(self) -> None:
        ns = args(
            dataset="default_performance",
            allocator="ptmalloc",
            bench_filter="vec::bench_with_capacity_1000",
            dry_run=True,
        )
        features = ["bench_ptmalloc"]
        with mock.patch.object(
            driver,
            "collect_bench_list",
            return_value=["vec::bench_with_capacity_1000", "vec::bench_new"],
        ):
            selection = driver.semantic_harness_selection(ns, features)
        self.assertFalse(selection["required"])
        self.assertEqual(selection["strategy"], "validated-libtest-filter")
        self.assertEqual(selection["selected_target_benches"], ["vec::bench_with_capacity_1000"])
        self.assertEqual(selection["bench_list_count"], 2)

    def test_plain_filter_rejects_zero_match(self) -> None:
        ns = args(
            dataset="default_performance",
            allocator="ptmalloc",
            bench_filter="semantic_auto_metadata",
            dry_run=True,
        )
        with mock.patch.object(driver, "collect_bench_list", return_value=["vec::bench_new"]):
            with self.assertRaisesRegex(ValueError, "matched no non-sentinel"):
                driver.semantic_harness_selection(ns, ["bench_ptmalloc"])

    def test_build_command_uses_skip_selection_instead_of_hiding_sentinels(self) -> None:
        ns = args(dry_run=False)
        features = ["bench_ourself", "pac"]
        selection = {
            "derived_skips": ["binary_heap::bench_push", "vec::bench_new"],
            "deferred_for_dry_run": False,
        }
        command = driver.build_cargo_command(ns, features, selection)
        rendered = " ".join(command)
        self.assertIn("--test-threads=1", command)
        self.assertIn("--nocapture", command)
        self.assertIn("--skip", command)
        self.assertIn("binary_heap::bench_push", command)
        self.assertNotIn(" -- vec::bench_with_capacity_1000", rendered)

    def test_rust_toolchain_latest_alias_routes_cargo_command(self) -> None:
        ns = args(
            dataset="default_performance",
            allocator="unialloc",
            variant_feature=None,
            rust_toolchain="latest",
            bench_filter=None,
        )
        command = driver.build_cargo_command(ns, ["bench_ourself"], {})
        provenance = driver.rust_toolchain_provenance(ns)
        self.assertIn("+nightly", command)
        self.assertEqual(provenance["effective_toolchain"], "nightly")
        self.assertEqual(provenance["repo_toolchain"], driver.read_repo_rust_toolchain())
        self.assertTrue(provenance["source_accepted_newer_toolchain"])
        self.assertFalse(provenance["paper_exact_toolchain"])
        self.assertTrue(driver.toolchain_claim_grade_blockers(provenance))

    def test_repo_default_newer_pin_is_not_paper_exact(self) -> None:
        provenance = driver.rust_toolchain_provenance(args(rust_toolchain=None))
        self.assertTrue(provenance["uses_repo_rust_toolchain"])
        self.assertFalse(provenance["paper_exact_toolchain"])
        self.assertEqual(
            provenance["paper_exact_rust_toolchain"],
            "nightly-2022-07-01",
        )
        self.assertTrue(driver.toolchain_claim_grade_blockers(provenance))

    def test_explicit_paper_toolchain_is_paper_exact(self) -> None:
        provenance = driver.rust_toolchain_provenance(
            args(rust_toolchain="nightly-2022-07-01")
        )
        self.assertTrue(provenance["paper_exact_toolchain"])
        self.assertEqual(driver.toolchain_claim_grade_blockers(provenance), [])

    def test_rust_toolchain_system_omits_rustup_override(self) -> None:
        ns = args(
            dataset="default_performance",
            allocator="unialloc",
            variant_feature=None,
            rust_toolchain="system",
            bench_filter=None,
        )
        command = driver.build_cargo_command(ns, ["bench_ourself"], {})
        self.assertNotIn("+nightly-2022-07-01", command)
        self.assertNotIn("+nightly", command)
        provenance = driver.rust_toolchain_provenance(ns)
        self.assertTrue(provenance["uses_system_toolchain"])
        self.assertFalse(provenance["paper_exact_toolchain"])

    def test_bench_list_fingerprint_includes_effective_toolchain(self) -> None:
        repo_toolchain = driver.read_repo_rust_toolchain()
        repo_fingerprint = driver.bench_list_fingerprint(["bench_ourself"], repo_toolchain)
        alternate_toolchain = "stable" if repo_toolchain == "nightly" else "nightly"
        alternate_fingerprint = driver.bench_list_fingerprint(
            ["bench_ourself"], alternate_toolchain
        )
        self.assertNotEqual(repo_fingerprint["source_digest"], alternate_fingerprint["source_digest"])
        self.assertEqual(alternate_fingerprint["rust_toolchain"], alternate_toolchain)

    def test_timing_rows_exclude_sentinels_and_apply_filter(self) -> None:
        rows = [
            {"benchmark": "aaa_semantic_auto_metadata_enable", "ns_per_iter": 1},
            {"benchmark": "vec::bench_with_capacity_1000", "ns_per_iter": 10},
            {"benchmark": "vec::bench_new", "ns_per_iter": 2},
            {"benchmark": "zzz_semantic_auto_metadata_report", "ns_per_iter": 1},
        ]
        selected = driver.select_timing_rows(rows, args())
        self.assertEqual(
            [row["benchmark"] for row in selected["timing_rows"]],
            ["vec::bench_with_capacity_1000"],
        )
        self.assertEqual(
            [row["benchmark"] for row in selected["excluded_harness_rows"]],
            [
                "aaa_semantic_auto_metadata_enable",
                "zzz_semantic_auto_metadata_report",
            ],
        )


    def test_semantic_policy_claim_grade_rejects_layout_derived_basis(self) -> None:
        status = driver.semantic_policy_claim_grade_status(
            {"required": True},
            [
                {
                    "source": "std_bench_auto_metadata",
                    "event": "semantic_harness_state",
                    "type_id_basis": "layout-derived-size-align",
                }
            ],
        )
        self.assertFalse(status["ready"])
        self.assertIn("layout-derived", " ".join(status["blockers"]))

    def test_semantic_policy_claim_grade_accepts_compiler_assigned_basis(self) -> None:
        status = driver.semantic_policy_claim_grade_status(
            {"required": True},
            [
                {
                    "source": "std_bench_auto_metadata",
                    "event": "semantic_harness_state",
                    "type_id_basis": "compiler-assigned-allocation-site-object-type-id-consuming-stream",
                    "compiler_site_id_stream_mode": "consuming-stream",
                }
            ],
        )
        self.assertTrue(status["ready"])
        self.assertEqual(status["blockers"], [])

    def test_parse_semantic_events_keeps_probe_json(self) -> None:
        stdout = "\n".join(
            [
                "test vec::bench_with_capacity_1000 ... bench: 16 ns/iter (+/- 1)",
                'test zzz_semantic_auto_metadata_report ... {"source":"std_bench_auto_metadata","event":"semantic_harness_state","stats_feature":false}',
                '{"source":"std_bench_auto_metadata","event":"pac_metadata_auth_probe","attempted":true,"reused":true}',
                '{"source":"other","event":"ignored"}',
                "{not json",
            ]
        )
        events = driver.parse_semantic_events(stdout)
        self.assertEqual(len(events), 2)
        self.assertEqual(events[0]["event"], "semantic_harness_state")
        self.assertFalse(events[0]["stats_feature"])
        self.assertEqual(events[1]["event"], "pac_metadata_auth_probe")
        self.assertTrue(events[1]["reused"])

    def test_parse_bench_rows_accepts_newer_decimal_ns(self) -> None:
        rows = driver.parse_bench_rows(
            "test vec::bench_with_capacity_1000 ... bench:           9.37 ns/iter (+/- 0.39) = 111111 MB/s"
        )
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["benchmark"], "vec::bench_with_capacity_1000")
        self.assertAlmostEqual(rows[0]["ns_per_iter"], 9.37)
        self.assertAlmostEqual(rows[0]["deviation_ns"], 0.39)

    def test_scudo_auto_falls_back_to_ld_preload_runtime(self) -> None:
        runtime = "/usr/lib/llvm-16/lib/clang/16/lib/linux/libclang_rt.scudo_standalone-aarch64.so"
        ns = args(
            dataset="default_performance",
            allocator="scudo",
            scudo_mode="auto",
            scudo_runtime_library=runtime,
        )
        with mock.patch.object(driver, "scudo_toolchain_probe", return_value={"ok": False}), mock.patch.object(
            driver,
            "scudo_runtime_probe",
            return_value={"ok": True, "configured_runtime_library": runtime},
        ), mock.patch.dict(driver.os.environ, {}, clear=True):
            execution = driver.scudo_execution_probe(ns)
            env = driver.cargo_subprocess_env(ns)
        self.assertTrue(execution["ok"], execution)
        self.assertEqual(execution["selected_mode"], "ld-preload")
        self.assertEqual(env["UNIALLOC_SCUDO_RUNTIME_LIBRARY"], runtime)
        self.assertEqual(env["LD_PRELOAD"].split()[0], runtime)
        self.assertNotIn(driver.SCUDO_SANITIZER_RUSTFLAGS, env.get("RUSTFLAGS", ""))

    def test_scudo_validate_accepts_ld_preload_when_toolchain_rejects(self) -> None:
        runtime = "/usr/lib/llvm-16/lib/clang/16/lib/linux/libclang_rt.scudo_standalone-aarch64.so"
        ns = args(
            dataset="default_performance",
            allocator="scudo",
            scudo_mode="auto",
            scudo_runtime_library=runtime,
        )
        with mock.patch.object(driver, "scudo_toolchain_probe", return_value={"ok": False}), mock.patch.object(
            driver,
            "scudo_runtime_probe",
            return_value={"ok": True, "configured_runtime_library": runtime},
        ):
            self.assertIsNone(driver.validate_args(ns))

    def test_scudo_validate_fails_closed_without_toolchain_or_runtime(self) -> None:
        ns = args(dataset="default_performance", allocator="scudo", scudo_mode="auto")
        with mock.patch.object(driver, "scudo_toolchain_probe", return_value={"ok": False}), mock.patch.object(
            driver,
            "scudo_runtime_probe",
            return_value={"ok": False, "configured_runtime_library": None},
        ):
            self.assertEqual(driver.validate_args(ns), 2)


if __name__ == "__main__":
    unittest.main()
