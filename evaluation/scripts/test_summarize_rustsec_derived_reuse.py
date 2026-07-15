#!/usr/bin/env python3
"""Tests for the deterministic derived-reuse evidence summarizer."""

from __future__ import annotations

import copy
import hashlib
import importlib.util
import json
import pathlib
import tempfile
import unittest


ROOT = pathlib.Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "evaluation/scripts/summarize_rustsec_derived_reuse.py"
CATALOG = ROOT / "evaluation/config/rustsec_heap_expansion_harnesses.json"
RAW = {
    case_id: ROOT
    / (
        "docs/evidence/rustsec-security-expansion-20260714/raw/derived/"
        f"{case_id}-derived-reuse-experiment.json"
    )
    for case_id in ("RSH-041", "RSH-042")
}
SINGLE_PROFILE_INPUTS = {
    "rsh002": (
        ROOT / "evaluation/config/rustsec_heap_harnesses.json",
        "RSH-002",
        ROOT / "evaluation/raw/rsh002-derived-final-current-20260715/experiment.json",
    ),
    "rsh008": (
        ROOT / "evaluation/config/rustsec_heap_harnesses.json",
        "RSH-008",
        ROOT / "evaluation/raw/rsh008-derived-final-current-20260715/experiment.json",
    ),
    "rsh052": (
        ROOT / "evaluation/config/rustsec_heap_strong_batch_a_harnesses.json",
        "RSH-052",
        ROOT / "evaluation/raw/rsh052-derived-final-current-20260715/experiment.json",
    ),
    "rsh064": (
        ROOT / "evaluation/config/rustsec_heap_neon_node_harnesses.json",
        "RSH-064",
        ROOT / "evaluation/raw/rsh064-derived-final-current-20260715/experiment.json",
    ),
}
PROFILE_SHAPES_WITHOUT_RAW = {
    "rsh055": {
        "advisory_id": "RUSTSEC-2020-0145",
        "case_id": "RSH-055",
        "catalog": ROOT
        / "evaluation/config/rustsec_heap_strong_batch_b_harnesses.json",
        "catalog_counts": {
            "derived_reuse_scenario_count": 1,
            "repository_scenario_count": 5,
        },
        "catalog_source": "unialloc-strong-candidate-batch-b-fragment",
    },
    "rsh065": {
        "advisory_id": "RUSTSEC-2023-0054",
        "case_id": "RSH-065",
        "catalog": ROOT
        / "evaluation/config/rustsec_heap_strong_batch_c_harnesses.json",
        "catalog_counts": {
            "derived_reuse_scenario_count": 3,
            "repository_scenario_count": 11,
        },
        "catalog_source": "unialloc-strong-candidate-batch-c-fragment",
    },
    "rsh066": {
        "advisory_id": "RUSTSEC-2024-0007",
        "case_id": "RSH-066",
        "catalog": ROOT
        / "evaluation/config/rustsec_heap_strong_batch_c_harnesses.json",
        "catalog_counts": {
            "derived_reuse_scenario_count": 3,
            "repository_scenario_count": 11,
        },
        "catalog_source": "unialloc-strong-candidate-batch-c-fragment",
    },
    "rsh067": {
        "advisory_id": "RUSTSEC-2025-0004",
        "case_id": "RSH-067",
        "catalog": ROOT
        / "evaluation/config/rustsec_heap_strong_batch_c_harnesses.json",
        "catalog_counts": {
            "derived_reuse_scenario_count": 3,
            "repository_scenario_count": 11,
        },
        "catalog_source": "unialloc-strong-candidate-batch-c-fragment",
    },
    "rsh068": {
        "advisory_id": "RUSTSEC-2025-0016",
        "case_id": "RSH-068",
        "catalog": ROOT
        / "evaluation/config/rustsec_heap_strong_batch_f_harnesses.json",
        "catalog_counts": {
            "derived_reuse_scenario_count": 2,
            "repository_scenario_count": 7,
        },
        "catalog_source": "unialloc-strong-rustsec-batch-f-harness-catalog",
    },
    "rsh069": {
        "advisory_id": "RUSTSEC-2025-0022",
        "case_id": "RSH-069",
        "catalog": ROOT
        / "evaluation/config/rustsec_heap_strong_batch_f_harnesses.json",
        "catalog_counts": {
            "derived_reuse_scenario_count": 2,
            "repository_scenario_count": 7,
        },
        "catalog_source": "unialloc-strong-rustsec-batch-f-harness-catalog",
    },
}
spec = importlib.util.spec_from_file_location("derived_reuse_summary", SCRIPT)
assert spec is not None and spec.loader is not None
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


def record(path: pathlib.Path) -> dict[str, object]:
    payload = path.read_bytes()
    try:
        display = str(path.resolve().relative_to(ROOT))
    except ValueError:
        display = str(path.resolve())
    return {
        "path": display,
        "bytes": len(payload),
        "sha256": hashlib.sha256(payload).hexdigest(),
    }


class DerivedReuseSummaryTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.catalog = json.loads(CATALOG.read_text(encoding="utf-8"))
        cls.catalog_artifact = record(CATALOG)
        cls.experiments = {
            case_id: (
                json.loads(path.read_text(encoding="utf-8")),
                record(path),
            )
            for case_id, path in RAW.items()
        }
        cls.single_profiles = {}
        for profile_name, (catalog_path, case_id, experiment_path) in (
            SINGLE_PROFILE_INPUTS.items()
        ):
            cls.single_profiles[profile_name] = {
                "catalog": json.loads(catalog_path.read_text(encoding="utf-8")),
                "catalog_artifact": record(catalog_path),
                "case_id": case_id,
                "experiments": {
                    case_id: (
                        json.loads(experiment_path.read_text(encoding="utf-8")),
                        record(experiment_path),
                    )
                },
            }

    def build(
        self,
        *,
        catalog: dict[str, object] | None = None,
        catalog_artifact: dict[str, object] | None = None,
        experiments: dict[str, tuple[dict[str, object], dict[str, object]]]
        | None = None,
        profile_name: str = "expansion",
    ) -> dict[str, object]:
        return module.build_summary(
            copy.deepcopy(catalog if catalog is not None else self.catalog),
            copy.deepcopy(
                catalog_artifact
                if catalog_artifact is not None
                else self.catalog_artifact
            ),
            copy.deepcopy(experiments if experiments is not None else self.experiments),
            profile_name=profile_name,
        )

    def build_profile(self, profile_name: str) -> dict[str, object]:
        inputs = self.single_profiles[profile_name]
        return self.build(
            catalog=inputs["catalog"],
            catalog_artifact=inputs["catalog_artifact"],
            experiments=inputs["experiments"],
            profile_name=profile_name,
        )

    def test_builds_current_schema_deterministically(self) -> None:
        first = self.build()
        second = self.build()

        self.assertEqual(first, second)
        self.assertEqual(first["schema_version"], 1)
        self.assertEqual(
            first["source"], "unialloc-rustsec-derived-reuse-expansion-summary"
        )
        self.assertEqual(
            first["counts"],
            {
                "compiler_automatic_victim_coverage_count": 0,
                "derived_reuse_scenario_count": 2,
                "source_vulnerability_detection_validated_count": 0,
                "source_vulnerability_mitigation_inferred_count": 0,
                "validated_cross_identity_reuse_edge_count": 2,
            },
        )
        self.assertEqual(
            [(row["case_id"], len(row["arms"])) for row in first["scenarios"]],
            [("RSH-041", 6), ("RSH-042", 8)],
        )
        for artifact in [first["catalog"], *first["raw_experiment_inputs"]]:
            path = ROOT / artifact["path"]
            payload = path.read_bytes()
            self.assertEqual(artifact["bytes"], len(payload))
            self.assertEqual(artifact["sha256"], hashlib.sha256(payload).hexdigest())

        for scenario in first["scenarios"]:
            edge = scenario["edge_evaluation"]
            self.assertTrue(edge["validated"])
            self.assertEqual(
                edge["status"], "cross_identity_reuse_edge_blocked_and_reported"
            )
            self.assertTrue(edge["manual_victim_identity_annotation"])
            self.assertFalse(edge["compiler_automatic_victim_coverage"])
            self.assertFalse(edge["source_vulnerability_detection_validated"])

        rendered = json.dumps(first, indent=2, sort_keys=True) + "\n"
        self.assertEqual(json.loads(rendered), first)

    def test_single_case_profiles_share_the_strict_generic_contract(self) -> None:
        expected_sources = {
            "rsh002": "unialloc-rustsec-rsh002-derived-reuse-summary",
            "rsh008": "unialloc-rustsec-rsh008-derived-reuse-summary",
            "rsh052": "unialloc-rustsec-rsh052-derived-reuse-summary",
            "rsh064": "unialloc-rustsec-rsh064-derived-reuse-summary",
        }
        for profile_name, expected_source in expected_sources.items():
            with self.subTest(profile=profile_name):
                summary = self.build_profile(profile_name)
                case_id = self.single_profiles[profile_name]["case_id"]
                self.assertEqual(summary["source"], expected_source)
                self.assertEqual(
                    summary["counts"]["validated_cross_identity_reuse_edge_count"],
                    1,
                )
                self.assertEqual(
                    [(row["case_id"], len(row["arms"])) for row in summary["scenarios"]],
                    [(case_id, 6)],
                )
                self.assertEqual(summary["scenarios"][0]["repetitions_requested"], 3)

    def test_new_single_case_profile_shapes_without_raw_artifacts(self) -> None:
        for profile_name, expected in PROFILE_SHAPES_WITHOUT_RAW.items():
            with self.subTest(profile=profile_name):
                case_id = expected["case_id"]
                profile = module.PROFILES[profile_name]
                self.assertEqual(profile.case_ids, (case_id,))
                self.assertEqual(
                    profile.advisory_ids, {case_id: expected["advisory_id"]}
                )
                self.assertEqual(profile.catalog_path, expected["catalog"])
                self.assertEqual(profile.catalog_source, expected["catalog_source"])
                self.assertEqual(
                    profile.catalog_count_contract, expected["catalog_counts"]
                )
                self.assertEqual(
                    profile.expected_variants[case_id],
                    ("system", "typed_plain", "typeiso"),
                )
                self.assertTrue(profile.typed_allocator_expected_oracle[case_id])
                self.assertEqual(
                    profile.source,
                    f"unialloc-rustsec-{profile_name}-derived-reuse-summary",
                )
                self.assertIn("manually attributed", profile.boundary)
                self.assertIn(case_id, profile.boundary)
                self.assertIn("limited", profile.boundary)

                default = (
                    ROOT
                    / "evaluation"
                    / "raw"
                    / f"{profile_name}-derived-final-current-20260715"
                    / "experiment.json"
                )
                self.assertEqual(profile.default_experiments, {case_id: default})
                self.assertEqual(
                    module.experiment_selections(profile, None), {case_id: default}
                )

                catalog = json.loads(
                    expected["catalog"].read_text(encoding="utf-8")
                )
                selected = module.catalog_inputs(catalog, profile)
                self.assertEqual(set(selected), {case_id})

    def test_rsh064_matches_existing_single_case_schema_and_boundary(self) -> None:
        summary = self.build_profile("rsh064")

        self.assertEqual(summary["schema_version"], 1)
        self.assertEqual(
            summary["source"], "unialloc-rustsec-rsh064-derived-reuse-summary"
        )
        self.assertEqual(
            summary["boundary"],
            "This artifact validates one manually attributed exploit-enabling "
            "cross-identity reuse edge. Automatic compiler coverage of the published "
            "Neon Vec/external-buffer source path remains deferred.",
        )
        self.assertEqual(summary["scenarios"][0]["case_id"], "RSH-064")
        self.assertEqual(
            summary["scenarios"][0]["annotation"]["claim_scope"],
            "derived RSH-064 cross-identity Vec<u8>-backing-store-to-Replacement "
            "reuse edge only",
        )

    def test_rejects_case_and_scenario_mismatches(self) -> None:
        mutations = []
        wrong_scenario = copy.deepcopy(self.experiments)
        wrong_scenario["RSH-041"][0]["scenario_id"] = "RSH-041-wrong"
        mutations.append(wrong_scenario)
        wrong_case = copy.deepcopy(self.experiments)
        wrong_case["RSH-042"][0]["arms"][0]["case_id"] = "RSH-041"
        mutations.append(wrong_case)

        for experiments in mutations:
            with self.subTest(), self.assertRaises(module.SummaryError):
                self.build(experiments=experiments)

    def test_rejects_invalid_edge_evaluation(self) -> None:
        experiments = copy.deepcopy(self.experiments)
        experiments["RSH-041"][0]["type_isolation_reuse_edge_evaluation"][
            "validated"
        ] = False

        with self.assertRaises(module.SummaryError):
            self.build(experiments=experiments)

    def test_rejects_missing_arms_and_repetition_counts(self) -> None:
        missing = copy.deepcopy(self.experiments)
        missing["RSH-041"][0]["arms"].pop()
        bad_count = copy.deepcopy(self.experiments)
        bad_count["RSH-042"][0]["arms"][0]["repetition_summary"]["executed"] = 2

        for experiments in (missing, bad_count):
            with self.subTest(), self.assertRaises(module.SummaryError):
                self.build(experiments=experiments)

    def test_rejects_source_hash_drift(self) -> None:
        catalog = copy.deepcopy(self.catalog)
        case = next(row for row in catalog["cases"] if row["case_id"] == "RSH-041")
        scenario = next(
            row
            for row in case["scenarios"]
            if row["classification_role"] == "derived_reuse_experiment"
        )
        scenario["source_sha256"] = "0" * 64

        with self.assertRaises(module.SummaryError):
            self.build(catalog=catalog)

    def test_rejects_artifact_hash_drift(self) -> None:
        mutations = []
        fingerprint = copy.deepcopy(self.experiments)
        fingerprint["RSH-041"][0]["arms"][0]["fingerprint"] = "0" * 64
        mutations.append(fingerprint)
        stream = copy.deepcopy(self.experiments)
        stream["RSH-042"][0]["arms"][0]["build"]["stderr_sha256"] = "0" * 64
        mutations.append(stream)

        for experiments in mutations:
            with self.subTest(), self.assertRaises(module.SummaryError):
                self.build(experiments=experiments)

    def test_rsh064_fails_closed_on_coherent_causal_and_source_tampering(self) -> None:
        inputs = self.single_profiles["rsh064"]
        with tempfile.TemporaryDirectory() as temporary:
            directory = pathlib.Path(temporary)

            causal_raw = copy.deepcopy(inputs["experiments"]["RSH-064"][0])
            typeiso_arm = next(
                arm
                for arm in causal_raw["arms"]
                if arm["archive_variant"] == "vulnerable"
                and arm["allocator_variant"] == "typeiso"
            )
            typeiso_arm["repetition_summary"][
                "address_reuse_observation_count"
            ] = 1
            causal_path = directory / "causal-tamper.json"
            causal_path.write_text(json.dumps(causal_raw), encoding="utf-8")
            causal_experiments = {
                "RSH-064": (causal_raw, record(causal_path)),
            }
            with self.assertRaisesRegex(module.SummaryError, "causal contract"):
                self.build(
                    catalog=inputs["catalog"],
                    catalog_artifact=inputs["catalog_artifact"],
                    experiments=causal_experiments,
                    profile_name="rsh064",
                )

            source_catalog = copy.deepcopy(inputs["catalog"])
            case = next(
                row for row in source_catalog["cases"] if row["case_id"] == "RSH-064"
            )
            scenario = next(
                row
                for row in case["scenarios"]
                if row.get("classification_role") == "derived_reuse_experiment"
            )
            scenario["source_sha256"] = "0" * 64
            catalog_path = directory / "source-tamper.json"
            catalog_path.write_text(json.dumps(source_catalog), encoding="utf-8")
            with self.assertRaisesRegex(module.SummaryError, "source hash mismatch"):
                self.build(
                    catalog=source_catalog,
                    catalog_artifact=record(catalog_path),
                    experiments=inputs["experiments"],
                    profile_name="rsh064",
                )

    def test_rsh064_rejects_input_artifact_digest_tampering(self) -> None:
        inputs = self.single_profiles["rsh064"]
        experiments = copy.deepcopy(inputs["experiments"])
        experiments["RSH-064"][1]["sha256"] = "0" * 64

        with self.assertRaisesRegex(module.SummaryError, "artifact hash mismatch"):
            self.build(
                catalog=inputs["catalog"],
                catalog_artifact=inputs["catalog_artifact"],
                experiments=experiments,
                profile_name="rsh064",
            )


if __name__ == "__main__":
    unittest.main()
