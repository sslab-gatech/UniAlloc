#!/usr/bin/env python3
"""Regression tests for the primary macro allocator baseline campaign."""

from __future__ import annotations

import json
import os
import pathlib
import tempfile
import unittest
from unittest import mock

from evaluation.scripts import immutable_evidence
from evaluation.scripts import run_primary_macro_allocator_baselines as campaign
from evaluation.scripts import type_isolation_suite_contract


SUITE_PATH = type_isolation_suite_contract.CURRENT_SUITE_PATH
TCMALLOC_IDENTITY = {
    "revision": "12f255231938d30493186b0a037feedd70f5a1c1",
    "library_sha256": "5f99dcf644a7e1e138439fed5d42216c5313ad28e2557f936a250643e0f42081",
    "hpaa_active": 1,
    "malloc_provider_is_self": 1,
    "label": "google-tcmalloc-modern-hpaa",
}


class PrimaryMacroAllocatorBaselineTests(unittest.TestCase):
    def setUp(self) -> None:
        self.contract = campaign.load_campaign_contract(SUITE_PATH)
        self.protocol = campaign.build_protocol(
            self.contract, toolchain="nightly-2026-06-11"
        )

    def test_variant_registry_preserves_runtime_build_aliases(self) -> None:
        self.assertEqual(
            campaign.VARIANT_ORDER,
            (
                "unialloc",
                "mimalloc",
                "mimalloc_no_thp",
                "google_tcmalloc",
                "jemalloc",
            ),
        )
        self.assertEqual(campaign.build_variant_for("mimalloc_no_thp"), "mimalloc")
        self.assertEqual(campaign.build_variant_for("google_tcmalloc"), "system")

    def test_target_execution_contracts_match_primary_runners(self) -> None:
        contracts = campaign.TARGET_EXECUTION_CONTRACTS
        for target in ("collections", "oxipng"):
            self.assertEqual(
                contracts[target],
                {"cpu_list": "0-15", "numa_node": 0, "runtime_overrides": {}},
            )
        for target in ("redb", "actix_web"):
            self.assertEqual(contracts[target]["cpu_list"], "16-31")
            self.assertEqual(contracts[target]["runtime_overrides"], {})
        for target in ("polars", "swc", "rustpython"):
            self.assertEqual(contracts[target]["cpu_list"], "32-63")
            self.assertEqual(contracts[target]["numa_node"], 1)
            self.assertEqual(
                contracts[target]["runtime_overrides"],
                {
                    "POLARS_MAX_THREADS": "1",
                    "RAYON_NUM_THREADS": "1",
                    "TOKIO_WORKER_THREADS": "1",
                },
            )
        with mock.patch.object(
            campaign.matrix,
            "measurement_command_prefix",
            return_value=["numactl", "--physcpubind=16-31", "--membind=0"],
        ) as prefix:
            campaign._runtime_prefix(
                "redb", "unialloc", tcmalloc_runtime={}
            )
            prefix.assert_called_once_with("16-31", 0)

    def test_full_plan_uses_candidate_specific_paired_cohorts(self) -> None:
        plan = campaign.build_plan(
            self.protocol,
            target_ids=tuple(self.contract.targets),
            variant_ids=campaign.VARIANT_ORDER,
            tcmalloc_identity=TCMALLOC_IDENTITY,
        )
        harness_count = sum(len(target.harnesses) for target in self.contract.targets.values())
        self.assertEqual(len(plan.cells), harness_count * 4 * 2 * 4)
        self.assertEqual(len(plan.cohorts), len(self.contract.targets) * 4)
        self.assertTrue(
            any(
                cell.relative_path.startswith("cells/oxipng/cli_issue_141_t1/")
                and cell.relative_path.endswith("/unialloc/measurement-01.json")
                for cell in plan.cells
            )
        )
        self.assertEqual(len({cell.cell_fingerprint for cell in plan.cells}), len(plan.cells))

    def test_adding_variant_adds_candidate_and_fresh_unialloc_anchor(self) -> None:
        one = campaign.build_plan(
            self.protocol, target_ids=("redb",), variant_ids=("mimalloc",)
        )
        two = campaign.build_plan(
            self.protocol,
            target_ids=("redb",),
            variant_ids=("mimalloc", "jemalloc"),
        )
        added = {cell.relative_path for cell in two.cells} - {
            cell.relative_path for cell in one.cells
        }
        self.assertEqual(len(added), 4 * 2 * 4)
        self.assertEqual(
            {cell.identity.variant for cell in two.cells if cell.relative_path in added},
                       {"unialloc", "jemalloc"},
        )

    def test_tcmalloc_identity_change_only_changes_google_cohort(self) -> None:
        changed = {**TCMALLOC_IDENTITY, "library_sha256": "a" * 64}
        variants = ("mimalloc", "google_tcmalloc", "jemalloc")
        first = campaign.build_plan(
            self.protocol,
            target_ids=("redb",),
            variant_ids=variants,
            tcmalloc_identity=TCMALLOC_IDENTITY,
        )
        second = campaign.build_plan(
            self.protocol,
            target_ids=("redb",),
            variant_ids=variants,
            tcmalloc_identity=changed,
        )
        first_ids = {row.subject_variant: row.cohort_id for row in first.cohorts}
        second_ids = {row.subject_variant: row.cohort_id for row in second.cohorts}
        self.assertEqual(first_ids["mimalloc"], second_ids["mimalloc"])
        self.assertEqual(first_ids["jemalloc"], second_ids["jemalloc"])
        self.assertNotEqual(first_ids["google_tcmalloc"], second_ids["google_tcmalloc"])

    def test_base_protocol_and_build_fingerprints_ignore_tcmalloc(self) -> None:
        other = campaign.build_protocol(
            self.contract,
            toolchain="nightly-2026-06-11",
            tcmalloc_identity={"different": True},
        )
        self.assertEqual(self.protocol.fingerprint, other.fingerprint)
        for variant in ("unialloc", "mimalloc", "jemalloc", "system"):
            self.assertEqual(
                campaign.build_contract_fingerprint(
                    self.protocol, target_id="redb", build_variant=variant
                ),
                campaign.build_contract_fingerprint(
                    other, target_id="redb", build_variant=variant
                ),
            )

    def test_campaign_manifest_is_immutable_and_suite_bound(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            binding = type_isolation_suite_contract.bind_suite_manifest(
                self.contract.suite, destination=root / "suite.json"
            )
            path = campaign.ensure_campaign_manifest(
                root, self.protocol, reuse=False, suite_binding=binding
            )
            value = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(value["suite_manifest"]["sha256"], binding["sha256"])
            path.chmod(0o600)
            path.write_text("{}\n", encoding="utf-8")
            with self.assertRaisesRegex(campaign.CampaignError, "protocol mismatch"):
                campaign.ensure_campaign_manifest(
                    root, self.protocol, reuse=True, suite_binding=binding
                )

    def test_reuse_validates_full_identity_metrics_and_artifacts(self) -> None:
        expected = campaign.CellIdentity(
            "cohort", "redb", "bulk_small", "jemalloc", "measurement", 2
        )
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            artifact = root / "stdout"
            artifact.write_bytes(b"ok")
            identity = {
                **expected.as_dict(),
                "protocol_fingerprint": self.protocol.fingerprint,
                "binary_sha256": "1" * 64,
                "selector": "bulk-small",
                "performance_source": "benchmark-owned-operation-seconds",
                "rss_work_model": "fixed_work",
                "runtime_environment_sha256": "2" * 64,
                "command_contract_sha256": "3" * 64,
            }
            extra_identity = {
                key: value
                for key, value in identity.items()
                if key not in expected.as_dict()
            }
            row = {
                "evidence_schema_version": 1,
                "identity": identity,
                "metrics": {
                    "performance": 1.0,
                    "performance_unit": "seconds",
                    "peak_rss_mib": 2.0,
                },
                "artifacts": {"stdout": immutable_evidence.artifact_ref(artifact)},
                "success": True,
                "correctness": {"passed": True},
            }
            path = root / "cell.json"
            path.write_text(json.dumps(row), encoding="utf-8")
            campaign.validate_reusable_cell(
                path, expected=expected, expected_identity=extra_identity
            )
            for key in (
                "selector",
                "performance_source",
                "rss_work_model",
                "runtime_environment_sha256",
                "command_contract_sha256",
                "binary_sha256",
            ):
                mutated = json.loads(json.dumps(row))
                mutated["identity"][key] = "wrong"
                path.write_text(json.dumps(mutated), encoding="utf-8")
                with self.assertRaisesRegex(campaign.CampaignError, "identity mismatch"):
                    campaign.validate_reusable_cell(
                        path, expected=expected, expected_identity=extra_identity
                    )

    def test_cell_reuse_rederives_performance_and_rss_from_artifacts(self) -> None:
        target = self.contract.targets["collections"]
        harness = target.harnesses[0]
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            binary = root / "binary"
            binary.write_bytes(b"binary")
            binary.chmod(0o755)
            stdout = root / "stdout"
            stdout.write_text(
                "test binary_heap::bench_push ... bench: "
                "123,456 ns/iter (+/- 10)\n",
                encoding="utf-8",
            )
            stderr = root / "stderr"
            stderr.write_bytes(b"")
            gnu_time = root / "gnu-time.txt"
            gnu_time.write_text(
                "UNIALLOC_GNU_TIME\t1.25\t0.50\t97%\t6144\t1\t22\t3\t4\t0\n",
                encoding="utf-8",
            )
            command = [
                str(binary),
                "--bench",
                "--exact",
                harness.selector,
                "--test-threads=1",
            ]
            row = {
                "variant": "unialloc",
                "metrics": {
                    "performance": 123456.0,
                    "performance_unit": "ns_per_iter",
                    "peak_rss_mib": 6.0,
                },
                "artifacts": {
                    "binary": immutable_evidence.artifact_ref(binary),
                    "stdout": immutable_evidence.artifact_ref(stdout),
                    "stderr": immutable_evidence.artifact_ref(stderr),
                    "gnu_time": immutable_evidence.artifact_ref(gnu_time),
                },
                "command": command,
                "measured_command": command,
                "runtime_policy": {"command_prefix": []},
                "correctness": {"passed": True},
                "allocator_runtime_identity": campaign._tcmalloc_cell_identity(
                    "unialloc", b""
                ),
                "mimalloc_thp_runtime": campaign.mimalloc_thp_runtime_evidence(
                    "unialloc", b""
                ),
            }
            campaign.validate_cell_metrics_from_artifacts(
                row, target=target, harness=harness
            )
            row["metrics"]["peak_rss_mib"] = 7.0
            with self.assertRaisesRegex(campaign.CampaignError, "peak RSS"):
                campaign.validate_cell_metrics_from_artifacts(
                    row, target=target, harness=harness
                )

    def test_summary_pairs_only_within_same_cohort(self) -> None:
        cells: list[dict[str, object]] = []
        for target_id, harness_id in (
            ("oxipng", "cli_issue_141_t1"),
            ("swc", "large_parser"),
        ):
            for round_number, reference, subject, subject_rss in (
                (1, 10.0, 20.0, 110.0),
                (2, 20.0, 20.0, 120.0),
                (3, 40.0, 20.0, 130.0),
            ):
                cells += [
                    self._cell(
                        target_id,
                        harness_id,
                        "cohort-m",
                        "unialloc",
                        round_number,
                        reference,
                        100.0,
                    ),
                    self._cell(
                        target_id,
                        harness_id,
                        "cohort-m",
                        "mimalloc",
                        round_number,
                        subject,
                        subject_rss,
                    ),
                ]
        summary = campaign.summarize_cells(cells, self.contract)
        oxipng = summary["workloads"]["oxipng/cli_issue_141_t1"]["mimalloc"]
        swc = summary["workloads"]["swc/large_parser"]["mimalloc"]
        self.assertEqual(oxipng["performance_cost_ratio_median"], 1.0)
        self.assertAlmostEqual(oxipng["peak_rss_ratio_median"], 1.2)
        self.assertTrue(oxipng["peak_rss_headline_eligible"])
        self.assertFalse(swc["peak_rss_headline_eligible"])

    def test_allocator_sources_and_current_dependency_pins_are_explicit(self) -> None:
        snapshot = pathlib.Path("/tmp/frozen-allocator")
        self.assertIn("UniAlloc", campaign.allocator_source("unialloc"))
        self.assertIn("MiMalloc", campaign.allocator_source("mimalloc"))
        self.assertIn("PR_GET_THP_DISABLE", campaign.allocator_source("mimalloc"))
        self.assertIn("Jemalloc", campaign.allocator_source("jemalloc"))
        joined = "".join(campaign.dependencies_for("mimalloc", snapshot)).replace(
            " ", ""
        )
        self.assertIn('version="=0.1.52"', joined)
        self.assertIn('libmimalloc-sys="=0.1.49"', joined)
        jemalloc = "".join(
            campaign.dependencies_for("jemalloc", snapshot)
        ).replace(" ", "")
        self.assertIn('version="=0.7.0"', jemalloc)

    def test_timed_environment_is_target_scoped_and_libc_default(self) -> None:
        contamination = {
            "LD_PRELOAD": "/tmp/wrong.so",
            "MALLOC_CONF": "dirty_decay_ms:0",
            "GLIBC_TUNABLES": "glibc.pthread.rseq=0",
            "UNIALLOC_STATS": "1",
        }
        with tempfile.TemporaryDirectory() as directory, mock.patch.dict(
            os.environ, contamination, clear=False
        ):
            redb = campaign._runtime_environment(
                "redb",
                variant="mimalloc",
                raw_dir=pathlib.Path(directory),
                runtime_dependency=None,
            )
            polars = campaign._runtime_environment(
                "polars",
                variant="unialloc",
                raw_dir=pathlib.Path(directory),
                runtime_dependency=None,
            )
            self.assertNotIn("RAYON_NUM_THREADS", redb)
            self.assertEqual(polars["RAYON_NUM_THREADS"], "1")
            self.assertNotIn("GLIBC_TUNABLES", redb)
            self.assertEqual(redb["MIMALLOC_ALLOW_THP"], "1")
            policy = campaign._runtime_policy_record(
                target_id="redb",
                variant="mimalloc",
                prefix=("numactl", "--physcpubind=16-31"),
                environment=redb,
                tcmalloc_runtime={},
            )
        self.assertEqual(policy["rseq_policy"], "libc_default")
        self.assertEqual(policy["target_execution_contract"]["cpu_list"], "16-31")

    def test_runtime_environment_evidence_is_content_addressed_and_immutable(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            environment = type_isolation_suite_contract.clean_runtime_environment(
                root / "tmp", source={}
            )
            first, artifact = campaign.persist_runtime_environment(root, environment)
            second, reused = campaign.persist_runtime_environment(root, environment)
            self.assertEqual(first, second)
            self.assertEqual(artifact, reused)
            pathlib.Path(artifact["path"]).chmod(0o600)
            pathlib.Path(artifact["path"]).write_text("{}\n", encoding="utf-8")
            with self.assertRaises(campaign.CampaignError):
                campaign.persist_runtime_environment(root, environment)

    def test_system_source_absence_proof_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            source = root / "main.rs"
            manifest = root / "Cargo.toml"
            source.write_text("fn main() {}\n", encoding="utf-8")
            manifest.write_text(
                "[package]\nname='x'\nversion='0.1.0'\n", encoding="utf-8"
            )
            self.assertTrue(
                campaign.system_source_absence_proof([source], [manifest])["success"]
            )
            source.write_text(
                "#[global_allocator]\nstatic A: u8 = 0;\n", encoding="utf-8"
            )
            with self.assertRaisesRegex(campaign.CampaignError, "absence proof"):
                campaign.system_source_absence_proof([source], [manifest])

    def test_system_symbol_absence_proof_fails_on_allocator_symbols(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            binary = pathlib.Path(directory) / "binary"
            binary.write_bytes(b"x")
            binary.chmod(0o755)
            clean = {
                "stdout": b"0000 T main\n",
                "stderr": b"",
                "exit_code": 0,
                "timed_out": False,
                "command": [],
                "wall_seconds": 0.0,
            }
            with mock.patch.object(campaign.matrix, "execute", return_value=clean):
                self.assertTrue(
                    campaign.allocator_activation_proof(binary, "system")["success"]
                )
            dirty = {**clean, "stdout": b"0000 T mi_malloc\n"}
            with mock.patch.object(campaign.matrix, "execute", return_value=dirty):
                with self.assertRaisesRegex(campaign.CampaignError, "activation proof"):
                    campaign.allocator_activation_proof(binary, "system")

    def test_reusable_build_rechecks_source_logs_binary_and_activation(self) -> None:
        target = self.contract.targets["redb"]
        fingerprint = campaign.build_contract_fingerprint(
            self.protocol, target_id="redb", build_variant="system"
        )
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            worktree = root / "source"
            worktree.mkdir()
            lock = worktree / "Cargo.lock"
            lock.write_text("version = 4\n", encoding="utf-8")
            source = worktree / "main.rs"
            source.write_text("fn main() {}\n", encoding="utf-8")
            manifest = worktree / "Cargo.toml"
            manifest.write_text(
                "[package]\nname='x'\nversion='0.1.0'\n", encoding="utf-8"
            )
            binary = root / "binary"
            binary.write_bytes(b"binary")
            binary.chmod(0o755)
            stdout = root / "build.stdout"
            stderr = root / "build.stderr"
            stdout.write_bytes(b"ok")
            stderr.write_bytes(b"")
            artifact = {
                "path": str(binary),
                "sha256": campaign.sha256_file(binary),
                "size_bytes": binary.stat().st_size,
            }
            harness_binaries = {
                harness.id: dict(artifact) for harness in target.harnesses
            }
            record = {
                "schema_version": campaign.BUILD_SCHEMA_VERSION,
                "protocol_fingerprint": self.protocol.fingerprint,
                "target_id": "redb",
                "variant": "system",
                "source_ref": target.source_ref,
                "source_commit": target.source_commit,
                "success": True,
                "build_fingerprint": fingerprint,
                "worktree": str(worktree),
                "derived_cargo_lock_sha256": campaign.sha256_file(lock),
                "commands": [
                    {
                        "exit_code": 0,
                        "stdout": str(stdout),
                        "stdout_sha256": campaign.sha256_file(stdout),
                        "stderr": str(stderr),
                        "stderr_sha256": campaign.sha256_file(stderr),
                    }
                ],
                "cargo_target_dir": str(root / "removed-target"),
                "disposable_cargo_target_removed": True,
                "harness_binaries": harness_binaries,
                "activation": {"primary": {"success": True}},
                "system_allocator_absence": campaign.system_source_absence_proof(
                    [source], [manifest]
                ),
            }
            record["build_id"] = campaign.canonical_json_sha256(
                {
                    "build_fingerprint": fingerprint,
                    "target_id": "redb",
                    "variant": "system",
                    "source_commit": target.source_commit,
                    "harness_binaries": harness_binaries,
                }
            )
            path = root / "build.json"
            path.write_text(json.dumps(record), encoding="utf-8")
            with mock.patch.object(
                campaign,
                "allocator_activation_proof",
                return_value={"success": True},
            ):
                campaign.validate_reusable_build(
                    path,
                    target=target,
                    variant="system",
                    protocol_fingerprint=self.protocol.fingerprint,
                    build_fingerprint=fingerprint,
                )
                source.write_text("#[global_allocator]\nstatic A: u8 = 0;\n")
                with self.assertRaisesRegex(campaign.CampaignError, "absence proof"):
                    campaign.validate_reusable_build(
                        path,
                        target=target,
                        variant="system",
                        protocol_fingerprint=self.protocol.fingerprint,
                        build_fingerprint=fingerprint,
                    )

    def test_mimalloc_pr_get_thp_disable_runtime_proof_is_exact(self) -> None:
        self.assertEqual(
            campaign.mimalloc_thp_runtime_evidence(
                "mimalloc", b"UNIALLOC_MIMALLOC_PR_GET_THP_DISABLE=0\n"
            )["pr_get_thp_disable"],
            0,
        )
        self.assertEqual(
            campaign.mimalloc_thp_runtime_evidence(
                "mimalloc_no_thp", b"UNIALLOC_MIMALLOC_PR_GET_THP_DISABLE=1\n"
            )["pr_get_thp_disable"],
            1,
        )
        for stderr in (
            b"",
            b"UNIALLOC_MIMALLOC_PR_GET_THP_DISABLE=error\n",
        ):
            with self.assertRaisesRegex(campaign.CampaignError, "mismatch"):
                campaign.mimalloc_thp_runtime_evidence("mimalloc", stderr)

    def test_tcmalloc_authentication_is_conditional_in_plan_only_mode(self) -> None:
        runtime = {
            "library": "/tmp/libtcmalloc.so",
            "library_sha256": TCMALLOC_IDENTITY["library_sha256"],
            "label": TCMALLOC_IDENTITY["label"],
            "runtime_requirements": {
                "revision": TCMALLOC_IDENTITY["revision"],
                "hpaa_active": 1,
                "malloc_provider_is_self": 1,
            },
        }
        with tempfile.TemporaryDirectory() as directory, mock.patch.object(
            campaign, "authenticate_tcmalloc"
        ) as authenticate:
            self.assertEqual(
                campaign.main(
                    [
                        "--plan-only",
                        "--raw-dir",
                        directory,
                        "--targets",
                        "redb",
                        "--variants",
                        "mimalloc",
                    ]
                ),
                0,
            )
            authenticate.assert_not_called()
        with tempfile.TemporaryDirectory() as directory, mock.patch.object(
            campaign, "authenticate_tcmalloc", return_value=runtime
        ) as authenticate:
            self.assertEqual(
                campaign.main(
                    [
                        "--plan-only",
                        "--raw-dir",
                        directory,
                        "--targets",
                        "redb",
                        "--variants",
                        "google_tcmalloc",
                    ]
                ),
                0,
            )
            authenticate.assert_called_once()

    def test_measurement_schedule_is_bounded_and_persists_lock_quiescence(self) -> None:
        plan = campaign.build_plan(
            self.protocol, target_ids=("redb",), variant_ids=("mimalloc",)
        )
        dummy_build = {"unialloc": {}, "mimalloc": {}}
        source = campaign.SourceContext("redb", pathlib.Path("/tmp"), {})
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            suite = root / "suite.json"
            suite.write_bytes(b"suite")
            suite_binding = immutable_evidence.artifact_ref(suite)
            with mock.patch.object(
                campaign.psr,
                "wait_for_build_quiescence",
                return_value={"success": True, "target_id": "redb"},
            ), mock.patch.object(
                campaign,
                "execute_planned_cell",
                side_effect=lambda cell, **kwargs: {
                    **cell.identity.as_dict(),
                    "success": True,
                },
            ) as execute:
                rows = campaign.run_measurement_plan(
                    plan=plan,
                    protocol=self.protocol,
                    contract=self.contract,
                    builds={"redb": dummy_build},
                    sources={"redb": source},
                    raw_dir=root,
                    timeout=1,
                    reuse=False,
                    tcmalloc_runtime={},
                    runtime_dependency=None,
                    polars_csv=None,
                    suite_binding=suite_binding,
                    quiet_seconds=0,
                )
            self.assertEqual(len(rows), len(plan.cells))
            self.assertEqual(execute.call_count, len(plan.cells))
            self.assertTrue(list((root / "locks/redb").glob("*.json")))
            self.assertTrue(list((root / "quiescence/redb").glob("*.json")))

    def _cell(
        self,
        target: str,
        harness: str,
        cohort: str,
        variant: str,
        round_number: int,
        performance: float,
        rss: float,
    ) -> dict[str, object]:
        return {
            "schema_version": campaign.CELL_SCHEMA_VERSION,
            "target_id": target,
            "harness_id": harness,
            "cohort_id": cohort,
            "variant": variant,
            "phase": "measurement",
            "round": round_number,
            "success": True,
            "correctness": {"passed": True},
            "performance": performance,
            "peak_rss_mib": rss,
        }


if __name__ == "__main__":
    unittest.main()
