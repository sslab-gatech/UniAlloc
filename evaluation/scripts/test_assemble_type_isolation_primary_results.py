#!/usr/bin/env python3

from __future__ import annotations

import hashlib
import json
import math
import shutil
import statistics
import subprocess
import tempfile
import unittest
from pathlib import Path

from evaluation.scripts import assemble_type_isolation_primary_results as assembler
from evaluation.scripts import immutable_evidence


ROOT = Path(__file__).resolve().parents[2]
SUITE_PATH = ROOT / "evaluation/config/type_isolation_primary_suite.json"
CURRENT_SUITE_PATH = (
    ROOT / "evaluation/config/type_isolation_primary_suite_v3_ce8af7b.json"
)
SCRIPT_PATH = ROOT / "evaluation/scripts/assemble_type_isolation_primary_results.py"
PLOT_SCRIPT_PATH = ROOT / "evaluation/scripts/plot_type_isolation_primary_suite.py"
GATES = (
    "correctness",
    "build_success",
    "allocator_activation",
    "actual_mir_provenance",
    "stats_disabled",
    "compiler_route_equivalent",
    "source_audit_retained",
)
VARIANTS = ("unialloc", "typed_plain", "typeiso_perf")
TOTAL_HARNESSES = 34
IMPLEMENTATION_REVISION = "f5d0c19c1cc5b56fdac3282d69333dd8c85d4cf2"
IMPLEMENTATION_SHA256 = (
    "cab1e580c08e2b16308bae75501716049ba428b040fb04bf305269c9ba9eaf01"
)
FIXED_WORK_TARGETS = {"oxipng", "redb", "polars"}
FIXED_WORK_HARNESS_COUNT = 14


def bind_test_suite(suite_path: Path, evidence_root: Path) -> dict[str, object]:
    payload = suite_path.read_bytes()
    digest = hashlib.sha256(payload).hexdigest()
    path = evidence_root.parent / f"suite-manifest-{digest}.json"
    path.write_bytes(payload)
    return {
        "path": str(path.resolve()),
        "sha256": digest,
        "bytes": len(payload),
    }


def artifact(path: Path, payload: bytes) -> dict[str, object]:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(payload)
    return immutable_evidence.artifact_ref(path)


def raw_measurement_record(
    *,
    path: Path,
    suite: dict[str, object],
    suite_binding: dict[str, object],
    target: dict[str, object],
    harness: dict[str, object],
    variant: str,
    phase: str,
    round_number: int,
    performance: float,
    peak_rss_mib: float,
    implementation_revision: str,
    implementation_sha256: str,
    binary_path: Path,
) -> tuple[str, str]:
    artifact_root = path.parent / f"{path.stem}-artifacts"
    identity = {
        "suite_id": suite["suite_id"],
        "suite_manifest_sha256": suite_binding["sha256"],
        "target_id": target["id"],
        "harness_id": harness["id"],
        "variant": variant,
        "phase": phase,
        "round": round_number,
        "source_commit": target["source"]["commit"],
        "implementation_revision": implementation_revision,
        "implementation_sha256": implementation_sha256,
        "binary_sha256": hashlib.sha256(binary_path.read_bytes()).hexdigest(),
    }
    metrics = {
        "performance": performance,
        "performance_unit": harness["performance_unit"],
        "peak_rss_mib": peak_rss_mib,
    }
    record = {
        "evidence_schema_version": 1,
        "identity": identity,
        "metrics": metrics,
        "artifacts": {
            "binary": immutable_evidence.artifact_ref(binary_path),
            "stdout": artifact(artifact_root / "stdout", b"ok\n"),
            "stderr": artifact(artifact_root / "stderr", b""),
            "gnu_time": artifact(artifact_root / "gnu-time.txt", b"1.0\n"),
        },
        "suite_manifest": suite_binding,
        "correctness": {"oracle": "synthetic fixture"},
        **identity,
        **metrics,
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(immutable_evidence.canonical_json_bytes(record))
    return str(path.resolve()), hashlib.sha256(path.read_bytes()).hexdigest()


def synchronize_summary_metric(row: dict[str, object]) -> None:
    path = Path(str(row["record_path"]))
    record = json.loads(path.read_text(encoding="utf-8"))
    for field in ("performance", "peak_rss_mib"):
        record[field] = row[field]
        record["metrics"][field] = row[field]
    path.write_bytes(immutable_evidence.canonical_json_bytes(record))
    row["record_sha256"] = hashlib.sha256(path.read_bytes()).hexdigest()


def target_result(
    target: dict[str, object],
    evidence_root: Path,
    *,
    measured_rounds: int = 5,
    implementation_revision: str = IMPLEMENTATION_REVISION,
    implementation_sha256: str = IMPLEMENTATION_SHA256,
    suite_path: Path = SUITE_PATH,
) -> dict[str, object]:
    suite = json.loads(suite_path.read_text(encoding="utf-8"))
    suite_binding = bind_test_suite(suite_path, evidence_root)
    build_records: dict[str, str] = {}
    build_binary_sha256: dict[str, str] = {}
    binary_paths: dict[str, Path] = {}
    for variant in VARIANTS:
        path = (
            evidence_root.parent / "builds" / str(target["id"]) / variant / "build.json"
        )
        path.parent.mkdir(parents=True, exist_ok=True)
        binary_path = path.parent / "benchmark-binary"
        binary_path.write_bytes(f"{target['id']}:{variant}".encode())
        binary_sha256 = hashlib.sha256(binary_path.read_bytes()).hexdigest()
        path.write_text(
            json.dumps(
                {
                    "success": True,
                    "allocator_activation": {"success": True},
                    "actual_mir_provenance": True,
                    "stats_feature_enabled": False,
                    "source_commit": target["source"]["commit"],
                    "implementation_revision": implementation_revision,
                    "unialloc_implementation_sha256": implementation_sha256,
                    "binary_sha256": binary_sha256,
                },
                sort_keys=True,
            )
            + "\n",
            encoding="utf-8",
        )
        build_records[variant] = str(path.resolve())
        build_binary_sha256[variant] = binary_sha256
        binary_paths[variant] = binary_path

    harnesses: list[dict[str, object]] = []
    for harness_index, harness in enumerate(target["harnesses"]):
        measurements: list[dict[str, object]] = []
        for round_number in range(1, measured_rounds + 1):
            for variant_index, variant in enumerate(VARIANTS):
                performance = (
                    1.0 + 0.01 * variant_index + 0.001 * harness_index
                )
                peak_rss_mib = 100.0 + variant_index + 0.1 * round_number
                record_path, record_sha256 = raw_measurement_record(
                    path=(
                        evidence_root
                        / str(target["id"])
                        / str(harness["id"])
                        / "measurements"
                        / f"round-{round_number}-{variant}.json"
                    ),
                    suite=suite,
                    suite_binding=suite_binding,
                    target=target,
                    harness=harness,
                    variant=variant,
                    phase="measurement",
                    round_number=round_number,
                    performance=performance,
                    peak_rss_mib=peak_rss_mib,
                    implementation_revision=implementation_revision,
                    implementation_sha256=implementation_sha256,
                    binary_path=binary_paths[variant],
                )
                measurements.append(
                    {
                        "round": round_number,
                        "variant": variant,
                        "performance": performance,
                        "peak_rss_mib": peak_rss_mib,
                        "record_path": record_path,
                        "record_sha256": record_sha256,
                    }
                )
        warmups: dict[str, list[dict[str, str]]] = {}
        for variant in VARIANTS:
            path = (
                evidence_root
                / str(target["id"])
                / str(harness["id"])
                / f"{variant}.json"
            )
            path.parent.mkdir(parents=True, exist_ok=True)
            record_path, record_sha256 = raw_measurement_record(
                path=path,
                suite=suite,
                suite_binding=suite_binding,
                target=target,
                harness=harness,
                variant=variant,
                phase="warmup",
                round_number=0,
                performance=1.0,
                peak_rss_mib=2.0,
                implementation_revision=implementation_revision,
                implementation_sha256=implementation_sha256,
                binary_path=binary_paths[variant],
            )
            warmups[variant] = [
                {
                    "record_path": record_path,
                    "record_sha256": record_sha256,
                    "sha256": record_sha256,
                }
            ]
        harnesses.append(
            {
                "id": harness["id"],
                "metric_direction": harness["metric_direction"],
                "gates": {gate: True for gate in GATES},
                "warmup_evidence": warmups,
                "measurements": measurements,
                "evidence": {"raw_path": f"raw/{harness['id']}.json"},
            }
        )
    source_audit = evidence_root.parent / "audits" / f"{target['id']}.json"
    source_audit.parent.mkdir(parents=True, exist_ok=True)
    source_audit.write_text('{"success":true}\n', encoding="utf-8")
    result = {
        "schema_version": 1,
        "target_id": target["id"],
        "source_commit": target["source"]["commit"],
        "implementation_revision": implementation_revision,
        "implementation_sha256": implementation_sha256,
        "suite_id": suite["suite_id"],
        "suite_manifest_sha256": suite_binding["sha256"],
        "suite_manifest_path": suite_binding["path"],
        "measured_rounds": measured_rounds,
        "build_records": build_records,
        "harnesses": harnesses,
        "evidence": {"source_audit": str(source_audit.resolve())},
    }
    if target["id"] == "rustpython":
        soname = "libpython3.13.so.1.0"
        library_content = b"test-libpython-runtime"
        library_sha256 = hashlib.sha256(library_content).hexdigest()
        runtime_variants: dict[str, dict[str, object]] = {}
        for variant in VARIANTS:
            ldd_path = evidence_root.parent / "ldd" / f"{variant}.ldd"
            ldd_path.parent.mkdir(parents=True, exist_ok=True)
            ldd_path.write_text(
                f"{soname} => /host/python/lib/{soname} (0x00000000)\n",
                encoding="utf-8",
            )
            runtime_variants[variant] = {
                "success": True,
                "binary_sha256": build_binary_sha256[variant],
                "resolved_library_soname": soname,
                "resolved_library_sha256": library_sha256,
                "ldd_record": str(ldd_path.resolve()),
                "ldd_record_sha256": hashlib.sha256(ldd_path.read_bytes()).hexdigest(),
            }
        result["inputs"] = {
            "rustpython_dynamic_runtime": {
                "python_executable": "/host/python/bin/python3.13",
                "library_directory": "/host/python/lib",
                "library_path": f"/host/python/lib/{soname}",
                "library_soname": soname,
                "library_bytes": len(library_content),
                "library_sha256": library_sha256,
                "variants": runtime_variants,
            }
        }
    return result


class AssembleTypeIsolationPrimaryResultsTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.uv = shutil.which("uv")
        if cls.uv is None:
            raise unittest.SkipTest("uv is required")
        cls.suite = json.loads(SUITE_PATH.read_text(encoding="utf-8"))

    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary_directory.name)
        self.targets_dir = self.root / "targets"
        self.targets_dir.mkdir()
        self.output = self.root / "results.json"
        for target in self.suite["targets"]:
            (self.targets_dir / f"{target['id']}.json").write_text(
                json.dumps(target_result(target, self.root / "warmups"), indent=2)
                + "\n",
                encoding="utf-8",
            )

    def tearDown(self) -> None:
        self.temporary_directory.cleanup()

    def run_assembler(
        self, *, check: bool = True, suite_path: Path = SUITE_PATH
    ) -> subprocess.CompletedProcess[str]:
        suite_digest = hashlib.sha256(suite_path.read_bytes()).hexdigest()
        bound_suite = self.root / f"suite-manifest-{suite_digest}.json"
        bound_suite.write_bytes(suite_path.read_bytes())
        return subprocess.run(
            [
                self.uv,
                "run",
                str(SCRIPT_PATH),
                "--suite",
                str(suite_path),
                "--targets-dir",
                str(self.targets_dir),
                "--output",
                str(self.output),
                "--bound-suite-manifest",
                str(bound_suite),
            ],
            cwd=ROOT,
            check=check,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )

    def rewrite_target(self, target_id: str, value: dict[str, object]) -> None:
        (self.targets_dir / f"{target_id}.json").write_text(
            json.dumps(value, indent=2) + "\n", encoding="utf-8"
        )

    def target_result(
        self, target: dict[str, object], *, suite_path: Path = SUITE_PATH
    ) -> dict[str, object]:
        return target_result(
            target, self.root / "warmups", suite_path=suite_path
        )

    def test_complete_target_results_assemble_into_renderer_schema(self) -> None:
        completed = self.run_assembler()
        self.assertIn(str(self.output), completed.stdout)
        result = json.loads(self.output.read_text(encoding="utf-8"))
        self.assertEqual("complete", result["status"])
        self.assertEqual(IMPLEMENTATION_REVISION, result["implementation_revision"])
        self.assertEqual(IMPLEMENTATION_SHA256, result["implementation_sha256"])
        self.assertEqual(
            self.suite["analysis_amendments"], result["analysis_amendments"]
        )
        self.assertEqual(
            "96fa64009550d52f89549dd2124e3b1a402a42b7f1fd1750888600dd67bb8a6f",
            result["original_campaign_manifest_sha256"],
        )
        self.assertEqual(
            [target["id"] for target in self.suite["targets"]],
            [target["id"] for target in result["targets"]],
        )
        self.assertEqual(
            TOTAL_HARNESSES,
            sum(len(target["harnesses"]) for target in result["targets"]),
        )
        self.assertEqual(
            {
                "accepted_interval": [0.85, 1.15],
                "classification": "all_routes_equivalent",
                "fail_count": 0,
                "metric": "paired_execution_cost_ratio",
                "pass_count": TOTAL_HARNESSES,
                "total_count": TOTAL_HARNESSES,
            },
            result["compiler_route_attribution"],
        )
        self.assertEqual(
            IMPLEMENTATION_REVISION,
            result["targets"][0]["implementation_revision"],
        )
        self.assertEqual(
            IMPLEMENTATION_SHA256,
            result["targets"][0]["implementation_sha256"],
        )
        self.assertEqual(
            {"compiler_route", "policy_increment", "end_to_end"},
            set(result["comparison_families"]),
        )
        self.assertEqual(
            set(VARIANTS),
            set(result["targets"][0]["harnesses"][0]["warmup_attestations"]),
        )
        for target in result["targets"]:
            builds = target["compact_provenance"]["builds"]
            self.assertEqual(set(VARIANTS), set(builds))
            for build in builds.values():
                self.assertTrue(build["success"])
                self.assertTrue(build["allocator_activation"])
                self.assertTrue(build["actual_mir_provenance"])
                self.assertTrue(build["stats_disabled"])
                self.assertRegex(build["record_sha256"], r"^[0-9a-f]{64}$")
                self.assertRegex(build["binary_sha256"]["primary"], r"^[0-9a-f]{64}$")
            expected_model = self.suite["targets"][
                [row["id"] for row in self.suite["targets"]].index(target["id"])
            ]["rss_work_model"]
            self.assertEqual(expected_model, target["rss_work_model"])
            expected_eligibility = (
                "core" if expected_model == "fixed_work" else "diagnostic_only"
            )
            self.assertEqual(expected_eligibility, target["rss_comparison_eligibility"])
            self.assertTrue(
                all(
                    harness["rss_work_model"] == expected_model
                    and harness["rss_comparison_eligibility"] == expected_eligibility
                    for harness in target["harnesses"]
                )
            )
            suite_target = next(
                row for row in self.suite["targets"] if row["id"] == target["id"]
            )
            suite_harnesses = {
                harness["id"]: harness for harness in suite_target["harnesses"]
            }
            for harness in target["harnesses"]:
                contract = suite_harnesses[harness["id"]]
                self.assertEqual(
                    contract["performance_unit"], harness["performance_unit"]
                )
                self.assertEqual(
                    contract["performance_source"], harness["performance_source"]
                )
        serialized = self.output.read_text(encoding="utf-8")
        self.assertNotIn("/home/hanqing", serialized)

    def test_current_suite_derives_exact_three_round_contract(self) -> None:
        suite = json.loads(CURRENT_SUITE_PATH.read_text(encoding="utf-8"))
        implementation = suite["implementation"]
        for target in suite["targets"]:
            self.rewrite_target(
                str(target["id"]),
                target_result(
                    target,
                    self.root / "current-warmups",
                    measured_rounds=3,
                    implementation_revision=implementation["git_revision"],
                    implementation_sha256=implementation["canonical_sha256"],
                    suite_path=CURRENT_SUITE_PATH,
                ),
            )
        completed = self.run_assembler(suite_path=CURRENT_SUITE_PATH)
        self.assertEqual(0, completed.returncode)
        result = json.loads(self.output.read_text(encoding="utf-8"))
        self.assertEqual(suite["suite_id"], result["suite_id"])
        self.assertTrue(
            all(
                {row["round"] for row in harness["measurements"]} == {1, 2, 3}
                for target in result["targets"]
                for harness in target["harnesses"]
            )
        )

        broken = json.loads(
            (self.targets_dir / "collections.json").read_text(encoding="utf-8")
        )
        broken["harnesses"][0]["measurements"].extend(
            {
                **row,
                "round": 4,
            }
            for row in broken["harnesses"][0]["measurements"][:3]
        )
        self.rewrite_target("collections", broken)
        rejected = self.run_assembler(check=False, suite_path=CURRENT_SUITE_PATH)
        self.assertEqual(2, rejected.returncode)
        self.assertIn("exact measured rounds 1 through 3", rejected.stderr)

    def test_rustpython_runtime_dependency_is_compact_and_host_path_free(self) -> None:
        self.run_assembler()
        result = json.loads(self.output.read_text(encoding="utf-8"))
        rustpython = next(
            target for target in result["targets"] if target["id"] == "rustpython"
        )
        provenance = rustpython["compact_provenance"]
        runtime = provenance["rustpython_dynamic_runtime"]
        self.assertEqual(
            {
                "library_soname",
                "library_bytes",
                "library_sha256",
                "variants",
            },
            set(runtime),
        )
        self.assertEqual("libpython3.13.so.1.0", runtime["library_soname"])
        self.assertGreater(runtime["library_bytes"], 0)
        self.assertRegex(runtime["library_sha256"], r"^[0-9a-f]{64}$")
        self.assertEqual(set(VARIANTS), set(runtime["variants"]))
        for variant in VARIANTS:
            proof = runtime["variants"][variant]
            self.assertEqual(
                {
                    "success",
                    "binary_sha256",
                    "resolved_library_soname",
                    "resolved_library_sha256",
                    "ldd_record_sha256",
                },
                set(proof),
            )
            self.assertTrue(proof["success"])
            self.assertEqual(
                provenance["builds"][variant]["binary_sha256"]["primary"],
                proof["binary_sha256"],
            )
            self.assertEqual(
                runtime["library_soname"], proof["resolved_library_soname"]
            )
            self.assertEqual(
                runtime["library_sha256"], proof["resolved_library_sha256"]
            )
            self.assertRegex(proof["ldd_record_sha256"], r"^[0-9a-f]{64}$")
        serialized = json.dumps(runtime, sort_keys=True)
        self.assertNotIn("/host/", serialized)
        self.assertNotIn(str(self.root), serialized)
        self.assertNotIn("python_executable", serialized)
        self.assertNotIn("library_path", serialized)
        self.assertNotIn('ldd_record"', serialized)

    def test_comparison_families_materialize_harness_target_and_suite_ratios(
        self,
    ) -> None:
        self.run_assembler()
        result = json.loads(self.output.read_text(encoding="utf-8"))
        family_contracts = {
            family["id"]: family for family in self.suite["comparison_families"]
        }

        for target in result["targets"]:
            for family_id, contract in family_contracts.items():
                harness_execution: list[float] = []
                harness_rss: list[float] = []
                for harness in target["harnesses"]:
                    rows = {
                        (row["round"], row["variant"]): row
                        for row in harness["measurements"]
                    }
                    rounds = sorted({round_number for round_number, _ in rows})
                    subject = contract["subject"]
                    reference = contract["reference"]
                    if harness["metric_direction"] == "lower_is_better":
                        execution = [
                            rows[(round_number, subject)]["performance"]
                            / rows[(round_number, reference)]["performance"]
                            for round_number in rounds
                        ]
                    else:
                        execution = [
                            rows[(round_number, reference)]["performance"]
                            / rows[(round_number, subject)]["performance"]
                            for round_number in rounds
                        ]
                    rss = [
                        rows[(round_number, subject)]["peak_rss_mib"]
                        / rows[(round_number, reference)]["peak_rss_mib"]
                        for round_number in rounds
                    ]
                    expected_execution = float(statistics.median(execution))
                    expected_rss = float(statistics.median(rss))
                    observed = harness["comparison_families"][family_id]
                    self.assertTrue(
                        math.isclose(
                            expected_execution,
                            observed["execution_cost_ratio_median"],
                            rel_tol=1e-12,
                        )
                    )
                    self.assertTrue(
                        math.isclose(
                            expected_rss,
                            observed["peak_rss_ratio_median"],
                            rel_tol=1e-12,
                        )
                    )
                    harness_execution.append(expected_execution)
                    harness_rss.append(expected_rss)
                target_aggregate = target["comparison_families"][family_id]
                expected_target_execution = math.exp(
                    math.fsum(math.log(value) for value in harness_execution)
                    / len(harness_execution)
                )
                expected_target_rss = math.exp(
                    math.fsum(math.log(value) for value in harness_rss)
                    / len(harness_rss)
                )
                self.assertTrue(
                    math.isclose(
                        expected_target_execution,
                        target_aggregate["execution_cost_ratio_geometric_mean"],
                        rel_tol=1e-12,
                    )
                )
                self.assertTrue(
                    math.isclose(
                        expected_target_rss,
                        target_aggregate[
                            "peak_rss_process_observed_ratio_geometric_mean"
                        ],
                        rel_tol=1e-12,
                    )
                )
                self.assertEqual(
                    len(harness_execution),
                    target_aggregate["route_equivalent_harness_count"],
                )
                self.assertTrue(
                    math.isclose(
                        expected_target_execution,
                        target_aggregate[
                            "route_equivalent_execution_cost_ratio_geometric_mean"
                        ],
                        rel_tol=1e-12,
                    )
                )
                if target["id"] in FIXED_WORK_TARGETS:
                    self.assertEqual(
                        len(harness_rss),
                        target_aggregate["fixed_work_harness_count"],
                    )
                    self.assertTrue(
                        math.isclose(
                            expected_target_rss,
                            target_aggregate[
                                "fixed_work_peak_rss_ratio_geometric_mean"
                            ],
                            rel_tol=1e-12,
                        )
                    )
                else:
                    self.assertEqual(0, target_aggregate["fixed_work_harness_count"])
                    self.assertIsNone(
                        target_aggregate["fixed_work_peak_rss_ratio_geometric_mean"]
                    )

        for family_id in family_contracts:
            target_values = [
                target["comparison_families"][family_id] for target in result["targets"]
            ]
            observed = result["comparison_families"][family_id]
            for source_field, result_field in (
                (
                    "execution_cost_ratio_geometric_mean",
                    "execution_cost_ratio_geometric_mean",
                ),
                (
                    "peak_rss_process_observed_ratio_geometric_mean",
                    "peak_rss_process_observed_ratio_geometric_mean",
                ),
                (
                    "route_equivalent_execution_cost_ratio_geometric_mean",
                    "route_equivalent_execution_cost_ratio_geometric_mean",
                ),
            ):
                expected = math.exp(
                    math.fsum(math.log(value[source_field]) for value in target_values)
                    / len(target_values)
                )
                self.assertTrue(
                    math.isclose(expected, observed[result_field], rel_tol=1e-12)
                )
            fixed_values = [
                value["fixed_work_peak_rss_ratio_geometric_mean"]
                for target, value in zip(result["targets"], target_values, strict=True)
                if target["id"] in FIXED_WORK_TARGETS
            ]
            expected_fixed = math.exp(
                math.fsum(math.log(value) for value in fixed_values) / len(fixed_values)
            )
            self.assertTrue(
                math.isclose(
                    expected_fixed,
                    observed["fixed_work_peak_rss_ratio_geometric_mean"],
                    rel_tol=1e-12,
                )
            )
            self.assertEqual(3, observed["fixed_work_target_count"])
            self.assertEqual(
                FIXED_WORK_HARNESS_COUNT, observed["fixed_work_harness_count"]
            )
            self.assertEqual(7, observed["route_equivalent_target_count"])
            self.assertEqual(
                TOTAL_HARNESSES, observed["route_equivalent_harness_count"]
            )

    def test_execution_cost_ratios_respect_higher_is_better_metrics(self) -> None:
        suite = json.loads(json.dumps(self.suite))
        suite["schema_version"] = 2
        suite["measurement_contract"] = {
            "warmup_rounds": 1,
            "measured_rounds": 5,
        }
        suite["publication"] = {
            "namespace": "test-higher-is-better",
            "result_namespace": "test-higher-is-better",
        }
        suite["targets"][0]["harnesses"][0]["metric_direction"] = "higher_is_better"
        suite_path = self.root / "higher-is-better-suite.json"
        suite_path.write_text(json.dumps(suite, indent=2) + "\n", encoding="utf-8")
        for suite_target in suite["targets"]:
            self.rewrite_target(
                str(suite_target["id"]),
                self.target_result(suite_target, suite_path=suite_path),
            )
        target = self.target_result(suite["targets"][0], suite_path=suite_path)
        harness = target["harnesses"][0]
        harness["metric_direction"] = "higher_is_better"
        for row in harness["measurements"]:
            row["performance"] = {
                "unialloc": 100.0,
                "typed_plain": 50.0,
                "typeiso_perf": 25.0,
            }[row["variant"]]
            synchronize_summary_metric(row)
        harness["gates"]["compiler_route_equivalent"] = False
        self.rewrite_target("collections", target)

        self.run_assembler(suite_path=suite_path)

        result = json.loads(self.output.read_text(encoding="utf-8"))
        comparisons = result["targets"][0]["harnesses"][0]["comparison_families"]
        self.assertEqual(
            2.0, comparisons["compiler_route"]["execution_cost_ratio_median"]
        )
        self.assertEqual(
            2.0, comparisons["policy_increment"]["execution_cost_ratio_median"]
        )
        self.assertEqual(4.0, comparisons["end_to_end"]["execution_cost_ratio_median"])

    def test_invalid_rss_work_model_fails_closed(self) -> None:
        suite = json.loads(json.dumps(self.suite))
        suite["schema_version"] = 2
        suite["measurement_contract"] = {
            "warmup_rounds": 1,
            "measured_rounds": 5,
        }
        suite["publication"] = {
            "namespace": "test-invalid-rss",
            "result_namespace": "test-invalid-rss",
        }
        suite["targets"][0]["rss_work_model"] = "ambiguous"
        suite_path = self.root / "invalid-rss-work-model-suite.json"
        suite_path.write_text(json.dumps(suite, indent=2) + "\n", encoding="utf-8")
        completed = self.run_assembler(check=False, suite_path=suite_path)
        self.assertEqual(2, completed.returncode)
        self.assertIn("rss_work_model", completed.stderr)
        self.assertFalse(self.output.exists())

    def test_compact_build_provenance_normalizes_campaign_shapes(self) -> None:
        base = {
            "source_commit": "1" * 40,
            "unialloc_implementation_sha256": IMPLEMENTATION_SHA256,
            "audit": {
                "audit_sha256": "a" * 64,
                "audit_file_count": 2,
                "total_compiler_rewrites_applied": 11,
            },
        }
        fixtures = {
            "collections": {
                **base,
                "success": True,
                "allocator_activation": {"success": True},
                "actual_mir_provenance": True,
                "stats_feature_enabled": False,
                "binaries": {"bench": {"sha256": "b" * 64}},
            },
            "redb_actix": {
                **base,
                "exit_code": 0,
                "timed_out": False,
                "activation": {"passed": True},
                "actual_mir_rewrite": True,
                "stats_enabled": False,
                "binary_sha256": "c" * 64,
            },
            "prs": {
                **base,
                "success": True,
                "activation": {"success": True},
                "actual_mir_provenance": True,
                "stats_enabled": False,
                "binary_sha256": "d" * 64,
            },
        }
        for name, fixture in fixtures.items():
            with self.subTest(name=name):
                path = self.root / f"{name}-build.json"
                path.write_text(json.dumps(fixture), encoding="utf-8")
                compact = assembler.compact_build_record(str(path))
                self.assertIsNotNone(compact)
                self.assertTrue(compact["success"])
                self.assertTrue(compact["allocator_activation"])
                self.assertTrue(compact["actual_mir_provenance"])
                self.assertTrue(compact["stats_disabled"])
                self.assertEqual(
                    IMPLEMENTATION_SHA256, compact["implementation_sha256"]
                )
                self.assertEqual("1" * 40, compact["source_commit"])
                self.assertEqual("a" * 64, compact["audit"]["audit_sha256"])
                self.assertEqual(
                    11, compact["audit"]["total_compiler_rewrites_applied"]
                )
                self.assertTrue(compact["binary_sha256"])

    def test_compact_build_provenance_normalizes_actix_multi_build(self) -> None:
        fixture = {
            "commands": [
                {"exit_code": 0, "timed_out": False},
                {"exit_code": 0, "timed_out": False},
            ],
            "activation": {
                "service": {"passed": True, "binary_sha256": "e" * 64},
                "router": {"success": True, "binary_sha256": "f" * 64},
            },
            "actual_mir_rewrite": True,
            "stats_enabled": False,
            "source_commit": "1" * 40,
            "unialloc_implementation_sha256": IMPLEMENTATION_SHA256,
            "audit": {
                "audit_sha256": "a" * 64,
                "audit_file_count": 2,
                "total_compiler_rewrites_applied": 11,
            },
        }

        def compact(record: dict[str, object]) -> dict[str, object]:
            path = self.root / "actix-build.json"
            path.write_text(json.dumps(record), encoding="utf-8")
            result = assembler.compact_build_record(str(path))
            self.assertIsNotNone(result)
            return result

        result = compact(fixture)
        self.assertTrue(result["success"])
        self.assertTrue(result["allocator_activation"])
        self.assertEqual(
            {"service": "e" * 64, "router": "f" * 64},
            result["binary_sha256"],
        )

        failed_commands = (
            [],
            [{"exit_code": 1, "timed_out": False}],
            [{"exit_code": 0, "timed_out": True}],
        )
        for commands in failed_commands:
            with self.subTest(commands=commands):
                candidate = {**fixture, "commands": commands}
                self.assertFalse(compact(candidate)["success"])

        empty_activation = compact({**fixture, "activation": {}})
        self.assertFalse(empty_activation["allocator_activation"])
        self.assertNotIn("binary_sha256", empty_activation)

        failed_activation_fixture = json.loads(json.dumps(fixture))
        failed_activation_fixture["activation"]["service"]["passed"] = False
        failed_activation = compact(failed_activation_fixture)
        self.assertFalse(failed_activation["allocator_activation"])
        self.assertEqual(
            {"service": "e" * 64, "router": "f" * 64},
            failed_activation["binary_sha256"],
        )

    def test_route_failure_is_retained_as_an_attribution_limit(self) -> None:
        target = self.target_result(self.suite["targets"][0])
        harness = target["harnesses"][0]
        harness["gates"]["compiler_route_equivalent"] = False
        for row in harness["measurements"]:
            if row["variant"] == "typed_plain":
                row["performance"] = 53.9
            elif row["variant"] == "typeiso_perf":
                row["performance"] = 53.1
            synchronize_summary_metric(row)
        self.rewrite_target("collections", target)

        self.run_assembler()

        result = json.loads(self.output.read_text(encoding="utf-8"))
        self.assertEqual("complete_with_attribution_limits", result["status"])
        self.assertEqual(
            TOTAL_HARNESSES,
            sum(len(row["harnesses"]) for row in result["targets"]),
        )
        attribution = result["compiler_route_attribution"]
        self.assertEqual(TOTAL_HARNESSES - 1, attribution["pass_count"])
        self.assertEqual(1, attribution["fail_count"])
        self.assertEqual(TOTAL_HARNESSES, attribution["total_count"])
        self.assertEqual("attribution_limits_present", attribution["classification"])
        retained = result["targets"][0]["harnesses"][0]
        self.assertTrue(retained["gates"]["build_success"])
        self.assertFalse(retained["gates"]["compiler_route_equivalent"])
        self.assertEqual(
            "outside_predeclared_interval",
            retained["compiler_route_attribution"]["classification"],
        )
        self.assertGreater(
            retained["compiler_route_attribution"]["median_cost_ratio"], 1.15
        )
        target_aggregate = result["targets"][0]["comparison_families"]
        for family_id in ("compiler_route", "end_to_end"):
            aggregate = target_aggregate[family_id]
            self.assertEqual(4, aggregate["route_equivalent_harness_count"])
            self.assertGreater(
                aggregate["execution_cost_ratio_geometric_mean"],
                aggregate["route_equivalent_execution_cost_ratio_geometric_mean"],
            )
        for aggregate in result["comparison_families"].values():
            self.assertEqual(
                TOTAL_HARNESSES - 1,
                aggregate["route_equivalent_harness_count"],
            )

    def test_assembled_result_renders_after_raw_warmups_are_deleted(self) -> None:
        self.run_assembler()
        shutil.rmtree(self.root / "warmups")
        output_dir = self.root / "figure"
        completed = subprocess.run(
            [
                self.uv,
                "run",
                str(PLOT_SCRIPT_PATH),
                "--suite",
                str(SUITE_PATH),
                "--results",
                str(self.output),
                "--output-dir",
                str(output_dir),
            ],
            cwd=ROOT,
            check=False,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        self.assertEqual(0, completed.returncode, completed.stderr)
        self.assertTrue((output_dir / "type-isolation-primary-suite.svg").is_file())

    def test_missing_target_fails_closed_and_preserves_existing_output(self) -> None:
        self.output.write_text("preserve-me", encoding="utf-8")
        (self.targets_dir / "actix_web.json").unlink()
        completed = self.run_assembler(check=False)
        self.assertEqual(2, completed.returncode)
        self.assertIn("actix_web", completed.stderr)
        self.assertEqual("preserve-me", self.output.read_text(encoding="utf-8"))

    def test_missing_build_provenance_fails_closed_and_preserves_output(self) -> None:
        self.output.write_text("preserve-me", encoding="utf-8")
        target = self.target_result(self.suite["targets"][0])
        del target["build_records"]["typed_plain"]
        self.rewrite_target("collections", target)
        completed = self.run_assembler(check=False)
        self.assertEqual(2, completed.returncode)
        self.assertIn("build provenance mismatch", completed.stderr)
        self.assertIn("typed_plain", completed.stderr)
        self.assertEqual("preserve-me", self.output.read_text(encoding="utf-8"))

    def test_failed_build_provenance_state_fails_closed(self) -> None:
        mutations = {
            "success": {"success": False},
            "allocator_activation": {"allocator_activation": {"success": False}},
            "actual_mir_provenance": {"actual_mir_provenance": False},
            "stats_disabled": {"stats_feature_enabled": True},
        }
        for expected_state, mutation in mutations.items():
            with self.subTest(state=expected_state):
                self.output.write_text("preserve-me", encoding="utf-8")
                target = self.target_result(self.suite["targets"][0])
                build_path = Path(target["build_records"]["typed_plain"])
                build = json.loads(build_path.read_text(encoding="utf-8"))
                build.update(mutation)
                build_path.write_text(json.dumps(build), encoding="utf-8")
                self.rewrite_target("collections", target)
                completed = self.run_assembler(check=False)
                self.assertEqual(2, completed.returncode)
                self.assertIn(expected_state, completed.stderr)
                self.assertEqual("preserve-me", self.output.read_text(encoding="utf-8"))

    def test_invalid_rustpython_runtime_provenance_fails_closed(self) -> None:
        rustpython_suite = next(
            target for target in self.suite["targets"] if target["id"] == "rustpython"
        )
        mutations = (
            "missing_contract",
            "invalid_soname",
            "invalid_bytes",
            "invalid_library_sha256",
            "missing_variant",
            "extra_variant",
            "failed_proof",
            "resolved_soname_mismatch",
            "resolved_sha_mismatch",
            "binary_mismatch",
            "ldd_digest_mismatch",
            "missing_ldd_record",
        )
        for mutation in mutations:
            with self.subTest(mutation=mutation):
                self.output.write_text("preserve-me", encoding="utf-8")
                target = self.target_result(rustpython_suite)
                inputs = target["inputs"]
                runtime = inputs["rustpython_dynamic_runtime"]
                proofs = runtime["variants"]
                proof = proofs["typed_plain"]
                if mutation == "missing_contract":
                    del inputs["rustpython_dynamic_runtime"]
                elif mutation == "invalid_soname":
                    runtime["library_soname"] = "/host/python/libpython.so"
                elif mutation == "invalid_bytes":
                    runtime["library_bytes"] = 0
                elif mutation == "invalid_library_sha256":
                    runtime["library_sha256"] = "invalid"
                elif mutation == "missing_variant":
                    del proofs["typed_plain"]
                elif mutation == "extra_variant":
                    proofs["extra"] = json.loads(json.dumps(proof))
                elif mutation == "failed_proof":
                    proof["success"] = False
                elif mutation == "resolved_soname_mismatch":
                    proof["resolved_library_soname"] = "libpython-other.so"
                elif mutation == "resolved_sha_mismatch":
                    proof["resolved_library_sha256"] = "0" * 64
                elif mutation == "binary_mismatch":
                    proof["binary_sha256"] = "0" * 64
                elif mutation == "ldd_digest_mismatch":
                    proof["ldd_record_sha256"] = "0" * 64
                elif mutation == "missing_ldd_record":
                    proof["ldd_record"] = str(self.root / "missing.ldd")
                self.rewrite_target("rustpython", target)
                completed = self.run_assembler(check=False)
                self.assertEqual(2, completed.returncode)
                self.assertIn(
                    "rustpython.inputs.rustpython_dynamic_runtime", completed.stderr
                )
                self.assertEqual("preserve-me", self.output.read_text(encoding="utf-8"))

    def test_source_mismatch_fails_closed(self) -> None:
        target = self.target_result(self.suite["targets"][0])
        target["source_commit"] = "0" * 40
        self.rewrite_target("collections", target)
        completed = self.run_assembler(check=False)
        self.assertEqual(2, completed.returncode)
        self.assertIn("source commit mismatch", completed.stderr)
        self.assertFalse(self.output.exists())

    def test_implementation_mismatch_fails_closed_and_preserves_output(self) -> None:
        for field, invalid in (
            ("implementation_revision", "0" * 40),
            ("implementation_sha256", "0" * 64),
        ):
            with self.subTest(field=field):
                self.output.write_text("preserve-me", encoding="utf-8")
                target = self.target_result(self.suite["targets"][0])
                target[field] = invalid
                self.rewrite_target("collections", target)
                completed = self.run_assembler(check=False)
                self.assertEqual(2, completed.returncode)
                self.assertIn(field, completed.stderr)
                self.assertEqual("preserve-me", self.output.read_text(encoding="utf-8"))

    def test_false_gate_fails_closed(self) -> None:
        target = self.target_result(self.suite["targets"][0])
        target["harnesses"][0]["gates"]["actual_mir_provenance"] = False
        self.rewrite_target("collections", target)
        completed = self.run_assembler(check=False)
        self.assertEqual(2, completed.returncode)
        self.assertIn("actual_mir_provenance", completed.stderr)
        self.assertFalse(self.output.exists())

    def test_build_success_is_required_as_stored_evidence(self) -> None:
        for replacement in (False, None):
            with self.subTest(replacement=replacement):
                self.output.write_text("preserve-me", encoding="utf-8")
                target = self.target_result(self.suite["targets"][0])
                gates = target["harnesses"][0]["gates"]
                if replacement is None:
                    del gates["build_success"]
                else:
                    gates["build_success"] = replacement
                self.rewrite_target("collections", target)
                completed = self.run_assembler(check=False)
                self.assertEqual(2, completed.returncode)
                self.assertIn("build_success", completed.stderr)
                self.assertEqual("preserve-me", self.output.read_text(encoding="utf-8"))

    def test_warmup_evidence_is_required_and_digest_validated(self) -> None:
        for mutation in ("missing_variant", "digest_mismatch"):
            with self.subTest(mutation=mutation):
                self.output.write_text("preserve-me", encoding="utf-8")
                target = self.target_result(self.suite["targets"][0])
                warmups = target["harnesses"][0]["warmup_evidence"]
                if mutation == "missing_variant":
                    del warmups["typed_plain"]
                else:
                    warmups["typed_plain"][0]["record_sha256"] = "0" * 64
                self.rewrite_target("collections", target)
                completed = self.run_assembler(check=False)
                self.assertEqual(2, completed.returncode)
                self.assertIn("warmup", completed.stderr)
                self.assertEqual("preserve-me", self.output.read_text(encoding="utf-8"))

    def test_exactly_one_warmup_per_variant_is_required(self) -> None:
        target = self.target_result(self.suite["targets"][0])
        harness = target["harnesses"][0]
        original = Path(harness["warmup_evidence"]["typed_plain"][0]["record_path"])
        duplicate = original.with_name("typed_plain-duplicate.json")
        duplicate.write_bytes(original.read_bytes())
        harness["warmup_evidence"]["typed_plain"].append(
            {
                "record_path": str(duplicate.resolve()),
                "record_sha256": hashlib.sha256(duplicate.read_bytes()).hexdigest(),
                "sha256": hashlib.sha256(duplicate.read_bytes()).hexdigest(),
            }
        )
        self.rewrite_target("collections", target)
        completed = self.run_assembler(check=False)
        self.assertEqual(2, completed.returncode)
        self.assertIn("exactly one warmup", completed.stderr)
        self.assertFalse(self.output.exists())

    def test_measured_round_set_must_be_exactly_one_through_five(self) -> None:
        target = self.target_result(self.suite["targets"][0])
        harness = target["harnesses"][0]
        for variant in VARIANTS:
            template = next(
                row for row in harness["measurements"] if row["variant"] == variant
            )
            harness["measurements"].append({**template, "round": 6})
        self.rewrite_target("collections", target)
        completed = self.run_assembler(check=False)
        self.assertEqual(2, completed.returncode)
        self.assertIn("exact measured rounds 1 through 5", completed.stderr)
        self.assertFalse(self.output.exists())

    def test_claimed_route_equivalence_is_recomputed_from_paired_rounds(self) -> None:
        target = self.target_result(self.suite["targets"][0])
        for row in target["harnesses"][0]["measurements"]:
            if row["variant"] == "typed_plain":
                row["performance"] = 2.0
                synchronize_summary_metric(row)
        self.rewrite_target("collections", target)
        completed = self.run_assembler(check=False)
        self.assertEqual(2, completed.returncode)
        self.assertIn("stored compiler route classification", completed.stderr)
        self.assertFalse(self.output.exists())

    def test_committed_measurement_digest_identity_metric_and_artifact_are_bound(
        self,
    ) -> None:
        for mutation, expected in (
            ("summary_metric", "metric mismatch"),
            ("record_digest", "raw record digest mismatch"),
            ("record_identity", "identity mismatch"),
            ("artifact_content", "artifact stdout content differs"),
        ):
            with self.subTest(mutation=mutation):
                self.output.unlink(missing_ok=True)
                target = self.target_result(self.suite["targets"][0])
                row = target["harnesses"][0]["measurements"][0]
                record_path = Path(str(row["record_path"]))
                record = json.loads(record_path.read_text(encoding="utf-8"))
                if mutation == "summary_metric":
                    row["performance"] = float(row["performance"]) + 1.0
                elif mutation == "record_digest":
                    record_path.write_text(
                        record_path.read_text(encoding="utf-8") + " ",
                        encoding="utf-8",
                    )
                elif mutation == "record_identity":
                    record["identity"]["round"] = 99
                    record_path.write_bytes(
                        immutable_evidence.canonical_json_bytes(record)
                    )
                    row["record_sha256"] = hashlib.sha256(
                        record_path.read_bytes()
                    ).hexdigest()
                else:
                    Path(record["artifacts"]["stdout"]["path"]).write_bytes(
                        b"tampered\n"
                    )
                self.rewrite_target("collections", target)
                completed = self.run_assembler(check=False)
                self.assertEqual(2, completed.returncode)
                self.assertIn(expected, completed.stderr)
                self.assertFalse(self.output.exists())

    def test_stored_route_failure_must_match_paired_rounds(self) -> None:
        target = self.target_result(self.suite["targets"][0])
        target["harnesses"][0]["gates"]["compiler_route_equivalent"] = False
        self.rewrite_target("collections", target)
        completed = self.run_assembler(check=False)
        self.assertEqual(2, completed.returncode)
        self.assertIn("stored compiler route classification", completed.stderr)
        self.assertFalse(self.output.exists())


if __name__ == "__main__":
    unittest.main()
