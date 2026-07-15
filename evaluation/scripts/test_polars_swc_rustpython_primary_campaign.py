#!/usr/bin/env python3

from __future__ import annotations

import contextlib
import hashlib
import importlib.util
import io
import json
import os
import pathlib
import subprocess
import sys
import tempfile
import unittest
from unittest import mock


ROOT = pathlib.Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "evaluation" / "scripts" / "polars_swc_rustpython_primary_campaign.py"
SPEC = importlib.util.spec_from_file_location("primary_campaign", SCRIPT)
assert SPEC is not None and SPEC.loader is not None
campaign = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = campaign
SPEC.loader.exec_module(campaign)


class PolarsSwcRustPythonPrimaryCampaignTests(unittest.TestCase):
    def test_exact_f5_allocator_archive_matches_canonical_suite_digest(self) -> None:
        revision = "f5d0c19c1cc5b56fdac3282d69333dd8c85d4cf2"
        with tempfile.TemporaryDirectory() as temporary:
            record = campaign.snapshot_allocator(pathlib.Path(temporary), revision)
        self.assertEqual(revision, record["allocator_revision"])
        self.assertEqual("git_revision", record["source_kind"])
        self.assertEqual("git_archive", record["snapshot_source"])
        self.assertEqual(131, record["implementation_file_count"])
        self.assertEqual(4_426_670, record["implementation_total_bytes"])
        self.assertEqual(
            "cab1e580c08e2b16308bae75501716049ba428b040fb04bf305269c9ba9eaf01",
            record["unialloc_implementation_sha256"],
        )

    def test_snapshot_allocator_rejects_cross_mode_raw_dir_reuse(self) -> None:
        revision = campaign.SUITE_IMPLEMENTATION_REVISION
        scenarios = (
            (revision, False, None, True),
            (None, True, revision, False),
        )
        for (
            first_revision,
            first_working_tree,
            second_revision,
            second_working_tree,
        ) in scenarios:
            with self.subTest(first_working_tree=first_working_tree):
                with tempfile.TemporaryDirectory() as temporary:
                    raw_dir = pathlib.Path(temporary)
                    campaign.snapshot_allocator(
                        raw_dir,
                        first_revision,
                        current_working_tree=first_working_tree,
                    )
                    with self.assertRaisesRegex(
                        campaign.CampaignError, "snapshot source mode"
                    ):
                        campaign.snapshot_allocator(
                            raw_dir,
                            second_revision,
                            current_working_tree=second_working_tree,
                        )

    def test_exact_snapshot_reuse_rejects_frozen_context_drift(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            raw_dir = pathlib.Path(temporary)
            record = campaign.snapshot_allocator(
                raw_dir, campaign.SUITE_IMPLEMENTATION_REVISION
            )
            snapshot = pathlib.Path(record["path"])
            with (snapshot / "Cargo.toml").open("a", encoding="utf-8") as handle:
                handle.write("\n# post-freeze drift\n")
            with self.assertRaisesRegex(
                campaign.CampaignError, "snapshot context digest changed"
            ):
                campaign.snapshot_allocator(
                    raw_dir, campaign.SUITE_IMPLEMENTATION_REVISION
                )

    def test_current_working_tree_snapshot_is_explicit_and_diagnostic(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            record = campaign.snapshot_allocator(
                pathlib.Path(temporary), current_working_tree=True
            )
            snapshot = pathlib.Path(record["path"])
            self.assertEqual("working_tree", record["source_kind"])
            self.assertEqual("working_tree_copy", record["snapshot_source"])
            self.assertEqual(
                campaign.git_output(ROOT, "rev-parse", "HEAD"),
                record["repository_head"],
            )
            self.assertEqual(
                hashlib.sha256(record["repository_status"].encode()).hexdigest(),
                record["repository_status_sha256"],
            )
            self.assertEqual(
                campaign.tree_digest(
                    campaign.implementation_files(snapshot),
                    snapshot,
                    trailing_nul=True,
                ),
                record["unialloc_implementation_sha256"],
            )
            for name in ("Cargo.toml", "Cargo.lock", "rust-toolchain"):
                self.assertTrue((snapshot / name).is_file())
            self.assertTrue((snapshot / "unialloc/.cargo/config").is_file())
            self.assertIn("unialloc/.cargo/config", record["campaign_snapshot_files"])
            self.assertEqual(
                campaign.tree_digest(
                    campaign.working_tree_context_files(snapshot), snapshot
                ),
                record["campaign_snapshot_sha256"],
            )

    def test_current_working_tree_cli_controls_affinity_and_cannot_publish(
        self,
    ) -> None:
        primary = campaign.parse_args(["--targets", "swc", "--phase", "build"])
        self.assertEqual(
            campaign.SUITE_IMPLEMENTATION_REVISION, primary.allocator_revision
        )
        self.assertFalse(primary.current_working_tree)
        self.assertEqual("32-63", primary.cpu_list)
        self.assertEqual("1", primary.numa_node)

        diagnostic = campaign.parse_args(
            [
                "--targets",
                "swc",
                "--phase",
                "build",
                "--current-working-tree",
                "--cpu-list",
                "96-99",
                "--numa-node",
                "1",
            ]
        )
        self.assertIsNone(diagnostic.allocator_revision)
        self.assertEqual("96-99", diagnostic.cpu_list)
        self.assertFalse(
            campaign.primary_publication_allowed(
                {
                    "status": "complete",
                    "campaign_classification": "diagnostic_current_worktree",
                    "primary_eligible": False,
                },
                current_working_tree=True,
            )
        )
        with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            campaign.parse_args(
                [
                    "--allocator-revision",
                    campaign.SUITE_IMPLEMENTATION_REVISION,
                    "--current-working-tree",
                ]
            )
        with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            campaign.parse_args(["--cpu-list", "96-99"])

    def test_specs_match_current_suite_pins_and_harness_order(self) -> None:
        campaign.validate_specs_against_suite()
        suite = json.loads(campaign.SUITE_PATH.read_text(encoding="utf-8"))
        self.assertEqual(
            suite["implementation"]["git_revision"],
            campaign.SUITE_IMPLEMENTATION_REVISION,
        )
        self.assertEqual(
            suite["implementation"]["canonical_sha256"],
            campaign.SUITE_IMPLEMENTATION_SHA256,
        )
        self.assertEqual(
            "96fa64009550d52f89549dd2124e3b1a402a42b7f1fd1750888600dd67bb8a6f",
            campaign.PREREGISTRATION_SUITE_MANIFEST_SHA256,
        )
        self.assertEqual(
            hashlib.sha256(campaign.SUITE_PATH.read_bytes()).hexdigest(),
            campaign.ANALYSIS_SUITE_MANIFEST_SHA256,
        )
        self.assertEqual(
            campaign.ANALYSIS_SUITE_MANIFEST_SHA256,
            campaign.SUITE_MANIFEST_SHA256,
        )
        targets = {row["id"]: row for row in suite["targets"]}
        for target_id, spec in campaign.TARGET_SPECS.items():
            self.assertEqual(targets[target_id]["source"]["commit"], spec.commit)
            self.assertEqual(
                [row["id"] for row in targets[target_id]["harnesses"]],
                list(spec.harness_filters),
            )

    def test_engine_crate_allowlists_cover_each_real_workload(self) -> None:
        polars = set(campaign.TARGET_SPECS["polars"].target_crates)
        self.assertTrue(
            {
                "polars_core",
                "polars_lazy",
                "polars_mem_engine",
                "polars_expr",
                "polars_io",
                "polars_plan",
                "polars_stream",
                "polars_compute",
            }.issubset(polars)
        )
        swc = set(campaign.TARGET_SPECS["swc"].target_crates)
        self.assertTrue(
            {
                "swc",
                "swc_ecma_parser",
                "swc_ecma_codegen",
                "swc_ecma_transforms",
            }.issubset(swc)
        )
        rustpython = set(campaign.TARGET_SPECS["rustpython"].target_crates)
        self.assertTrue(
            {
                "rustpython_vm",
                "rustpython_compiler",
                "rustpython_codegen",
                "rustpython_ruff_python_parser",
            }.issubset(rustpython)
        )

    def test_polars_driver_implements_every_predeclared_operation(self) -> None:
        for harness_id in campaign.TARGET_SPECS["polars"].harness_filters:
            self.assertIn(f'"{harness_id}"', campaign.POLARS_SOURCE)
        self.assertIn("const ROWS: usize = 1_000_000", campaign.POLARS_SOURCE)
        self.assertIn("LazyCsvReader", campaign.POLARS_SOURCE)
        self.assertIn("operation_seconds", campaign.POLARS_SOURCE)
        self.assertIn('"temporal"', campaign.POLARS_MANIFEST)
        self.assertIn("DataFrame::new(ROWS", campaign.POLARS_SOURCE)
        self.assertIn("frame.columns()", campaign.POLARS_SOURCE)

    def test_criterion_parser_returns_central_estimate_in_seconds(self) -> None:
        output = b"sample time:   [7.9132 ms 7.9397 ms 7.9693 ms]\n"
        self.assertAlmostEqual(0.0079397, campaign.criterion_seconds(output))
        output = "time: [900.0 µs 1.25 ms 1.5 ms]".encode()
        self.assertAlmostEqual(0.00125, campaign.criterion_seconds(output))

    def test_compiler_route_gate_uses_five_same_round_ratios(self) -> None:
        rows = []
        for round_number in range(1, 6):
            rows.extend(
                [
                    {
                        "round": round_number,
                        "variant": "unialloc",
                        "performance": 1.0,
                    },
                    {
                        "round": round_number,
                        "variant": "typed_plain",
                        "performance": 1.14,
                    },
                    {
                        "round": round_number,
                        "variant": "typeiso_perf",
                        "performance": 1.15,
                    },
                ]
            )
        accepted, median = campaign.route_equivalent(rows)
        self.assertTrue(accepted)
        self.assertAlmostEqual(1.14, median)
        rows[1]["performance"] = 2.0
        rows[4]["performance"] = 2.0
        rows[7]["performance"] = 2.0
        accepted, median = campaign.route_equivalent(rows)
        self.assertFalse(accepted)
        self.assertAlmostEqual(2.0, median)

    def test_failed_target_is_schema_conformant_and_fail_closed(self) -> None:
        spec = campaign.TARGET_SPECS["swc"]
        with tempfile.TemporaryDirectory() as temporary:
            record = campaign.failed_target(
                spec, "build failed", pathlib.Path(temporary)
            )
        self.assertEqual(1, record["schema_version"])
        self.assertEqual("swc", record["target_id"])
        self.assertEqual(spec.commit, record["source_commit"])
        self.assertEqual(
            list(spec.harness_filters), [row["id"] for row in record["harnesses"]]
        )
        for harness in record["harnesses"]:
            self.assertEqual([], harness["measurements"])
            self.assertEqual(
                {gate: False for gate in campaign.REQUIRED_GATES},
                harness["gates"],
            )

    def test_measurement_contract_pins_children_and_uses_shared_lock(self) -> None:
        self.assertEqual("32-63", campaign.MEASUREMENT_CPU_LIST)
        self.assertEqual("1", campaign.MEASUREMENT_NUMA_NODE)
        self.assertEqual(
            ROOT
            / "evaluation"
            / "raw"
            / "type-isolation-primary-v1"
            / "primary-measurement.lock",
            campaign.MEASUREMENT_LOCK,
        )
        args = campaign.parse_args(["--targets", "swc", "--phase", "warmup"])
        self.assertEqual(("swc",), args.targets)
        self.assertLessEqual(args.jobs, 32)
        self.assertEqual(1, args.warmups)
        self.assertEqual(5, args.rounds)
        with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            campaign.parse_args(["--targets", "swc", "--rounds", "6"])
        command = campaign.command_for(
            campaign.TARGET_SPECS["swc"],
            pathlib.Path("/tmp/bench"),
            "large_parser",
            None,
        )
        expected_prefix = [
            "numactl",
            "--physcpubind=32-63",
            "--membind=1",
            "taskset",
            "-c",
            "32-63",
        ]
        self.assertEqual(expected_prefix, campaign.measurement_prefix())
        self.assertEqual("/tmp/bench", command[0])
        self.assertTrue(
            campaign.is_build_process(
                "unialloc-rustc", "unialloc-rustc-wrapper rustc --crate-name polars"
            )
        )
        self.assertFalse(campaign.is_build_process("python3", "benchmark.py"))

    def test_quiescence_uses_diagnostic_measurement_affinity(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            with (
                mock.patch.object(
                    campaign, "active_build_processes", side_effect=([], [])
                ) as active,
                mock.patch.object(
                    campaign.time, "time", side_effect=(10.0, 10.0, 12.0)
                ),
                mock.patch.object(campaign.time, "sleep"),
                mock.patch.object(
                    campaign.os, "getloadavg", return_value=(0.0, 0.0, 0.0)
                ),
            ):
                record = campaign.wait_for_build_quiescence(
                    pathlib.Path(temporary),
                    "swc",
                    timeout=10,
                    quiet_seconds=1,
                    cpu_list="96-99",
                    numa_node="3",
                )
        self.assertEqual("96-99", record["cpu_list"])
        self.assertEqual(3, record["numa_node"])
        self.assertEqual(
            [mock.call("96-99"), mock.call("96-99")], active.call_args_list
        )

    def test_diagnostic_metadata_always_clears_core_eligibility(self) -> None:
        record: dict[str, object] = {"status": "failed"}
        campaign.apply_diagnostic_result_metadata(
            record,
            {
                "source_kind": "working_tree",
                "repository_head": "a" * 40,
                "repository_status": " M unialloc/src/lib.rs",
                "repository_status_sha256": "b" * 64,
                "unialloc_implementation_sha256": "c" * 64,
                "campaign_snapshot_sha256": "d" * 64,
                "campaign_snapshot_file_count": 5,
            },
            cpu_list="96-99",
            numa_node="3",
        )
        self.assertIs(record["core_eligible"], False)

    def test_warmup_evidence_is_variant_keyed_and_digest_bound(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            rows = []
            for variant in campaign.VARIANTS:
                record_path = pathlib.Path(temporary) / f"{variant}.json"
                record_path.write_text(
                    json.dumps(
                        {
                            "target_id": "swc",
                            "harness_id": "large_parser",
                            "variant": variant,
                            "phase": "warmup",
                            "round": 0,
                            "performance": 1.0,
                            "peak_rss_mib": 2.0,
                        }
                    ),
                    encoding="utf-8",
                )
                rows.append({"variant": variant, "raw_record": str(record_path)})
            evidence = campaign.warmup_evidence(rows)
        self.assertEqual(set(campaign.VARIANTS), set(evidence))
        for variant in campaign.VARIANTS:
            self.assertEqual(1, len(evidence[variant]))
            self.assertRegex(evidence[variant][0]["sha256"], r"^[0-9a-f]{64}$")

    def test_build_success_gate_requires_all_three_build_records(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = pathlib.Path(temporary)
            patch = root / "allocator.patch"
            patch.touch()
            binaries = {
                variant: {
                    "success": True,
                    "activation": {"success": True},
                    "actual_mir_provenance": variant != "unialloc",
                    "stats_enabled": False,
                    "audit_dir": str(root),
                    "allocator_patch": str(patch),
                }
                for variant in campaign.VARIANTS
            }
            gates = campaign.harness_gates(
                binaries, correctness=True, compiler_route_equivalent=True
            )
            self.assertTrue(gates["build_success"])
            binaries["unialloc"]["success"] = False
            gates = campaign.harness_gates(
                binaries, correctness=True, compiler_route_equivalent=True
            )
            self.assertFalse(gates["build_success"])

    def test_harness_performance_contract_uses_uniform_seconds(self) -> None:
        polars = campaign.performance_contract(campaign.TARGET_SPECS["polars"])
        self.assertEqual("seconds", polars["performance_unit"])
        self.assertEqual(
            "benchmark-owned-operation-seconds", polars["performance_source"]
        )
        for target_id in ("swc", "rustpython"):
            criterion = campaign.performance_contract(campaign.TARGET_SPECS[target_id])
            self.assertEqual("seconds", criterion["performance_unit"])
            self.assertEqual(
                "criterion-median-seconds-per-iteration",
                criterion["performance_source"],
            )

    def test_build_complete_archive_is_byte_exact_and_sha_indexed(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = pathlib.Path(temporary)
            output = root / "results" / "swc.json"
            output.parent.mkdir(parents=True)
            content = (
                b'{"status":"build_complete","suite_manifest_sha256":"'
                + campaign.PREREGISTRATION_SUITE_MANIFEST_SHA256.encode()
                + b'","target_id":"swc"}\n'
            )
            output.write_bytes(content)
            record = campaign.archive_build_complete_result(output, "swc", root)
            self.assertIsNotNone(record)
            assert record is not None
            archive = pathlib.Path(record["archive_path"])
            self.assertEqual(content, archive.read_bytes())
            self.assertEqual(hashlib.sha256(content).hexdigest(), record["sha256"])
            index = json.loads(
                (root / "results" / "build-complete" / "index.json").read_text()
            )
            self.assertEqual(record, index["records"]["swc"])

    def test_rustpython_runtime_library_is_sys_relative_and_digest_attested(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = pathlib.Path(temporary)
            version = (
                f"{campaign.sys.version_info.major}.{campaign.sys.version_info.minor}"
            )
            executable = root / "bin" / f"python{version}"
            executable.parent.mkdir()
            executable.touch()
            library = root / "lib" / f"libpython{version}.so.1.0"
            library.parent.mkdir()
            library.write_bytes(b"test-libpython")
            with mock.patch.object(campaign.sys, "executable", str(executable)):
                contract = campaign.rustpython_runtime_contract()
            self.assertEqual(str(executable.resolve()), contract["python_executable"])
            self.assertEqual(str(library.resolve()), contract["library_path"])
            self.assertEqual(
                hashlib.sha256(library.read_bytes()).hexdigest(),
                contract["library_sha256"],
            )
            environment = campaign.apply_runtime_environment(
                campaign.TARGET_SPECS["rustpython"],
                {"LD_LIBRARY_PATH": "/existing"},
                contract,
            )
            self.assertEqual(
                f"{library.parent.resolve()}:/existing",
                environment["LD_LIBRARY_PATH"],
            )
            binaries = {}
            for variant in campaign.VARIANTS:
                binary = root / "binaries" / variant
                binary.parent.mkdir(parents=True, exist_ok=True)
                binary.write_bytes(variant.encode())
                binaries[variant] = {"binary": str(binary)}
            ldd_stdout = (
                f"{contract['library_soname']} => {library.resolve()} (0x00000000)\n"
            ).encode()
            execution = {
                "command": ["ldd"],
                "exit_code": 0,
                "timed_out": False,
                "stdout": ldd_stdout,
                "stderr": b"",
                "wall_seconds": 0.01,
            }
            with mock.patch.object(campaign, "execute", return_value=execution):
                proof = campaign.prove_rustpython_runtime(
                    binaries, root / "raw", contract
                )
            self.assertEqual(set(campaign.VARIANTS), set(proof["variants"]))
            for variant in campaign.VARIANTS:
                record = proof["variants"][variant]
                self.assertTrue(record["success"])
                self.assertEqual(
                    contract["library_sha256"],
                    record["resolved_library_sha256"],
                )
                self.assertEqual(
                    contract["library_soname"],
                    record["resolved_library_soname"],
                )

    def test_swc_patch_removes_upstream_mimalloc_activation(self) -> None:
        source = "extern crate swc_malloc;\nfn main() {}\n"
        body = source.replace("extern crate swc_malloc;", "", 1).lstrip()
        patched = campaign.allocator_source().lstrip() + "\n" + body
        self.assertNotIn("extern crate swc_malloc", patched)
        self.assertIn("UNIALLOC_PRIMARY_ALLOCATOR", patched)

    def test_force_load_wrapper_exposes_dependency_path_to_every_crate(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = pathlib.Path(temporary)
            driver = root / "driver"
            driver.write_text(
                "#!/usr/bin/env python3\n"
                "import json, sys\n"
                "print(json.dumps(sys.argv[1:]))\n",
                encoding="utf-8",
            )
            driver.chmod(0o755)
            dependency_dir = root / "deps"
            dependency_dir.mkdir()
            rlib = dependency_dir / "libunialloc-test.rlib"
            rlib.touch()
            wrapper = campaign.ensure_force_load_wrapper(root / "wrapper")
            env = os.environ.copy()
            env.update(
                {
                    "UNIALLOC_FORCE_LOAD_DRIVER": str(driver),
                    "UNIALLOC_FORCE_LOAD_RLIB": str(rlib),
                    "UNIALLOC_FORCE_LOAD_DEPENDENCY_DIR": str(dependency_dir),
                    "UNIALLOC_RUSTC_TARGET_CRATES": "selected",
                }
            )

            unselected = subprocess.run(
                [wrapper, "/bin/true", "--crate-name", "downstream"],
                env=env,
                check=True,
                capture_output=True,
                text=True,
            )
            unselected_args = json.loads(unselected.stdout)
            self.assertEqual(
                ["-L", f"dependency={dependency_dir}"], unselected_args[-2:]
            )
            self.assertNotIn("--extern", unselected_args)

            selected = subprocess.run(
                [wrapper, "/bin/true", "--crate-name", "selected"],
                env=env,
                check=True,
                capture_output=True,
                text=True,
            )
            selected_args = json.loads(selected.stdout)
            dependency_index = selected_args.index("-L")
            selected_index = selected_args.index("--extern")
            self.assertLess(dependency_index, selected_index)
            self.assertEqual(
                f"dependency={dependency_dir}",
                selected_args[dependency_index + 1],
            )
            self.assertEqual(
                f"force:unialloc={rlib}", selected_args[selected_index + 1]
            )

    def test_direct_load_wrapper_preserves_cargo_graph_and_targets_one_crate(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = pathlib.Path(temporary)
            rustc = root / "rustc"
            rustc.write_text(
                "#!/usr/bin/env python3\n"
                "import json, sys\n"
                "print(json.dumps(sys.argv[1:]))\n",
                encoding="utf-8",
            )
            rustc.chmod(0o755)
            dependency_dir = root / "deps"
            dependency_dir.mkdir()
            rlib = dependency_dir / "libunialloc-baseline.rlib"
            rlib.touch()
            wrapper = campaign.ensure_direct_load_wrapper(root / "wrapper")
            env = os.environ.copy()
            env.update(
                {
                    "UNIALLOC_DIRECT_LOAD_RLIB": str(rlib),
                    "UNIALLOC_DIRECT_LOAD_DEPENDENCY_DIR": str(dependency_dir),
                    "UNIALLOC_DIRECT_LOAD_TARGET_CRATE": "selected",
                }
            )
            unselected = subprocess.run(
                [wrapper, rustc, "--crate-name", "dependency"],
                env=env,
                check=True,
                capture_output=True,
                text=True,
            )
            unselected_args = json.loads(unselected.stdout)
            self.assertEqual(
                ["-L", f"dependency={dependency_dir}"], unselected_args[-2:]
            )
            self.assertNotIn("--extern", unselected_args)

            selected = subprocess.run(
                [wrapper, rustc, "--crate-name", "selected"],
                env=env,
                check=True,
                capture_output=True,
                text=True,
            )
            selected_args = json.loads(selected.stdout)
            self.assertEqual(1, selected_args.count("--extern"))
            extern_index = selected_args.index("--extern")
            self.assertEqual(f"force:unialloc={rlib}", selected_args[extern_index + 1])


if __name__ == "__main__":
    unittest.main()
