#!/usr/bin/env python3

from __future__ import annotations

import importlib.util
import contextlib
import hashlib
import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[2]
SCRIPT = (
    ROOT / "evaluation" / "scripts" / "type_isolation_primary_collections_oxipng.py"
)
SUITE = ROOT / "evaluation/config/type_isolation_primary_suite.json"
IMPLEMENTATION_REVISION = "f5d0c19c1cc5b56fdac3282d69333dd8c85d4cf2"
IMPLEMENTATION_SHA256 = (
    "cab1e580c08e2b16308bae75501716049ba428b040fb04bf305269c9ba9eaf01"
)


def load_runner():
    spec = importlib.util.spec_from_file_location("collections_oxipng_runner", SCRIPT)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot import {SCRIPT}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def warmup_evidence(runner, spec, root: Path) -> dict[str, object]:
    evidence: dict[str, object] = {}
    for harness in spec.harnesses:
        variants: dict[str, list[dict[str, str]]] = {}
        for variant in runner.VARIANTS:
            path = root / harness.id / variant / "record.json"
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(
                json.dumps(
                    {
                        "target_id": spec.id,
                        "harness_id": harness.id,
                        "variant": variant,
                        "phase": "warmup",
                        "round": 0,
                        "performance": 1.0,
                        "peak_rss_mib": 2.0,
                    },
                    sort_keys=True,
                )
                + "\n",
                encoding="utf-8",
            )
            variants[variant] = [
                {
                    "record_path": str(path.resolve()),
                    "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
                }
            ]
        evidence[harness.id] = variants
    return evidence


class CollectionsOxipngPrimaryCampaignTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.runner = load_runner()
        cls.suite = json.loads(SUITE.read_text(encoding="utf-8"))

    def test_target_contract_matches_the_predeclared_suite(self) -> None:
        suite_targets = {
            target["id"]: target
            for target in self.suite["targets"]
            if target["id"] in {"collections", "oxipng"}
        }
        self.assertEqual({"collections", "oxipng"}, set(self.runner.TARGETS))
        for target_id, spec in self.runner.TARGETS.items():
            suite_target = suite_targets[target_id]
            self.assertEqual(suite_target["source"]["commit"], spec.commit)
            self.assertEqual(
                [harness["id"] for harness in suite_target["harnesses"]],
                [harness.id for harness in spec.harnesses],
            )
            self.assertEqual(
                [harness["selector"] for harness in suite_target["harnesses"]],
                [harness.selector for harness in spec.harnesses],
            )

    def test_libtest_parser_requires_one_exact_benchmark(self) -> None:
        parsed = self.runner.parse_libtest_benchmark(
            "test binary_heap::bench_push ... bench: 123,456 ns/iter (+/- 10)\n",
            "binary_heap::bench_push",
        )
        self.assertEqual(123456.0, parsed)
        with self.assertRaises(self.runner.CampaignError):
            self.runner.parse_libtest_benchmark(
                "test binary_heap::bench_pop ... bench: 123 ns/iter\n",
                "binary_heap::bench_push",
            )

    def test_compiler_route_gate_uses_paired_median_and_declared_bounds(self) -> None:
        rows = []
        for round_number, ratio in enumerate((0.86, 1.00, 1.14), 1):
            rows.extend(
                (
                    {
                        "round": round_number,
                        "variant": "unialloc",
                        "performance": 100.0,
                    },
                    {
                        "round": round_number,
                        "variant": "typed_plain",
                        "performance": 100.0 * ratio,
                    },
                )
            )
        equivalent, median_ratio = self.runner.compiler_route_equivalence(rows)
        self.assertTrue(equivalent)
        self.assertEqual(1.0, median_ratio)
        rows[-1]["performance"] = 300.0
        rows[-3]["performance"] = 300.0
        rows[-5]["performance"] = 300.0
        equivalent, median_ratio = self.runner.compiler_route_equivalence(rows)
        self.assertFalse(equivalent)
        self.assertEqual(3.0, median_ratio)

    def test_git_revision_snapshot_materializes_all_implementation_components(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            snapshot, digest = self.runner.freeze_unialloc_implementation(
                Path(temporary),
                IMPLEMENTATION_REVISION,
            )
            audit = json.loads((snapshot / "snapshot.json").read_text(encoding="utf-8"))
            self.assertEqual("git_revision", audit["source_kind"])
            self.assertEqual(IMPLEMENTATION_REVISION, audit["implementation_revision"])
            self.assertEqual(digest, audit["unialloc_implementation_sha256"])
            self.assertEqual(IMPLEMENTATION_SHA256, digest)
            for relative_path in (
                "unialloc/src/lib.rs",
                "alloc_macros/src/lib.rs",
                "tools/unialloc-rustc-pass/unialloc-rustc-mir-rewrite-dry-run.rs",
            ):
                expected = self.runner.subprocess.run(
                    ["git", "show", f"{IMPLEMENTATION_REVISION}:{relative_path}"],
                    cwd=ROOT,
                    check=True,
                    stdout=self.runner.subprocess.PIPE,
                ).stdout
                self.assertEqual(expected, (snapshot / relative_path).read_bytes())

    def test_implementation_revision_option_is_retained(self) -> None:
        args = self.runner.parse_args(
            [
                "--implementation-revision",
                IMPLEMENTATION_REVISION,
                "--describe",
            ]
        )
        self.assertEqual(IMPLEMENTATION_REVISION, args.implementation_revision)
        self.assertEqual(3, args.rounds)

    def test_primary_implementation_requires_preregistered_f5_pin(self) -> None:
        self.runner.validate_primary_implementation(
            IMPLEMENTATION_REVISION, IMPLEMENTATION_SHA256
        )
        with self.assertRaisesRegex(
            self.runner.CampaignError, "predeclared implementation revision"
        ):
            self.runner.validate_primary_implementation("0" * 40, IMPLEMENTATION_SHA256)
        with self.assertRaisesRegex(
            self.runner.CampaignError, "predeclared implementation digest"
        ):
            self.runner.validate_primary_implementation(
                IMPLEMENTATION_REVISION, "0" * 64
            )

        legacy = self.runner.parse_args(["--targets", "collections"])
        self.assertTrue(legacy.diagnostic_current_worktree)
        self.assertIsNone(legacy.implementation_revision)
        self.assertEqual(3, legacy.rounds)

        with (
            mock.patch.object(
                self.runner,
                "resolve_implementation_revision",
                return_value="0" * 40,
            ),
            contextlib.redirect_stderr(io.StringIO()),
        ):
            self.assertEqual(
                2,
                self.runner.main(
                    [
                        "--implementation-revision",
                        "HEAD",
                        "--describe",
                    ]
                ),
            )

    def test_measure_only_diagnostic_rejects_stale_working_tree_identity(self) -> None:
        identity = self.runner.current_working_tree_source_identity()
        self.runner.validate_diagnostic_measure_source(identity)

        stale_implementation = dict(identity)
        stale_implementation["unialloc_implementation_sha256"] = "0" * 64
        with self.assertRaisesRegex(
            self.runner.CampaignError, "working-tree implementation digest"
        ):
            self.runner.validate_diagnostic_measure_source(stale_implementation)

        stale_context = dict(identity)
        stale_context["campaign_snapshot_sha256"] = "0" * 64
        with self.assertRaisesRegex(
            self.runner.CampaignError, "working-tree context digest"
        ):
            self.runner.validate_diagnostic_measure_source(stale_context)

    def test_current_working_tree_snapshot_is_diagnostic_and_complete(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            snapshot, digest = self.runner.freeze_unialloc_implementation(
                Path(temporary)
            )
            audit = json.loads((snapshot / "snapshot.json").read_text())
            self.assertEqual("working_tree", audit["source_kind"])
            self.assertEqual(
                "diagnostic_current_worktree", audit["campaign_classification"]
            )
            self.assertFalse(audit["primary_eligible"])
            self.assertEqual(digest, audit["unialloc_implementation_sha256"])
            self.assertEqual(
                hashlib.sha256(audit["repository_status"].encode()).hexdigest(),
                audit["repository_status_sha256"],
            )
            for name in ("Cargo.toml", "Cargo.lock", "rust-toolchain"):
                self.assertTrue((snapshot / name).is_file())
            self.assertTrue((snapshot / "unialloc/.cargo/config").is_file())
            self.assertEqual(
                self.runner.campaign_snapshot_digest(snapshot)[0],
                audit["campaign_snapshot_sha256"],
            )

    def test_current_working_tree_affinity_is_diagnostic_only(self) -> None:
        diagnostic = self.runner.parse_args(
            [
                "--targets",
                "collections",
                "--current-working-tree",
                "--rounds",
                "3",
                "--cpu-list",
                "96-99",
                "--numa-node",
                "1",
            ]
        )
        self.assertTrue(diagnostic.diagnostic_current_worktree)
        self.assertEqual(3, diagnostic.rounds)
        self.assertEqual("96-99", diagnostic.cpu_list)
        self.assertEqual(1, diagnostic.numa_node)
        metadata = self.runner.diagnostic_result_metadata(
            {
                "source_kind": "working_tree",
                "primary_eligible": False,
                "repository_head": "a" * 40,
                "repository_status": " M unialloc/src/lib.rs",
                "repository_status_sha256": "b" * 64,
                "implementation_revision": "a" * 40,
                "unialloc_implementation_sha256": "c" * 64,
                "campaign_snapshot_sha256": "d" * 64,
                "campaign_snapshot_file_count": 5,
                "campaign_snapshot_size_bytes": 1234,
            },
            cpu_list="96-99",
            numa_node=1,
            measured_rounds=3,
        )
        self.assertIs(metadata["primary_eligible"], False)
        self.assertIs(metadata["core_eligible"], False)
        self.assertEqual(3, metadata["measured_rounds"])
        diagnostic_path = self.runner.target_result_path(
            Path("/tmp/raw"),
            "collections",
            diagnostic_current_worktree=True,
        )
        self.assertEqual(
            Path("/tmp/raw/diagnostic-results/collections.json"), diagnostic_path
        )
        self.assertNotEqual(Path("/tmp/raw/targets/collections.json"), diagnostic_path)
        with self.assertRaises(SystemExit):
            self.runner.parse_args(
                [
                    "--implementation-revision",
                    IMPLEMENTATION_REVISION,
                    "--current-working-tree",
                ]
            )
        primary = self.runner.parse_args(
            [
                "--implementation-revision",
                IMPLEMENTATION_REVISION,
                "--rounds",
                "3",
            ]
        )
        self.assertEqual(3, primary.rounds)
        with self.assertRaises(SystemExit):
            self.runner.parse_args(
                [
                    "--implementation-revision",
                    IMPLEMENTATION_REVISION,
                    "--rounds",
                    "5",
                ]
            )
        with self.assertRaises(SystemExit):
            self.runner.parse_args(["--current-working-tree", "--rounds", "5"])
        with self.assertRaises(SystemExit):
            self.runner.parse_args(
                ["--current-working-tree", "--rounds", "4"]
            )
        with self.assertRaises(SystemExit):
            self.runner.parse_args(
                [
                    "--implementation-revision",
                    IMPLEMENTATION_REVISION,
                    "--cpu-list",
                    "96-99",
                ]
            )

    def test_diagnostic_result_accepts_three_complete_paired_rounds(self) -> None:
        spec = self.runner.TARGETS["oxipng"]
        measurements = {
            harness.id: [
                {
                    "round": round_number,
                    "variant": variant,
                    "performance": (
                        float(round_number)
                        if variant == "typed_plain"
                        else 1.0
                    ),
                    "peak_rss_mib": 2.0,
                }
                for round_number in range(1, 4)
                for variant in self.runner.VARIANTS
            ]
            for harness in spec.harnesses
        }
        with tempfile.TemporaryDirectory() as temporary:
            raw_root = Path(temporary)
            result = self.runner.build_target_result(
                spec,
                implementation_revision=IMPLEMENTATION_REVISION,
                implementation_sha256=IMPLEMENTATION_SHA256,
                warmup_evidence=warmup_evidence(
                    self.runner, spec, raw_root / "warmups"
                ),
                measurements=measurements,
                build_gates={
                    "build_success": True,
                    "allocator_activation": True,
                    "actual_mir_provenance": True,
                    "stats_disabled": True,
                    "source_audit_retained": True,
                },
                raw_root=raw_root,
                source_audit_path=raw_root / "source-audit.json",
                build_records={variant: {} for variant in self.runner.VARIANTS},
                raw_records={harness.id: [] for harness in spec.harnesses},
                measured_rounds=3,
            )
        self.assertEqual(3, result["measured_rounds"])
        self.assertEqual(9, len(result["harnesses"][0]["measurements"]))
        self.assertEqual(
            2.0,
            result["harnesses"][0]["evidence"][
                "compiler_route_median_cost_ratio"
            ],
        )

    def test_target_result_keeps_false_gates_and_all_raw_evidence(self) -> None:
        spec = self.runner.TARGETS["collections"]
        measurements = {
            harness.id: [
                {
                    "round": round_number,
                    "variant": variant,
                    "performance": 1.0,
                    "peak_rss_mib": 2.0,
                }
                for round_number in range(1, 4)
                for variant in self.runner.VARIANTS
            ]
            for harness in spec.harnesses
        }
        with tempfile.TemporaryDirectory() as temporary:
            raw_root = Path(temporary)
            result = self.runner.build_target_result(
                spec,
                implementation_revision=IMPLEMENTATION_REVISION,
                implementation_sha256=IMPLEMENTATION_SHA256,
                warmup_evidence=warmup_evidence(
                    self.runner, spec, raw_root / "warmups"
                ),
                measurements=measurements,
                build_gates={
                    "build_success": True,
                    "allocator_activation": True,
                    "actual_mir_provenance": False,
                    "stats_disabled": True,
                    "source_audit_retained": True,
                },
                raw_root=raw_root,
                source_audit_path=raw_root / "source-audit.json",
                build_records={variant: {} for variant in self.runner.VARIANTS},
                raw_records={harness.id: [] for harness in spec.harnesses},
            )
        self.assertEqual("collections", result["target_id"])
        self.assertEqual(spec.commit, result["source_commit"])
        self.assertEqual(5, len(result["harnesses"]))
        self.assertFalse(result["harnesses"][0]["gates"]["actual_mir_provenance"])
        self.assertTrue(result["harnesses"][0]["gates"]["build_success"])
        self.assertTrue(result["harnesses"][0]["gates"]["correctness"])
        self.assertEqual(
            set(self.runner.VARIANTS),
            set(result["harnesses"][0]["warmup_evidence"]),
        )
        self.assertIn("build_records", result["evidence"])

    def test_core_validation_retains_route_limits_and_rejects_data_failures(
        self,
    ) -> None:
        spec = self.runner.TARGETS["collections"]
        measurements = {
            harness.id: [
                {
                    "round": round_number,
                    "variant": variant,
                    "performance": 2.0 if variant == "typed_plain" else 1.0,
                    "peak_rss_mib": 2.0,
                }
                for round_number in range(1, 4)
                for variant in self.runner.VARIANTS
            ]
            for harness in spec.harnesses
        }
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        raw_root = Path(temporary.name)
        result = self.runner.build_target_result(
            spec,
            implementation_revision=IMPLEMENTATION_REVISION,
            implementation_sha256=IMPLEMENTATION_SHA256,
            warmup_evidence=warmup_evidence(self.runner, spec, raw_root / "warmups"),
            measurements=measurements,
            build_gates={
                "build_success": True,
                "allocator_activation": True,
                "actual_mir_provenance": True,
                "stats_disabled": True,
                "source_audit_retained": True,
            },
            raw_root=raw_root,
            source_audit_path=raw_root / "source-audit.json",
            build_records={variant: {} for variant in self.runner.VARIANTS},
            raw_records={harness.id: [] for harness in spec.harnesses},
        )
        self.runner.validate_core_result(result)
        self.assertTrue(
            all(
                harness["gates"]["compiler_route_equivalent"] is False
                for harness in result["harnesses"]
            )
        )
        result["harnesses"][0]["gates"]["stats_disabled"] = False
        with self.assertRaisesRegex(self.runner.CampaignError, "stats_disabled"):
            self.runner.validate_core_result(result)

        result["harnesses"][0]["gates"]["stats_disabled"] = True
        del result["harnesses"][0]["warmup_evidence"]["typed_plain"]
        with self.assertRaisesRegex(self.runner.CampaignError, "warmup"):
            self.runner.validate_core_result(result)

    def test_campaign_publication_validates_every_target_before_writing(self) -> None:
        def record(
            target_id: str,
            *,
            eligible: bool,
            evidence_root: Path,
            measured_rounds: int = 3,
        ) -> dict[str, object]:
            spec = self.runner.TARGETS[target_id]
            harnesses: list[dict[str, object]] = []
            for harness in spec.harnesses:
                path = evidence_root / target_id / harness.id / "warmup.json"
                path.parent.mkdir(parents=True, exist_ok=True)
                entries: dict[str, list[dict[str, str]]] = {}
                for variant in self.runner.VARIANTS:
                    value = path.with_name(f"{variant}.json")
                    value.write_text(
                        json.dumps(
                            {
                                "target_id": target_id,
                                "harness_id": harness.id,
                                "variant": variant,
                                "phase": "warmup",
                                "round": 0,
                                "performance": 1.0,
                                "peak_rss_mib": 1.0,
                            }
                        ),
                        encoding="utf-8",
                    )
                    entries[variant] = [
                        {
                            "record_path": str(value.resolve()),
                            "sha256": hashlib.sha256(value.read_bytes()).hexdigest(),
                        }
                    ]
                harnesses.append(
                    {
                        "id": harness.id,
                        "gates": {
                            gate: eligible for gate in self.runner.CORE_REQUIRED_GATES
                        },
                        "warmup_evidence": entries,
                        "measurements": [
                            {
                                "round": round_number,
                                "variant": variant,
                                "performance": 1.0,
                                "peak_rss_mib": 1.0,
                            }
                            for round_number in range(1, measured_rounds + 1)
                            for variant in self.runner.VARIANTS
                        ],
                    }
                )
            return {
                "schema_version": 1,
                "target_id": target_id,
                "status": "complete",
                "measured_rounds": measured_rounds,
                "implementation_revision": IMPLEMENTATION_REVISION,
                "implementation_sha256": IMPLEMENTATION_SHA256,
                "harnesses": harnesses,
            }

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            local = root / "local"
            destination = root / "published"
            local.mkdir()
            first = local / "collections.json"
            second = local / "oxipng.json"
            first.write_text(
                json.dumps(
                    record("collections", eligible=True, evidence_root=root / "warmups")
                ),
                encoding="utf-8",
            )
            second.write_text(
                json.dumps(
                    record("oxipng", eligible=False, evidence_root=root / "warmups")
                ),
                encoding="utf-8",
            )
            destination.mkdir()
            sentinel = destination / "collections.json"
            sentinel.write_text("preserve-me", encoding="utf-8")

            with self.assertRaisesRegex(self.runner.CampaignError, "core gates"):
                self.runner.publish_target_results(
                    (first, second), destination=destination
                )
            self.assertEqual("preserve-me", sentinel.read_text(encoding="utf-8"))
            self.assertFalse((destination / "oxipng.json").exists())

            second.write_text(
                json.dumps(
                    record("oxipng", eligible=True, evidence_root=root / "warmups")
                ),
                encoding="utf-8",
            )
            diagnostic = json.loads(first.read_text(encoding="utf-8"))
            diagnostic.update(
                {
                    "campaign_classification": "diagnostic_current_worktree",
                    "primary_eligible": False,
                }
            )
            first.write_text(json.dumps(diagnostic), encoding="utf-8")
            with self.assertRaisesRegex(self.runner.CampaignError, "diagnostic"):
                self.runner.publish_target_results(
                    (first, second), destination=destination
                )

            for invalid_status in (None, "preflight_passed"):
                incomplete = record(
                    "collections", eligible=True, evidence_root=root / "warmups"
                )
                if invalid_status is None:
                    incomplete.pop("status")
                else:
                    incomplete["status"] = invalid_status
                first.write_text(json.dumps(incomplete), encoding="utf-8")
                with self.assertRaisesRegex(self.runner.CampaignError, "complete status"):
                    self.runner.publish_target_results(
                        (first, second), destination=destination
                    )

            incomplete = record(
                "collections", eligible=True, evidence_root=root / "warmups"
            )
            incomplete["harnesses"][0]["measurements"].pop()
            first.write_text(json.dumps(incomplete), encoding="utf-8")
            with self.assertRaisesRegex(self.runner.CampaignError, "complete paired"):
                self.runner.publish_target_results(
                    (first, second), destination=destination
                )

            unsupported = record(
                "collections", eligible=True, evidence_root=root / "warmups"
            )
            unsupported["measured_rounds"] = 4
            first.write_text(json.dumps(unsupported), encoding="utf-8")
            with self.assertRaisesRegex(self.runner.CampaignError, "three measured"):
                self.runner.publish_target_results(
                    (first, second), destination=destination
                )

            first.write_text(
                json.dumps(
                    record("collections", eligible=True, evidence_root=root / "warmups")
                ),
                encoding="utf-8",
            )
            invalid_identities = (
                (
                    "wrong revision",
                    {"implementation_revision": "0" * 40},
                    (),
                    "predeclared implementation revision",
                ),
                (
                    "wrong digest",
                    {"implementation_sha256": "0" * 64},
                    (),
                    "predeclared implementation digest",
                ),
                (
                    "missing identity",
                    {},
                    ("implementation_revision", "implementation_sha256"),
                    "predeclared implementation revision",
                ),
            )
            for label, replacements, removals, message in invalid_identities:
                with self.subTest(label=label):
                    invalid = record(
                        "oxipng", eligible=True, evidence_root=root / "warmups"
                    )
                    invalid.update(replacements)
                    for field in removals:
                        invalid.pop(field)
                    second.write_text(json.dumps(invalid), encoding="utf-8")
                    with self.assertRaisesRegex(self.runner.CampaignError, message):
                        self.runner.publish_target_results(
                            (first, second), destination=destination
                        )
                    self.assertEqual(
                        "preserve-me", sentinel.read_text(encoding="utf-8")
                    )
                    self.assertFalse((destination / "oxipng.json").exists())

            second.write_text(
                json.dumps(
                    record(
                        "oxipng",
                        eligible=True,
                        evidence_root=root / "warmups",
                        measured_rounds=self.runner.LEGACY_PRIMARY_ROUNDS,
                    )
                ),
                encoding="utf-8",
            )
            first.write_text(
                json.dumps(
                    record("collections", eligible=True, evidence_root=root / "warmups")
                ),
                encoding="utf-8",
            )
            published = self.runner.publish_target_results(
                (first, second), destination=destination
            )
            self.assertEqual(
                (destination / "collections.json", destination / "oxipng.json"),
                published,
            )

    def test_incomplete_measurements_fail_closed(self) -> None:
        spec = self.runner.TARGETS["oxipng"]
        measurements = {harness.id: [] for harness in spec.harnesses}
        with self.assertRaises(self.runner.CampaignError):
            self.runner.build_target_result(
                spec,
                implementation_revision=IMPLEMENTATION_REVISION,
                implementation_sha256=IMPLEMENTATION_SHA256,
                warmup_evidence={},
                measurements=measurements,
                build_gates={
                    "build_success": True,
                    "allocator_activation": True,
                    "actual_mir_provenance": True,
                    "stats_disabled": True,
                    "source_audit_retained": True,
                },
                raw_root=Path("/tmp/raw"),
                source_audit_path=Path("/tmp/raw/source-audit.json"),
                build_records={variant: {} for variant in self.runner.VARIANTS},
                raw_records={harness.id: [] for harness in spec.harnesses},
            )


if __name__ == "__main__":
    unittest.main()
