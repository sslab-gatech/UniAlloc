#!/usr/bin/env python3
"""Unit tests for the RustSec allocator experiment orchestrator."""

from __future__ import annotations

import contextlib
import importlib.util
import io
import json
import os
import pathlib
import tempfile
import unittest
from unittest import mock


ROOT = pathlib.Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "evaluation" / "scripts" / "run_rustsec_heap_experiment.py"
spec = importlib.util.spec_from_file_location("rustsec_heap_experiment", SCRIPT)
assert spec is not None and spec.loader is not None
experiment = importlib.util.module_from_spec(spec)
spec.loader.exec_module(experiment)


def execution(
    exit_code: int,
    text: str = "",
    *,
    stderr: str = "",
    timed_out: bool = False,
):
    return {
        "exit_code": exit_code,
        "stdout": text,
        "stderr": stderr,
        "timed_out": timed_out,
    }


class RustSecHeapExperimentTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = pathlib.Path(self.temporary.name)

    def write_manifest_and_lock(
        self, *, direct_unialloc: bool, locked_unialloc: bool
    ) -> tuple[pathlib.Path, pathlib.Path]:
        manifest = self.root / "Cargo.toml"
        dependency = (
            'unialloc = { path = "/repo/unialloc" }\n'
            if direct_unialloc
            else 'demo = "=1.0.0"\n'
        )
        manifest.write_text(
            '[package]\nname="demo"\nversion="0.0.0"\n[dependencies]\n'
            + dependency,
            encoding="utf-8",
        )
        packages = [
            {"name": "demo", "version": "0.0.0"},
            {"name": "unialloc", "version": "0.1.0"}
            if locked_unialloc
            else {"name": "other", "version": "1.0.0"},
        ]
        lock = self.root / "Cargo.lock"
        lines = ["version = 4", ""]
        for package in packages:
            lines.extend(
                [
                    "[[package]]",
                    f'name = "{package["name"]}"',
                    f'version = "{package["version"]}"',
                    "",
                ]
            )
        lock.write_text("\n".join(lines), encoding="utf-8")
        return manifest, lock

    def cargo_metadata_record(
        self,
        *,
        features: list[str],
        manifest_path: pathlib.Path | None = None,
        extra_packages: list[dict[str, object]] | None = None,
        extra_nodes: list[dict[str, object]] | None = None,
        source: str | None = None,
    ) -> dict[str, object]:
        manifest = manifest_path or experiment.ROOT / "unialloc" / "Cargo.toml"
        package_id = (
            f"path+{(experiment.ROOT / 'unialloc').resolve().as_uri()}#0.1.0"
        )
        package = {
            "name": "unialloc",
            "id": package_id,
            "manifest_path": str(manifest),
            "source": source,
        }
        node = {"id": package_id, "features": features}
        payload = {
            "packages": [package, *(extra_packages or [])],
            "resolve": {"nodes": [node, *(extra_nodes or [])]},
        }
        stdout = json.dumps(payload, sort_keys=True)
        return {
            "stdout": stdout,
            "stdout_sha256": experiment.sha256_bytes(stdout.encode("utf-8")),
        }

    def test_force_loaded_arm_rejects_direct_or_locked_unialloc(self) -> None:
        manifest, lock = self.write_manifest_and_lock(
            direct_unialloc=True, locked_unialloc=True
        )
        force = self.root / "libunialloc.rlib"
        force.write_bytes(b"rlib")

        with self.assertRaisesRegex(experiment.ExperimentError, "mixed UniAlloc identity"):
            experiment.validate_allocator_identity(
                "typeiso", manifest, lock, force_rlib=force
            )

    def test_typeiso_preflight_accepts_force_only_topology_without_built_rlib(self) -> None:
        manifest, lock = self.write_manifest_and_lock(
            direct_unialloc=False, locked_unialloc=False
        )

        experiment.validate_allocator_identity(
            "typeiso",
            manifest,
            lock,
            force_rlib=None,
            require_direct_lock=False,
        )

    def test_reclaim_variants_use_feature_matched_direct_unialloc_dependencies(self) -> None:
        manifest, lock = self.write_manifest_and_lock(
            direct_unialloc=True, locked_unialloc=True
        )
        for variant in ("reclaim_plain", "reclaim_checks"):
            with self.subTest(variant=variant):
                experiment.validate_allocator_identity(
                    variant, manifest, lock, force_rlib=None
                )
                project = self.root / variant
                project.mkdir()
                (project / "Cargo.toml").write_text(
                    '[package]\nname="demo"\nversion="0.0.0"\n[dependencies]\n',
                    encoding="utf-8",
                )
                with mock.patch.object(
                    experiment.realworld,
                    "find_cached_spin",
                    return_value=self.root / "spin",
                ), mock.patch.object(experiment.realworld, "add_spin_patch"):
                    experiment.configure_manifest(project, variant)
                configured = (project / "Cargo.toml").read_text(encoding="utf-8")
                self.assertIn('default-features = false', configured)
                self.assertIn('"stats"', configured)
                self.assertEqual(
                    '"reclaim_checks"' in configured,
                    variant == "reclaim_checks",
                )
        self.assertEqual(
            experiment.direct_unialloc_feature_evidence("reclaim_plain"),
            {
                "features": ["stats"],
                "default_features_enabled": False,
            },
        )
        self.assertEqual(
            experiment.direct_unialloc_feature_evidence("reclaim_checks"),
            {
                "features": ["stats", "reclaim_checks"],
                "default_features_enabled": False,
            },
        )
        self.assertIsNone(experiment.direct_unialloc_feature_evidence("typeiso"))

    def test_cargo_metadata_attestation_records_exact_reclaim_feature_contracts(
        self,
    ) -> None:
        cases = (
            ("unialloc", ["stats", "default", "pthread_dtor", "rseq"], True),
            ("reclaim_plain", ["stats"], False),
            ("reclaim_checks", ["stats", "reclaim_checks"], False),
        )
        tokens: set[str] = set()
        for variant, features, default_resolved in cases:
            with self.subTest(variant=variant):
                record = self.cargo_metadata_record(features=features)
                attestation = experiment.attest_direct_unialloc_metadata(
                    variant, record
                )
                self.assertEqual(attestation["schema_version"], 1)
                self.assertEqual(attestation["package_count"], 1)
                self.assertEqual(attestation["resolve_node_count"], 1)
                self.assertEqual(
                    attestation["manifest_path"],
                    str((experiment.ROOT / "unialloc" / "Cargo.toml").resolve()),
                )
                self.assertIsNone(attestation["package_source"])
                self.assertEqual(attestation["resolved_features"], sorted(features))
                self.assertEqual(attestation["expected_features"], sorted(features))
                self.assertEqual(
                    attestation["default_feature_resolved"], default_resolved
                )
                self.assertEqual(
                    attestation["default_features_expected"], default_resolved
                )
                self.assertEqual(
                    attestation["cargo_metadata_stdout_sha256"],
                    record["stdout_sha256"],
                )
                contract = {
                    key: value
                    for key, value in attestation.items()
                    if key
                    not in (
                        "cargo_metadata_stdout_sha256",
                        "feature_contract_sha256",
                    )
                }
                self.assertEqual(
                    attestation["feature_contract_sha256"],
                    experiment.canonical_sha256(contract),
                )
                tokens.add(attestation["feature_contract_sha256"])
        self.assertEqual(len(tokens), 3)

    def test_cargo_metadata_attestation_rejects_spoofed_identity_and_graph(
        self,
    ) -> None:
        package_id = (
            f"path+{(experiment.ROOT / 'unialloc').resolve().as_uri()}#0.1.0"
        )
        duplicate_package = {
            "name": "unialloc",
            "id": package_id + "-duplicate",
            "manifest_path": str(
                (experiment.ROOT / "unialloc" / "Cargo.toml").resolve()
            ),
            "source": None,
        }
        invalid = (
            (
                self.cargo_metadata_record(
                    features=["stats"],
                    manifest_path=self.root / "spoof" / "Cargo.toml",
                ),
                "manifest path does not match",
            ),
            (
                self.cargo_metadata_record(
                    features=["stats"], extra_packages=[duplicate_package]
                ),
                "exactly one UniAlloc package",
            ),
            (
                self.cargo_metadata_record(
                    features=["stats"],
                    extra_nodes=[{"id": package_id, "features": ["stats"]}],
                ),
                "exactly one UniAlloc resolve node",
            ),
            (
                self.cargo_metadata_record(
                    features=["stats"], source="registry+https://example.invalid"
                ),
                "must be a path dependency",
            ),
        )
        for record, message in invalid:
            with self.subTest(message=message):
                with self.assertRaisesRegex(experiment.ExperimentError, message):
                    experiment.attest_direct_unialloc_metadata(
                        "reclaim_plain", record
                    )

    def test_cargo_metadata_attestation_rejects_feature_and_default_leakage(
        self,
    ) -> None:
        invalid = (
            (["stats", "type_isolation"], "variant contract"),
            (["default", "stats"], "default-feature resolution"),
            (["stats", "stats"], "contain duplicates"),
        )
        for features, message in invalid:
            with self.subTest(features=features):
                with self.assertRaisesRegex(experiment.ExperimentError, message):
                    experiment.attest_direct_unialloc_metadata(
                        "reclaim_plain",
                        self.cargo_metadata_record(features=features),
                    )

        corrupted_hash = self.cargo_metadata_record(features=["stats"])
        corrupted_hash["stdout_sha256"] = "0" * 64
        with self.assertRaisesRegex(experiment.ExperimentError, "hash does not match"):
            experiment.attest_direct_unialloc_metadata(
                "reclaim_plain", corrupted_hash
            )

    def test_direct_arm_fingerprint_requires_and_binds_metadata_attestation(
        self,
    ) -> None:
        project = self.root / "fingerprint-project"
        (project / ".cargo").mkdir(parents=True)
        (project / "Cargo.toml").write_text("[package]\nname='demo'\n", encoding="utf-8")
        (project / "Cargo.lock").write_text("version = 4\n", encoding="utf-8")
        (project / ".cargo" / "config.toml").write_text("", encoding="utf-8")
        report = {
            "scenario_id": "RSH-test",
            "source_sha256": "1" * 64,
            "archive_sha256": "2" * 64,
            "cargo_lock_sha256": "3" * 64,
        }
        attestation = experiment.attest_direct_unialloc_metadata(
            "reclaim_plain", self.cargo_metadata_record(features=["stats"])
        )
        arguments = {
            "catalog_sha256": "4" * 64,
            "report": report,
            "archive_variant": "vulnerable",
            "allocator_variant": "reclaim_plain",
            "project": project,
            "transformed": {},
            "toolchain": {"name": experiment.TOOLCHAIN},
            "tools": None,
            "target_crates": ("demo",),
            "rustflags": (),
            "subject_cargo_features": {},
        }
        with mock.patch.object(
            experiment.realworld, "implementation_digest", return_value="5" * 64
        ):
            payload = experiment.arm_fingerprint_payload(
                **arguments,
                cargo_metadata_attestation=attestation,
            )
            self.assertEqual(payload["cargo_metadata_attestation"], attestation)
            with self.assertRaisesRegex(
                experiment.ExperimentError, "lacks Cargo metadata attestation"
            ):
                experiment.arm_fingerprint_payload(
                    **arguments,
                    cargo_metadata_attestation=None,
                )

    def test_allocator_cargo_feature_overrides_are_validated_and_applied(self) -> None:
        scenario = {
            "allocator_cargo_feature_overrides": {
                "system": ["sqlite", "__with_asan_tests"],
                "reclaim_checks": ["sqlite"],
            }
        }

        def project(name: str) -> pathlib.Path:
            root = self.root / name
            root.mkdir()
            (root / "Cargo.toml").write_text(
                '[package]\nname="demo"\nversion="0.0.0"\n[dependencies]\n'
                '"diesel" = { path = "../diesel", default-features = true, '
                'features = ["sqlite", "__with_asan_tests"] }\n',
                encoding="utf-8",
            )
            return root

        reclaim_project = project("reclaim")
        reclaim = experiment.configure_subject_dependency_features(
            reclaim_project,
            crate_name="diesel",
            scenario=scenario,
            allocator_variant="reclaim_checks",
        )
        self.assertEqual(reclaim["catalog_features"], ["sqlite", "__with_asan_tests"])
        self.assertEqual(reclaim["effective_features"], ["sqlite"])
        self.assertTrue(reclaim["override_applied"])
        self.assertTrue(reclaim["default_features_enabled"])
        self.assertEqual(
            experiment.subject_dependency_configuration(
                reclaim_project / "Cargo.toml", "diesel"
            )["features"],
            ["sqlite"],
        )

        plain = experiment.configure_subject_dependency_features(
            project("plain"),
            crate_name="diesel",
            scenario=scenario,
            allocator_variant="reclaim_plain",
        )
        self.assertEqual(plain["effective_features"], ["sqlite"])
        self.assertTrue(plain["override_applied"])
        self.assertEqual(plain["override_allocator_variant"], "reclaim_checks")
        self.assertEqual(
            plain["effective_features"], reclaim["effective_features"]
        )

        system = experiment.configure_subject_dependency_features(
            project("system"),
            crate_name="diesel",
            scenario=scenario,
            allocator_variant="system",
        )
        self.assertEqual(system["effective_features"], ["sqlite", "__with_asan_tests"])
        self.assertTrue(system["override_applied"])

        default = experiment.configure_subject_dependency_features(
            project("default"),
            crate_name="diesel",
            scenario=scenario,
            allocator_variant="unialloc",
        )
        self.assertEqual(default["effective_features"], ["sqlite", "__with_asan_tests"])
        self.assertFalse(default["override_applied"])

    def test_allocator_cargo_feature_overrides_fail_closed(self) -> None:
        invalid = (
            ({"allocator_cargo_feature_overrides": []}, "must be an object"),
            (
                {"allocator_cargo_feature_overrides": {"unknown": ["sqlite"]}},
                "unknown allocator variant",
            ),
            (
                {"allocator_cargo_feature_overrides": {"system": "sqlite"}},
                "feature lists",
            ),
            (
                {
                    "allocator_cargo_feature_overrides": {
                        "system": ["sqlite", "sqlite"]
                    }
                },
                "duplicate Cargo features",
            ),
            (
                {"allocator_cargo_feature_overrides": {"system": ["bad feature"]}},
                "invalid Cargo feature",
            ),
            (
                {"allocator_cargo_feature_overrides": {"system": [1]}},
                "invalid Cargo feature",
            ),
            (
                {"allocator_cargo_feature_overrides": {"reclaim_plain": []}},
                "requires the matched reclaim_checks override",
            ),
            (
                {
                    "allocator_cargo_feature_overrides": {
                        "reclaim_plain": ["sqlite"],
                        "reclaim_checks": [],
                    }
                },
                "must match",
            ),
        )
        for scenario, message in invalid:
            with self.subTest(scenario=scenario):
                with self.assertRaisesRegex(experiment.ExperimentError, message):
                    experiment.allocator_cargo_feature_overrides(scenario)

        self.assertEqual(
            experiment.allocator_cargo_feature_overrides(
                {"allocator_cargo_feature_overrides": {"system": []}}
            ),
            {"system": []},
        )

    def test_witness_transformation_preserves_source_and_exports_run(self) -> None:
        source = """//! Pinned witness.
#![forbid(unsafe_code)]
use std::hint::black_box;

fn main() {
    black_box(7);
}
"""
        transformed = experiment.transform_witness_source(source)

        self.assertIn("//! Pinned witness.", transformed)
        self.assertIn("#![forbid(unsafe_code)]", transformed)
        self.assertIn("pub(crate) fn run() {", transformed)
        self.assertNotIn("fn main()", transformed)
        self.assertEqual(transformed.count("pub(crate) fn run"), 1)

    def test_witness_transformation_rejects_ambiguous_mains(self) -> None:
        with self.assertRaisesRegex(experiment.ExperimentError, "exactly one"):
            experiment.transform_witness_source("fn main() {}\nfn main() {}\n")

    def test_subject_global_allocator_is_preserved_only_for_system_arm(self) -> None:
        source = """use mimalloc::MiMalloc;

#[global_allocator]
static GLOBAL: MiMalloc = MiMalloc;

fn main() {}
"""
        system = experiment.subject_allocator_topology(source, "system")
        typeiso = experiment.subject_allocator_topology(source, "typeiso")

        self.assertTrue(system["subject_allocator_preserved"])
        self.assertTrue(system["substitution_supported"])
        self.assertFalse(system["wrapper_defines_global_allocator"])
        self.assertFalse(typeiso["substitution_supported"])
        self.assertIn("duplicate", typeiso["unsupported_reason"])

        project = self.root / "subject-allocator"
        (project / "src").mkdir(parents=True)
        (project / "src/main.rs").write_text(source, encoding="utf-8")
        experiment.transform_witness(
            project, "system", allocator_topology=system
        )
        self.assertNotIn(
            "global_allocator",
            (project / "src/main.rs").read_text(encoding="utf-8"),
        )
        self.assertIn(
            "#[global_allocator]",
            (project / "src/witness.rs").read_text(encoding="utf-8"),
        )

    def test_unclear_global_allocator_token_fails_closed(self) -> None:
        topology = experiment.subject_allocator_topology(
            "#[cfg_attr(feature = \"alloc\", global_allocator)]\nfn main() {}\n",
            "unialloc",
        )

        self.assertTrue(topology["subject_global_allocator_detected"])
        self.assertTrue(topology["fail_closed"])
        self.assertFalse(topology["substitution_supported"])

    def test_stats_wrapper_escapes_json_for_a_rust_string_literal(self) -> None:
        source = experiment.stats_wrapper(
            "typeiso", vulnerability_edge_hooks=True
        )

        self.assertIn(r'\"allocator\":\"typeiso\"', source)
        self.assertNotIn('={{"allocator"', source)
        for field in experiment.REUSE_DENIAL_FIELDS:
            with self.subTest(field=field):
                self.assertIn(rf'\"{field}\":{{}}', source)
                self.assertIn(f"stats.{field}", source)
        self.assertIn(".with_flags(1)", source)
        self.assertIn(experiment.REUSE_DENIAL_PREFIX, source)

    def test_typed_plain_wrapper_uses_manual_identity_without_isolation_flag(self) -> None:
        source = experiment.stats_wrapper(
            "typed_plain", vulnerability_edge_hooks=True
        )

        self.assertIn("with_vulnerability_edge_identity", source)
        self.assertIn(".with_flags(0)", source)
        self.assertIn("report_vulnerability_edge_reuse_denial", source)

    def test_system_wrapper_exposes_noop_vulnerability_edge_hooks(self) -> None:
        source = experiment.wrapper_source(
            "system", vulnerability_edge_hooks=True
        )

        self.assertIn("with_vulnerability_edge_identity", source)
        self.assertIn("report_vulnerability_edge_reuse_denial() {}", source)
        self.assertNotIn("AllocationMetadata", source)

    def test_ordinary_wrapper_omits_special_case_vulnerability_hooks(self) -> None:
        source = experiment.wrapper_source("typeiso")

        self.assertNotIn("with_vulnerability_edge_identity", source)
        self.assertNotIn("UNIALLOC_SECURITY_REUSE_DENIAL", source)

    def test_reclaim_wrappers_are_allocator_visible_without_type_metadata(self) -> None:
        for variant in ("reclaim_plain", "reclaim_checks"):
            with self.subTest(variant=variant):
                source = experiment.wrapper_source(variant)
                self.assertIn(rf'\"allocator\":\"{variant}\"', source)
                self.assertNotIn("with_semantic_metadata", source)

    def test_legacy_allocator_abi_bridge_routes_all_entrypoints_to_active_allocator(
        self,
    ) -> None:
        source = experiment.wrapper_source(
            "reclaim_checks", legacy_allocator_abi_bridge=True
        )

        for symbol in (
            "__rust_alloc",
            "__rust_dealloc",
            "__rust_realloc",
            "__rust_alloc_zeroed",
            "__rust_grow_in_place",
            "__rust_shrink_in_place",
        ):
            with self.subTest(symbol=symbol):
                self.assertIn(f'extern "Rust" fn {symbol}', source)
        self.assertEqual(source.count("#[unsafe(no_mangle)]"), 6)
        self.assertIn("GlobalAlloc::alloc(&ALLOCATOR, layout)", source)
        self.assertIn("GlobalAlloc::dealloc(&ALLOCATOR, ptr, layout)", source)
        self.assertIn("GlobalAlloc::realloc(&ALLOCATOR, ptr, layout, new_size)", source)
        self.assertIn("GlobalAlloc::alloc_zeroed(&ALLOCATOR, layout)", source)

    def test_legacy_allocator_abi_bridge_is_explicit_and_type_checked(self) -> None:
        self.assertFalse(experiment.legacy_allocator_abi_bridge_requested({}))
        self.assertTrue(
            experiment.legacy_allocator_abi_bridge_requested(
                {"legacy_allocator_abi_bridge": True}
            )
        )
        with self.assertRaisesRegex(
            experiment.ExperimentError, "legacy_allocator_abi_bridge must be a boolean"
        ):
            experiment.legacy_allocator_abi_bridge_requested(
                {"legacy_allocator_abi_bridge": "yes"}
            )

    def test_miri_system_ground_truth_preserves_rust_default_allocator(self) -> None:
        source = "fn main() {}\n"

        miri = experiment.effective_allocator_topology(source, "system", "miri")
        native = experiment.effective_allocator_topology(
            source, "system", "native_alignment"
        )
        miri_wrapper = experiment.wrapper_source(
            "system",
            preserve_default_system_allocator=miri[
                "default_system_allocator_preserved"
            ],
        )
        native_wrapper = experiment.wrapper_source(
            "system",
            preserve_default_system_allocator=native[
                "default_system_allocator_preserved"
            ],
        )

        self.assertFalse(miri["wrapper_defines_global_allocator"])
        self.assertEqual(miri["effective_allocator_owner"], "rust_default_allocator")
        self.assertNotIn("global_allocator", miri_wrapper)
        self.assertTrue(native["wrapper_defines_global_allocator"])
        self.assertEqual(native["effective_allocator_owner"], "experiment_wrapper")
        self.assertIn("global_allocator", native_wrapper)

    def test_target_normalization_includes_subject_and_harness_once(self) -> None:
        self.assertEqual(
            experiment.normalize_target_crates(
                "thin-vec", "rsh-020-harness", "thin_vec"
            ),
            ("thin_vec", "rsh_020_harness"),
        )

    def test_scenario_compiler_targets_default_and_require_harness(self) -> None:
        case = {"case_id": "RSH-008", "crate": "lru"}

        self.assertEqual(
            experiment.compiler_target_crates(case, {}),
            ("lru", "rsh_008_harness"),
        )
        manual_exclusion = {
            "classification_role": "derived_reuse_experiment",
            "type_isolation_edge_annotation": {
                "kind": "manual_exact_vulnerability_edge_identity"
            },
            "compiler_target_crates": ["rsh-008-harness"],
            "compiler_target_exclusion": {
                "subject_crate": "lru",
                "reason": "manual victim identity",
                "claim_boundary": "automatic subject coverage remains unvalidated",
                "compiler_automatic_victim_coverage": False,
            },
        }
        self.assertEqual(
            experiment.compiler_target_crates(case, manual_exclusion),
            ("rsh_008_harness",),
        )

        with self.assertRaisesRegex(
            experiment.ExperimentError, "manually attributed derived reuse"
        ):
            experiment.compiler_target_crates(
                case,
                {"compiler_target_crates": ["rsh-008-harness"]},
            )

        missing_boundary = dict(manual_exclusion)
        missing_boundary["compiler_target_exclusion"] = {
            **manual_exclusion["compiler_target_exclusion"],
            "claim_boundary": "",
        }
        with self.assertRaisesRegex(
            experiment.ExperimentError, "claim_boundary must be a nonempty string"
        ):
            experiment.compiler_target_crates(case, missing_boundary)

        for override, message in (
            ([], "must be a nonempty list"),
            ("rsh-008-harness", "must be a nonempty list"),
            (["lru"], "must include harness crate rsh_008_harness"),
            (["rsh-008-harness", 8], "entries must be strings"),
        ):
            with self.subTest(override=override):
                with self.assertRaisesRegex(experiment.ExperimentError, message):
                    experiment.compiler_target_crates(
                        case, {"compiler_target_crates": override}
                    )

    def test_binary_artifact_records_resolved_path_size_and_hash(self) -> None:
        binary = self.root / "demo-binary"
        binary.write_bytes(b"pinned-binary")

        record = experiment.file_artifact_record(binary)
        self.assertEqual(record["path"], str(binary.resolve()))
        self.assertEqual(record["bytes"], len(b"pinned-binary"))
        self.assertEqual(record["sha256"], experiment.sha256_file(binary))

    def test_sanitizer_ground_truth_is_system_only(self) -> None:
        report = {"rustflags": ["-Ainvalid_reference_casting"]}

        self.assertEqual(experiment.execution_mode("asan", "system"), "asan")
        self.assertEqual(
            experiment.execution_mode("asan", "typeiso"), "native_diagnostic"
        )
        self.assertIn(
            "-Zsanitizer=address", experiment.arm_rustflags(report, "asan")
        )
        self.assertNotIn(
            "-Zsanitizer=address",
            experiment.arm_rustflags(report, "native_diagnostic"),
        )
        self.assertEqual(
            experiment.effective_required_environment(
                {
                    "ASAN_OPTIONS": "detect_leaks=0",
                    "CC": "clang",
                    "CFLAGS": "-fsanitize=address -fno-omit-frame-pointer",
                },
                "native_diagnostic",
            ),
            {"CC": "clang"},
        )

    def test_historical_miri_filters_current_compatibility_lints(self) -> None:
        report = {
            "rustflags": [
                *experiment.harness.COMPATIBILITY_RUSTFLAGS,
                "-Copt-level=0",
            ]
        }
        scenario = {"oracle_toolchain": "nightly-2022-07-01"}

        policy = experiment.effective_rustflag_policy(report, scenario, "miri")

        self.assertEqual(policy["effective"], ["-Copt-level=0"])
        self.assertEqual(
            policy["removed"], experiment.harness.COMPATIBILITY_RUSTFLAGS
        )
        self.assertEqual(
            policy["reason"],
            "historical_miri_toolchain_predates_current_compatibility_lints",
        )
        self.assertEqual(
            policy["effective_oracle_toolchain"], "nightly-2022-07-01"
        )

    def test_current_miri_and_native_diagnostics_retain_compatibility_lints(self) -> None:
        report = {"rustflags": experiment.harness.COMPATIBILITY_RUSTFLAGS}

        current = experiment.effective_rustflag_policy(report, {}, "miri")
        native = experiment.effective_rustflag_policy(
            report,
            {"oracle_toolchain": "nightly-2022-07-01"},
            "native_diagnostic",
        )

        self.assertEqual(current["effective"], report["rustflags"])
        self.assertEqual(native["effective"], report["rustflags"])
        self.assertEqual(current["removed"], [])
        self.assertEqual(native["removed"], [])

    def test_force_rlib_build_strips_host_rustflags_and_records_environment(self) -> None:
        root = self.root / "toolchain"
        record = root / "force-load/stats-type_isolation/build.json"
        experiment.write_json(record, {"success": True, "legacy": True})
        observed: dict[str, object] = {}

        def fake_build(*args, **kwargs):
            observed["rustflags"] = os.environ.get("RUSTFLAGS")
            observed["encoded"] = os.environ.get("CARGO_ENCODED_RUSTFLAGS")
            observed["stale_record_exists"] = record.exists()
            return {"success": True, "rlib": "/tmp/example.rlib"}

        with mock.patch.dict(
            os.environ,
            {
                "RUSTFLAGS": "-Zhost-poison",
                "CARGO_ENCODED_RUSTFLAGS": "-Cdebuginfo=2",
            },
        ):
            with mock.patch.object(
                experiment.realworld,
                "build_force_load_rlib",
                side_effect=fake_build,
            ):
                force, effective = experiment.build_force_load_rlib_sanitized(
                    root, jobs=1, timeout=30
                )
            self.assertEqual(os.environ["RUSTFLAGS"], "-Zhost-poison")
            self.assertEqual(
                os.environ["CARGO_ENCODED_RUSTFLAGS"], "-Cdebuginfo=2"
            )

        self.assertIsNone(observed["rustflags"])
        self.assertIsNone(observed["encoded"])
        self.assertFalse(observed["stale_record_exists"])
        self.assertEqual(effective, experiment.FORCE_BUILD_ENVIRONMENT)
        self.assertEqual(force["experiment_tool_build_environment"], effective)
        persisted = json.loads(record.read_text(encoding="utf-8"))
        self.assertEqual(persisted["experiment_tool_build_environment"], effective)

    def test_native_address_parser_accepts_stale_value_label(self) -> None:
        signature = experiment.finding_signature(
            "native_address_trace",
            "stale_value=0x1234 replacement=0x1234",
        )

        self.assertEqual(signature, "address_reuse_observed")

    def test_patched_native_address_oracle_can_require_safe_same_identity_reuse(
        self,
    ) -> None:
        contract = experiment.parse_expected_oracle_contract(
            tool="native_address_trace",
            archive_variant="patched",
            expected="safe same-identity same address grooming exits 0",
        )

        self.assertEqual(contract["parse_status"], "parsed")
        self.assertEqual(contract["kind"], "clean_exit")
        self.assertIn("address_reuse", contract["required_evidence"])

    def test_native_assertion_oracle_rejects_an_unrelated_panic(self) -> None:
        expected = "observed arena bytes differ from 0x01 and assertion fails"
        unrelated = experiment.validate_expected_oracle(
            tool="native_value_oracle",
            mode="native_value_oracle",
            archive_variant="vulnerable",
            expected=expected,
            build=execution(0),
            runs=[execution(101, "thread 'main' panicked at unrelated failure")],
        )
        assertion = experiment.validate_expected_oracle(
            tool="native_value_oracle",
            mode="native_value_oracle",
            archive_variant="vulnerable",
            expected=expected,
            build=execution(0),
            runs=[
                execution(
                    101,
                    "thread 'main' panicked at src/main.rs: assertion "
                    "`left == right` failed",
                )
            ],
        )

        self.assertFalse(unrelated["expected_oracle_observed"])
        self.assertEqual(assertion["status"], "tool_finding_observed")
        self.assertTrue(assertion["expected_oracle_observed"])

    def test_fingerprint_change_wipes_target_and_same_fingerprint_reuses_it(self) -> None:
        output = self.root / "output"
        target = output / "arms/vulnerable/typeiso/target"
        record = output / "arms/vulnerable/typeiso/fingerprint.json"
        target.mkdir(parents=True)
        marker = target / "stale"
        marker.write_text("stale", encoding="utf-8")
        experiment.write_json(
            record,
            {"schema_version": 1, "fingerprint": "a" * 64},
        )

        reset = experiment.reset_target_for_fingerprint(
            target, record, "b" * 64, output_root=output
        )
        self.assertTrue(reset)
        self.assertFalse(marker.exists())

        retained = target / "retained"
        retained.write_text("retained", encoding="utf-8")
        reset = experiment.reset_target_for_fingerprint(
            target, record, "b" * 64, output_root=output
        )
        self.assertFalse(reset)
        self.assertTrue(retained.exists())

    def test_typeiso_same_fingerprint_forces_target_rebuild_for_fresh_audits(self) -> None:
        output = self.root / "output"
        target = output / "arms/vulnerable/typeiso/target"
        record = output / "arms/vulnerable/typeiso/fingerprint.json"
        target.mkdir(parents=True)
        marker = target / "cached-dependency"
        marker.write_text("cached", encoding="utf-8")
        experiment.write_json(
            record,
            {"schema_version": 1, "fingerprint": "a" * 64},
        )

        reset = experiment.reset_target_for_fingerprint(
            target,
            record,
            "a" * 64,
            output_root=output,
            force_reset_reason="typeiso_complete_pass_audit_regeneration",
        )

        self.assertTrue(reset)
        self.assertFalse(marker.exists())
        recorded = json.loads(record.read_text(encoding="utf-8"))
        self.assertEqual(
            recorded["target_reset_reasons"],
            ["typeiso_complete_pass_audit_regeneration"],
        )
        self.assertTrue(recorded["full_rebuild_required"])

    def test_arm_artifacts_are_recreated_without_stale_repetition_logs(self) -> None:
        output = self.root / "output"
        artifacts = output / "arms/vulnerable/system/artifacts"
        artifacts.mkdir(parents=True)
        stale = artifacts / "run-010.stderr"
        stale.write_text("old run", encoding="utf-8")

        experiment.reset_arm_artifact_directory(artifacts, output_root=output)

        self.assertTrue(artifacts.is_dir())
        self.assertFalse(stale.exists())

    def test_run_requires_unsafe_opt_in_before_execution(self) -> None:
        stderr = io.StringIO()
        with mock.patch.object(experiment, "execute_experiment") as execute_mock:
            with contextlib.redirect_stderr(stderr):
                status = experiment.main(
                    [
                        "--action",
                        "run",
                        "--scenario",
                        "RSH-002-derived-reuse",
                        "--output-dir",
                        str(self.root / "output"),
                    ]
                )

        self.assertEqual(status, 2)
        execute_mock.assert_not_called()
        self.assertIn("--execute-unsafe", stderr.getvalue())

    def test_preflight_reports_subject_allocator_topology_for_full_matrix(self) -> None:
        case = {"case_id": "RSH-030", "crate": "mimalloc"}
        scenario = {
            "required_environment": {},
            "oracle": {"tool": "native_alignment"},
            "allocator_cargo_feature_overrides": {
                "system": ["secure"],
                "reclaim_checks": [],
            },
        }
        source = """use mimalloc::MiMalloc;
#[global_allocator]
static GLOBAL: MiMalloc = MiMalloc;
fn main() {}
"""

        def materialize(
            catalog,
            scenario_id,
            archive_variant,
            output,
            cache,
            allow_download,
        ):
            project = output / "project"
            (project / "src").mkdir(parents=True)
            (project / "src/main.rs").write_text(source, encoding="utf-8")
            (project / "Cargo.toml").write_text(
                '[package]\nname="rsh-030-harness"\nversion="0.0.0"\n'
                '[dependencies]\n"mimalloc" = { path = "../mimalloc", '
                'default-features = true, features = [] }\n',
                encoding="utf-8",
            )
            (project / "Cargo.lock").write_text(
                'version = 4\n\n[[package]]\nname="rsh-030-harness"\n'
                'version="0.0.0"\n\n[[package]]\nname="mimalloc"\n'
                'version="0.1.32"\n',
                encoding="utf-8",
            )
            return {
                "scenario_id": scenario_id,
                "required_environment": {},
                "rustflags": [],
                "source_sha256": "1" * 64,
                "archive_sha256": "2" * 64,
                "cargo_lock_sha256": "3" * 64,
            }

        output = self.root / "preflight"
        with (
            mock.patch.object(
                experiment.harness,
                "select_scenario",
                return_value=(case, scenario),
            ),
            mock.patch.object(
                experiment.harness, "materialize", side_effect=materialize
            ),
            mock.patch.object(
                experiment,
                "toolchain_identity",
                return_value={"name": experiment.TOOLCHAIN},
            ),
        ):
            result = experiment.preflight(
                catalog={},
                catalog_path=SCRIPT,
                scenario_id="RSH-030-advisory",
                variants=experiment.VARIANTS,
                archive_variants=("vulnerable",),
                repetitions=1,
                output_dir=output,
                cache_root=self.root / "cache",
                allow_download=False,
            )

        arms = {arm["allocator_variant"]: arm for arm in result["planned_arms"]}
        self.assertEqual(
            result["allocator_cargo_feature_overrides"],
            {"system": ["secure"], "reclaim_checks": []},
        )
        self.assertEqual(
            arms["system"]["subject_cargo_features"]["effective_features"],
            ["secure"],
        )
        self.assertEqual(
            arms["reclaim_checks"]["subject_cargo_features"]["effective_features"],
            [],
        )
        self.assertEqual(
            arms["reclaim_plain"]["subject_cargo_features"]["effective_features"],
            [],
        )
        self.assertEqual(
            arms["reclaim_plain"]["subject_cargo_features"][
                "override_allocator_variant"
            ],
            "reclaim_checks",
        )
        self.assertTrue(arms["system"]["supported"])
        self.assertTrue(
            arms["system"]["allocator_topology"]["subject_allocator_preserved"]
        )
        for variant in (
            "unialloc",
            "reclaim_plain",
            "reclaim_checks",
            "typed_plain",
            "typeiso",
        ):
            with self.subTest(variant=variant):
                self.assertFalse(arms[variant]["supported"])
                self.assertFalse(arms[variant]["allocator_substitution_supported"])
                self.assertIn("duplicate", arms[variant]["unsupported_reason"])

    def test_expected_unsupported_allocator_topology_is_not_matrix_error(self) -> None:
        output = self.root / "matrix"
        preflight_record = {
            "toolchain": {"name": experiment.TOOLCHAIN},
            "planned_arms": [
                {"allocator_variant": "system", "supported": True},
                {"allocator_variant": "typeiso", "supported": False},
            ],
        }
        system = {
            "execution_status": "completed",
            "allocator_variant": "system",
            "oracle_validation": {"expected_oracle_observed": True, "status": "ok"},
            "pass_audit_validation": {"valid": None},
            "runtime_stats_validation": {"valid": None},
            "build": execution(0),
            "repetition_summary": {"executed": 1},
        }
        unsupported = {
            "execution_status": "unsupported_allocator_topology",
            "allocator_variant": "typeiso",
            "unsupported_reason": "subject allocator preserved by system arm",
        }
        with (
            mock.patch.object(
                experiment, "preflight", return_value=preflight_record
            ),
            mock.patch.object(
                experiment, "prepare_typeiso_tools"
            ) as prepare_tools,
            mock.patch.object(
                experiment, "run_arm", side_effect=[system, unsupported]
            ),
        ):
            result = experiment.execute_experiment(
                catalog={},
                catalog_path=SCRIPT,
                scenario_id="RSH-030-advisory",
                variants=("system", "typeiso"),
                archive_variants=("vulnerable",),
                repetitions=1,
                output_dir=output,
                cache_root=self.root / "cache",
                allow_download=False,
                jobs=1,
                build_timeout=30,
                run_timeout=30,
            )

        prepare_tools.assert_not_called()
        self.assertTrue(result["orchestration_success"])
        self.assertEqual(result["unexpected_arm_count"], 0)

    def test_invalid_clean_allocator_stats_fail_orchestration(self) -> None:
        output = self.root / "matrix-invalid-stats"
        preflight_record = {
            "toolchain": {"name": experiment.TOOLCHAIN},
            "planned_arms": [
                {"allocator_variant": "unialloc", "supported": True}
            ],
        }
        arm = {
            "execution_status": "completed",
            "allocator_variant": "unialloc",
            "oracle_validation": {
                "expected_oracle_observed": False,
                "status": "native_clean_runs_inconclusive",
            },
            "pass_audit_validation": {"valid": None},
            "runtime_stats_validation": {"valid": False},
            "build": execution(0),
            "repetition_summary": {"executed": 1},
        }
        with (
            mock.patch.object(
                experiment, "preflight", return_value=preflight_record
            ),
            mock.patch.object(experiment, "run_arm", return_value=arm),
        ):
            result = experiment.execute_experiment(
                catalog={},
                catalog_path=SCRIPT,
                scenario_id="RSH-test",
                variants=("unialloc",),
                archive_variants=("vulnerable",),
                repetitions=1,
                output_dir=output,
                cache_root=self.root / "cache",
                allow_download=False,
                jobs=1,
                build_timeout=30,
                run_timeout=30,
            )

        self.assertFalse(result["orchestration_success"])
        self.assertEqual(result["unexpected_arm_count"], 1)

    def test_vulnerable_clean_exit_never_becomes_mitigation(self) -> None:
        result = experiment.validate_expected_oracle(
            tool="asan",
            mode="asan",
            archive_variant="vulnerable",
            expected="AddressSanitizer reports heap-use-after-free",
            build=execution(0),
            runs=[execution(0)],
        )

        self.assertEqual(result["status"], "clean_tool_runs_observed")
        self.assertFalse(result["expected_oracle_observed"])
        self.assertFalse(result["mitigation_inferred"])

    def test_asan_finding_is_oracle_evidence_without_mitigation_claim(self) -> None:
        result = experiment.validate_expected_oracle(
            tool="asan",
            mode="asan",
            archive_variant="vulnerable",
            expected="AddressSanitizer reports heap-use-after-free",
            build=execution(0),
            runs=[
                execution(
                    1,
                    "ERROR: AddressSanitizer: heap-use-after-free\n"
                    "SUMMARY: AddressSanitizer: heap-use-after-free",
                )
            ],
        )

        self.assertEqual(
            result["tool_finding_signatures"], ["asan_heap_use_after_free"]
        )
        self.assertTrue(result["expected_oracle_observed"])
        self.assertFalse(result["mitigation_inferred"])

    def test_vulnerable_asan_requires_the_expected_finding_class(self) -> None:
        result = experiment.validate_expected_oracle(
            tool="asan",
            mode="asan",
            archive_variant="vulnerable",
            expected="AddressSanitizer reports heap-use-after-free",
            build=execution(0),
            runs=[
                execution(
                    1,
                    "ERROR: AddressSanitizer: heap-buffer-overflow\n"
                    "SUMMARY: AddressSanitizer: heap-buffer-overflow",
                )
            ],
        )

        self.assertEqual(result["status"], "tool_finding_class_mismatch")
        self.assertEqual(
            result["tool_finding_signatures"], ["asan_heap_buffer_overflow"]
        )
        self.assertFalse(result["expected_oracle_observed"])
        self.assertFalse(result["mitigation_inferred"])

    def test_unknown_and_ambiguous_oracle_descriptions_fail_closed(self) -> None:
        cases = (
            ("AddressSanitizer reports memory corruption", "unknown"),
            (
                "AddressSanitizer reports double-free and heap-use-after-free",
                "ambiguous",
            ),
        )

        for expected, parse_status in cases:
            with self.subTest(parse_status=parse_status):
                result = experiment.validate_expected_oracle(
                    tool="asan",
                    mode="asan",
                    archive_variant="vulnerable",
                    expected=expected,
                    build=execution(0),
                    runs=[
                        execution(
                            1,
                            "ERROR: AddressSanitizer: heap-use-after-free",
                        )
                    ],
                )

                self.assertEqual(
                    result["status"], f"oracle_contract_{parse_status}"
                )
                self.assertEqual(
                    result["expected_contract"]["parse_status"], parse_status
                )
                self.assertFalse(result["expected_oracle_observed"])
                self.assertFalse(result["mitigation_inferred"])

    def test_vulnerable_asan_accepts_each_pinned_finding_class(self) -> None:
        cases = (
            (
                "AddressSanitizer reports attempting double-free",
                "ERROR: AddressSanitizer: attempting double-free",
                "asan_double_free",
            ),
            (
                "AddressSanitizer reports heap-use-after-free",
                "ERROR: AddressSanitizer: heap-use-after-free",
                "asan_heap_use_after_free",
            ),
            (
                "AddressSanitizer reports heap-buffer-overflow",
                "ERROR: AddressSanitizer: heap-buffer-overflow",
                "asan_heap_buffer_overflow",
            ),
            (
                "AddressSanitizer reports bad free of stack address",
                "SUMMARY: AddressSanitizer: bad-free",
                "asan_bad_free",
            ),
            (
                "ASan reports a wild out-of-bounds write",
                "AddressSanitizer:DEADLYSIGNAL\n"
                "SUMMARY: AddressSanitizer: SEGV",
                "asan_deadly_fault",
            ),
        )

        for expected, output, signature in cases:
            with self.subTest(signature=signature):
                result = experiment.validate_expected_oracle(
                    tool="asan",
                    mode="asan",
                    archive_variant="vulnerable",
                    expected=expected,
                    build=execution(0),
                    runs=[execution(1, output)],
                )

                self.assertEqual(result["status"], "tool_finding_observed")
                self.assertEqual(result["tool_finding_signatures"], [signature])
                self.assertTrue(result["expected_oracle_observed"])
                self.assertFalse(result["mitigation_inferred"])

    def test_rsh_002_reuse_oracle_conjoins_address_and_invalid_free(self) -> None:
        expected = (
            "old address equals Replacement address and stale invalid free "
            "is reported"
        )
        address = "original=0x1234 replacement=0x1234\n"
        invalid_free = "SUMMARY: AddressSanitizer: bad-free"

        finding_only = experiment.validate_expected_oracle(
            tool="asan",
            mode="asan",
            archive_variant="vulnerable",
            expected=expected,
            build=execution(0),
            runs=[execution(1, invalid_free)],
        )
        address_only = experiment.validate_expected_oracle(
            tool="asan",
            mode="asan",
            archive_variant="vulnerable",
            expected=expected,
            build=execution(0),
            runs=[execution(1, address)],
        )
        conjunction = experiment.validate_expected_oracle(
            tool="asan",
            mode="asan",
            archive_variant="vulnerable",
            expected=expected,
            build=execution(0),
            runs=[execution(1, address + invalid_free)],
        )

        self.assertEqual(
            finding_only["status"], "tool_finding_conjunction_mismatch"
        )
        self.assertFalse(finding_only["expected_oracle_observed"])
        self.assertFalse(address_only["expected_oracle_observed"])
        self.assertEqual(conjunction["status"], "tool_finding_observed")
        self.assertTrue(conjunction["expected_oracle_observed"])
        self.assertFalse(conjunction["mitigation_inferred"])

    def test_patched_clean_control_is_recorded_separately(self) -> None:
        result = experiment.validate_expected_oracle(
            tool="asan",
            mode="asan",
            archive_variant="patched",
            expected="exit 0 under ASan",
            build=execution(0),
            runs=[execution(0)],
        )

        self.assertEqual(result["status"], "clean_tool_runs_observed")
        self.assertTrue(result["expected_oracle_observed"])
        self.assertFalse(result["mitigation_inferred"])

    def test_patched_safe_panic_controls_require_exact_safe_evidence(self) -> None:
        descriptions = {
            "RSH-020": "only intentional panic; no sanitizer finding",
            "RSH-029": (
                "upstream 0.10.2 rejects the undersized heap with an "
                "assertion and no sanitizer finding"
            ),
            "RSH-032": "checked multiplication rejects with a safe Rust panic",
        }
        panic = "thread 'main' panicked at src/main.rs:12:5"
        sanitizer = "ERROR: AddressSanitizer: heap-buffer-overflow"

        for scenario, expected in descriptions.items():
            with self.subTest(scenario=scenario, evidence="valid"):
                result = experiment.validate_expected_oracle(
                    tool="asan",
                    mode="asan",
                    archive_variant="patched",
                    expected=expected,
                    build=execution(0),
                    runs=[execution(101, panic)],
                )
                self.assertEqual(
                    result["status"], "patched_safe_panic_control_observed"
                )
                self.assertTrue(result["expected_oracle_observed"])
                self.assertFalse(result["mitigation_inferred"])

            for evidence, run in (
                ("missing-panic", execution(101, "process aborted")),
                ("zero-exit", execution(0, panic)),
                ("sanitizer", execution(1, panic + "\n" + sanitizer)),
            ):
                with self.subTest(scenario=scenario, evidence=evidence):
                    result = experiment.validate_expected_oracle(
                        tool="asan",
                        mode="asan",
                        archive_variant="patched",
                        expected=expected,
                        build=execution(0),
                        runs=[run],
                    )
                    self.assertFalse(result["expected_oracle_observed"])
                    self.assertFalse(result["mitigation_inferred"])

    def test_patched_compile_rejection_requires_expected_error_code(self) -> None:
        result = experiment.validate_expected_oracle(
            tool="native_value_oracle",
            mode="native_value_oracle",
            archive_variant="patched",
            expected="source rejected with E0505",
            build=execution(101, "error[E0505]: cannot move out"),
            runs=[],
        )

        self.assertEqual(
            result["status"], "patched_control_compile_rejection_observed"
        )
        self.assertTrue(result["expected_oracle_observed"])
        self.assertFalse(result["mitigation_inferred"])

    def test_native_allocator_clean_exit_stays_diagnostic_and_inconclusive(self) -> None:
        result = experiment.validate_expected_oracle(
            tool="asan",
            mode="native_diagnostic",
            archive_variant="vulnerable",
            expected="AddressSanitizer reports heap-use-after-free",
            build=execution(0),
            runs=[execution(0), execution(0)],
        )

        self.assertEqual(result["status"], "native_clean_runs_inconclusive")
        self.assertEqual(result["oracle_role"], "allocator_native_diagnostic")
        self.assertFalse(result["expected_oracle_observed"])
        self.assertFalse(result["mitigation_inferred"])

    def test_repetition_summary_aggregates_without_mitigation_inference(self) -> None:
        allocator_panic = (
            "thread 'main' (4242) panicked at "
            "/repo/unialloc/src/alloc_api/type_isolation.rs:11368:13:\n"
            "pointer already released\n"
            "note: run with `RUST_BACKTRACE=1` environment variable to display a "
            "backtrace\n"
        )
        observations = experiment.repetition_observations(
            tool="asan",
            mode="native_diagnostic",
            runs=[
                execution(0),
                execution(101, stderr=allocator_panic),
            ],
        )

        summary = experiment.summarize_repetitions(10, observations)
        self.assertEqual(summary["requested"], 10)
        self.assertEqual(summary["executed"], 2)
        self.assertEqual(summary["clean_exit_count"], 1)
        self.assertEqual(summary["native_diagnostic_signal_count"], 1)
        self.assertFalse(summary["mitigation_inferred"])

    def test_release_check_requires_bound_unialloc_panic_site(self) -> None:
        allocator_panic = (
            "thread 'main' (4242) panicked at "
            "/repo/unialloc/src/alloc_api/type_isolation.rs:11368:13:\n"
            "pointer already released\n"
            "note: run with `RUST_BACKTRACE=1` environment variable to display a "
            "backtrace\n"
        )
        record = execution(
            134,
            stderr=(
                "thread 'main' (4242) panicked at src/witness.rs:40:13:\n"
                "intentional witness panic\n"
                + allocator_panic
                + "fatal runtime error: failed to initiate panic\n"
            ),
        )

        self.assertEqual(
            experiment.native_diagnostic_signature(record),
            "unialloc_pointer_already_released_check",
        )

    def test_release_check_rejects_spoofed_or_unbound_text(self) -> None:
        exact_signature = "unialloc_pointer_already_released_check"
        allocator_panic = (
            "thread 'main' (4242) panicked at "
            "/repo/unialloc/src/alloc_api/type_isolation.rs:11368:13:\n"
            "pointer already released\n"
        )
        adversarial = (
            ("zero-exit-stderr", execution(0, stderr=allocator_panic)),
            ("nonzero-stdout", execution(101, allocator_panic)),
            (
                "message-only-stderr",
                execution(101, stderr="pointer already released\n"),
            ),
            (
                "witness-source-site",
                execution(
                    101,
                    stderr=(
                        "thread 'main' panicked at src/witness.rs:12:5:\n"
                        "pointer already released\n"
                    ),
                ),
            ),
            (
                "lookalike-source-site",
                execution(
                    101,
                    stderr=(
                        "thread 'main' panicked at "
                        "src/alloc_api/type_isolation.rs:11368:13:\n"
                        "pointer already released\n"
                    ),
                ),
            ),
            (
                "nonadjacent-message",
                execution(
                    101,
                    stderr=(
                        "thread 'main' panicked at "
                        "/repo/unialloc/src/alloc_api/type_isolation.rs:11368:13:\n"
                        "witness-injected separator\n"
                        "pointer already released\n"
                    ),
                ),
            ),
            (
                "nonexact-message",
                execution(
                    101,
                    stderr=(
                        "thread 'main' panicked at "
                        "/repo/unialloc/src/alloc_api/type_isolation.rs:11368:13:\n"
                        "pointer already released by witness\n"
                    ),
                ),
            ),
            (
                "timed-out",
                execution(101, stderr=allocator_panic, timed_out=True),
            ),
        )
        for label, record in adversarial:
            with self.subTest(label=label):
                self.assertNotEqual(
                    experiment.native_diagnostic_signature(record), exact_signature
                )

    def test_pass_audit_validation_is_presence_only_and_never_efficacy(self) -> None:
        with mock.patch.object(
            experiment.realworld, "validate_typeiso_audit_presence"
        ) as validate:
            result = experiment.validate_audit_coverage(
                allocator_variant="typeiso",
                summary={
                    "crate_names": ["demo", "rsh_001_harness"],
                    "total_compiler_rewrites_applied": 0,
                    "semantic_rewrites_applied": 0,
                },
                target_crates=("demo", "rsh_001_harness"),
                build=execution(0),
            )

        validate.assert_called_once()
        self.assertTrue(result["valid"])
        self.assertEqual(result["status"], "target_crate_presence_validated")
        self.assertEqual(result["rewrite_route"], "none")
        self.assertEqual(
            result["validation_scope"],
            "target_crate_presence_and_force_load_topology_only",
        )
        self.assertFalse(result["critical_site_coverage_validated"])
        self.assertFalse(result["efficacy_eligible"])
        self.assertEqual(
            result["efficacy_blockers"],
            ["critical_site_coverage_not_validated", "no_compiler_rewrite_observed"],
        )
        self.assertFalse(result["claim_grade"])


    def test_pass_audit_validation_reports_direct_only_rewrite_route(self) -> None:
        result = experiment.validate_audit_coverage(
            allocator_variant="typed_plain",
            summary={
                "audit_file_count": 1,
                "crate_names": ["demo"],
                "direct_allocator_rewrites_applied": 2,
                "direct_layout_allocator_rewrites_applied": 0,
                "semantic_rewrites_applied": 0,
                "total_compiler_rewrites_applied": 2,
            },
            target_crates=("demo",),
            build=execution(0),
        )

        self.assertTrue(result["valid"])
        self.assertEqual(result["rewrite_route"], "direct_only")
        self.assertNotIn(
            "no_compiler_rewrite_observed", result["efficacy_blockers"]
        )

    def test_catalog_allocator_exclusion_covers_all_direct_reclaim_arms(self) -> None:
        scenario = {
            "unsupported_allocator_variants": {
                "unialloc": "libc lockfile conflict"
            }
        }

        self.assertEqual(
            experiment.catalog_allocator_exclusion(scenario, "unialloc"),
            "libc lockfile conflict",
        )
        for variant in ("reclaim_plain", "reclaim_checks"):
            with self.subTest(variant=variant):
                self.assertEqual(
                    experiment.catalog_allocator_exclusion(scenario, variant),
                    "libc lockfile conflict",
                )
        self.assertIsNone(
            experiment.catalog_allocator_exclusion(scenario, "typed_plain")
        )

    def test_catalog_records_rsh028_unialloc_exclusion_rsh031_toolchain_and_rsh032_flags(self) -> None:
        catalog = experiment.harness.load_catalog(experiment.DEFAULT_CATALOG)
        _case, rsh028 = experiment.harness.select_scenario(catalog, "RSH-028-upstream")
        _case, rsh031 = experiment.harness.select_scenario(catalog, "RSH-031-upstream")
        _case, rsh032 = experiment.harness.select_scenario(catalog, "RSH-032-upstream")

        self.assertIn("unialloc", rsh028["unsupported_allocator_variants"])
        self.assertIn("libc", rsh028["unsupported_allocator_variants"]["unialloc"])
        self.assertEqual(rsh031["oracle_toolchain"], "nightly-2022-07-01")
        self.assertEqual(experiment.oracle_toolchain(rsh031, "miri"), "nightly-2022-07-01")
        self.assertEqual(experiment.oracle_toolchain(rsh031, "native_diagnostic"), experiment.TOOLCHAIN)
        self.assertEqual(
            rsh032["rustflags"],
            ["-Copt-level=0", "-Coverflow-checks=off", "-Zub-checks=no"],
        )
        self.assertIn("high-address out-of-bounds read", rsh032["oracle"]["vulnerable"])

    def test_rsh031_lockfiles_are_v3_for_historical_miri(self) -> None:
        catalog = experiment.harness.load_catalog(experiment.DEFAULT_CATALOG)
        case, _scenario = experiment.harness.select_scenario(catalog, "RSH-031-upstream")
        for variant in ("vulnerable", "patched"):
            with self.subTest(variant=variant):
                lock = experiment.ROOT / case["lockfiles"][variant]["path"]
                text = lock.read_text(encoding="utf-8")
                self.assertIn("version = 3", text)
                self.assertEqual(
                    experiment.sha256_file(lock),
                    case["lockfiles"][variant]["sha256"],
                )

    def test_clean_allocator_runs_require_valid_matching_runtime_stats(self) -> None:
        counters = {
            "allocator": "typeiso",
            "total_allocations": 3,
            "typed_allocations": 1,
            "fallback_allocations": 2,
            "typed_deallocations": 1,
            "fallback_deallocations": 1,
            "cache_hits": 0,
            "cache_inserts": 1,
            "cache_bypasses": 0,
            "typed_cache_wrong_identity_denials": 0,
            "last_wrong_identity_requested_type_id": 0,
            "last_wrong_identity_retained_type_id": 0,
            "last_wrong_identity_requested_module_id": 0,
            "last_wrong_identity_retained_module_id": 0,
            "last_wrong_identity_requested_callsite": 0,
            "last_wrong_identity_size": 0,
            "last_wrong_identity_align": 0,
            "side_cache_entries": 1,
            "side_cache_corrupt_slots": 0,
        }
        valid = execution(
            0, experiment.STATS_PREFIX + json.dumps(counters, sort_keys=True)
        )
        missing = execution(0, "clean run without stats")
        wrong_allocator = dict(counters, allocator="typed_plain")
        mismatch = execution(
            0,
            experiment.STATS_PREFIX
            + json.dumps(wrong_allocator, sort_keys=True),
        )
        bad_sum = dict(counters, total_allocations=99)
        invalid_invariant = execution(
            0, experiment.STATS_PREFIX + json.dumps(bad_sum, sort_keys=True)
        )

        self.assertTrue(
            experiment.validate_runtime_stats("typeiso", [valid])["valid"]
        )
        for run in (missing, mismatch, invalid_invariant):
            with self.subTest(text=run["stdout"]):
                result = experiment.validate_runtime_stats("typeiso", [run])
                self.assertFalse(result["valid"])
                self.assertEqual(result["status"], "invalid_clean_run_stats")

    def test_reuse_denial_event_binds_to_the_compiler_audited_replacement_site(
        self,
    ) -> None:
        annotation = {
            "kind": "manual_exact_vulnerability_edge_identity",
            "victim_type_id": 101,
            "victim_module_id": 202,
            "expected_layout": {"size": 1016, "align": 8},
            "replacement_source_file_fragment": "src/witness.rs:",
            "replacement_semantic_type_fragment": "Box<witness::Replacement",
        }
        report = {
            "allocator": "typeiso",
            "typed_cache_wrong_identity_denials": 1,
            "last_wrong_identity_requested_type_id": 303,
            "last_wrong_identity_retained_type_id": 101,
            "last_wrong_identity_requested_module_id": 404,
            "last_wrong_identity_retained_module_id": 202,
            "last_wrong_identity_requested_callsite": 505,
            "last_wrong_identity_size": 1016,
            "last_wrong_identity_align": 8,
        }
        audit_dir = self.root / "audits"
        audit_dir.mkdir()
        experiment.write_json(
            audit_dir / "harness.json",
            {
                "rewrite_candidates": [
                    {
                        "allocation_site_id": "replacement-site",
                        "type_id": 303,
                        "module_id": 404,
                        "callsite": 505,
                        "source_span": "src/witness.rs:42:23: 42:74",
                        "semantic_object_type": (
                            "std::boxed::Box<witness::Replacement, "
                            "std::alloc::Global>"
                        ),
                        "rewrite_status": (
                            "actual_semantic_scope_enter_exit_rewrite_applied"
                        ),
                    }
                ]
            },
        )
        run = execution(
            101,
            experiment.REUSE_DENIAL_PREFIX + json.dumps(report) + "\n"
            "original=0x1000 replacement=0x2000",
        )

        result = experiment.validate_reuse_denial_evidence(
            "typeiso", [run], annotation=annotation, audit_dir=audit_dir
        )

        self.assertTrue(result["valid"])
        self.assertTrue(result["direct_reuse_edge_coverage_observed"])
        row = result["repetitions"][0]
        self.assertEqual(
            row["status"],
            "cross_identity_reuse_denial_bound_to_replacement_site",
        )
        self.assertEqual(
            row["requested_site_binding"]["source_span"],
            "src/witness.rs:42:23: 42:74",
        )
        self.assertFalse(row["address_reuse_observed"])

    def test_reuse_denial_binds_runtime_compiler_type_id_scope(self) -> None:
        annotation = {
            "kind": "manual_exact_vulnerability_edge_identity",
            "victim_type_id": 101,
            "victim_module_id": 202,
            "expected_layout": {"size": 64, "align": 1},
            "replacement_source_file_fragment": "src/witness.rs:",
            "replacement_semantic_type_fragment": "Box<witness::Replacement",
        }
        report = {
            "allocator": "typeiso",
            "typed_cache_wrong_identity_denials": 1,
            "last_wrong_identity_requested_type_id": 303,
            "last_wrong_identity_retained_type_id": 101,
            "last_wrong_identity_requested_module_id": 404,
            "last_wrong_identity_retained_module_id": 202,
            "last_wrong_identity_requested_callsite": 505,
            "last_wrong_identity_size": 64,
            "last_wrong_identity_align": 1,
        }
        audit_dir = self.root / "audits"
        audit_dir.mkdir()
        experiment.write_json(
            audit_dir / "harness.json",
            {
                "rewrite_candidates": [
                    {
                        "allocation_site_id": "runtime-typeid-site",
                        "type_id": 0,
                        "type_id_basis": (
                            "monomorphized_compiler_type_id_runtime"
                        ),
                        "module_id": 404,
                        "callsite": 505,
                        "source_span": "src/witness.rs:79:23: 79:66",
                        "semantic_object_type": (
                            "std::boxed::Box<witness::Replacement, "
                            "std::alloc::Global>"
                        ),
                        "rewrite_status": (
                            "actual_semantic_scope_generic_type_rewrite_applied"
                        ),
                        "replacement_symbol": (
                            "__unialloc_semantic_scope_push_for_rust_type"
                        ),
                    }
                ]
            },
        )
        run = execution(
            0,
            experiment.REUSE_DENIAL_PREFIX + json.dumps(report) + "\n"
            "original=0x1000 replacement=0x2000",
        )

        result = experiment.validate_reuse_denial_evidence(
            "typeiso", [run], annotation=annotation, audit_dir=audit_dir
        )

        self.assertTrue(result["direct_reuse_edge_coverage_observed"])
        binding = result["repetitions"][0]["requested_site_binding"]
        self.assertEqual(
            binding["type_id_binding"],
            "monomorphized_compiler_type_id_runtime",
        )
        self.assertEqual(binding["requested_runtime_type_id"], 303)

    def test_reuse_denial_zero_report_is_a_valid_ablation_observation(self) -> None:
        annotation = {
            "victim_type_id": 101,
            "victim_module_id": 202,
            "expected_layout": {"size": 1016, "align": 8},
            "replacement_source_file_fragment": "src/witness.rs:",
            "replacement_semantic_type_fragment": "Box<witness::Replacement",
        }
        report = {"allocator": "typed_plain"}
        report.update({field: 0 for field in experiment.REUSE_DENIAL_FIELDS})
        run = execution(
            0,
            experiment.REUSE_DENIAL_PREFIX + json.dumps(report) + "\n"
            "original=0x1000 replacement=0x1000",
        )

        result = experiment.validate_reuse_denial_evidence(
            "typed_plain", [run], annotation=annotation, audit_dir=self.root
        )

        self.assertTrue(result["valid"])
        self.assertEqual(result["reuse_denial_event_count"], 0)
        self.assertFalse(result["direct_reuse_edge_coverage_observed"])
        self.assertTrue(result["repetitions"][0]["address_reuse_observed"])

    def test_reuse_edge_matrix_requires_ground_truth_ablation_and_bound_signal(
        self,
    ) -> None:
        scenario = {
            "type_isolation_edge_annotation": {
                "kind": "manual",
                "claim_scope": (
                    "The manually attributed derived RSH-042 A-to-B "
                    "replacement edge only."
                ),
            }
        }
        arms = [
            {
                "archive_variant": "vulnerable",
                "allocator_variant": "system",
                "oracle_validation": {"expected_oracle_observed": True},
                "repetition_summary": {"address_reuse_observation_count": 2},
            },
            {
                "archive_variant": "vulnerable",
                "allocator_variant": "typed_plain",
                "repetition_summary": {"address_reuse_observation_count": 2},
                "reuse_denial_evidence": {"reuse_denial_event_count": 0},
            },
            {
                "archive_variant": "vulnerable",
                "allocator_variant": "typeiso",
                "repetition_summary": {"address_reuse_observation_count": 0},
                "reuse_denial_evidence": {
                    "direct_reuse_edge_coverage_observed": True,
                    "bound_replacement_site_count": 2,
                },
            },
            {
                "archive_variant": "patched",
                "allocator_variant": "system",
                "oracle_validation": {"expected_oracle_observed": True},
                "repetition_summary": {"address_reuse_observation_count": 2},
            },
            {
                "archive_variant": "patched",
                "allocator_variant": "typed_plain",
                "repetition_summary": {"address_reuse_observation_count": 2},
                "reuse_denial_evidence": {
                    "matching_reuse_denial_event_count": 0
                },
            },
            {
                "archive_variant": "patched",
                "allocator_variant": "typeiso",
                "repetition_summary": {"address_reuse_observation_count": 2},
                "reuse_denial_evidence": {
                    "matching_reuse_denial_event_count": 0,
                    "bound_replacement_site_count": 0,
                },
            },
        ]

        result = experiment.evaluate_annotated_reuse_edge_matrix(
            scenario, arms, repetitions=2
        )

        self.assertTrue(result["validated"])
        self.assertEqual(
            result["status"], "cross_identity_reuse_edge_blocked_and_reported"
        )
        self.assertTrue(result["typeiso_reuse_edge_blocked_and_reported"])
        self.assertTrue(result["vulnerability_specific_detection_signal"])
        self.assertFalse(result["source_vulnerability_detection_validated"])
        self.assertEqual(
            result["claim_scope"],
            "The manually attributed derived RSH-042 A-to-B replacement edge only.",
        )

    def test_reuse_edge_matrix_requires_both_patched_allocator_controls(
        self,
    ) -> None:
        scenario = {"type_isolation_edge_annotation": {"kind": "manual"}}
        arms = [
            {
                "archive_variant": "vulnerable",
                "allocator_variant": "system",
                "oracle_validation": {"expected_oracle_observed": True},
                "repetition_summary": {"address_reuse_observation_count": 2},
            },
            {
                "archive_variant": "vulnerable",
                "allocator_variant": "typed_plain",
                "repetition_summary": {"address_reuse_observation_count": 2},
                "reuse_denial_evidence": {"reuse_denial_event_count": 0},
            },
            {
                "archive_variant": "vulnerable",
                "allocator_variant": "typeiso",
                "repetition_summary": {"address_reuse_observation_count": 0},
                "reuse_denial_evidence": {
                    "direct_reuse_edge_coverage_observed": True,
                    "bound_replacement_site_count": 2,
                },
            },
            {
                "archive_variant": "patched",
                "allocator_variant": "system",
                "oracle_validation": {"expected_oracle_observed": True},
                "repetition_summary": {"address_reuse_observation_count": 2},
            },
            {
                "archive_variant": "patched",
                "allocator_variant": "typed_plain",
                "repetition_summary": {"address_reuse_observation_count": 2},
                "reuse_denial_evidence": {
                    "matching_reuse_denial_event_count": 0
                },
            },
            {
                "archive_variant": "patched",
                "allocator_variant": "typeiso",
                "repetition_summary": {"address_reuse_observation_count": 2},
                "reuse_denial_evidence": {
                    "matching_reuse_denial_event_count": 0,
                    "bound_replacement_site_count": 0,
                },
            },
        ]

        for missing_allocator in ("typed_plain", "typeiso"):
            with self.subTest(missing_allocator=missing_allocator):
                incomplete = [
                    arm
                    for arm in arms
                    if not (
                        arm["archive_variant"] == "patched"
                        and arm["allocator_variant"] == missing_allocator
                    )
                ]
                result = experiment.evaluate_annotated_reuse_edge_matrix(
                    scenario, incomplete, repetitions=2
                )

                self.assertFalse(result["validated"])
                self.assertEqual(result["status"], "incomplete_required_arm_matrix")
                self.assertEqual(
                    result["missing_required_arms"],
                    [f"patched/{missing_allocator}"],
                )

    def test_reuse_edge_matrix_rejects_unexercised_patched_reuse_controls(
        self,
    ) -> None:
        scenario = {"type_isolation_edge_annotation": {"kind": "manual"}}
        arms = [
            {
                "archive_variant": "vulnerable",
                "allocator_variant": "system",
                "oracle_validation": {"expected_oracle_observed": True},
                "repetition_summary": {"address_reuse_observation_count": 2},
            },
            {
                "archive_variant": "vulnerable",
                "allocator_variant": "typed_plain",
                "repetition_summary": {"address_reuse_observation_count": 2},
                "reuse_denial_evidence": {"reuse_denial_event_count": 0},
            },
            {
                "archive_variant": "vulnerable",
                "allocator_variant": "typeiso",
                "repetition_summary": {"address_reuse_observation_count": 0},
                "reuse_denial_evidence": {
                    "direct_reuse_edge_coverage_observed": True,
                    "bound_replacement_site_count": 2,
                },
            },
        ]
        for allocator in ("system", "typed_plain", "typeiso"):
            arm = {
                "archive_variant": "patched",
                "allocator_variant": allocator,
                "repetition_summary": {"address_reuse_observation_count": 0},
            }
            if allocator == "system":
                arm["oracle_validation"] = {"expected_oracle_observed": True}
            else:
                arm["reuse_denial_evidence"] = {
                    "matching_reuse_denial_event_count": 0,
                    "bound_replacement_site_count": 0,
                }
            arms.append(arm)

        result = experiment.evaluate_annotated_reuse_edge_matrix(
            scenario, arms, repetitions=2
        )

        self.assertFalse(result["validated"])
        self.assertFalse(result["patched_system_control_reproduced"])
        self.assertEqual(result["status"], "reuse_edge_criteria_not_satisfied")

    def test_abnormal_allocator_run_records_stats_unavailable(self) -> None:
        result = experiment.validate_runtime_stats(
            "unialloc", [execution(101, "panic before wrapper telemetry")]
        )

        self.assertIsNone(result["valid"])
        self.assertEqual(
            result["status"], "stats_unavailable_due_to_early_termination"
        )
        self.assertEqual(
            result["repetitions"][0]["status"],
            "stats_unavailable_due_to_early_termination",
        )
        self.assertFalse(result["efficacy_eligible"])


if __name__ == "__main__":
    unittest.main()
