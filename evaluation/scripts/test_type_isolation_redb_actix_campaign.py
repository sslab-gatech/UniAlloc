#!/usr/bin/env python3

from __future__ import annotations

import importlib.util
import hashlib
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "evaluation" / "scripts" / "type_isolation_redb_actix_campaign.py"
MANIFEST = (
    ROOT
    / "evaluation"
    / "config"
    / "type_isolation_primary_suite_v5_ce8af7b.json"
)


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
            self.assertEqual(137, snapshot.file_count)
            self.assertEqual(5_297_425, snapshot.size_bytes)
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
        primary = self.campaign.parse_args(
            ["--suite", str(MANIFEST), "--targets", "redb"]
        )
        self.assertEqual(
            self.campaign.PINNED_IMPLEMENTATION_REVISION,
            primary.unialloc_revision,
        )
        self.assertFalse(primary.current_working_tree)
        self.assertEqual(3, primary.rounds)
        self.assertEqual(MANIFEST.resolve(), primary.suite)
        self.assertEqual(
            ROOT
            / "evaluation/raw/type-isolation-primary-v5-ce8af7b/campaigns/redb-actix",
            primary.raw_dir,
        )
        self.assertEqual(
            ROOT / "evaluation/raw/type-isolation-primary-v5-ce8af7b/targets",
            primary.publication_dir,
        )
        diagnostic = self.campaign.parse_args(
            [
                "--targets",
                "redb",
                "--current-working-tree",
                "--rounds",
                "3",
                "--required-cpus",
                "96-99",
            ]
        )
        self.assertIsNone(diagnostic.unialloc_revision)
        self.assertEqual(3, diagnostic.rounds)
        self.assertEqual("96-99", diagnostic.required_cpus)
        record = {"status": "complete"}
        with tempfile.TemporaryDirectory() as directory:
            self.campaign.mark_diagnostic_current_worktree(
                record,
                self.campaign.materialize_working_tree_implementation(Path(directory)),
                required_cpus="96-99",
                measured_rounds=3,
            )
        self.assertFalse(record["primary_eligible"])
        self.assertFalse(record["core_eligible"])
        self.assertEqual(3, record["measured_rounds"])
        self.assertFalse(
            self.campaign.primary_publication_allowed(
                record, diagnostic_current_worktree=True
            )
        )
        self.assertTrue(
            self.campaign.primary_publication_allowed(
                {"status": "complete", "measured_rounds": 3},
                diagnostic_current_worktree=False,
            )
        )
        self.assertTrue(
            self.campaign.primary_publication_allowed(
                {"status": "complete", "measured_rounds": 5},
                diagnostic_current_worktree=False,
                measured_rounds=5,
            )
        )
        self.assertFalse(
            self.campaign.primary_publication_allowed(
                {"status": "complete", "measured_rounds": 4},
                diagnostic_current_worktree=False,
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
        self.assertEqual(
            3,
            self.campaign.parse_args(
                ["--targets", "redb", "--rounds", "3"]
            ).rounds,
        )
        with self.assertRaises(SystemExit):
            self.campaign.parse_args(["--targets", "redb", "--rounds", "5"])
        with self.assertRaises(SystemExit):
            self.campaign.parse_args(
                ["--targets", "redb", "--current-working-tree", "--rounds", "5"]
            )
        with self.assertRaises(SystemExit):
            self.campaign.parse_args(
                ["--targets", "redb", "--current-working-tree", "--rounds", "4"]
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

    def test_actix_measurement_identity_uses_selected_bench_executable(self) -> None:
        spec = self.campaign.TARGETS["actix_web"]
        harness = next(
            row for row in spec.harnesses if row.id == "get_body_async_burst"
        )
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            server = root / "server-bench"
            service = root / "service-bench"
            server.write_bytes(b"server executable")
            service.write_bytes(b"service executable")
            build_path = root / "build.json"
            build_path.write_text("{}\n", encoding="utf-8")
            build = {
                "executables": {
                    "server": str(server),
                    "service": str(service),
                },
                "build_path": str(build_path),
                "audit": None,
            }
            implementation = self.campaign.ImplementationSnapshot(
                revision="revision",
                sha256="implementation",
                path=root,
                manifest_path=root / "snapshot.json",
                file_count=1,
                size_bytes=1,
                repository=root,
            )
            contract = mock.Mock(
                suite_id="suite",
                manifest_sha256="manifest",
            )

            def measured_result(command, **kwargs):
                rss_path = kwargs["rss_path"]
                rss_path.parent.mkdir(parents=True, exist_ok=True)
                rss_path.write_text(
                    "UNIALLOC_GNU_TIME\t0.1\t0.1\t100%\t4096\t0\t1\t0\t0\t0\n",
                    encoding="utf-8",
                )
                return {
                    "stdout": b"time:   [1.0 us 1.1 us 1.2 us]\n",
                    "stderr": b"",
                    "exit_code": 0,
                    "timed_out": False,
                    "peak_rss_kib": 4096,
                    "command": ["/usr/bin/time", *command],
                    "measured_command": command,
                    "wall_seconds": 0.1,
                    "gnu_time_exit_status": 0,
                }

            with mock.patch.object(
                self.campaign.matrix,
                "run_measured",
                side_effect=measured_result,
            ):
                summary = self.campaign.execute_one(
                    spec,
                    harness,
                    "typeiso_perf",
                    build,
                    raw_dir=root / "raw",
                    round_index=0,
                    timeout=30,
                    warmup=True,
                    implementation=implementation,
                    contract=contract,
                    suite_binding={},
                )

            record = json.loads(
                Path(summary["record_path"]).read_text(encoding="utf-8")
            )
            selected_sha256 = hashlib.sha256(server.read_bytes()).hexdigest()
            self.assertEqual(str(server), record["measured_command"][0])
            self.assertEqual(
                selected_sha256,
                record["identity"]["binary_sha256"],
            )
            self.assertEqual(
                str(server.resolve()),
                record["artifacts"]["binary"]["path"],
            )
            self.assertEqual(
                selected_sha256,
                record["artifacts"]["binary"]["sha256"],
            )

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

    def test_committed_metrics_are_rederived_from_stdout_and_gnu_time(self) -> None:
        spec = self.campaign.TARGETS["actix_web"]
        harness = spec.harnesses[0]
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            stdout = root / "stdout"
            stderr = root / "stderr"
            gnu_time = root / "time"
            stdout.write_text("time:   [1.0 us 1.1 us 1.2 us]\n", encoding="utf-8")
            stderr.write_bytes(b"")
            gnu_time.write_text(
                "UNIALLOC_GNU_TIME\t0.1\t0.1\t100%\t4096\t0\t1\t0\t0\t0\n",
                encoding="utf-8",
            )
            record = {
                "evidence_schema_version": 1,
                "identity": {},
                "metrics": {
                    "performance": 1.1e-6,
                    "performance_unit": "seconds",
                    "peak_rss_mib": 4.0,
                },
                "artifacts": {
                    "stdout": self.campaign.immutable_evidence.artifact_ref(stdout),
                    "stderr": self.campaign.immutable_evidence.artifact_ref(stderr),
                    "gnu_time": self.campaign.immutable_evidence.artifact_ref(
                        gnu_time
                    ),
                },
            }
            self.campaign.validate_committed_measurement(
                record,
                expected_identity={},
                spec=spec,
                harness=harness,
            )
            record["metrics"]["peak_rss_mib"] = 5.0
            with self.assertRaises(
                self.campaign.immutable_evidence.ImmutableEvidenceError
            ):
                self.campaign.validate_committed_measurement(
                    record,
                    expected_identity={},
                    spec=spec,
                    harness=harness,
                )

    def test_actix_failed_requests_are_a_correctness_failure_at_exit_zero(self) -> None:
        spec = self.campaign.TARGETS["actix_web"]
        harness = next(
            row for row in spec.harnesses if row.id == "get_body_async_burst"
        )
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "measurement.json"
            result = {
                "stdout": b"time:   [1.0 us 1.1 us 1.2 us]\n",
                "stderr": b"failed 28 requests (might be bench timeout)\n",
                "exit_code": 0,
                "timed_out": False,
            }
            with self.assertRaises(
                self.campaign.HarnessCorrectnessError
            ) as captured:
                self.campaign.persist_measurement(
                    result,
                    path=path,
                    spec=spec,
                    harness=harness,
                    variant="typeiso_perf",
                    round_index=1,
                    build={},
                    runtime_environment=self.campaign.clean_runtime_environment(
                        Path(directory)
                    ),
                    identity={},
                    suite_binding={},
                    gnu_time_path=Path(directory) / "gnu-time.txt",
                )

            self.assertEqual(harness.id, captured.exception.harness_id)
            self.assertIn("failed 28 requests", str(captured.exception))
            self.assertEqual(result["stdout"], path.with_suffix(".stdout").read_bytes())
            self.assertEqual(result["stderr"], path.with_suffix(".stderr").read_bytes())

            record = self.campaign.empty_target_result(spec, Path(directory))
            for row in record["harnesses"]:
                row["gates"] = {
                    gate: True for gate in self.campaign.REQUIRED_GATES
                }
            record["harnesses"][0]["measurements"] = [{"round": 1}]
            self.campaign.mark_target_ineligible(
                record,
                str(captured.exception),
                retain_measurements=True,
                correctness_harness_id=captured.exception.harness_id,
            )
            failed_harness = next(
                row for row in record["harnesses"] if row["id"] == harness.id
            )
            self.assertEqual("ineligible", record["status"])
            self.assertFalse(failed_harness["gates"]["correctness"])

        self.assertEqual(
            0,
            self.campaign.actix_failed_request_count(
                "failed 0 requests (might be bench timeout)"
            ),
        )

    def test_actix_get_body_patch_bounds_and_validates_every_request(self) -> None:
        original = """\
fn benchmark(iters: u64) {
                let start = std::time::Instant::now();
                // benchmark body

                let burst = (0..iters).map(|_| client.send());
                let resps = join_all(burst).await;

                let elapsed = start.elapsed();

                // if there are failed requests that might be an issue
                let failed = resps.iter().filter(|r| r.is_err()).count();
                if failed > 0 {
                    eprintln!("failed {} requests (might be bench timeout)", failed);
                };

                elapsed
}
"""
        with tempfile.TemporaryDirectory() as directory:
            worktree = Path(directory)
            source = worktree / "actix-web" / "benches" / "server.rs"
            source.parent.mkdir(parents=True)
            source.write_text(original, encoding="utf-8")

            audit = self.campaign.patch_actix_get_body_benchmark(worktree)
            patched = source.read_text(encoding="utf-8")

            self.assertEqual("actix-web/benches/server.rs", audit["path"])
            self.assertEqual(
                hashlib.sha256(original.encode()).hexdigest(),
                audit["upstream_sha256"],
            )
            self.assertEqual(
                hashlib.sha256(patched.encode()).hexdigest(),
                audit["patched_sha256"],
            )
            self.assertIn("const MAX_IN_FLIGHT_REQUESTS: u64 = 8;", patched)
            self.assertIn(
                "remaining.min(MAX_IN_FLIGHT_REQUESTS)",
                patched,
            )
            self.assertIn(
                'response.expect("get_body_async_burst request failed")', patched
            )
            self.assertIn("response.status().is_success()", patched)
            self.assertIn("let body = response", patched)
            self.assertIn(".body()", patched)
            self.assertIn('expect("get_body_async_burst body read failed")', patched)
            self.assertIn("assert_eq!(", patched)
            self.assertIn("body.as_ref(),", patched)
            self.assertIn("STR.as_bytes(),", patched)
            self.assertNotIn("might be bench timeout", patched)
            with self.assertRaises(self.campaign.CampaignError):
                self.campaign.patch_actix_get_body_benchmark(worktree)

            for _package, _bench, _selector, relative_path in set(
                self.campaign.ACTIX_BENCHES.values()
            ):
                path = worktree / relative_path
                path.parent.mkdir(parents=True, exist_ok=True)
                if not path.exists():
                    path.write_text("fn main() {}\n", encoding="utf-8")
            source_audit = self.campaign.append_actix_instrumentation(
                worktree, [audit]
            )
            server_audit = next(
                row
                for row in source_audit
                if row["path"] == "actix-web/benches/server.rs"
            )
            self.assertEqual(
                audit["upstream_sha256"], server_audit["upstream_sha256"]
            )
            self.assertEqual(
                audit["patched_sha256"], server_audit["patched_sha256"]
            )
            self.assertEqual(
                hashlib.sha256(source.read_bytes()).hexdigest(),
                server_audit["instrumented_sha256"],
            )

    def test_cpu_set_parser_expands_ranges(self) -> None:
        self.assertEqual({1, 3, 4, 5, 8}, self.campaign.parse_cpu_set("1,3-5,8"))
        with self.assertRaises(self.campaign.CampaignError):
            self.campaign.parse_cpu_set("5-3")

    def test_result_validator_accepts_retained_legacy_five_round_artifact(self) -> None:
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
            self.campaign.validate_target_result(
                record, measured_rounds=self.campaign.LEGACY_ROUNDS
            )
            record["harnesses"][0]["measurements"].pop()
            with self.assertRaises(self.campaign.CampaignError):
                self.campaign.validate_target_result(
                    record, measured_rounds=self.campaign.LEGACY_ROUNDS
                )

    def test_diagnostic_result_validator_accepts_three_complete_paired_rounds(
        self,
    ) -> None:
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
                    }
                    for round_index in range(1, 4)
                    for variant in self.campaign.VARIANTS
                ]
            self.campaign.validate_target_result(record)
            for harness in record["harnesses"]:
                passed, ratio = self.campaign.compiler_route_equivalence(
                    harness["measurements"], measured_rounds=3
                )
                self.assertTrue(passed)
                self.assertEqual(1.0, ratio)

    def test_compiler_route_gate_uses_paired_median_ratio(self) -> None:
        rows = [
            {"round": round_index, "variant": variant, "performance": value}
            for round_index in range(1, 4)
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
                    for round_index in range(1, 4)
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
            self.assertEqual(9, len(record["harnesses"][1]["measurements"]))

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

    def test_timed_children_use_clean_libc_default_runtime(self) -> None:
        with tempfile.TemporaryDirectory() as directory, mock.patch.dict(
            os.environ,
            {
                "PATH": "/usr/bin",
                "USER": "test",
                "LD_PRELOAD": "/tmp/wrong.so",
                "MALLOC_CONF": "dirty_decay_ms:0",
                "GLIBC_TUNABLES": "glibc.pthread.rseq=0",
                "UNIALLOC_LOWERING_POLICY_FLAGS": "99",
            },
            clear=True,
        ):
            environment = self.campaign.clean_runtime_environment(Path(directory))
            record = self.campaign.suite_contract.runtime_environment_record(
                environment
            )
        self.assertEqual(
            {"PATH", "USER", "LANG", "LC_ALL", "TMPDIR"}, set(environment)
        )
        self.assertEqual("libc_default", record["rseq_policy"])
        self.assertEqual(
            self.campaign.suite_contract.HOST_PRIMARY_MEASUREMENT_LOCK,
            self.campaign.MEASUREMENT_LOCK_PATH,
        )


if __name__ == "__main__":
    unittest.main()
