#!/usr/bin/env python3

from __future__ import annotations

import importlib.util
import hashlib
import json
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "evaluation" / "scripts" / "type_isolation_redb_actix_campaign.py"
MANIFEST = ROOT / "evaluation" / "config" / "type_isolation_primary_suite.json"


def load_campaign():
    spec = importlib.util.spec_from_file_location("redb_actix_campaign", SCRIPT)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"unable to import {SCRIPT}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


class RedbActixCampaignTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.campaign = load_campaign()
        cls.suite = json.loads(MANIFEST.read_text(encoding="utf-8"))

    def add_warmup_evidence(self, record: dict, directory: Path) -> None:
        for harness in record["harnesses"]:
            for variant in self.campaign.VARIANTS:
                path = directory / f"{harness['id']}-{variant}.json"
                payload = {
                    "target_id": record["target_id"],
                    "harness_id": harness["id"],
                    "variant": variant,
                    "phase": "warmup",
                    "round": 0,
                    "performance": 1.0,
                    "peak_rss_mib": 2.0,
                }
                path.write_text(json.dumps(payload), encoding="utf-8")
                harness["warmup_evidence"][variant] = [
                    {
                        "record_path": str(path),
                        "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
                    }
                ]

    def test_exact_pins_and_harnesses_match_primary_suite(self) -> None:
        self.assertEqual(
            self.suite["implementation"]["git_revision"],
            self.campaign.PINNED_IMPLEMENTATION_REVISION,
        )
        self.assertEqual(
            self.suite["implementation"]["canonical_sha256"],
            self.campaign.PINNED_IMPLEMENTATION_SHA256,
        )
        targets = {target["id"]: target for target in self.suite["targets"]}
        for target_id in ("redb", "actix_web"):
            with self.subTest(target=target_id):
                expected = targets[target_id]
                actual = self.campaign.TARGETS[target_id]
                self.assertEqual(expected["source"]["ref"], actual.source_ref)
                self.assertEqual(expected["source"]["commit"], actual.source_commit)
                self.assertEqual(
                    [row["id"] for row in expected["harnesses"]],
                    [row.id for row in actual.harnesses],
                )
                self.assertTrue(
                    all(
                        row.metric_direction == "lower_is_better"
                        for row in actual.harnesses
                    )
                )

    def test_exact_git_revision_materializes_the_canonical_stream(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            snapshot = self.campaign.materialize_implementation_revision(
                Path(directory), self.campaign.PINNED_IMPLEMENTATION_REVISION
            )
            self.assertEqual(
                self.campaign.PINNED_IMPLEMENTATION_REVISION, snapshot.revision
            )
            self.assertEqual(
                self.campaign.PINNED_IMPLEMENTATION_SHA256, snapshot.sha256
            )
            self.assertEqual(131, snapshot.file_count)
            self.assertEqual(4_426_670, snapshot.size_bytes)
            manifest = self.campaign.verify_implementation_snapshot(snapshot)
            self.assertTrue(
                all(
                    str(snapshot.path) in str(snapshot.path / row["path"])
                    for row in manifest["git_blobs"]
                )
            )
            frozen_source = snapshot.path / "unialloc/src/lib.rs"
            frozen_source.write_bytes(frozen_source.read_bytes() + b"\n")
            with self.assertRaises(self.campaign.CampaignError):
                self.campaign.verify_implementation_snapshot(snapshot)

    def test_current_working_tree_snapshot_is_diagnostic_and_self_verifying(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            snapshot = self.campaign.materialize_working_tree_implementation(
                Path(directory)
            )
            manifest = self.campaign.verify_implementation_snapshot(snapshot)
            self.assertEqual("working_tree", manifest["source_kind"])
            self.assertEqual(
                "diagnostic_current_worktree",
                manifest["campaign_classification"],
            )
            self.assertFalse(manifest["primary_eligible"])
            self.assertEqual(snapshot.revision, manifest["repository_head"])
            self.assertEqual(
                self.campaign.snapshot_stream(
                    snapshot.path, [row["path"] for row in manifest["files"]]
                )[0],
                manifest["campaign_snapshot_sha256"],
            )
            self.assertEqual(
                hashlib.sha256(manifest["repository_status"].encode()).hexdigest(),
                manifest["repository_status_sha256"],
            )
            for name in ("Cargo.toml", "Cargo.lock", "rust-toolchain"):
                self.assertTrue((snapshot.path / name).is_file())

    def test_current_working_tree_cli_is_explicit_and_never_publishable(self) -> None:
        primary = self.campaign.parse_args(["--targets", "redb"])
        self.assertEqual(
            self.campaign.PINNED_IMPLEMENTATION_REVISION,
            primary.unialloc_revision,
        )
        self.assertFalse(primary.current_working_tree)
        diagnostic = self.campaign.parse_args(
            [
                "--targets",
                "redb",
                "--current-working-tree",
                "--required-cpus",
                "96-99",
            ]
        )
        self.assertIsNone(diagnostic.unialloc_revision)
        self.assertEqual("96-99", diagnostic.required_cpus)
        record = {"status": "complete"}
        with tempfile.TemporaryDirectory() as directory:
            self.campaign.mark_diagnostic_current_worktree(
                record,
                self.campaign.materialize_working_tree_implementation(Path(directory)),
                required_cpus="96-99",
            )
        self.assertFalse(record["primary_eligible"])
        self.assertFalse(record["core_eligible"])
        self.assertFalse(
            self.campaign.primary_publication_allowed(
                record, diagnostic_current_worktree=True
            )
        )
        with self.assertRaises(SystemExit):
            self.campaign.parse_args(
                [
                    "--unialloc-revision",
                    self.campaign.PINNED_IMPLEMENTATION_REVISION,
                    "--current-working-tree",
                ]
            )

    def test_redb_manifest_uses_the_copied_allocator_path(self) -> None:
        frozen = Path("/tmp/exact-f5d0c19/unialloc")
        manifest = self.campaign.redb_manifest(
            variant="unialloc",
            checkout=Path("/tmp/redb-v4.1.0"),
            unialloc_path=frozen,
        )
        self.assertIn(str(frozen), manifest)
        self.assertNotIn(str(ROOT / "unialloc"), manifest)

    def test_primary_variants_use_a_clean_head_compatible_feature_mapping(self) -> None:
        self.assertEqual(
            ((), True),
            self.campaign.unialloc_configuration_for_primary_variant("unialloc"),
        )
        for variant in ("typed_plain", "typeiso_perf"):
            with self.subTest(variant=variant):
                self.assertEqual(
                    (("type_isolation",), True),
                    self.campaign.unialloc_configuration_for_primary_variant(variant),
                )
        with self.assertRaises(self.campaign.CampaignError):
            self.campaign.unialloc_configuration_for_primary_variant("unsupported")
        dependency = self.campaign.cargo_unialloc_dependency(
            "unialloc", Path("/tmp/exact-f5d0c19/unialloc")
        )
        self.assertIn("/tmp/exact-f5d0c19/unialloc", dependency or "")

    def test_campaign_has_no_live_allocator_or_pass_build_route(self) -> None:
        source = SCRIPT.read_text(encoding="utf-8")
        for live_route in (
            'ROOT / "unialloc"',
            "matrix.implementation_digest()",
            "matrix.build_force_load_rlib(",
            "matrix.ensure_wrapper(",
        ):
            with self.subTest(live_route=live_route):
                self.assertNotIn(live_route, source)

    def test_redb_source_materializes_all_preregistered_work_amounts(self) -> None:
        source = self.campaign.REDB_RUNNER_SOURCE
        for literal in ("200_000", "2_000", "100_000", "512 * 1024"):
            self.assertIn(literal, source)
        for harness in self.campaign.TARGETS["redb"].harnesses:
            self.assertIn(f'"{harness.id}"', source)
        self.assertIn("Durability::None", source)
        self.assertIn("UNIALLOC_REDB_ACTIX_RESULT=", source)

    def test_actix_commands_select_upstream_benchmarks(self) -> None:
        commands = {
            harness.id: self.campaign.actix_benchmark_selection(harness.id)
            for harness in self.campaign.TARGETS["actix_web"].harnesses
        }
        self.assertEqual(
            ("actix-web", "service", "async_service_direct"),
            commands["async_service_direct"],
        )
        self.assertEqual(
            ("actix-web", "service", "async_web_service_direct"),
            commands["async_web_service_direct"],
        )
        self.assertEqual(
            ("actix-web", "server", "get_body_async_burst"),
            commands["get_body_async_burst"],
        )
        self.assertEqual(
            ("actix-http", "response-body-compression", "compression responses/gzip"),
            commands["compression_gzip"],
        )
        self.assertEqual(
            ("actix-router", "router", "Compare Routers/actix"),
            commands["router_actix"],
        )
        command, _cwd = self.campaign.measured_command(
            self.campaign.TARGETS["actix_web"],
            self.campaign.TARGETS["actix_web"].harnesses[0],
            {"executables": {"service": "/tmp/service-bench"}},
            Path("/tmp/run"),
        )
        self.assertIn("--bench", command)
        self.assertLess(command.index("--bench"), command.index("async_service_direct"))

    def test_actix_baseline_force_wrapper_preserves_upstream_lock(self) -> None:
        source = self.campaign.BASELINE_FORCE_WRAPPER_SOURCE
        self.assertIn("rustc = pathlib.Path(arguments.pop(0))", source)
        self.assertIn('f"force:unialloc={rlib}"', source)
        self.assertIn("os.execv(str(rustc), [str(rustc), *arguments])", source)
        self.assertNotIn("UNIALLOC_ACTUAL_MIR_REWRITE", source)
        self.assertLess(
            source.index('arguments.extend(["-L"'), source.index("if crate in targets:")
        )
        self.assertFalse(hasattr(self.campaign, "add_actix_baseline_dependencies"))

    def test_typeiso_force_wrapper_exposes_transitive_rlib_directory(self) -> None:
        source = self.campaign.transitive_typeiso_force_wrapper_source()
        self.assertLess(
            source.index('arguments.extend(["-L"'), source.index("if selected:")
        )
        selected = source[source.index("if selected:") :]
        self.assertIn('f"force:unialloc={rlib}"', selected)

    def test_criterion_estimate_parser_normalizes_seconds(self) -> None:
        parse = self.campaign.parse_criterion_estimate_seconds
        self.assertAlmostEqual(1.5e-9, parse("time:   [1.2 ns 1.5 ns 1.7 ns]"))
        self.assertAlmostEqual(2.5e-6, parse("time:   [2.1 us 2.5 us 2.9 us]"))
        self.assertAlmostEqual(3.5e-3, parse("time:   [3.1 ms 3.5 ms 3.9 ms]"))
        self.assertAlmostEqual(4.5, parse("time:   [4.1 s 4.5 s 4.9 s]"))
        with self.assertRaises(self.campaign.CampaignError):
            parse("Benchmarking without a complete estimate")

    def test_cpu_set_parser_expands_ranges(self) -> None:
        self.assertEqual({1, 3, 4, 5, 8}, self.campaign.parse_cpu_set("1,3-5,8"))
        with self.assertRaises(self.campaign.CampaignError):
            self.campaign.parse_cpu_set("5-3")

    def test_result_validator_requires_five_complete_paired_rounds(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            raw = Path(directory)
            record = self.campaign.empty_target_result(
                self.campaign.TARGETS["redb"], raw
            )
            self.add_warmup_evidence(record, raw)
            gates = {gate: True for gate in self.campaign.REQUIRED_GATES}
            for harness in record["harnesses"]:
                harness["gates"] = dict(gates)
                harness["measurements"] = [
                    {
                        "round": round_index,
                        "variant": variant,
                        "performance": 1.0,
                        "peak_rss_mib": 2.0,
                        "raw_path": "raw.json",
                        "build_path": "build.json",
                        "audit_path": "audit.json",
                    }
                    for round_index in range(1, 6)
                    for variant in self.campaign.VARIANTS
                ]
            self.campaign.validate_target_result(record)
            record["harnesses"][0]["measurements"].pop()
            with self.assertRaises(self.campaign.CampaignError):
                self.campaign.validate_target_result(record)

    def test_compiler_route_gate_uses_paired_median_ratio(self) -> None:
        rows = [
            {"round": round_index, "variant": variant, "performance": value}
            for round_index in range(1, 6)
            for variant, value in (
                ("unialloc", 1.0),
                ("typed_plain", 1.1),
                ("typeiso_perf", 1.2),
            )
        ]
        passed, ratio = self.campaign.compiler_route_equivalence(rows)
        self.assertTrue(passed)
        self.assertAlmostEqual(1.1, ratio)
        for row in rows:
            if row["variant"] == "typed_plain":
                row["performance"] = 1.16
        passed, ratio = self.campaign.compiler_route_equivalence(rows)
        self.assertFalse(passed)
        self.assertAlmostEqual(1.16, ratio)

    def test_route_failure_is_a_retained_attribution_limit(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            raw = Path(directory)
            record = self.campaign.empty_target_result(
                self.campaign.TARGETS["redb"], raw
            )
            self.add_warmup_evidence(record, raw)
            for harness in record["harnesses"]:
                harness["gates"] = {gate: True for gate in self.campaign.REQUIRED_GATES}
                harness["measurements"] = [
                    {
                        "round": round_index,
                        "variant": variant,
                        "performance": 1.0,
                        "peak_rss_mib": 2.0,
                    }
                    for round_index in range(1, 6)
                    for variant in self.campaign.VARIANTS
                ]
            record["harnesses"][1]["gates"]["compiler_route_equivalent"] = False

            status = self.campaign.classify_complete_result(record)

            self.assertEqual("complete_with_attribution_limits", status)
            self.assertTrue(record["core_eligible"])
            self.assertEqual(
                ["transaction_churn"],
                [row["harness_id"] for row in record["attribution_limits"]],
            )
            self.assertEqual(15, len(record["harnesses"][1]["measurements"]))

    def test_build_gate_rejects_mixed_implementation_digests(self) -> None:
        activation = {"passed": True}
        audit = {
            "audit_file_count": 1,
            "semantic_rewrites_applied": 1,
            "crate_names": list(self.campaign.TARGETS["redb"].target_crates),
        }
        builds = {
            "unialloc": {
                "stats_enabled": False,
                "activation": activation,
                "frozen_implementation_sha256": "one",
            },
            "typed_plain": {
                "stats_enabled": False,
                "activation": activation,
                "actual_mir_rewrite": True,
                "audit": audit,
                "typeiso": {"audit_dir": "/tmp"},
                "frozen_implementation_sha256": "one",
            },
            "typeiso_perf": {
                "stats_enabled": False,
                "activation": activation,
                "actual_mir_rewrite": True,
                "audit": audit,
                "typeiso": {"audit_dir": "/tmp"},
                "frozen_implementation_sha256": "two",
            },
        }
        gates = self.campaign.build_gates(self.campaign.TARGETS["redb"], builds)
        self.assertFalse(gates["source_audit_retained"])

    def test_build_success_gate_requires_all_three_successful_records(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            binary = Path(directory) / "runner"
            binary.write_bytes(b"runner")
            builds = {
                variant: {
                    "exit_code": 0,
                    "timed_out": False,
                    "binary": str(binary),
                }
                for variant in self.campaign.VARIANTS
            }
            self.assertTrue(
                self.campaign.build_records_succeeded(
                    self.campaign.TARGETS["redb"], builds
                )
            )
            builds["typeiso_perf"]["exit_code"] = 1
            self.assertFalse(
                self.campaign.build_records_succeeded(
                    self.campaign.TARGETS["redb"], builds
                )
            )

    def test_failure_record_retains_false_gates_and_no_measurements(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            raw = Path(directory)
            record = self.campaign.empty_target_result(
                self.campaign.TARGETS["actix_web"], raw
            )
            failed = self.campaign.mark_target_ineligible(record, "build failed")
            self.assertEqual(["build failed"], failed["blockers"])
            for harness in failed["harnesses"]:
                self.assertEqual([], harness["measurements"])
                self.assertTrue(
                    all(value == [] for value in harness["warmup_evidence"].values())
                )
                self.assertTrue(
                    all(value is False for value in harness["gates"].values())
                )

    def test_post_measurement_failure_retains_raw_evidence(self) -> None:
        record = self.campaign.empty_target_result(
            self.campaign.TARGETS["redb"], Path("/tmp/raw")
        )
        record["harnesses"][0]["measurements"] = [{"round": 1}]
        record["harnesses"][0]["gates"]["correctness"] = True
        failed = self.campaign.mark_target_ineligible(
            record, "route gate failed", retain_measurements=True
        )
        self.assertEqual([{"round": 1}], failed["harnesses"][0]["measurements"])
        self.assertTrue(failed["harnesses"][0]["gates"]["correctness"])


if __name__ == "__main__":
    unittest.main()
